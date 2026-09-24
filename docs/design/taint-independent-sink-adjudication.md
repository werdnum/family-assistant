# Taint-Independent Sink Adjudication

## Status

Proposed. Builds on [risk-adjudicated-taint-enforcement.md](risk-adjudicated-taint-enforcement.md),
whose adjudicator this design keeps, and changes when it runs. Companion to
[memory-authorship-provenance.md](memory-authorship-provenance.md), which takes memory off turn
taint for the same underlying reason.

## Problem

Turn taint was meant to separate turns that have read outside content from turns that have not, so
that checks run only where an injection could be steering the model. Production data says that
separation does not exist for the sinks that matter.

A read-only reconstruction over 30 days of production history (2026-08-25 to 2026-09-24, 871 turns,
memory-curator rows excluded) measured how many turns would carry external taint under narrower
rules than today's indefinite carry-over:

| Definition of an externally tainted turn                              | Turns | Share |
| --------------------------------------------------------------------- | ----: | ----: |
| Started at `unknown_external` (today's stamps)                        |   426 | 48.9% |
| Introduced external content itself                                    |   503 | 57.7% |
| Itself, or any earlier turn still in the prompt window                |   695 | 79.8% |
| As above, with time, maps, transit and weather graded as machine data |   693 | 79.6% |
| As above, and calendar search treated as trusted                      |   659 | 75.7% |

The web window is 100 messages over 30 days, so on the web the window rule barely differs from
indefinite carry-over (91% of web turns). Most turns that do anything useful read something from
outside the household, and that is the product working as intended rather than a mislabel to fix.

The decisive number is what gating on taint could save once the grading is fixed. In the same
period, 282 turns evaluated one of the four high-risk sinks (ambient prompt write, arbitrary
external message, attacker-addressable egress, sandbox network), about nine a day. Classified by the
tools that introduced their taint, in the turn or its prompt window:

| Sink turns, by what tainted them                                                      | Turns |
| ------------------------------------------------------------------------------------- | ----: |
| Genuine: web, browser, mail, drive, documents, GitHub, travel, shopping, code runs    |   211 |
| Mislabel-only: delegation, `get_note`, `jq_query`, calendar, diagnostics and the like |    71 |

Three quarters of sink turns read real outside content first: browsing and then messaging someone,
or researching and then running code, is what those sinks are for. No grading makes those turns
clean. The mislabel-only turns are the most that honest grading could exempt from review, and it is
an upper bound: most of them contain only `delegate_to_service`, whose delegate may itself have
browsed. The reviewer allowed 55 of them, asked for confirmation on 12 and denied 2.

So a perfectly graded taint gate would skip at most a quarter of reviews, about two a day, and would
make every grading error on the clean side a skipped review. Always adjudicating costs those two
reviews a day and makes grading errors harmless to the sinks.

## Decision

**High-risk sinks are adjudicated on every turn, whatever the turn's taint.** The four sink classes
above resolve to `adjudicate` at every tier, the trusted pole included. The adjudicator already
decides from the household's own words and the tool call rather than from the tier; this makes its
invocation unconditional instead of taint-triggered.

**Reads are not gated on taint.** `sensitive_read_broadening` stays at `audit`, as production
already runs it. Gating reads protects against an injection that widens what the model knows, but
what the model knows only matters if it can leave, and every way out is now adjudicated. The
confirmation friction on `get_note` and calendar reads that the earlier audits found goes away with
the gate.

**Turn taint stays, as information.** The tracker keeps recording sources, the provenance digest
keeps telling the adjudicator what the turn has read, and the audit table keeps its events. What
turn taint stops doing is deciding whether a high-risk sink is checked.

**Provenance keeps its enforcing role where it is per object.** Write-time admission of ambient
notes, skills and automations
([ambient-note-admission-at-write-time.md](ambient-note-admission-at-write-time.md)) and memory's
authorship rule judge a specific stored artifact by where its content came from. That is where
provenance stops persistent poisoning, and none of it depends on the turn being clean.

With the high-risk cells no longer tier-dependent, observe mode produces a verdict for every call
enforce would gate, so the audit data projects enforce's confirmations, denials and review latency
directly. Enforce still adds that friction, since it blocks on the review and applies the verdict;
what changes is that the projection no longer depends on how turns happened to be tainted.

## Grading fixes that still matter

Turn taint no longer gates the high-risk sinks, but it still feeds the adjudicator's digest, the
audit data and the remaining low-risk cells, so the grading should be honest. The reconstruction
named the largest distortions:

- **Delegation results are graded by the delegate, not by a static tag.** `delegate_to_service` was
  the largest single introducer (21% of turns) because its static `unspecified` tag resolves to
  `unknown_external` whatever the delegate read, while the delegate's actual taint already returns
  with its result.
- **Tools that read an attachment inherit its provenance.** `jq_query` and `read_text_attachment`
  are statically untrusted when their input is an attachment with a stored stamp.
- **Tools that add no new text add no source.** Charts, image transforms and generation, time
  conversion and highlighting produce output from what the model passed in.
- **Structured third-party data is `recognized_machine`.** Maps, transit, weather, flight and hotel
  search, shopping listings, Home Assistant events and camera frames. Free-text reviews stay
  `unknown_external`.
- **The app's own operational data is `trusted_internal`.** Telemetry, error logs, delegation status
  and automation definitions.
- **Calendar reads are graded per calendar.** The household's own calendars are trusted, subscribed
  feeds are machine data, and events other people created are external.

## Deliberate simplifications

- **Every high-risk sink call costs a review, including a person asking to send their own message.**
  That is at most about two extra reviews a day on current traffic, plus the adjudicator's latency
  on those calls. A trusted turn's review is also the easiest one the adjudicator gets, since the
  request and the action come from the same person.
- **An injection can steer what the model reads.** Reads are audited, not gated. The data it reads
  can reach the user in a reply, which is the user's own channel, and can leave only through a sink
  the adjudicator reviews.
- **Profiles that ask for a stricter posture keep it.** A profile or `operator_minimum` can still
  set `confirm` or `deny` on any cell; the change is to the shipped defaults.
- **Irreversible local sinks are out of scope.** Home Assistant actions resolve to `home_local` and
  note deletion and automation creation to `artifact_write`, which the matrix allows or audits at
  every tier. That gap exists today under observe mode and is unchanged by this design, before or
  after enforce; giving those actions their own floors is a separate decision.
- **Window expiry is not built.** It moves the tainted share from 80% to 58% at best, which changes
  no decision above.

## Work plan

1. **Tier-independent high-risk cells.** The shipped matrix resolves the four high-risk sink classes
   to `adjudicate` at every tier, and `sensitive_read_broadening` to `audit`. Verified by matrix
   tests at each tier, and by a functional test that a trusted-only turn sending an external message
   goes through the adjudicator.
2. **Grading fixes.** Delegation, attachment inheritance and no-new-text tools first, since they
   account for most of the mislabelled volume; per-calendar grading after. Verified per tool by
   result-taint tests, and in production by rerunning the reconstruction query.
3. **Enforce in production.** The kube-config ConfigMap moves `taint_policy.mode` to `enforce` once
   the first milestone is deployed and the existing rollout gates on projected confirmations,
   denials and review latency pass on the new cells.
