# Telegram Rich Messages

## Status

Proposed design for review. **Blocked on upstream library support** (see
[Dependency Blocker](#dependency-blocker)).

This design describes how Family Assistant could adopt Telegram's **Rich Messages** feature (Bot API
10.1, released 2026-06-11) to render assistant replies as structured documents — section headings,
tables, lists, collapsible detail sections, block/pull quotes, and code blocks — and to stream
AI-generated replies into the chat as they are produced.

It is intentionally scoped as a forward-looking design: the feature cannot be implemented today
because `python-telegram-bot` does not yet expose the new API surface. The doc captures the target
architecture so that implementation can start the moment the dependency is available, and so the
prerequisite library upgrade (tracked separately) is justified.

## Background

Telegram Bot API 10.1 added "Rich Messages", a document-grade formatting model for bot messages.
Unlike the existing `parse_mode` formatting (`MarkdownV2`/`HTML`), which produces a single styled
text string constrained to inline entities, a rich message renders as a structured document with
block-level structure (headings, tables, lists, dividers, collapsible sections).

Key facts (verified against the Bot API changelog and reference, 2026-06-14):

- **Send markdown/HTML, receive blocks.** This is the critical distinction. The bot **sends** a rich
  message by passing an `InputRichMessage`, which carries the content as **markdown or HTML** (one
  of the two) — there is no need to construct a block tree by hand. Telegram parses that input and
  the **received** message exposes the structured `RichMessage` (a tree of `RichBlock*` objects) on
  `Message.rich_message`. So `RichBlock*` is the *parsed/received* representation, not the send
  input. (This corrects an earlier draft of this doc that proposed building `RichBlock*` objects to
  send.) The accepted markdown is GitHub-Flavored-Markdown-like, which is what makes "send GFM and
  get a document" accurate.
- **New methods.** `sendRichMessage` (input: `InputRichMessage`) and `sendRichMessageDraft`
  (streaming), plus a `rich_message` parameter on `editMessageText`. These are separate from
  `sendMessage`.
- **Received block types** (in `Message.rich_message`) include: `RichBlockParagraph`,
  `RichBlockSectionHeading`, `RichBlockList` / `RichBlockListItem`, `RichBlockTable` /
  `RichBlockTableCell`, `RichBlockPreformatted` (code), `RichBlockBlockQuotation`,
  `RichBlockPullQuotation`, `RichBlockDivider`, `RichBlockDetails` (collapsible),
  `RichBlockMathematicalExpression`, media blocks (`RichBlockPhoto`, `RichBlockVideo`,
  `RichBlockAnimation`, `RichBlockAudio`, `RichBlockVoiceNote`), and `RichBlockThinking`.
- **Streaming is draft-based and private-chat-only.** `sendRichMessageDraft` streams an ephemeral
  preview (reported as a short-lived draft, on the order of ~30s, keyed by a `draft_id`) and is only
  valid for a private chat. It returns a success flag, **not** a persisted `Message`; the finished
  output must be committed with a normal `sendRichMessage`. There is no opened draft message object
  to edit via `editMessageText`. Group and forum-topic chats must use the non-draft path.
- **Message length.** The reference does not (as of this writing) state a numeric maximum for rich
  messages, and we have not been able to confirm whether the classic 4096-character text limit is
  raised. This must be re-verified before relying on "longer messages" as a benefit.

> Note: the precise `InputRichMessage` field names (`markdown`/`html`) and the exact
> `sendRichMessageDraft` contract (return type, draft lifetime, private-chat constraint) are taken
> from the Bot API 10.1 reference and a code review of an earlier draft; they must be re-confirmed
> against PTB's typed bindings during the spike, since the docs page is large and the
> machine-readable spec mirrors had not caught up at time of writing.

## Dependency Blocker

Rich Messages cannot be implemented until `python-telegram-bot` exposes the 10.1 surface.

- We pin `python-telegram-bot[ext]>=21.0,<22.0` in `pyproject.toml`.
- The latest released PTB (22.8) advertises support for **Bot API 10.0**, not 10.1. There is no PTB
  release with `send_rich_message` yet.

Adoption therefore requires two sequential upgrades:

1. **PTB 21.x → 22.x** — clears the `<22.0` cap and reaches API 10.0 parity. Tracked as a separate
   PR; it has standalone value and no dependency on Rich Messages.
2. **PTB 22.x → the point release that adds 10.1 / Rich Messages** — a smaller bump once available.

This doc assumes step 2 has landed before implementation begins.

## Current State

Assistant replies reach Telegram as a single markdown-derived string:

- The LLM produces markdown-ish text. `convert_to_telegram_markdown()`
  (`src/family_assistant/telegram/markdown_utils.py:37`) runs it through the `telegramify_markdown`
  library to produce `MarkdownV2`, plus a post-processing fix for the library's `<`/`>` escaping bug
  (`fix_telegramify_markdown_escaping`, `markdown_utils.py:13`).
- `TelegramChatInterface.send_message()` (`src/family_assistant/telegram/interface.py:49`) maps the
  `parse_mode` string to a `telegram.constants.ParseMode`, sends via `bot.send_message`, and on a
  `BadRequest` "Can't parse entities" error retries once as plain text (`interface.py:115`) so
  messages are never lost.
- Long replies are split: `TELEGRAM_MAX_MESSAGE_LENGTH = 4000`
  (`src/family_assistant/telegram/handler.py:71`), and `_send_message_chunks()` (`handler.py:299`)
  breaks the text with `TextChunker` on natural boundaries. This produces multiple sequential
  messages for long content such as the daily brief.
- The transport abstraction is the `ChatInterface` protocol (`src/family_assistant/interfaces.py`),
  implemented by `TelegramChatInterface` and the web `WebChatInterface` (which ignores
  `parse_mode`).

Limitations this design addresses:

- Tables, headings, and collapsible sections degrade to flat text.
- Long structured output is split mid-document into several chunks.
- Replies appear only when complete; there is no progressive rendering.

## Goals

- Render assistant replies as native Telegram Rich Messages when the content is structured
  (headings, tables, lists, code blocks, quotes, collapsible detail).
- In **private chats**, stream long / slow replies progressively via `sendRichMessageDraft`, then
  commit the finished reply with `sendRichMessage`. Group and forum-topic chats use the non-draft
  send path (no streaming).
- Reuse the assistant's existing markdown output as the source of truth — no second authoring format
  for the LLM to learn.
- Preserve the existing `MarkdownV2` path and plain-text fallback as a graceful degradation when
  rich rendering is unavailable or fails.
- Keep the web interface unchanged in behaviour.

## Non-Goals

- Do not require the LLM to emit `RichBlock*` JSON. The LLM continues to produce markdown.
- Do not remove the `MarkdownV2` path; it remains the fallback.
- Do not build rich rendering for the web UI in this iteration (the React frontend already renders
  markdown natively).
- Do not implement media-heavy blocks (collage, slideshow, map) in v1.

## Proposed Design

### Overview

Because `sendRichMessage` accepts markdown/HTML directly (via `InputRichMessage`), the assistant's
existing markdown output can be passed through almost unchanged — no block-construction layer is
needed. The work is therefore mostly about routing: choosing the rich send path when available, and
preserving the current `MarkdownV2` ladder as fallback.

```
LLM markdown
   │
   ├── (rich available) ──► InputRichMessage(markdown=…) ──► sendRichMessage
   │                                                          (private chat: draft-stream first)
   │
   └── (fallback)       ──► convert_to_telegram_markdown() ──► send_message (MarkdownV2)
                                                                  └─► plain-text on parse error
```

### Components

1. **Rich payload from markdown** (new, in `telegram/rich_messages.py`). Build an `InputRichMessage`
   from the assistant's markdown. This is mostly pass-through, but it is **not** a blind wrapper —
   the builder must normalize constructs whose rich-markdown meaning differs from plain text:

   - **Image syntax → text.** In Telegram rich markdown, `![alt](url)` is a *media block*, so
     passing it through would turn a text reply into a media send — which v1 explicitly excludes and
     which may require media permissions or be rejected. The builder escapes or rewrites image
     syntax (e.g. to a plain link `[alt](url)`) so image references stay textual until media blocks
     are intentionally supported.
   - **GFM dialect reconciliation.** A small normalization step may be needed to match the markdown
     subset Telegram accepts (e.g. table or task-list syntax) — to be confirmed during the spike.

   Note there is **no** `markdown_to_rich_blocks` block builder: `RichBlock*` is the received
   representation, not the send input.

2. **Route the real Telegram reply path through a rich-aware sender.** The main assistant reply path
   is **not** `TelegramChatInterface.send_message`; the handler sends replies directly via
   `_send_message_chunks()` → `context.bot.send_message` (`handler.py:299`, call sites around
   `handler.py:807`+ and `handler.py:1328`+). A change confined to `TelegramChatInterface` would
   therefore leave ordinary Telegram conversations on the old `MarkdownV2`/chunking path. Introduce
   a single rich-aware send helper (e.g. `send_rich_or_fallback(bot, chat_id, markdown, …)`) and
   call it from **both** the handler's reply path and `TelegramChatInterface.send_message`, so the
   two send paths stay consistent. Callers keep passing markdown; the helper decides rich vs.
   fallback. The web path (`WebChatInterface`) is untouched.

3. **Capability detection.** Probe whether the installed PTB / bot exposes `send_rich_message`
   (attribute check on `bot`) once at startup, store the result, and branch on it. If unavailable,
   behaviour is byte-for-byte the current `MarkdownV2` path.

4. **Streaming (phase 2, private chats only).** In a private chat, stream incremental output via
   `sendRichMessageDraft` (an ephemeral ~30s preview keyed by `draft_id`, returning a success flag
   rather than a `Message`), then **persist the finished reply with a normal `sendRichMessage`** —
   do not attempt to "finalize" the draft via `editMessageText`, as there is no opened draft message
   to edit and the preview disappears when it expires. Group/forum chats skip streaming and use the
   non-draft path.

   **Stream parseable snapshots, not raw token prefixes.** Each draft update is parsed as a rich
   message, but a raw token prefix is frequently invalid markdown while a code fence, table row,
   link, or `<details>` block is half-written — sending those would fail the draft parse and defeat
   streaming for exactly the structured replies this feature targets. The streamer therefore needs a
   buffering/repair step that emits only syntactically valid snapshots: buffer until the in-progress
   block closes, or temporarily close it (e.g. append a closing fence) so each emitted snapshot
   parses, then send the next snapshot once more content arrives. Until such a snapshot is
   available, show a placeholder/typing draft rather than a broken one.

   This also requires the processing layer to expose incremental output (today replies are returned
   whole), so streaming is split into its own phase and depends on incremental generation from
   `ProcessingService`.

### Fallback and error handling

Mirror the existing robustness, but **fall back only for expected, specific errors** — do not wrap
the whole rich path in a blanket `except`. While the feature is under development a broad catch
would hide converter bugs or unexpected PTB/API shapes behind a silent MarkdownV2 reply, defeating
tests and masking real defects (contrary to the Fail-Fast principle in AGENTS.md).

- If `sendRichMessage` raises a `BadRequest` that indicates the input was rejected (e.g. an
  unparseable-markup error), fall back to `MarkdownV2`, then to plain text — the same ladder as
  `interface.py:115` today. This is a known, expected failure mode worth absorbing so a message is
  never lost.
- Any other exception (programming errors in the payload builder, unexpected API responses,
  capability-probe inconsistencies) is allowed to propagate so it surfaces in tests and logs. Once
  the path is validated and enabled by default, we can broaden the absorbed set if real-world
  failure modes justify it.

### Message length

If Rich Messages raise or remove the 4096-char limit, the `_send_message_chunks` splitting
(`handler.py:299`) can be skipped for the rich path, sending structured content as a single coherent
document. This is a benefit **contingent on verifying the new limit** — until verified, keep
chunking as a safety net.

## Milestones

1. **Spike & verify** (no production code): confirm a PTB release with Rich Messages exists; confirm
   the `InputRichMessage` field names (`markdown`/`html`), the `sendRichMessageDraft` contract
   (return type, draft lifetime, private-chat constraint), and the message-size limit against PTB's
   typed bindings; check that representative assistant output (daily brief, a table, a code block)
   renders correctly when sent as markdown.
2. **Rich send path** routed through the shared rich-aware sender (handler reply path +
   `TelegramChatInterface`), behind capability detection and a config flag, with the narrowed
   fallback ladder. Independently shippable; default off until validated.
3. **Enable by default** once validated against real chats; update length handling to skip chunking
   on the rich path if the limit allows.
4. **Streaming** (private chats) via `sendRichMessageDraft` + final `sendRichMessage`, once
   incremental generation is available from the processing layer.

## Testing

- Unit tests for the markdown → `InputRichMessage` payload builder: headings, nested lists, tables,
  fenced code (with language), block quotes, collapsible details, and special-character handling.
- Functional Telegram tests (`tests/functional/telegram/`) asserting: rich path used when capability
  present (via the shared sender on the handler reply path, not just the interface); fallback to
  `MarkdownV2` then plain text on an input-rejected `BadRequest`; and that an unexpected error
  propagates rather than being silently swallowed.
- A capability-absent test pinning current behaviour to guard against regressions.

## Documentation

On user-visible rollout, update `docs/user/USER_GUIDE.md` and describe the capability in the system
prompt (`prompts.yaml`) so the assistant knows its replies can include tables and collapsible
sections in Telegram.

## Open Questions

- What markdown dialect does `InputRichMessage` accept (GFM tables, task lists, headings), and how
  much normalization of our output is required?
- What is the actual maximum size of a rich message, and is the 4096-char text limit changed?
- What exactly does `sendRichMessageDraft` return, how long does the draft live, and is it strictly
  private-chat-only?
- Which PTB release adds `send_rich_message` / `InputRichMessage`, and what is its minimum Python /
  API version?
- Streaming: what is the minimal incremental-output hook needed from `ProcessingService`?
