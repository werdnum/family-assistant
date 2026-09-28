# Council of Models

Issue #1288. Builds on exact-model delegation presets (#1278).

## Goal

A user-facing **council** for broad exploratory work and hard design or speculative questions:
several strong models each research the question independently, review each other's work over a
bounded number of rounds, and a coordinator synthesises the strongest supported answer, the useful
alternatives, and the uncertainties that could change it. The value sought is intellectual diversity
and consequential criticism, not voting or consensus.

## Shape: two profiles and a skill

| Component                             | Responsibility                                                           |
| ------------------------------------- | ------------------------------------------------------------------------ |
| `council` profile (`/council`)        | Brief the members, run the phases, synthesise. Facilitator, not a voter. |
| `council_member` profile              | One research or review turn, on whichever preset the coordinator names.  |
| `Council Deliberation` built-in skill | The procedure and the phase instructions.                                |

**Why a profile and a skill rather than either alone.** What has to be *enforced* belongs to
configuration: which models may sit (the member admits exactly the presets), who may delegate to
whom, what data each side sees, and history limits long enough to carry three phases. What has to be
*followed* is procedure, which a skill carries and a coordinator loads on demand. A skill alone
would have to run inside the main assistant, which holds household data and every tool; a profile
alone would bury a long procedure in a system prompt.

One member profile serves every seat: the model is chosen per delegation with `model_tier`, so the
seats get the same prompt and tools and differ only in the model. The **roster** lives in the
coordinator's system prompt; the **admissions** live in `council_member.delegation_model_tiers`. A
shipped-config test checks that every preset the roster names is admitted and is a single model.

### Roster

The issue's preferred panel was Fable, Astra and Kimi. Astra is not a configured model, so the
shipped panel is **GPT-6 Sol, Claude Fable 5.1 and Kimi K3** — Sol named as Sol, not relabelled.
**Claude Opus 5.5** is admitted as an alternative Anthropic seat, used in place of Fable when a
request asks for it, never as a fourth seat. The coordinator runs on the `deep` tier.

### Confinement

Both profiles read the open web and each other's reports, so neither is given household data: no
aggregated context, and the coordinator's note reads are confined to the `council` label (grant plus
read floor), which admits the skill and nothing of the household's. The skill carries that label, so
no other profile — the members included — can load it; the members are told not to coordinate, and
the label is what makes that hold. Every profile may delegate to itself above its own policy, so
recursion is closed by `allowed_delegation_sources`: only `default_assistant` and `complex_tasks`
may convene a council, and only `council` may seat a member. A member's one outward action is a
sandboxed computation through `coder`, gated by the taint policy as everywhere else. Council
invocation authorises investigation and advice, not changes.

## The runtime gap, and the fix

A council is a delegated run (main assistant → council) that delegates in the background (council →
members). Before this change the delegation runtime finished a run on its first turn. For the
council that turn ends with "phase one under way", which became the council's result and woke the
main assistant with it; then each member's completion woke the coordinator and posted its reply
straight to the person, with nobody left to answer to. That is a limitation of nested delegation in
general, not of the council, so it is fixed there.

**A delegated run whose turn leaves its own background delegations outstanding waits for them.**

- At the end of a turn, the worker looks for the run's undelivered children: runs started from its
  subconversation that were handed off and not yet delivered. If there are any, the run moves from
  `running` to `awaiting_children` instead of finishing.
- A child that finishes while its parent is live is not delivered to the person. If the parent is
  waiting, the worker checks whether any child is still running; if none is, it moves the parent
  back to `queued` — a conditional update, so of several children finishing together exactly one
  succeeds — and enqueues a continuation turn. A parent still mid-turn picks the result up when its
  turn ends.
- The continuation turn writes every finished child's result into the parent's history as internal
  data rows, marks those children delivered in the same transaction, and runs the parent's profile
  on a system trigger naming them, with the run's frozen model selection.
- The same end-of-turn check then applies to the continuation. A turn that leaves nothing
  outstanding finalizes the run through the ordinary path, which wakes its caller.

So the coordinator is woken **once per phase**, with the whole phase in front of it, and never
busy-polls; the first member to finish is never taken for an answer; and a failed member is a
finished member whose failure arrives with the others' reports. The stale-run reaper now measures a
run from its current attempt's start rather than from its creation, so a continuation is not reaped
for how long its children took.

### Deliberate simplifications

- **A continuation that crashes fails the parent.** The children were marked delivered when their
  results were written into the parent's history, so they are not redelivered anywhere; the caller
  gets the parent's failure. This matches how an interrupted first turn is treated: a turn with side
  effects is not re-run.
- **A child that finishes after its parent has already finished** is delivered the ordinary way, as
  before this change. Reaching that needs the parent to fail or be reaped while children run.
- **No council-specific lifecycle.** No council table, retry framework, voting, dynamic recruitment,
  or cancellation beyond what delegation already has. Phase bookkeeping is the coordinator's own
  history plus `list_delegations`, which reports each run's preset.

## Verification

- `tests/functional/automations/test_council_delegation.py` runs main assistant → council → three
  members on real services, tools, worker and history, with one distinguishable fake client per
  preset: every phase and the synthesis delivered through the main assistant; each seat keeping its
  model and its own history, independent proposals, complete prior phases in later ones; the council
  waiting for its slowest member with no early answer; and a failed member reaching the coordinator
  alongside the others.
- `tests/unit/config/test_shipped_council_profiles.py` checks the shipped roster against the
  member's admissions, the skill's visibility, the confinement, and one-way delegation.
- The quality check the issue lists (an overcomplicated design, a false premise, a sound proposal
  that should survive, new evidence that should change the answer) needs real models and is left to
  a manual run against a deployment.
