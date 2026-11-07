"""The copy of a fetched file that a model is shown."""

from __future__ import annotations

import io

from PIL import Image

from family_assistant.tools.model_view import (
    MODEL_VIEW_MAX_EDGE,
    MODEL_VIEW_PDF_LIMIT,
    model_view_of,
)


def _encode(size: tuple[int, int], image_format: str, mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, "red").save(buffer, format=image_format)
    return buffer.getvalue()


def _decoded_size(content: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(content)) as image:
        assert image.format == "JPEG"
        return image.size


def test_large_photo_is_downscaled_to_a_jpeg() -> None:
    view = model_view_of(_encode((4000, 3000), "JPEG"), "image/jpeg")

    assert view is not None
    assert view.mime_type == "image/jpeg"
    assert _decoded_size(view.content) == (MODEL_VIEW_MAX_EDGE, 1176)
    assert (
        view.note
        == f"Shown to you downscaled from 4000x3000 to {MODEL_VIEW_MAX_EDGE}x1176."
    )


def test_small_png_is_shown_at_full_size_without_a_note() -> None:
    view = model_view_of(_encode((640, 480), "PNG", mode="RGBA"), "image/png")

    assert view is not None
    assert _decoded_size(view.content) == (640, 480)
    assert not view.note


def test_heic_becomes_a_jpeg_every_provider_reads() -> None:
    view = model_view_of(_encode((3024, 4032), "HEIF"), "image/heic")

    assert view is not None
    assert view.mime_type == "image/jpeg"
    assert _decoded_size(view.content) == (1176, MODEL_VIEW_MAX_EDGE)


def test_exif_rotation_is_applied_and_reported_in_display_orientation() -> None:
    image = Image.new("RGB", (4000, 3000), "red")
    exif = image.getexif()
    exif[0x0112] = 6  # Orientation: rotate 90 CW to display.
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)

    view = model_view_of(buffer.getvalue(), "image/jpeg")

    assert view is not None
    assert _decoded_size(view.content) == (1176, MODEL_VIEW_MAX_EDGE)
    assert "from 3000x4000" in view.note


def test_pdf_under_the_cap_passes_through() -> None:
    pdf = b"%PDF-1.4 tiny"

    view = model_view_of(pdf, "application/pdf")

    assert view is not None
    assert view.content == pdf
    assert view.mime_type == "application/pdf"


def test_pdf_over_the_cap_is_not_shown() -> None:
    assert model_view_of(b"0" * (MODEL_VIEW_PDF_LIMIT + 1), "application/pdf") is None


def test_undecodable_image_is_not_shown() -> None:
    assert model_view_of(b"not an image", "image/png") is None


def test_other_types_are_not_shown() -> None:
    assert model_view_of(b"PK\x03\x04", "application/zip") is None
