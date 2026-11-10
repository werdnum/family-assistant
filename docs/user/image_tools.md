# Image Tools Guide

This guide covers generating new images, transforming or combining existing images, and annotating
images with shapes in Telegram, the web app, and the iOS app. For video generation and charts, see
[media.md](media.md) and [data_visualization.md](data_visualization.md).

## Image Generation (`generate_image`)

Create new images from a text description when no reference image is involved:

- "Generate a photorealistic image of a mountain lake at sunrise"
- "Create an artistic illustration of a dragon flying over a castle"

**Style options:** `photorealistic`, `artistic`, or `auto` (default). Include subject, setting,
lighting, color palette, and framing details for better results.

## Image Transformation (`transform_image`)

Attach or reference one or more existing images and describe the edit, style transfer, variation, or
composition:

- **Content edits:** "Remove the car from this photo", "Replace the background with a beach scene",
  "Put the dog from the first image into the room from the second"
- **Style changes:** "Make this look like a watercolor painting", "Convert to high-contrast black
  and white"
- **Variations:** "Show this scene at night in winter"

## Image Highlighting (`highlight_image`)

Draw deterministic colored shapes on an attached image to mark objects or regions (rather than
making generative edits with `transform_image`):

- **Shapes:** `rectangle` (default) or `circle`
- **Colors:** `red` (default), `green`, `blue`, `yellow`, `orange`, `purple`, `cyan`, `magenta`
- **Examples:** "Highlight the faces in this photo with red rectangles", "Circle the bird in green"

## Input Formats and Limits

- **Supported formats:** JPEG, PNG, GIF, WebP, BMP, TIFF
- **Size limit:** 20 MB per image (see [attachments.md](attachments.md))
