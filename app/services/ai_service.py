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
    ambassador_face_crop_bytes: bytes | None = None,
) -> GenerationResult:
    """
    Generate a composite photo of the user and the TBC ambassador.

    Image layout sent to Gemini (interleaved text labels + images):
        [intro text + prompt]
        → "AMBASSADOR face close-up:" + AMBASSADOR face crop (if provided)
        → "AMBASSADOR full body reference:" + AMBASSADOR full photo
        → "USER reference photo(s):" + USER photo(s)
        → "AMBASSADOR identity anchor:" + AMBASSADOR full photo

    Args:
        user_photo_bytes_list: One or more selfies of the user (main + optional extra angles).
        ambassador_photo_bytes: Raw bytes of the ambassador's reference photo (JPEG/PNG).
        prompt_template: Generation prompt template (may contain {extra} placeholder).
        extra_prompt: Optional additional instructions from the user.
        ambassador_face_crop_bytes: Pre-cropped face region of the ambassador photo (optional).
                                    If None, only the full photo is used for the ambassador.

    Returns:
        GenerationResult with image bytes and cost information.
    """
    settings = get_settings()
    client = genai.Client(api_key=settings.google_ai_api_key)

    n = len(user_photo_bytes_list)

    # -------------------------------------------------------------------------
    # Assemble parts with interleaved text labels directly before each image.
    # The model processes tokens sequentially, so labelling each photo right
    # before it appears is more reliable than a single upfront IMAGE MAPPING block.
    # -------------------------------------------------------------------------
    def _make_part(data: bytes) -> types.Part:
        return types.Part(
            inline_data=types.Blob(mime_type=_detect_mime(data), data=data)
        )

    # Use replace instead of .format() to avoid KeyError if the admin adds {other_vars}
    raw_prompt = prompt_template.replace("{extra}", extra_prompt or "").strip()

    intro = (
        "Generate a photorealistic composite image following these instructions.\n"
        "Reproduce each person's face exactly as shown in the labeled reference photos below. "
        "NEVER swap Person 1 and Person 2.\n\n"
        + raw_prompt
    )

    ambassador_full_part = _make_part(ambassador_photo_bytes)

    parts: list[types.Part] = [types.Part(text=intro)]

    if ambassador_face_crop_bytes is not None:
        parts.append(types.Part(text="AMBASSADOR (Person 2) — face close-up for maximum facial detail:"))
        parts.append(_make_part(ambassador_face_crop_bytes))

    parts.append(types.Part(text="AMBASSADOR (Person 2) — full body reference photo:"))
    parts.append(ambassador_full_part)

    if n == 1:
        parts.append(types.Part(text="USER (Person 1) — reference photo:"))
        parts.append(_make_part(user_photo_bytes_list[0]))
    else:
        parts.append(types.Part(
            text=f"USER (Person 1) — {n} reference photos, use all together to reconstruct exact face:"
        ))
        for photo_bytes in user_photo_bytes_list:
            parts.append(_make_part(photo_bytes))

    parts.append(types.Part(text="AMBASSADOR (Person 2) — identity anchor (same person as above):"))
    parts.append(ambassador_full_part)

    contents = [types.Content(role="user", parts=parts)]

    # Total image count for cost estimation fallback
    extra_images = 1 if ambassador_face_crop_bytes is not None else 0  # amb face crop only
    total_images = n + 2 + extra_images  # user photos + amb×2 + optional amb face crop

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
