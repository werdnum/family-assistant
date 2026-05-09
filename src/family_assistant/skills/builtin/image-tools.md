---
name: Image Tools
description: Guide for AI image generation, image transformation/editing, and image highlighting/annotation features.
---

# Image Tools

## Three Capabilities

### 1. Generate New Images

Create images from text descriptions using AI (`generate_image` tool).

- "Generate an image of a sunset over mountains"
- "Create a photorealistic image of a golden retriever puppy"

**Styles**: photorealistic, artistic, or auto (default).

**Tips for better results**:

- Be descriptive (subject, setting, lighting, colors, mood)
- Specify composition (close-up, wide-angle)
- Mention art style if desired

### 2. Transform Existing Images

Send or attach one or more images with transformation instructions. Use multiple reference images
when the request combines subjects, transfers style, or uses one image as the primary scene and
another as a reference:

- **Edit content**: "Remove the car from this photo", "Add clouds to the sky"
- **Change style**: "Make this look like a watercolor painting", "Convert to black and white"
- **Create variations**: "Show this scene at night", "Make the colors more vibrant"
- **Combine references**: "Put the dog from the first image into the room from the second image",
  "Use the second image's style for the portrait in the first image"

### 3. Highlight and Annotate

Draw shapes on images to mark areas of interest:

- **Shapes**: Rectangle (default), Circle
- **Colors**: red (default), green, blue, yellow, orange, purple, cyan, magenta

Examples:

- "Highlight the cat with a red rectangle"
- "Circle all the faces in blue"

## Artist Profile

For complex creative work, use `/artist` or `/image` to activate the specialized artist profile with
enhanced prompt refinement and video generation access.

## Supported Formats

JPEG, PNG, GIF, WebP, BMP, TIFF. Max 20MB for processing.
