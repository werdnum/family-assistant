---
name: Image Tools
description: Generate images and video, edit or combine reference images, and annotate images with boxes or circles. Load before any image or video generation or editing request. Activates the image and video tools.
activate_tools:
  - generate_image
  - transform_image
  - highlight_image
  - generate_video
---

# Image Tools

## Choosing a Tool

- **`transform_image`** whenever the user provides a reference image, or wants a result that edits,
  builds on or stays visually consistent with an existing one: object removal or addition,
  background changes, retouching, recolouring, re-rendering in a new style, variations, combining
  several images ("put the dog from the first image into the room from the second"), or keeping a
  character consistent ("draw this same character doing X"). Pass the image ids via `images`,
  primary image first, and describe the change in `instruction`.
- **`generate_image`** only when there is no reference image and the user wants something created
  entirely from a text description.
- **`highlight_image`** for deterministic markup of a provided image: boxes, circles, arrows or
  labels that point something out ("circle the bird", "highlight the error"). `transform_image`
  makes generative edits and is the wrong tool for precise markup. Shapes are rectangle (default) or
  circle; colours are red (default), green, blue, yellow, orange, purple, cyan and magenta.
- **`generate_video`** for video.

Reference images are listed by id in the `<attachment_metadata>` block after the request. If the
request is about a specific image and you cannot find its id, or `transform_image` fails on it, do
not fall back to `generate_image`: a text-to-image call cannot see the picture, so it invents a
plausible substitute that looks like an answer and is not one. Say what went wrong instead.

## Writing Image Prompts

Expand a vague request into a detailed prompt: subject, action or context, art style
("photorealistic", "oil painting", "cinematic", "3D render", "anime"), lighting and mood, colour
palette, and framing ("wide angle", "close-up", "macro", "drone shot"). Describe anything the user
wants avoided. A useful order is subject, action or context, style, lighting, colour palette, then
framing, so "make a cat" becomes "A fluffy Persian cat sitting on a velvet cushion in a sunlit
Victorian room, photorealistic, 4k".

## Writing Video Prompts

- Describe the motion: what moves and how ("pan", "zoom", "rotate", "track"). Static prompts give
  static videos.
- Name the visual style ("cinematic", "grainy 8mm film", "crisp 4k").
- With `first_frame_image` or reference `images`, keep the prompt consistent with what they show.
- The default model (Gemini Omni Flash) makes fast, low-cost short clips. `negative_prompt` or
  `last_frame_image` switch to Veo automatically; pass a `veo-*` model id (for example
  `model="veo-3.1-generate-preview"`) for higher-quality cinematic clips. Veo clips default to 8
  seconds.
- Generation can take a few minutes; say so when you start one.

## After Generating

Results are displayed to the user automatically. An attachment message that appears after a
generation is that display, not a new request. Generate one image per request unless the user asks
for variations or more.

Supported input formats are JPEG, PNG, GIF, WebP, BMP and TIFF, up to 20MB.
