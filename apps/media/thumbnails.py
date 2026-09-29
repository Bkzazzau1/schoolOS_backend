"""Turning an image's real bytes into a small preview. The only media type SchoolOS builds a thumbnail for today;
video is validated, stored and served, but not yet transcoded or thumbnailed - see docs/MEDIA.md.
"""

from io import BytesIO

from PIL import Image, ImageOps

THUMBNAIL_MAX_SIDE = 480
THUMBNAIL_QUALITY = 82


def build_image_thumbnail(data: bytes) -> tuple[bytes, int, int, str]:
    """A JPEG thumbnail, its width and height, and its mime type. Orientation (a photo taken sideways) is applied
    before resizing, so a thumbnail never comes out rotated the way the raw bytes happened to be stored."""
    with Image.open(BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image) or image
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((THUMBNAIL_MAX_SIDE, THUMBNAIL_MAX_SIDE))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
        return buffer.getvalue(), image.width, image.height, "image/jpeg"


def probe_dimensions(data: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image) or image
            return image.width, image.height
    except Exception:  # noqa: BLE001 - a corrupt or unreadable image is reported by the caller as a failed verify
        return None
