# Taint Grading by Introduced Content

## Status

Proposed. Adjusts tool output grading under [runtime-taint-machinery.md](runtime-taint-machinery.md)
and leaves its matrix and the adjudicator of
[risk-adjudicated-taint-enforcement.md](risk-adjudicated-taint-enforcement.md) unchanged. Companion
to [memory-authorship-provenance.md](memory-authorship-provenance.md), which removes memory's
dependence on turn taint.

## Problem

Taint enforcement is too cautious to switch on, and much of that caution comes from grading rather
than from the household actually reading outside content. A tool is graded `unknown_external`
whenever it is tagged untrusted or left unspecified, whatever it actually put into the turn.

A read-only reconstruction over 30 days of production history (2026-08-25 to 2026-09-24, 871 turns,
memory-curator rows excluded) found:

- 58% of turns introduce `unknown_external` themselves, and 80% have it somewhere in their prompt
  window.
- The largest single introducer is `delegate_to_service`, in 21% of turns. Its result is graded
  `unknown_external` by a static `unspecified` tag, whatever the delegate actually read. `get_note`
  (9%), `search_calendar_events` (9%) and `jq_query` (7%) follow the web and browser tools.
- 282 turns called a sink the policy adjudicates or confirms. 71 of them were tainted only by tools
  that do not read arbitrary outside text: delegation, notes, attachment queries, calendar and
  diagnostics. The reviewer escalated 12 of those 71 to a confirmation and denied 2.

Those 71 turns, and the reviews, prompts and refusals they carry, are caution the household gets no
protection from. It is also the part of the friction that honest grading can remove without touching
any policy cell.

## Decision

**A tool's result is graded by the content it introduces, not by a blanket tag.** Each fix below
makes a tool report what it actually put into the turn. The fallback does not change: a tool that
declares nothing still resolves to `default_unspecified_tool_output_tier`, so a new or forgotten
tool stays cautious and only a deliberate grading makes anything cleaner.

- **Delegation returns the delegate's taint.** A synchronous delegation starts the delegate from the
  caller's state; the result carries the delegate turn's final state instead of a static
  `unspecified` source. A delegate that only read the calendar leaves the caller no dirtier than it
  was; one that browsed brings the browsing back.
- **Tools that read an attachment inherit its provenance.** `jq_query` and `read_text_attachment`
  take the stamp of the attachment they read rather than a static untrusted tag.
- **Tools that add no new text add no source.** Charts, image transforms and generation, time
  conversion and image highlighting produce output from what the model passed in, which the turn's
  taint already covers.
- **Structured third-party data is `recognized_machine`.** Maps, transit, weather, flight and hotel
  search, shopping listings and Home Assistant events. Free-text reviews stay `unknown_external`,
  and so does camera imagery: anyone can put text in front of a camera. At that tier the shipped
  matrix allows sensitive reads and low-bandwidth egress and audits messages to known users, which
  are the cells where these turns meet friction today.
- **Calendar reads are graded per event, by where the event's content came from.** An event a
  household member created by hand on a household calendar is the household's. An event the
  assistant wrote carries the provenance of the turn that wrote it, and is external if that record
  is missing or the event has changed since. Events on subscribed feeds or created by anyone else
  stay external.
- **Listing notes is bounded.** `list_notes` returns titles and 100-character previews, so it
  contributes at most `recognized_machine` however a listed note was stamped. Reading a note with
  `get_note` keeps the note's full stored tier.

Operational diagnostics (telemetry, error logs, delegation status) keep their untrusted tags:
unauthenticated callers can post telemetry and logs quote external input.

## Deliberate simplifications

- **A grading mistake on the clean side skips a review.** That is the cost of letting grading reduce
  friction at all. It is bounded by the unchanged fallback: only tools someone deliberately regraded
  can err clean, and each regrading is small enough to review on its own.
- **A bounded note preview can carry a short injection.** A hundred characters of a note stamped
  `unknown_external` now arrive at `recognized_machine`. Accepted as bounded risk: a preview is
  short, and the sinks that matter still confirm or adjudicate at that tier.
- **Listing names and titles in search results are seller-written.** A product title or a rental's
  name can carry a short injection under `recognized_machine`. Accepted for the same reason as note
  previews: the text is short, and arbitrary messages, attacker-addressable egress and sandbox
  network still confirm or adjudicate at that tier. Detail pages and reviews stay untrusted.
- **Genuine outside reading stays as cautious as today.** Three quarters of the turns that reach an
  adjudicated sink read the web, a browser, mail or documents first. This design does not relax
  them; any change there is a policy decision, not a grading fix.
- **Irreversible local actions are out of scope.** Home Assistant actions, note deletion and
  automation creation are allowed or audited at every tier today, and nothing here changes that.
- **Window expiry is not built.** Web history spans 100 messages over 30 days, so expiring taint
  with the window moves the tainted share from 80% to 58% at best and changes no decision here.

## Work plan

1. **Delegation and attachment inheritance.** These two account for most of the mislabel-only
   volume. Verified by result-taint tests: a delegate that reads only trusted data returns no
   external source, one that browses returns it, and a `jq_query` over a trusted attachment adds
   nothing.
2. **No-new-text tools and structured data.** Retag the tools listed above. Verified by per-tool
   result-taint tests.
3. **Calendar and note listing.** Per-event calendar provenance and the bounded `list_notes`
   contribution. Verified by tests on a mixed own/subscribed calendar and a listing that includes an
   externally stamped note.
4. **Measured in production.** After each milestone ships, rerun the production reconstruction and
   compare the tainted-turn share and the mislabel-only sink turns against the figures above.
