# Prompt Injection Defences: Assessment and Direction — October 2026

## Status

Point-in-time assessment and direction document, in the manner of
[project-assessment-2026-07.md](project-assessment-2026-07.md). It synthesises the taint, review and
confinement design docs, a survey of what comparable products shipped by October 2026, and thirty
days of production audit data (3 September to 3 October 2026) pulled by the engineer profile. It is
not an implementation design: the work it proposes is configuration, one correction to the taint
propagation rule, one sink cell the definition gate was designed against, tag hygiene and logging
fixes, and it names nothing that needs a new design document.

## Summary

The segregation bet was right, the judge was right, and the runtime taint layer has never been
switched on. Over roughly a year, each round of the enforcement question has ended with one more
mechanism: the epoch amnesty, risk adjudication, the shared reviewer, the eval harness, judge
tuning, write-time admission, executable-definition taint, definition amnesty. Each was argued
soundly from the previous round's findings, and each left enforcement one step away. The production
numbers say that step is now a configuration change. This document's single decision is to take it
and to declare the framework complete, so that the next round, if there is one, removes or re-points
mechanism rather than adding it.

Verdict in one paragraph: the architecture is better than any open-source peer and comparable in
shape to what Google ships in Chrome, the delivered protection today is a static allowlist with
confirmations and profile segregation, and the gap between the two is a mode flag. The remaining
false-positive cost of closing that gap is known, modest, and concentrated in places that config can
move. The industry has not solved the problem either; it ships judge-plus-rules-plus-confirms and
publishes residual rates. We are in a position to do the same.

## What comparable products do

Surveyed in October 2026. Every serious product has converged on the same four layers: model
training against injection, a classifier or probe over untrusted input, an action-level judge that
compares the proposed tool call to the user's request, and a small set of deterministic rules at the
dangerous sinks. The deterministic rules are strikingly narrow:

- OpenAI, Anthropic and the Dia browser independently landed on one rule for fetch: a URL may be
  requested only if the user typed it or search returned it; a model-synthesised URL is refused.
- Google's Chrome agent partitions origins per task into read-only and read-write sets and runs a
  critic model that is structurally denied page content. Google cites CaMeL and the dual-LLM pattern
  as inspiration. No numbers published.
- Anthropic's Claude Code auto mode and Claude in Chrome use an action classifier that sees only the
  user's words and the executable payload, with deterministic deny rules ahead of it. Anthropic is
  the only vendor publishing attack-success rates.
- Anthropic's Cowork wraps a VM in an allowlist egress proxy that rejects attacker-embedded API
  keys.
- Microsoft ships classifiers, spotlighting, deterministic markdown-image and link blocking, and a
  plan-review hook for Copilot Studio. FIDES remains research.
- OpenClaw, the dominant open-source personal agent, has tool allowlists, exec approvals and
  optional sandboxing, and says in its security policy that it has no taint tracking and no
  classifiers.

Nobody ships information-flow tracking. CaMeL and FIDES remain prototypes, and the papers that
operationalise them call production-grade IFC future work. What this project built, persisted
provenance on every message row and artifact, is the thing nobody else has, and its production value
has turned out to be two narrower things: selecting what the judge may see, and stamping stored
artifacts so that delayed injection through notes, skills and automations has a gate. Both are ahead
of the field. Microsoft, Notion and OpenClaw were each hit by stored or delayed injection in the
past year.

## What we built and what runs

| Layer                                                     | Built | Gates a call today |
| --------------------------------------------------------- | ----- | ------------------ |
| Processing-profile confinement (Rule of Two)              | yes   | yes                |
| Static tool policy, confirm list, `excluded_global_tools` | yes   | yes                |
| Durable cross-interface confirmations                     | yes   | yes                |
| Ambient-write admission for notes and skills              | yes   | yes, unexercised   |
| Executable-definition taint                               | yes   | partly             |
| Tool-call reviewer (judge)                                | yes   | shadow only        |
| Runtime taint matrix                                      | yes   | observe only       |
| Gemini native injection detection (visual browser path)   | yes   | yes                |
| Destination provenance for fetch and send                 | no    | no                 |
| Egress proxy around sandboxed code                        | no    | no                 |
| Input injection probe                                     | no    | no                 |

Scale: about 15 to 18 thousand lines, 1,700 tests and 38 percent of all design-doc text sit on the
taint, review and confinement layers. Roughly 10 percent of the backend.

## Production evidence, September 2026

From `taint_audit_events`, `confirmation_requests`, `message_history` and `notes`, 3 September to 3
October. Aggregates only.

**What enforce would cost.** The shipped matrix never asks for confirm or deny directly; every gate
is adjudicated by the judge. Under enforce with the judge deciding first:

| Measure                                       | Today | Under enforce       |
| --------------------------------------------- | ----- | ------------------- |
| Human prompts per day, median                 | 0.6   | about 2.6           |
| Human prompts per day, p90                    |       | about 6.6           |
| Outright blocks per month                     | 0     | 52, across 31 turns |
| Added latency per gated call, p50 / p90 / p99 | 0     | 3.4 s / 10 s / 22 s |

Without the judge, with every adjudicate cell a human prompt, the median is 8 gated turns a day.

**Where the friction is.** The judge allowed 74 percent of 1,305 shadow reviews, confirmed 22 and
denied 4. Browser egress in `browser_profile` produced 133 of the 285 confirms; that profile holds
no household context until a person hands back a logged-in session. Shell commands produced 45 of
the 52 denies. With no attacks in the traffic, every one of those is a false positive. Fallbacks
were rare and all resolved to confirm. The escalation path for repeated denies has never recorded an
event.

**Where the taint comes from.** 81.5 percent of untrusted-tier turns are untrusted before their
first tool call, carried in from history and from the 14 untrusted-tier notes that appear in 43
percent of untrusted evaluations. The largest in-turn introducer is delegation, whose result folds
the child's state back. The engineer profile's log and database reads are tagged untrusted, so its
turns are untrusted as a matter of course. Known-contact has never fired; recognised-machine fires
only from tool tags since 25 September.

**What the data refuted.** Memory review skips went to zero on 26 September. The home-automation
calls allowed at the untrusted tier were lights, climate and media; the deployment has no locks,
sirens or alarm panels, so the designed high-impact actuation split has no sink to guard. The Gmail
and Drive integration runs under a waiver, so mailbox content is already in the loop with no runtime
gate on egress.

**Logging defects.** `result_taint` records the turn's running tier, not the tool's own. Audit
sources redact external entries to identical stubs, so attribution is impossible from the audit
table alone. Coder delegation reviews carry no turn id. Two confirmation requests from May are still
pending past expiry. The ambient-write sink has no events, which is less surprising than it looks:
the gate merged on 23 September (PR #1256), so it has had ten days in production, an event needs a
note written with `include_in_prompt` or skill frontmatter in a turn carrying external material, the
model is told to leave `include_in_prompt` off, and an ungated write records nothing. The gate has
no production evidence either way.

## Why "one more" keeps happening

Three structural reasons, not a failure of discipline:

1. **Turn-level max-tier is the boolean the original design set out to avoid, with six shades.**
   CaMeL gets usable precision from value-level provenance; a turn-level maximum cannot separate
   "summarise this email and add the appointment" from an exfiltration, so the matrix alone could
   never be enabled. Every round since has been an attempt to add the missing discriminator from
   outside the matrix. The judge is that discriminator, and it is built.
2. **Two gates were set that cannot be satisfied.** The eval target of 300 clean independent attack
   units will not be reached; the three published runs have 62, 26 and 38. The friction budget of
   one prompt a day at p50 was set before any shadow data existed. A gate with no satisfiable path
   guarantees another round, which is the failure mode the review guidelines already name.
3. **The unit of work was the design document.** Each round produced a document whose natural output
   was a mechanism. The risk-adjudication design wrote the right rule, that additive mechanism needs
   a measured number and substitutive mechanism is preferred, and the next document inverted its own
   ordering.

The fear that this is a losing battle assumes a win condition. There is none: no vendor claims one,
and the adaptive-attack literature says no probabilistic layer survives a determined attacker. The
achievable end state is the one the industry occupies: enforced, measured, residual accepted and
written down. By that standard the battle is nearly over, and the last move is not a mechanism.

## The test every mechanism must pass

The point of all of this infrastructure is to stratify risk. A mechanism earns its place only if it
produces different outcomes for different risk in the traffic actually observed; one that returns
the same answer for nearly everything, whether that answer is "tainted" or "allow", carries no
information and buys nothing. Applied to the September data:

| Mechanism                                                      | Discriminates in production                                   | Disposition                                         |
| -------------------------------------------------------------- | ------------------------------------------------------------- | --------------------------------------------------- |
| Profile confinement                                            | Yes, by entry point                                           | Keep                                                |
| Sink classes                                                   | Yes: 73% of evaluations audit, 18% adjudicate                 | Keep, reduced to the egress question                |
| Tool-call reviewer                                             | Yes: 74% allow, 22% confirm, 4% deny                          | Keep                                                |
| Static confirm list                                            | Yes: 0.6 prompts a day, 17 of 18 approved                     | Keep                                                |
| Ambient-write admission                                        | No evidence: no ambient write in a tainted turn               | Keep; one wiring test, milestone 1                  |
| Executable-definition cure                                     | No: its creation gate sits on an audit cell, so it never runs | Build the cell it was designed against, milestone 3 |
| Source tiers                                                   | No: only the two poles occur                                  | Collapse to the four classes the predicates ask     |
| History carry-in                                               | No: 81.5% of turns tainted before any tool call               | Fix, milestone 3                                    |
| Outcome lattice, floors, operator minimum, redact, mode switch | No: never exercised                                           | Delete, milestone 5                                 |

Two consequences follow. A flag that is on for most turns is not a taint signal, it is a constant,
and the re-baking defect turned the turn-level tier into one; milestone 3 restores the signal. And
the parts of the framework that have never produced a second outcome are not defence in depth, they
are surface area, and the thirty-day milestone removes them.

## Decision

Switch `taint_policy.mode` to `enforce` with the matrix as shipped and no new code, review the 14
untrusted-tier notes in the artifact review UI, which is already in use, and then freeze the
framework.

Expected result: a median prompt count re-measured from shadow data once the signal fix has landed,
with the mailbox gap closed. The pre-fix figure is 2.6 a day; the fix removes carry-in adjudications
and adds executable-definition ones, so the post-fix figure is the one the flip is held to. That is
above the one-a-day budget the risk-adjudication design set, and this document withdraws that budget
as a gate: the number is measured, it is tolerable, and it will be re-read after thirty days.

Two configuration-only reductions were considered during review and rejected:

- **Auditing egress on `browser_profile`**, which produced about half the confirms. The profile
  holds no household context when it starts, but its handoff path lets a person log the shared
  browser in and hand it back, after which the agent reads private pages and an injected page could
  direct an ungated navigation carrying them. The profile is therefore not reliably without
  sensitive data, and it stays adjudicated. A handback-aware split, auditing egress only until a
  session has been handed back, is contingent work for the thirty-day review; the cost of not having
  it is about one gated turn a day.
- **Restricting the `sandbox_network` verdict space to allow and confirm**, which would have turned
  the shell-command denies into prompts. The adjudicate cell exposes only a verdict floor, so a
  ceiling is not expressible. Denies stand as deny-and-continue: the agent receives a structured
  refusal and can route around it, with escalation to a human after three consecutive or twenty
  per-turn denials. That escalation has never fired and is the first thing to watch under enforce. A
  verdict ceiling is a small code change if deny-and-continue proves wrong in practice.

**Framework freeze.** Until enforce has run for thirty days and the audit has been re-read:

- No new tiers, sink classes, outcomes or admission states, with the pre-flip corrections in
  milestone 3 exempted by name: the one executable-definition sink cell, the propagation-rule fix
  and the tag hygiene. The two middle tiers stay as they are; the email allowlists stay empty until
  mailbox sync exists to populate them.
- No input injection probe. Adaptive evaluation shows filter defences collapsing, and an
  escalate-only probe adds a classifier to a system whose judge already sees the payload.
- No content-derived or value-level stamping.
- No new design document on this subject. A finding goes into this document's successor as a
  one-line residual, or into an issue.
- The eval target of 300 attack units is withdrawn. The harness stays as a regression instrument for
  prompt and model changes. Shadow data is the friction instrument; the eval is the attack
  instrument; neither is a gate on enforcement.

## Work plan

Milestones deliver standalone value and are verified as stated. No calendar estimates. The order is:
establish facts, make the audit attributable, fix the signal, then enforce on it. Enforcing first
would not be useless, because the reviewer discriminates per call whatever the taint flag does, but
it would run the reviewer on nearly every egress call and the audit could not say why. The fixes
ahead of the flip are bounded corrections, not new mechanisms, and the flip is one reversible
configuration line: if they slip beyond a few weeks, flip anyway and fix under enforce.

1. **Establish two facts.** Half a day of queries and one test. Exercise the ambient-write gate
   once. It is implemented and wired for notes and skills at `add_or_update_note`, and runs the
   reviewer synchronously in observe mode too, but ambient writes are rare by instruction, an
   ungated write records nothing, and it has had ten days in production. In a throwaway
   conversation, read a web page, then write a note with `include_in_prompt` set; verified by a
   `taint_audit_events` row with `event_type = 'ambient_note_admission'` and
   `sink_class = 'ambient_prompt_write'` carrying a verdict, and a missing row is a bug to fix, not
   a design to write. This proves note and skill admission only; the executable-definition gate is a
   separate path that cannot engage until milestone 3 gives it an adjudicating cell, and is verified
   there. Then query `memory_change_log` outcomes and the admissible share of assistant rows. The
   curator transcript omits every assistant row whose stored turn tier is untrusted, which in
   production is most of them, so the curator reads user lines with the answers missing. Showing it
   those rows is not the fix: the memory write invariant refuses any edit whose provenance is not
   admissible for reuse, and a curator reading untrusted rows while it reads and writes household
   memory would hold all three Rule-of-Two properties. If the query confirms the diagnosis, the
   choice is between accepting the yield as the price of confinement and routing candidate entries
   from tainted stretches through the existing write-time admission review, so they land as
   `machine_reviewed` only on a judge verdict; that choice waits for the thirty-day review. Verified
   by the two queries producing a number.

2. **Make the audit attributable.** Record each tool's own result tier alongside the running tier.
   Keep tool name and source type on redacted audit sources. Stamp turn ids on delegation reviews.
   Make confirmation expiry run. Verified by a re-run of the September audit queries producing the
   per-tool attribution table that the first run could not, and separately by the two May requests
   reaching `expired` and a query for pending rows past `expires_at` returning none after the sweep.

3. **Fix the signal.** Addressed with prejudice, because turn-start poisoning is the largest single
   source of friction and a defect in the propagation rule, not a policy choice. Every row persists
   the turn's merged snapshot, history included, and `merge_history_taint` raises the next turn to
   each row's stored maximum. One web search therefore stamps every later row in the window, each of
   those stamps the rows after it, and a conversation never heals: the Telegram window is ten
   messages or two hours, yet 81.5 percent of untrusted turns were untrusted before their first tool
   call. The correction has two halves. On the write side, a row persists only the sources its own
   turn introduced. The tracker is told at `add_source` whether a source came from history, but it
   keeps only an aggregate bit, so per-source origin is new state this milestone adds: each source
   carries whether it was introduced or inherited, and a source present in both resolves to
   introduced. The row keeps its merged `max_tier`, which is what the row *is*: the reviewer still
   renders an influenced reply as a stub, and memory still refuses it. Beside it the row persists an
   introduced-tier maximum, because the source list is bounded to twelve entries and a child that
   introduced one untrusted source and then enough lower-tier ones would otherwise evict the detail
   that matters. On the read side, history contributes only the introduced sources and the
   introduced-tier maximum of rows actually in the prompt window, never the merged maximum. Taint
   then lasts exactly as long as the introducing text is in the prompt, which is the right duration,
   and ends when it leaves. The same rule applies to a delegation's folded-back state. A row written
   before this change carries neither the per-source origin nor the introduced-tier maximum, so it
   contributes under the existing amnesty rule instead: its first-hand typed sources, and nothing
   synthesised from its merged maximum, for as long as it is in the window. Old rows therefore keep
   tainting until they age out, and stop being re-recorded the moment the first new-format row
   follows them. The accepted residual is paraphrase beyond the window: a reply written under taint
   can restate an injected instruction after the source has aged out, and that reply is in the
   prompt. It carries no taint forward. Commodity injection relies on the attacker's own text being
   present, the reviewer still sees the influenced row as a stub while it is in the window, and the
   alternative is the infinite propagation this milestone removes.

   The same milestone makes the executable-definition creation gate actually run. Today an
   automation created by the default assistant in a tainted turn is stamped externally authored with
   no disposition and resolves uncured, so it shows up needing human review even though the gate
   exists: every tool that stamps a definition, the automation, listener and script tools and the
   one-shot callback tools alike, resolves to `artifact_write`, which is `audit` at every externally
   authored tier, and a gate only engages where the cell adjudicates. The executable-definition
   design assumed the risk document's executable-persistence sink split and says so; that split was
   never built. It is the one addition this document makes to the sink vocabulary, and it passes the
   stratification test by construction: a stored definition is future unattended execution, which a
   note is not, so giving it its own cell that adjudicates at externally authored tiers
   discriminates exactly where `artifact_write` cannot. The cell is assigned through a chokepoint,
   not a list of tools: one tag names a tool as an executable-definition writer, the sink resolver
   maps that tag to the cell, and the stamping helper that every definition write already has to
   pass through refuses to stamp from a call whose tool lacks the tag. A writer that forgets the tag
   then fails loudly at its first write rather than slipping into the audit cell. The restamp script
   fills absent records only and never touches an existing one, so the definitions already stamped
   uncured since 23 September are reviewed by hand in the artifact review UI, which is a handful,
   and nothing new is built for them. The uncured fires in 10.8 percent of untrusted evaluations
   come from both populations.

   Tag hygiene belongs here too. Confirm whether delegation results default to untrusted because of
   a tag mismatch between repository and deployment config, or because the children genuinely read
   the web. Confirm whether the engineer profile's database and log reads need the untrusted tag,
   given that profile's side effects are already judged by static review. Either answer is a valid
   outcome: a mismatch is fixed, and a justified tag is kept and its justification recorded here.

   Verified by the share of turns untrusted before their first tool call falling from 81.5 percent
   to the share whose window genuinely holds an introducing row; by Telegram turns more than two
   hours after a web search starting clean; by a row written before the change, carrying a web
   source, still tainting the next turn inside its window and not after; by a tainted-turn
   automation creation producing a reviewer verdict and a cured record; by a definition write from a
   tool without the executable-definition tag being refused at the stamping chokepoint; and, for tag
   hygiene, by delegation's share of in-turn introductions falling where a mismatch was fixed, or
   the measured share being reported with the reason a tag stands.

4. **Flip to enforce.** The mode change and the note review from the decision above, on a signal
   that now discriminates and with an audit that can say why. Re-read the shadow data after
   milestone 3 and record the median daily would-prompt count there as the ceiling; 2.6 a day is the
   pre-fix reference, not the ceiling. Verified by the taint-audit endpoint showing confirm and deny
   outcomes with `mode = enforce`, and by the median daily count in `confirmation_requests` over the
   first thirty days of enforce landing at or under the recorded post-fix figure.

5. **Cut the framework back to what discriminates.** After thirty days of enforce. Delete the policy
   matrix evaluator and its outcome lattice, tighten-only profile merging, `operator_minimum`,
   `matrix_overrides`, the `redact` outcome, the observe-versus-enforce mode and
   `require_taint_enforcement`. What an operator keeps is one deployment-wide map from the five
   gated sink classes, egress, sandbox, external message, executable definition and ambient prompt
   write, to review, confirm or deny, and nothing finer: it is the migration for a deployment that
   used `operator_minimum` to put a person on tainted egress, it is not relaxable by a profile
   because profiles no longer carry taint policy at all, and it is a dial rather than a mechanism,
   so it passes the stratification test by not claiming to discriminate. Collapse the six tiers to
   the four classes the code's predicates already distinguish: human-direct, model-composed inside
   the household, machine-reviewed, externally authored. Human-direct stays its own class because
   the reviewer uses it to select an originating request and to judge a destination echo, and a
   model-composed row must remain ineligible for both. The collapse is a read-time mapping, not a
   data rewrite: every stored tier name stays accepted, `trusted_user` maps to human-direct,
   `trusted_internal` to model-composed, `machine_reviewed` to reviewed, and the three external
   names to externally authored, so no message, note or definition stamp changes meaning and nothing
   is raised to unknown-external by the removal of a name. Keep per-row and per-artifact provenance,
   sink resolution reduced to "is this call egress, sandbox, an external message or an executable
   definition", the reviewer's provenance-selected view, and the admission gates on stored
   artifacts. In the taint module that is roughly lines 980 to 1812 of 2,163, plus most of the
   tracking provider's evaluation plumbing and about 450 lines of configuration reference. What
   remains is the Chrome agent's shape with better artifact provenance: a reviewer that runs on
   egress when the turn carries external content, sees household-authored intent plus
   machine-reviewed evidence and nothing else, and a write-time gate on anything that becomes
   ambient or executable. Subtractive by construction. The memory decision from milestone 1 is taken
   in the same review. Verified three ways: representative stamps written before the collapse, one
   per old tier name, classify under the predicates as they did before, including a
   `trusted_internal` stamp staying ineligible as an originating request and as a destination echo;
   the set of sink classes that reach the reviewer is unchanged; and the reviewer's allow, confirm
   and deny shares over the following thirty days each sit within five percentage points of the
   preceding thirty on the same cells, with the deleted configuration keys rejected at startup.

6. **Contingent, on evidence only.** After the same thirty days, if the audit indicts a cell:
   destination provenance for fetch and send as a gate rather than a judge hint, which replaces the
   largest confirm category with a rule; calendar provenance wired into the context provider, which
   is a tag fix; an allowlist egress proxy around worker and script sandboxes, which would retire
   the largest cell and all the shell denies; a handback-aware split of browser egress. Each is
   substitutive. Each needs a number from milestone 2 before it starts.

## Deliberate simplifications and accepted residuals

- A judge false-negative on an unfloored egress cell can authorise exfiltration. The judge has
  allowed no attack across every corpus, with at most 62 independent families, so the bound is weak.
  Accepted, because the alternative is confirm floors on every egress call, which is the
  configuration that stalled for a year.
- `browser_profile` stays adjudicated on egress because a handed-back session may be logged in. The
  residual is same-origin action under a loaded login, already recorded in the authenticated-site
  design, and about one gated turn a day of friction.
- `home_local` stays allow at every tier. The deployment has no high-impact actuators. The
  configuration reference should say that a deployment adding a lock, alarm or garage opener needs a
  gate on those domains before it is safe at any tier.
- Turn-level taint remains the enforcement unit. Value-level provenance is not coming.
- History taint lasts as long as the introducing text is in the prompt window and no longer. A reply
  that paraphrases an injected instruction after its source has aged out carries no taint forward;
  see milestone 3.
- The middle tiers are uncalibrated and will stay so until a mailbox connector supplies evidence.
- Shadow allows cure definition records. Cures are not re-judged when the reviewer improves.
- The agent model is a security control this project does not own. Published numbers show an
  order-of-magnitude spread in injection resistance between models of one generation. The standard
  tier runs a flash-class model. Measuring it the way the judge was measured is worth doing and is
  not blocked on anything here.

## Dropped

- High-impact home actuation sink and operator entity list: no actuators in the deployment.
- Input injection probe: collapses under adaptive attack, adds nothing the judge lacks.
- Content-derived per-field stamping, artifact healing, value-level taint.
- The 300-unit eval gate and the one-prompt-a-day gate as conditions on enforcement.
- Populating email sender allowlists ahead of mailbox sync.

## References

- [runtime-taint-machinery.md](runtime-taint-machinery.md), the framework.
- [runtime-taint-enforcement-operational-findings.md](runtime-taint-enforcement-operational-findings.md),
  the August audit.
- [risk-adjudicated-taint-enforcement.md](risk-adjudicated-taint-enforcement.md), the complexity
  budget this document holds the project to.
- [auto-tool-call-review.md](auto-tool-call-review.md), the judge.
- [tool-call-review-judge-tuning.md](tool-call-review-judge-tuning.md), the eval numbers.
- [ambient-note-admission-at-write-time.md](ambient-note-admission-at-write-time.md) and
  [executable-definition-taint.md](executable-definition-taint.md), the stored-artifact gates.
- [memory-authorship-provenance.md](memory-authorship-provenance.md), the curator fix.
- [project-assessment-2026-07.md](project-assessment-2026-07.md), the previous assessment.
