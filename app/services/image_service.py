"""
Image post-processing service.
Adds the TBC logo watermark to generated images.
"""
import io
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

# Watermark configuration
LOGO_PADDING = 16       # pixels from the corner
LOGO_MAX_WIDTH = 120    # max width of the logo in the output image
LOGO_MAX_HEIGHT = 80    # max height of the logo in the output image


def add_logo_watermark(image_bytes: bytes, logo_bytes: bytes) -> bytes:
    """
    Overlay the TBC logo in the top-right corner of the generated image.

    Args:
        image_bytes: Raw bytes of the generated image (JPEG or PNG).
        logo_bytes: Raw bytes of the TBC logo (PNG with transparency preferred).

    Returns:
        JPEG bytes of the watermarked image.
    """
    with Image.open(io.BytesIO(image_bytes)).convert("RGBA") as base:
        with Image.open(io.BytesIO(logo_bytes)).convert("RGBA") as logo:
            # Scale logo to fit within max dimensions while preserving aspect ratio
            logo_w, logo_h = logo.size
            scale = min(LOGO_MAX_WIDTH / logo_w, LOGO_MAX_HEIGHT / logo_h, 1.0)
            new_w = int(logo_w * scale)
            new_h = int(logo_h * scale)
            logo = logo.resize((new_w, new_h), Image.LANCZOS)

            # Position: top-right corner
            pos_x = base.width - new_w - LOGO_PADDING
            pos_y = LOGO_PADDING

            # Composite the logo onto the base image
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            overlay.paste(logo, (pos_x, pos_y), mask=logo)
            result = Image.alpha_composite(base, overlay)

        # Convert to RGB for JPEG output
        rgb = result.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=90)
        return buf.getvalue()


def add_logo_watermark_if_available(image_bytes: bytes, logo_path: str | None) -> bytes:
    """
    Apply watermark only if a logo file path is provided and the file exists.
    Returns the original image bytes if no logo is available.
    """
    if not logo_path:
        return image_bytes

    full_path = Path(logo_path)
    if not full_path.exists():
        logger.warning("Logo file not found at %s, skipping watermark", logo_path)
        return image_bytes

    try:
        logo_bytes = full_path.read_bytes()
        return add_logo_watermark(image_bytes, logo_bytes)
    except Exception:
        logger.exception("Failed to apply logo watermark, returning original image")
        return image_bytes
