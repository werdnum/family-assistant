# Photos, Images, Video, and Charts

**What's here:** understanding photos you send, generating and editing images, making short videos,
and turning data into charts.

## Understanding photos

Send a photo with your question in the same message:

- "What kind of flower is this?"
- "Can you describe what's in this picture?"
- "Read the text from this document image."

The assistant can describe scenes, extract text, identify objects, and answer specific questions
about what it sees. See [attachments.md](attachments.md) for how files move around a conversation.

## Generating and editing images

Use `/artist` for a mode dedicated to image and video work, or just ask.

- "Generate an image of a mountain sunset."
- "Create a photorealistic image of a cozy cabin in the woods."
- "Remove the person from this photo."
- "Make this image look like an oil painting."
- "Show the same scene at night."

The assistant can also combine several attached images — placing a subject from one into another
scene — use one image as a style reference, and annotate an image to highlight a particular region.

See [image_tools.md](image_tools.md) for the full guide.

## Generating video

- "Generate a video of a futuristic city with flying cars."
- "Create a video of a puppy playing in the grass, 16:9 aspect ratio."
- "Make a 4-second video of ocean waves crashing on rocks."

Short clips, in the aspect ratio you ask for, optionally guided by a reference image. Ask for a
"cinematic" or "high-quality" video and the assistant switches to a slower, more polished model.

## Charts and graphs

Use the charting mode — `/visualize` or `/chart` in Telegram, or the corresponding profile in the
web and iOS profile picker (see [slash-commands.md](slash-commands.md)) — and provide the data
inline or as an attached CSV or JSON file:

- `/visualize Create a bar chart showing sales by month from this CSV file`
- `/chart Generate a line graph of temperature trends`
- `/visualize the distribution of categories in this dataset`

This activates a mode built for charting, producing PNG images you can view, download, or share.
Bar, line, scatter, pie, area, time series, and more complex layouts are all supported.

Data often needs cleaning first — Home Assistant sensor history, for instance, contains
`unavailable` and `unknown` values that break a chart. The assistant can filter data before plotting
it.

See [data_visualization.md](data_visualization.md) and
[vega_lite_reference.md](vega_lite_reference.md).

## Troubleshooting

- **Image generation failed.** Rephrase and be more specific; some content isn't permitted. Naming a
  style ("photorealistic", "artistic") often helps.
- **An edit didn't do what you meant.** Describe exactly what should change, and break complex edits
  into steps rather than asking for everything at once.
- **A chart came out blank.** Usually the data: non-numeric values that need filtering, or field
  names in the spec that don't exist in the dataset.
