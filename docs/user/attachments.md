# Attachments

**What's here:** sending images and files to the assistant, what it can do with them, and how they
move between tools and people.

## Sending attachments

- **Telegram:** attach the file or photo to your message, with your question as the caption. A
  Telegram album counts as one message, so you get a single answer covering all the photos.
- **Web interface:** attach files in chat, or add them to your searchable documents one at a time on
  the **Upload Document** page.
- **iOS app:** attach from the composer, paste a copied image, or share a file into the app from
  another app.

**Supported types** include JPEG, PNG, GIF, WebP, BMP, and TIFF images, and PDF, plain text,
Markdown, JSON, and CSV documents. Size limits apply — typically 20 MB for images and 100 MB for
other files.

## What you can do with them

**Analyse an image:**

- "What kind of flower is this?"
- "Can you describe what's in this picture?"
- "Read the text from this document image."

**Generate or edit images:** create a new image from a description, edit an attached one (removing
an object, changing the style), combine several, or use one image as a style reference. See
[media.md](media.md) and [image_tools.md](image_tools.md).

**Process a document:** summarise it, extract specific information, or search within it. A file sent
in a chat stays with that conversation; to make its content permanently searchable, upload it on the
web interface's **Upload Document** page instead. See
[documents-and-search.md](documents-and-search.md).

**Ask about an attachment itself:**

- "Tell me about this attachment."
- "What files have I uploaded recently?"

The assistant can report size, type, upload time, and a description.

## How attachments behave

Every attachment gets a unique identifier the assistant can refer to for the rest of the
conversation. That means you can refer back to something you sent earlier, and the assistant can
pass an attachment from one tool to another — take a camera snapshot and then analyse it, edit an
image and then send the result, extract information from a document and save it as a note.

Attachments are scoped to your conversation: only you can access what you upload there, and each
conversation keeps its own collection.

## Sending attachments to other people

The assistant can forward an attachment to another known user — "send this image to John" — as part
of the same messaging feature described in
[interfaces.md](interfaces.md#messaging-other-people-in-your-household), including when the approval
step applies.

## Tips

- Be specific about what you want to know about an image; "what's wrong with this label?" beats
  "describe this".
- When uploading several files at once, say what each one is so the assistant can tell them apart.
- Very large files take longer to process and may be rejected outright.
