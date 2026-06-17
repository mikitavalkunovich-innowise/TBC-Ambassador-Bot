"""
Image post-processing service.

Two post-processing modes:
1. Frame compositing  — embed the generated photo into the branded Instagram
   frame template (auto-detects the white area, cover-crops the photo to fit).
2. Logo watermark     — fallback when no frame is configured; overlays the TBC
   logo in the top-right corner of the raw generated photo.
"""
import io
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

# --- Static frames bundled with the application ---
_STATIC_FRAMES_DIR = Path(__file__).parent.parent / "static" / "frames"

# ---------------------------------------------------------------------------
# WebP compression
# ---------------------------------------------------------------------------

WEBP_QUALITY = 85
WEBP_METHOD = 6          # slowest encoding = best compression ratio
WEBP_MAX_SIDE = 2048     # resize if longest side exceeds this


def compress_to_webp(
    data: bytes,
    quality: int = WEBP_QUALITY,
    max_side: int = WEBP_MAX_SIDE,
) -> bytes:
    """
    Convert any image bytes to WebP lossy.

    Achieves 25–34% smaller files vs JPEG at the same visual quality.
    Resizes proportionally if the longest side exceeds max_side.
    """
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=quality, method=WEBP_METHOD)
        return buf.getvalue()

# Watermark fallback configuration
LOGO_PADDING = 16
LOGO_MAX_WIDTH = 120
LOGO_MAX_HEIGHT = 80

_MIN_WHITE_BOX_PX = 100  # minimum side length to accept a detected transparent hole


# ---------------------------------------------------------------------------
# Frame compositing
# ---------------------------------------------------------------------------

def _find_transparent_box(frame: Image.Image) -> tuple[int, int, int, int] | None:
    """
    Return (left, top, right, bottom) of the transparent "hole" in the frame.

    The frame PNG ships with full transparency in the content placeholder area
    (alpha == 0) so the generated photo can be placed underneath.  This function
    creates a mask of transparent pixels and returns their bounding box.
    Returns None if no suitable transparent region is found.
    """
    # Extract alpha channel; transparent pixels become white (255) in the mask
    alpha = frame.getchannel("A")
    transparent_mask = alpha.point(lambda a: 255 if a < 128 else 0, mode="L")
    bbox = transparent_mask.getbbox()
    if bbox is None:
        return None
    if (bbox[2] - bbox[0]) < _MIN_WHITE_BOX_PX or (bbox[3] - bbox[1]) < _MIN_WHITE_BOX_PX:
        return None
    return bbox


def _cover_crop(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """
    Scale *image* so it fully covers *target_w* × *target_h*, then center-crop.
    Preserves the original aspect ratio (no black bars, no stretching).
    """
    img_w, img_h = image.size
    scale = max(target_w / img_w, target_h / img_h)
    new_w = max(int(img_w * scale), target_w)
    new_h = max(int(img_h * scale), target_h)
    scaled = image.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return scaled.crop((left, top, left + target_w, top + target_h))


def composite_into_frame(image_bytes: bytes, frame_bytes: bytes) -> bytes:
    """
    Embed the generated photo into the transparent hole of the frame overlay.

    The frame PNG contains a fully transparent rectangle (alpha=0) that acts as
    the content placeholder.  The pipeline:
    1. Detect the transparent bounding box in the frame.
    2. Cover-crop the generated photo to exactly fill that area.
    3. Build a full-frame canvas, place the cropped photo at the hole position,
       then alpha-composite the frame overlay on top so the branded border,
       logo, and hashtag text remain crisp above the photo.
    4. Return JPEG bytes of the final portrait image.
    """
    with Image.open(io.BytesIO(frame_bytes)).convert("RGBA") as frame:
        hole = _find_transparent_box(frame)

        if hole is None:
            logger.warning("Could not detect transparent area in frame template; returning compressed photo")
            return compress_to_webp(image_bytes)

        bx, by, bx2, by2 = hole
        box_w, box_h = bx2 - bx, by2 - by

        with Image.open(io.BytesIO(image_bytes)).convert("RGBA") as photo:
            cropped = _cover_crop(photo, box_w, box_h)

        # Start with a white canvas the same size as the frame
        canvas = Image.new("RGBA", frame.size, (255, 255, 255, 255))
        # Place the photo at the hole position
        canvas.paste(cropped, (bx, by))
        # Overlay the frame on top — branded areas cover the photo
        result = Image.alpha_composite(canvas, frame)

    buf = io.BytesIO()
    result.convert("RGB").save(buf, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
    return buf.getvalue()


def composite_into_frame_if_available(
    image_bytes: bytes,
    frame_path: str | None,
) -> bytes:
    """
    Apply frame compositing only when a valid frame path is provided.

    *frame_path* may be:
    - An absolute path to an admin-uploaded file  (e.g. /data/uploads/frames/…)
    - A path relative to app/static/frames/       (e.g. frame_ru.png)
    Falls back to returning the original bytes when no usable frame is found.
    """
    if not frame_path:
        return image_bytes

    path = Path(frame_path)
    if not path.is_absolute():
        path = _STATIC_FRAMES_DIR / path

    if not path.exists():
        logger.warning("Frame file not found: %s", path)
        return image_bytes

    try:
        return composite_into_frame(image_bytes, path.read_bytes())
    except Exception:
        logger.exception("Frame compositing failed, returning original image")
        return image_bytes


# ---------------------------------------------------------------------------
# Logo watermark (fallback)
# ---------------------------------------------------------------------------

def add_logo_watermark(image_bytes: bytes, logo_bytes: bytes) -> bytes:
    """Overlay the TBC logo in the top-right corner of the generated image."""
    with Image.open(io.BytesIO(image_bytes)).convert("RGBA") as base:
        with Image.open(io.BytesIO(logo_bytes)).convert("RGBA") as logo:
            logo_w, logo_h = logo.size
            scale = min(LOGO_MAX_WIDTH / logo_w, LOGO_MAX_HEIGHT / logo_h, 1.0)
            new_w, new_h = int(logo_w * scale), int(logo_h * scale)
            logo = logo.resize((new_w, new_h), Image.LANCZOS)

            pos_x = base.width - new_w - LOGO_PADDING
            pos_y = LOGO_PADDING

            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            overlay.paste(logo, (pos_x, pos_y), mask=logo)
            result = Image.alpha_composite(base, overlay)

        buf = io.BytesIO()
        result.convert("RGB").save(buf, format="JPEG", quality=90)
        return buf.getvalue()


def add_logo_watermark_if_available(image_bytes: bytes, logo_path: str | None) -> bytes:
    """Apply logo watermark only if the file exists; return original bytes otherwise."""
    if not logo_path:
        return image_bytes
    full_path = Path(logo_path)
    if not full_path.exists():
        logger.warning("Logo file not found at %s, skipping watermark", logo_path)
        return image_bytes
    try:
        return add_logo_watermark(image_bytes, full_path.read_bytes())
    except Exception:
        logger.exception("Failed to apply logo watermark, returning original image")
        return image_bytes
