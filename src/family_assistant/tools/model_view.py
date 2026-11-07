"""The copy of a fetched file that a model is shown.

A tool that stores a file in the attachment registry hands back a reference,
and a provider can only show the model bytes. Inlining the stored original is
the wrong copy for photos: an 8 MB drone shot costs as much request budget as
the rest of the turn, and several of them exceed Gemini's 20 MB inline limit,
while a model sees no more detail than a downscaled copy carries. Every image
is therefore re-encoded to a JPEG no longer than ``MODEL_VIEW_MAX_EDGE`` on its
long edge, which also turns HEIC (unreadable by Anthropic and OpenAI) into a
format every provider accepts. PDFs are passed through under a size cap.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

logger = logging.getLogger(__name__)

register_heif_opener()

# Anthropic's recommended maximum: larger images are scaled down server-side
# before the model sees them, so sending more pixels only costs bytes.
MODEL_VIEW_MAX_EDGE = 1568
MODEL_VIEW_JPEG_QUALITY = 85
MODEL_VIEW_PDF_LIMIT = 8 * 1024 * 1024


@dataclass(frozen=True)
class ModelView:
    """Bytes to put in front of the model, and a note on how they were derived."""

    content: bytes
    mime_type: str
    note: str


def _downscaled_jpeg(content: bytes) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    with Image.open(io.BytesIO(content)) as opened:
        original_size = opened.size
        # JPEG only: decode at a reduced scale instead of full resolution.
        opened.draft("RGB", (MODEL_VIEW_MAX_EDGE, MODEL_VIEW_MAX_EDGE))
        decoded_size = opened.size
        image = ImageOps.exif_transpose(opened)
        if image.size != decoded_size:
            original_size = (original_size[1], original_size[0])
        image.thumbnail(
            (MODEL_VIEW_MAX_EDGE, MODEL_VIEW_MAX_EDGE), Image.Resampling.LANCZOS
        )
        if image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info:
            # JPEG has no alpha; dropping it would show whatever colour the
            # transparent pixels carry, often black under black line art.
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        elif image.mode != "RGB":
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=MODEL_VIEW_JPEG_QUALITY)
        return buffer.getvalue(), original_size, image.size


def model_view_of(content: bytes, mime_type: str) -> ModelView | None:
    """The copy of ``content`` a model should see, or None if it gets none.

    None means the model is told about the file but not shown it: a type no
    provider renders, a PDF over the cap, or an image Pillow cannot decode.
    """
    if mime_type == "application/pdf":
        if len(content) > MODEL_VIEW_PDF_LIMIT:
            return None
        return ModelView(content=content, mime_type=mime_type, note="")
    if not mime_type.startswith("image/"):
        return None
    try:
        jpeg, original_size, shown_size = _downscaled_jpeg(content)
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
    ) as exc:
        logger.warning("Could not decode %s for the model: %s", mime_type, exc)
        return None
    note = ""
    if shown_size != original_size:
        note = (
            f"Shown to you downscaled from {original_size[0]}x{original_size[1]}"
            f" to {shown_size[0]}x{shown_size[1]}."
        )
    return ModelView(content=jpeg, mime_type="image/jpeg", note=note)
