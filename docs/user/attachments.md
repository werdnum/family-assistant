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

**Supported types** depend on where you're sending from:

- **Web chat:** JPEG, PNG, GIF, and WebP images; PDF, plain text, and Markdown; MP3, WAV, OGG, and
  WebM audio; MP4, WebM, and OGG video.
- **iOS chat:** JPEG, PNG, GIF, and WebP images, plus PDF, plain text, and Markdown.
- **Telegram:** images, documents, audio files, video, and voice notes — including formats the web
  chat won't take.
- **Upload Document page:** PDF, TXT, DOCX, DOC, HTML, and MD — documents rather than images, since
  the point of that page is indexing text you can search later.

If a file is rejected, converting it to a nearby format (an image to PNG, a spreadsheet to CSV sent
through Telegram) is usually the quickest way through.

Size limits apply: typically 20 MB for anything a model looks at or listens to — images, audio and
video — and 100 MB for other files. A recording over the limit is refused with its size rather than
accepted and then failed on, so trimming a long recording, or sending a lower-quality version of a
video, gets it through.

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

**Transcribe or describe audio and video:**

- "What does this voice note say?"
- "Summarise what's discussed in this recording."
- "What happens in this video?"

Not every model the assistant runs on can listen to audio or watch video. When one can't, it hands
the file to a model that can and works from what comes back, so asking is the same either way — it
just means an extra step before you get an answer. If part of a recording is inaudible, you'll be
told that rather than given a guess at it.

**Ask about an attachment itself:**

- "Tell me about this attachment."
- "What files have I uploaded recently?"

The assistant can report size, type, upload time, and a description.

## Attachments the assistant sends back

When the assistant attaches something to its reply — a generated image, a chart, a camera snapshot,
a file it fetched from your email — it arrives with the answer itself:

- **Images** appear inline, underneath the reply text, in both the web app and the iOS app. Tap or
  click one to see it full size, and use the download button beside it to save or share it.
- **Other files** (PDFs, documents, data files) appear as a named attachment with a download button
  rather than a preview.
- Images are part of the saved reply, so reopening the conversation later shows them again — they
  are not just a live-stream effect.

## How attachments behave

Every attachment gets a unique identifier the assistant can refer to for the rest of the
conversation. That means you can refer back to something you sent earlier, and the assistant can
pass an attachment from one tool to another — take a camera snapshot and then analyse it, edit an
image and then send the result, extract information from a document and save it as a note.

**Who can see them:** attachments belong to you, not to a single conversation. Other people can't
read what you upload, but you can — a later chat can pull up a file you sent earlier, and files
attached to a note stay reachable whenever that note comes up. Each attachment records the
conversation it arrived in, which is how the assistant keeps track of what you're referring to; it
isn't a wall between your own conversations.

If you want something genuinely out of reach, delete it rather than relying on starting a new chat.

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
