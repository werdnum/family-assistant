# Confirmation prompt capacity belongs to the interface

## Problem

Confirm-gated tool calls carrying a large payload were refused outright, before the user ever saw a
prompt. `confirmation_payload_block_reason` enumerated per-tool, per-field character caps —
delegation requests at 3000, worker task descriptions at 3000, the whole worker prompt at 3400,
generic (MCP) arguments at 3400, Gmail and Drive fields at 1200, every computer-use argument at 1200
— and every field the renderers touched was silently truncated at 1200 characters by
`_confirmation_value`. `delegate_to_service` applied its own copy of the delegation cap inside the
tool implementation.

Every one of those numbers was derived from a single fact: a Telegram message holds about 4096
characters. That fact was then enforced everywhere, including on interfaces where it is not true.

Two consequences:

- **The refusal happened at the wrong layer.** A tool implementation, and a policy layer that runs
  before any interface is consulted, decided that no interface could render the prompt. The
  interface that would actually render it was never asked.
- **The refusal was wrong on the web.** The web UI renders the confirmation prompt and the full tool
  arguments in a scrollable panel with no length bound at all. A user could read an over-cap payload
  in full, approve it, and watch the tool refuse the call they had just approved.

The caps also failed at the job they were meant to do. They covered an enumerated list of tools;
every tool *not* on that list still had its fields truncated to 1200 characters and shown to the
approver, who could then rubber-stamp content they had not seen. Enumeration decayed exactly as
`AGENTS.md` warns it does.

## Rule

**How much of a confirmation prompt an approver can read is a property of the interface that renders
it, and only that interface may refuse on those grounds.**

Renderers render the whole payload. Nothing is truncated and nothing is capped by tool name or field
name. When a prompt reaches an interface that cannot display it, that interface — and no earlier
layer — decides what to do about it, and its first choice is to route the approval somewhere that
can display it rather than to fail the call.

This turns an enumeration into a chokepoint: a new tool, a new MCP server, a new field is covered
automatically, because coverage no longer depends on anyone remembering to add it to a list.

## What each interface does

**Web** — unbounded. It already renders the full prompt and the full arguments; there is nothing to
enforce and nothing to refuse.

**Telegram** — bounded by its single-message budget (3800 characters, leaving headroom under
Telegram's 4096 for MarkdownV2 escaping). Over that budget it no longer truncates the prompt and
attaches Confirm/Cancel buttons to the fragment. Instead it sends a **notice**: a clearly labelled
preview of the beginning of the prompt, the full length, and a pointer to the web app — with **no
approval buttons**, so there is no way to approve from a partial view.

The durable confirmation record is created before delivery and is listed per user, not per
interface, so the request the notice describes is already waiting in the web app's pending
confirmations. The Telegram wait keeps running and resolves when the user approves or rejects it
there, exactly as it already does for any externally resolved confirmation.

Where no durable record exists — a Telegram confirmation with no confirmation service, no target
user or no tool call id — there is no other channel that could take the approval, so the call is
refused with a message saying so. That is the one remaining length-based refusal in the system, and
it names the real reason: not "this payload is too long", but "this interface cannot show it and
nothing else can take the approval".

## What is left of the pre-emptive guard

One check, and it is not about length: `spawn_worker`'s `context_paths` must be a list. Script
callers bypass JSON-schema validation, so a mapping passed there would render as no paths at all in
the prompt while the tool later iterated its keys as paths. The approver would be shown something
that is not what would run, which no interface capacity can fix. The function keeping that check is
renamed `confirmation_arguments_block_reason` to stop implying a size rule.

## Deliberate simplifications

- **No absolute ceiling on prompt length.** Tool arguments originate in a model's context window,
  which bounds them in practice; adding a global maximum would re-create the thing being removed
  here, one layer up.
- **Telegram gets a notice, not chunked delivery.** Splitting a long prompt across several messages
  and hanging the buttons off the last one would let Telegram approve anything, but it makes the
  approval surface a multi-message scroll-back — a worse place to review a payload than the web
  panel that already exists. The notice reaches the same outcome by pointing at the better surface.
- **Truncation is gone from the renderers, not made configurable.** A truncated field is only ever
  safe to display next to no approval control, which is a delivery-time decision; a renderer has no
  way to know whether one is about to be attached.
