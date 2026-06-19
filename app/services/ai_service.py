"""
AI image generation service using Gemini 3 Pro Image (Nano Banana Pro).
Model: gemini-3-pro-image
"""
import logging
from dataclasses import dataclass
from decimal import Decimal

from google import genai
from google.genai import types

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Cost constants for gemini-3-pro-image (as of May 2026)
# Source: https://ai.google.dev/gemini-api/docs/gemini-3
COST_PER_1K_INPUT_TOKENS = Decimal("0.002")   # $2 per 1M input tokens
COST_PER_OUTPUT_IMAGE = Decimal("0.134")       # $0.134 per generated image (1K resolution)
TOKENS_PER_INPUT_IMAGE = 560                   # Each input image costs 560 tokens


def _detect_mime(data: bytes) -> str:
    """Detect MIME type from file magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


@dataclass
class GenerationResult:
    image_bytes: bytes
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


def _calculate_cost(input_tokens: int) -> Decimal:
    """
    Calculate generation cost.
    Input tokens billed at $2/1M. Output is one image = $0.134.
    """
    input_cost = Decimal(str(input_tokens)) / Decimal("1000") * COST_PER_1K_INPUT_TOKENS
    return input_cost + COST_PER_OUTPUT_IMAGE


async def generate_composite_photo(
    user_photo_bytes_list: list[bytes],
    ambassador_photo_bytes: bytes,
    prompt_template: str,
    extra_prompt: str = "",
) -> GenerationResult:
    """
    Generate a composite photo of the user and the TBC ambassador.

    Args:
        user_photo_bytes_list: One or more JPEG selfies of the user (main + optional extra angles).
                               Multiple photos improve likeness accuracy, especially for
                               Turkic / Central Asian facial features.
        ambassador_photo_bytes: Raw bytes of the ambassador's reference photo (JPEG/PNG auto-detected).
        prompt_template: Generation prompt template (may contain {extra} placeholder).
        extra_prompt: Optional additional instructions from the user.

    Returns:
        GenerationResult with image bytes and cost information.
    """
    settings = get_settings()
    # Use the async client so the long-running Gemini call doesn't block the event loop
    client = genai.Client(api_key=settings.google_ai_api_key)

    # Build image mapping header.
    # Layout: ambassador (anchor) → user photos → ambassador (anchor again).
    # Sending the ambassador photo twice uses both available character slots
    # for Person 2, which significantly strengthens identity anchoring.
    n = len(user_photo_bytes_list)
    ambassador_slot = 1
    user_start = 2
    user_end = user_start + n - 1
    ambassador_repeat = user_end + 1

    if n > 1:
        identity_header = (
            f"IMAGE MAPPING: "
            f"Image {ambassador_slot} = AMBASSADOR (Person 2, first anchor). "
            f"Images {user_start}–{user_end} = USER (Person 1) — use all {n} photos together "
            f"to reconstruct their exact face, bone structure, skin tone, and hair. "
            f"Image {ambassador_repeat} = AMBASSADOR (Person 2, second anchor — same person, reinforces identity). "
            f"NEVER swap Person 1 and Person 2."
        )
    else:
        identity_header = (
            "IMAGE MAPPING: "
            f"Image {ambassador_slot} = AMBASSADOR (Person 2, first anchor). "
            f"Image {user_start} = USER (Person 1). "
            f"Image {ambassador_repeat} = AMBASSADOR (Person 2, second anchor — same person, reinforces identity). "
            "NEVER swap Person 1 and Person 2."
        )

    # Use replace instead of .format() to avoid KeyError if the admin adds {other_vars}
    raw_prompt = prompt_template.replace("{extra}", extra_prompt or "").strip()
    prompt_text = identity_header + "\n\n" + raw_prompt

    # Build parts: ambassador (anchor) → user photos → ambassador (anchor again)
    ambassador_part = types.Part(
        inline_data=types.Blob(
            mime_type=_detect_mime(ambassador_photo_bytes),
            data=ambassador_photo_bytes,
        )
    )
    parts: list[types.Part] = [types.Part(text=prompt_text)]
    parts.append(ambassador_part)
    for photo_bytes in user_photo_bytes_list:
        parts.append(
            types.Part(
                inline_data=types.Blob(
                    mime_type=_detect_mime(photo_bytes),
                    data=photo_bytes,
                )
            )
        )
    parts.append(ambassador_part)

    contents = [types.Content(role="user", parts=parts)]

    logger.info("Sending image generation request to gemini-3-pro-image")

    response = await client.aio.models.generate_content(
        model="gemini-3-pro-image",
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            system_instruction=(
                "You are a photorealistic image compositor. "
                "Your absolute constraint: reproduce every person's face exactly as shown "
                "in the reference photos — identical facial features, skin tone, ethnicity, "
                "hair, and proportions. Never alter, idealize, westernize, or average any "
                "person's appearance. The output must be indistinguishable from a real photograph."
            ),
        ),
    )

    # Extract image bytes from response
    image_bytes: bytes | None = None
    for candidate in (response.candidates or []):
        if not candidate.content or not candidate.content.parts:
            continue
        for part in candidate.content.parts:
            if part.inline_data and part.inline_data.data:
                image_bytes = part.inline_data.data
                break
        if image_bytes:
            break

    if image_bytes is None:
        raise ValueError("Gemini API returned no image in the response")

    # Extract token usage for cost tracking
    usage = response.usage_metadata
    total_images = len(user_photo_bytes_list) + 2  # user photos + ambassador sent twice
    input_tokens = (
        (usage.prompt_token_count if usage else None)
        or (total_images * TOKENS_PER_INPUT_IMAGE + 300)
    )
    output_tokens = (
        (usage.candidates_token_count if usage else None) or 1120
    )

    cost = _calculate_cost(input_tokens)

    logger.info(
        "Image generated. input_tokens=%d, cost=$%.6f",
        input_tokens,
        float(cost),
    )

    return GenerationResult(
        image_bytes=image_bytes,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
    )
