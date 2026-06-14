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

Telegram Bot API 10.1 added "Rich Messages", a structured block model for bot messages. Unlike the
existing `parse_mode` formatting (`MarkdownV2`/`HTML`), which produces a single styled text string
constrained to inline entities, Rich Messages model a message as a tree of typed blocks.

Key facts (verified against the Bot API changelog and reference, 2026-06-14):

- **Not a Markdown parser.** Telegram's pipeline is a structured entity/block model. There is no
  `parse_mode=GHFMD`. Content is built from `RichBlock*` objects, not a markdown string. The block
  set maps cleanly onto GitHub-Flavored Markdown constructs (headings, tables, fenced code, lists,
  quotes, dividers), which is why it is described as "document-grade" / GFM-like.
- **New methods.** `sendRichMessage` and `sendRichMessageDraft` (streaming), plus a `rich_message`
  parameter on `editMessageText`. These are separate from `sendMessage`.
- **Block types** include: `RichBlockParagraph`, `RichBlockSectionHeading`, `RichBlockList` /
  `RichBlockListItem`, `RichBlockTable` / `RichBlockTableCell`, `RichBlockPreformatted` (code),
  `RichBlockBlockQuotation`, `RichBlockPullQuotation`, `RichBlockDivider`, `RichBlockDetails`
  (collapsible), `RichBlockMathematicalExpression`, media blocks (`RichBlockPhoto`,
  `RichBlockVideo`, `RichBlockAnimation`, `RichBlockAudio`, `RichBlockVoiceNote`), and
  `RichBlockThinking`.
- **Streaming.** `sendRichMessageDraft` lets a bot push partial content and update it live,
  replacing the static "type indicator then one big block" experience.
- **Message length.** The reference does not (as of this writing) state a numeric maximum for rich
  messages, and we have not been able to confirm whether the classic 4096-character text limit is
  raised. This must be re-verified before relying on "longer messages" as a benefit.

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
- Stream long / slow replies progressively via `sendRichMessageDraft` + `editMessageText`.
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

Introduce a markdown → RichBlock conversion layer and an optional rich send path on the Telegram
interface, selected by capability detection with fallback to the current `MarkdownV2` path.

```
LLM markdown
   │
   ├── (rich available) ──► markdown_to_rich_blocks() ──► sendRichMessage / draft+edit
   │
   └── (fallback)       ──► convert_to_telegram_markdown() ──► send_message (MarkdownV2)
                                                                  └─► plain-text on parse error
```

### Components

1. **`markdown_to_rich_blocks(text) -> list[RichBlock]`** (new, in `telegram/rich_messages.py`).
   Parses the assistant's markdown into PTB `RichBlock*` objects. We already depend on
   `telegramify_markdown`, which parses GFM (including tables, task lists, code blocks); the
   converter walks that parse tree and emits blocks rather than a `MarkdownV2` string. Where
   `telegramify_markdown` does not expose a usable tree, fall back to a small CommonMark/GFM parser
   (e.g. `markdown-it-py`, already transitively available) — to be confirmed during spike.

2. **`ChatInterface` extension.** Add an optional capability so callers can request rich rendering
   without coupling to Telegram. Two options (decide at implementation):

   - (a) A new optional method `send_rich_message(conversation_id, blocks, ...)` with a default
     implementation that flattens to text and calls `send_message`.
   - (b) Keep `send_message(text, ...)` as the single entry point and let the Telegram
     implementation decide internally whether to render `text` as rich blocks.

   **Recommendation: (b).** Callers keep passing markdown; only `TelegramChatInterface` changes.
   This avoids touching every call site and keeps the web path identical. Rich rendering becomes an
   internal detail of the Telegram transport, gated by config + capability detection.

3. **Capability detection.** Probe whether the installed PTB / bot supports `send_rich_message`
   (attribute check on `bot`) once at startup, store the result, and branch on it. If unavailable,
   behaviour is byte-for-byte the current `MarkdownV2` path.

4. **Streaming (phase 2).** For long replies, open a draft with `sendRichMessageDraft` and update it
   via `editMessageText(rich_message=...)` as the assistant produces output. This requires the
   processing layer to expose incremental output; today replies are returned whole. Streaming is
   therefore split into its own phase and depends on incremental generation being available from
   `ProcessingService`.

### Fallback and error handling

Mirror the existing robustness:

- If `markdown_to_rich_blocks` raises, log and fall back to `convert_to_telegram_markdown` + the
  current send path. Rich rendering must never lose a message (consistent with the No Silent
  Failures / Fail-Fast-but-don't-lose-user-output principles in AGENTS.md).
- If `sendRichMessage` returns a `BadRequest`, fall back to `MarkdownV2`, then to plain text — the
  same ladder as `interface.py:115` today.

### Message length

If Rich Messages raise or remove the 4096-char limit, the `_send_message_chunks` splitting
(`handler.py:299`) can be skipped for the rich path, sending structured content as a single coherent
document. This is a benefit **contingent on verifying the new limit** — until verified, keep
chunking as a safety net.

## Milestones

1. **Spike & verify** (no production code): confirm a PTB release with Rich Messages exists; read
   the `RichBlock*` field docs and confirm the message-size limit; prototype
   `markdown_to_rich_blocks` against representative assistant output (daily brief, a table, a code
   block). Independently testable via unit tests on the converter.
2. **Converter + Telegram rich send path** behind capability detection and config flag, with full
   fallback ladder. Independently shippable; default off until validated.
3. **Enable by default** once validated against real chats; update length handling to skip chunking
   on the rich path if the limit allows.
4. **Streaming** via `sendRichMessageDraft` once incremental generation is available from the
   processing layer.

## Testing

- Unit tests for `markdown_to_rich_blocks`: headings, nested lists, tables, fenced code (with
  language), block quotes, collapsible details, and special-character escaping.
- Functional Telegram tests (`tests/functional/telegram/`) asserting: rich path used when capability
  present; fallback to `MarkdownV2` when converter raises; fallback to plain text on `BadRequest`.
- A capability-absent test pinning current behaviour to guard against regressions.

## Documentation

On user-visible rollout, update `docs/user/USER_GUIDE.md` and describe the capability in the system
prompt (`prompts.yaml`) so the assistant knows its replies can include tables and collapsible
sections in Telegram.

## Open Questions

- Does `telegramify_markdown` expose a parse tree we can reuse, or do we need a separate GFM parser?
- What is the actual maximum size of a rich message, and is the 4096-char text limit changed?
- Which PTB release adds `send_rich_message`, and what is its minimum Python / API version?
- Streaming: what is the minimal incremental-output hook needed from `ProcessingService`?
