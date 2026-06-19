"""
AI image generation service using Gemini 3 Pro Image (Nano Banana Pro).
Model: gemini-3-pro-image
"""
import io
import logging
from dataclasses import dataclass
from decimal import Decimal

from PIL import Image
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


def _face_crop(image_bytes: bytes) -> bytes:
    """Crop the middle vertical zone of an image where the face typically appears.

    Targets the 10%–65% vertical range, which captures the face and shoulders
    for both close-up selfies and 3/4-body portrait shots.
    Returns JPEG bytes at quality 92.
    """
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    top = int(h * 0.10)
    bottom = int(h * 0.65)
    cropped = img.crop((0, top, w, bottom))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


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

    Image layout sent to Gemini (with face crops):
        [prompt text]
        → AMBASSADOR face crop  (high-res facial detail)
        → AMBASSADOR full photo (body, clothing, context)
        → USER face crop        (high-res facial detail)
        → USER full photo(s)    (body, clothing, context)
        → AMBASSADOR full photo (second identity anchor)

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
    # Build the image mapping header so the model knows exactly who is who.
    # Image indices start from 1 and are laid out as:
    #   amb_face_crop | amb_full | user_face_crop | user_full(s) | amb_full_anchor
    # -------------------------------------------------------------------------
    idx = 1
    amb_face_idx: int | None = None
    if ambassador_face_crop_bytes is not None:
        amb_face_idx = idx
        idx += 1
    amb_full_idx = idx
    idx += 1
    user_face_idx = idx   # dynamically cropped from first user photo
    idx += 1
    user_start_idx = idx
    user_end_idx = idx + n - 1
    idx += n
    amb_anchor_idx = idx

    amb_face_label = (
        f"Image {amb_face_idx} = AMBASSADOR face crop (close-up, high detail). "
        if amb_face_idx is not None else ""
    )
    if n > 1:
        user_label = (
            f"Images {user_face_idx} = USER face crop (close-up). "
            f"Images {user_start_idx}–{user_end_idx} = USER full photos — use all {n} together "
            f"to reconstruct their exact face, bone structure, skin tone, and hair. "
        )
    else:
        user_label = (
            f"Image {user_face_idx} = USER face crop (close-up). "
            f"Image {user_start_idx} = USER full photo. "
        )

    identity_header = (
        "IMAGE MAPPING: "
        + amb_face_label
        + f"Image {amb_full_idx} = AMBASSADOR full photo (Person 2, first anchor). "
        + user_label
        + f"Image {amb_anchor_idx} = AMBASSADOR full photo (Person 2, second anchor — same person). "
        "NEVER swap Person 1 and Person 2."
    )

    # Use replace instead of .format() to avoid KeyError if the admin adds {other_vars}
    raw_prompt = prompt_template.replace("{extra}", extra_prompt or "").strip()
    prompt_text = identity_header + "\n\n" + raw_prompt

    # -------------------------------------------------------------------------
    # Assemble parts in the declared order
    # -------------------------------------------------------------------------
    def _make_part(data: bytes) -> types.Part:
        return types.Part(
            inline_data=types.Blob(mime_type=_detect_mime(data), data=data)
        )

    ambassador_full_part = _make_part(ambassador_photo_bytes)

    parts: list[types.Part] = [types.Part(text=prompt_text)]

    if ambassador_face_crop_bytes is not None:
        parts.append(_make_part(ambassador_face_crop_bytes))

    parts.append(ambassador_full_part)

    # User face crop (heuristic crop of the main selfie)
    user_face_crop = _face_crop(user_photo_bytes_list[0])
    parts.append(_make_part(user_face_crop))

    # All user photos (full)
    for photo_bytes in user_photo_bytes_list:
        parts.append(_make_part(photo_bytes))

    # Ambassador second anchor
    parts.append(ambassador_full_part)

    contents = [types.Content(role="user", parts=parts)]

    # Total image count for cost estimation fallback
    extra_images = (1 if ambassador_face_crop_bytes is not None else 0) + 1  # amb crop + user crop
    total_images = n + 2 + extra_images  # user photos + amb×2 + face crops

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
