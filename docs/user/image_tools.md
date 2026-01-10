# Image Tools Guide

This guide explains how to use the assistant's image capabilities, including generating new images
with AI and editing or annotating existing images.

## Overview

The assistant can help you with images in three main ways:

1. **Generate new images** from text descriptions using AI
2. **Transform existing images** by applying edits, style changes, or variations
3. **Highlight and annotate** images by drawing shapes to mark areas of interest

These features work across both Telegram and the Web Interface.

## Image Generation

Create new images from scratch by describing what you want to see.

### Basic Usage

Simply describe the image you want:

- "Generate an image of a sunset over mountains"
- "Create a picture of a cozy reading nook with warm lighting"
- "Make an image of a futuristic cityscape at night"

### Style Options

You can specify a style for your generated images:

- **Photorealistic**: Creates images that look like photographs with realistic lighting, textures,
  and details
- **Artistic**: Produces stylized illustrations with creative flair and composition
- **Auto** (default): The AI chooses the most appropriate style based on your description

Examples with style:

- "Generate a photorealistic image of a mountain lake at sunrise"
- "Create an artistic illustration of a dragon flying over a castle"
- "Make a photorealistic portrait of a calico cat"

### Tips for Better Results

1. **Be descriptive**: Include details about the subject, setting, lighting, colors, and mood
   - Instead of: "a cat"
   - Try: "a fluffy orange tabby cat sitting on a windowsill, afternoon sunlight streaming through
     the window"
2. **Specify the composition**: Mention camera angles or framing if relevant
   - "close-up of a blooming rose with water droplets"
   - "wide-angle view of a crowded city street"
3. **Describe the atmosphere**: Include mood and lighting details
   - "soft morning light", "dramatic shadows", "neon cyberpunk lighting"
4. **Mention art style if desired**: Reference specific styles or aesthetics
   - "in the style of watercolor painting"
   - "as a vintage travel poster"
   - "with a minimalist design"

## Image Transformation

Transform existing images by describing the changes you want. Send or attach an image along with
your transformation request.

### Types of Transformations

**Editing (modify content):**

- "Remove the car from this photo"
- "Add clouds to the sky"
- "Replace the background with a beach scene"

**Styling (change appearance):**

- "Make this look like a watercolor painting"
- "Convert this to black and white"
- "Apply a vintage sepia filter"
- "Make it look like an anime illustration"

**Variations (create alternatives):**

- "Show this scene at night"
- "Make the colors more vibrant"
- "Create a winter version of this landscape"

### How to Transform Images

1. Send or attach the image you want to modify
2. In the same message (or as a follow-up), describe what transformation you want
3. The assistant will process the image and return the transformed version

Examples:

- [Attach photo] "Make this look like an oil painting"
- [Attach photo] "Brighten this image and add more contrast"
- [Attach photo] "Convert to black and white with high contrast"

## Image Highlighting

Mark and annotate areas on images by drawing colored shapes. This is useful for pointing out
specific details, marking objects, or creating visual annotations.

### When to Use Highlighting

- Marking objects detected in an image ("highlight where the birds are")
- Pointing out areas of interest ("circle the damaged area")
- Creating visual annotations for instructions
- Marking multiple items for comparison

### Available Options

**Shapes:**

- **Rectangle**: Draws a rectangular outline (default)
- **Circle**: Draws a circular outline

**Colors:**

- red (default)
- green
- blue
- yellow
- orange
- purple
- cyan
- magenta

### Examples

- "Highlight the faces in this photo with red rectangles"
- "Circle the bird in green"
- "Mark all the vehicles with blue rectangles"
- "Highlight the text areas with yellow boxes"

The assistant can automatically detect objects in images and highlight them:

- "Find and highlight all the animals in this image"
- "Mark all the text visible in this photo"
- "Highlight the person on the left in red and the person on the right in blue"

## Working with Image Attachments

### Sending Images

**Via Telegram:**

- Send the image directly in the chat
- Add your request in the caption or a follow-up message

**Via Web Interface:**

- Use the attachment button to upload an image
- Include your request in the message

### Supported Formats

The assistant accepts common image formats:

- JPEG (.jpg, .jpeg)
- PNG (.png)
- GIF (.gif)
- WebP (.webp)
- BMP (.bmp)
- TIFF (.tiff)

### Size Limits

- Maximum file size: 20MB for image processing
- Very large images may be resized automatically for optimal processing

## Using the Artist Profile

For more complex creative work, you can use the specialized artist profile by starting your message
with `/artist` or `/image`. This activates a profile optimized for creative image work with:

- Enhanced prompt refinement
- Access to video generation capabilities
- Specialized knowledge of effective prompting techniques

Example:

- `/artist Create a detailed fantasy landscape with a magical forest and floating islands`
- `/image Generate a professional product photo of a coffee mug`

## Examples

### Creating Images

| Request                                                     | What You Get                               |
| ----------------------------------------------------------- | ------------------------------------------ |
| "Generate an image of a peaceful Japanese garden"           | An AI-generated image of a Japanese garden |
| "Create a photorealistic image of a golden retriever puppy" | A realistic-looking photo of a puppy       |
| "Make an artistic illustration of a wizard's library"       | A stylized drawing of a fantasy library    |

### Transforming Images

| Request (with attached image)  | What Happens                        |
| ------------------------------ | ----------------------------------- |
| "Make this black and white"    | Converts the image to grayscale     |
| "Add a blur effect"            | Applies a blur filter to the image  |
| "Make it look like a painting" | Applies an artistic painting effect |
| "Make it darker"               | Reduces the brightness of the image |

### Highlighting Images

| Request (with attached image)            | What Happens                                  |
| ---------------------------------------- | --------------------------------------------- |
| "Highlight the cat with a red rectangle" | Draws a red box around the cat                |
| "Circle all the faces in blue"           | Draws blue circles around detected faces      |
| "Mark the left corner with a green box"  | Draws a green rectangle in the specified area |

## Troubleshooting

**"Error generating image"**

- Try rephrasing your description
- Be more specific about what you want
- Check that your request doesn't violate content guidelines

**Image transformation not working as expected**

- Provide more specific instructions
- Try breaking complex transformations into simpler steps
- Some transformations work better on certain types of images

**Highlighting not accurate**

- Be more specific about what to highlight ("the red car on the left" vs "the car")
- If marking specific regions, describe their location clearly
- For automatic object detection, ensure the objects are clearly visible

**Image upload issues**

- Check that the file size is under 20MB
- Ensure the image format is supported (JPEG, PNG, GIF, WebP, BMP, TIFF)
- Try uploading a smaller version of the image

## Related Guides

- [Video Generation](USER_GUIDE.md#generate-videos) - Create AI-generated videos
- [Data Visualization](USER_GUIDE.md#create-data-visualizations) - Create charts and graphs from
  data
- [Working with Attachments](USER_GUIDE.md#4-working-with-attachments) - General attachment handling
