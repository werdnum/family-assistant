"""A stand-in picture for cameras and images, whose frames were never recorded.

It says plainly what it is, so a model looking at it is not misled into
describing a scene, and a grader can recognise it.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw


def placeholder_jpeg(
    title: str, moment: str, width: int = 640, height: int = 360
) -> bytes:
    image = Image.new("RGB", (width, height), (96, 96, 96))
    draw = ImageDraw.Draw(image)
    lines = [title, moment, "No recorded image: evaluation sandbox"]
    for index, line in enumerate(lines):
        draw.text((20, 20 + 24 * index), line, fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()
