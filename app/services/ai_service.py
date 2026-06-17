"""
AI image generation service using Gemini 3 Pro Image (Nano Banana Pro).
Model: gemini-3-pro-image
"""
import io
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


@dataclass
class GenerationResult:
    image_bytes: bytes
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


def _get_client() -> genai.Client:
    settings = get_settings()
    return genai.Client(api_key=settings.google_ai_api_key)


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
        ambassador_photo_bytes: JPEG bytes of the ambassador's reference photo.
        prompt_template: Generation prompt template (may contain {extra} placeholder).
        extra_prompt: Optional additional instructions from the user.

    Returns:
        GenerationResult with image bytes and cost information.
    """
    settings = get_settings()
    client = genai.Client(api_key=settings.google_ai_api_key)

    # Build the prompt — prepend a multi-photo identity header when extra angles are provided
    n = len(user_photo_bytes_list)
    if n > 1:
        identity_header = (
            f"IDENTITY REFERENCE: The user (Person 1) is shown in the first {n} photos provided "
            f"— use all {n} together to accurately reconstruct their exact facial features, "
            f"bone structure, skin tone, and hair. The ambassador (Person 2) is in the last photo."
        )
        raw_prompt = prompt_template.format(extra=extra_prompt or "").strip()
        prompt_text = identity_header + "\n\n" + raw_prompt
    else:
        prompt_text = prompt_template.format(extra=extra_prompt or "").strip()

    # Build parts: text prompt → all user photos → ambassador photo
    parts: list[types.Part] = [types.Part(text=prompt_text)]
    for photo_bytes in user_photo_bytes_list:
        parts.append(
            types.Part(
                inline_data=types.Blob(
                    mime_type="image/jpeg",
                    data=photo_bytes,
                )
            )
        )
    parts.append(
        types.Part(
            inline_data=types.Blob(
                mime_type="image/jpeg",
                data=ambassador_photo_bytes,
            )
        )
    )

    contents = [types.Content(role="user", parts=parts)]

    logger.info("Sending image generation request to gemini-3-pro-image")

    response = client.models.generate_content(
        model="gemini-3-pro-image",
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
        ),
    )

    # Extract image bytes from response
    image_bytes: bytes | None = None
    for candidate in response.candidates:
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
    total_images = len(user_photo_bytes_list) + 1  # user photos + ambassador
    input_tokens = usage.prompt_token_count if usage else (total_images * TOKENS_PER_INPUT_IMAGE + 300)
    output_tokens = usage.candidates_token_count if usage else 1120

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
