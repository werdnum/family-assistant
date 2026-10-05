# Human-Approved Actions as Reviewer Evidence

## Status

Proposed. Extends [auto-tool-call-review.md](auto-tool-call-review.md) and
[risk-adjudicated-taint-enforcement.md](risk-adjudicated-taint-enforcement.md). Leaves turn taint,
stored provenance and the sink matrix untouched.

## Problem

When a human approves a confirmation, nothing downstream learns that it happened. In a tainted turn
the approved call runs, and the next call the tool-call reviewer looks at sees only stubs for
everything since the taint arrived. An assistant row stamped `unknown_external` renders as
`conversation_provenance_stub`, and earlier tool calls are not rendered at all. If the follow-up
call carries on the action the human just approved, the reviewer has no evidence that it does, so it
tends to return `confirm`. The human then gets asked again about something they have already said
yes to.

What approval records today:

- **Ordinary tool calls:** approval lets that one call run and records nothing else.
  `_record_sink_approval` skips non-delegation tools on purpose.
- **Delegations:** approval records an `approved_sinks` key bound to the target profile and sink
  class, so the delegate's own gate does not ask again. Nothing reaches the reviewer.
- **Stored definitions:** an approval is recorded as the `HUMAN_CONFIRMED` disposition, and that
  disposition cures the definition's authoring taint.

This is friction of the kind [the taint-grading work](taint-grading-by-introduced-content.md) is
removing. It is not protection: the human has already made the decision the reviewer is asking
about.

## Principle

**An approval attests to an action, not to authorship.** The calendar-intake rule in
[risk-adjudicated-taint-enforcement.md](risk-adjudicated-taint-enforcement.md) still holds: "a
confirmation *authorizes the write*; it does not change who authored the stored text." Approved
content keeps its taint. A tool's result is stamped by what the tool reads, whoever approved the
call.

What does change is the reviewer's evidence. That a human read a rendered action and said yes is a
trusted fact, recorded by FA rather than written by the model. It belongs beside the human's own
words as evidence of intent, kept distinct from them.

## Design

1. **Record.** An approval writes an attestation that holds the confirmation's identity, the tool
   name, the arguments the human approved, the prompt text they read, who approved it, and when.
   `confirmation_requests` already stores almost all of this. The design depends on every approval
   path, live and durable, going through the confirmation service. A path that bypasses it produces
   no attestation, so the cost of a missed path is an extra prompt, never a widened gate.
2. **Render.** The reviewer prompt gets a new trusted block of human decisions. It covers every
   resolved confirmation in the conversation window the reviewer already reads, in order, and each
   entry gives the tool, the arguments, the decision (approved or declined) and the time. Declines
   are rendered too, so a later refusal of the same action is never hidden behind an earlier
   approval: the human's most recent decision is the one the judge sees last. The block's framing is
   the security boundary: it tells the judge that a human approved these actions and that their
   values are endorsed intent, and also that their prose fields may have been composed from
   untrusted content. The judge uses those fields to recognise a continuation, never as
   instructions.
3. **Echo.** An approved call's *destination* values count as trusted text for the destination echo,
   next to the active request and the originating request. Only the values at the approved tool's
   declared `destination_argument_paths` qualify: the human approved those as where the action goes,
   while an address that merely appears in a body or title was approved as content of a different
   action and endorses nothing as a destination. An echo stays a signal to the judge. It never
   authorizes anything on its own.

Only the reviewer's evidence changes. Taint tiers stay as they are, and approved content is not
promoted. Floor cells (`confirm` and `deny` in the matrix) still ask: reusing approvals past a floor
is capability-scoped approval reuse, which remains a separate contingent milestone that needs the
full (tool, destination, payload) tuple. The new evidence acts only at `adjudicate` cells, where the
judge's verdict is what decides between allowing a call and asking the human.

## Why arguments, not a summary

Confirmation renderers never truncate (`confirmation_value`), so the approved arguments are exactly
what the human was shown, or data the renderer fetched to show them. A renderer that showed
something other than its arguments, such as fetched event details, has those details in the stored
prompt text. The attestation carries both, and the judge sees the arguments alongside the prompt
text the human actually read.

## Deliberate simplifications

- **Scope is the conversation window, not the household.** An approval in one conversation is not
  evidence in another, and it does not reach automations or delegated turns through the originating
  request. A delegate whose handoff was approved already has `approved_sinks`.
- **No binding between the follow-up call and the approval.** The judge decides whether a later call
  is really a continuation. A deterministic match is what approval reuse would provide, and it is
  out of scope here.
- **Expired confirmations are not rendered.** Nobody decided anything, so they are not evidence
  either way.

## Work plan

1. **Attestation and rendering.** Reviewer prompt assembly takes approved actions from the
   conversation's confirmation records and renders them in the new block. Verified by
   prompt-assembly unit tests: an approval in a tainted turn renders, and an approval followed by a
   decline of the same action renders both in order. A reviewer-eval case checks, at an `adjudicate`
   cell (the default matrix's `arbitrary_external_message` under `unknown_external`), that a
   follow-up message to the recipient of an approved message is allowed, while a message to an
   unrelated recipient still escalates and a retry of a declined send does not pass.
2. **Echo.** Approved destination values feed `compute_trusted_destination_echo`. Verified by unit
   tests: a destination matching an approved call's destination echoes; one matching only an
   approved call's body text does not; an approval in a different conversation does not count.
3. **Measure.** The observe-mode audit counts reviewer `confirm` verdicts that follow an approval in
   the same turn, before and after the change.
