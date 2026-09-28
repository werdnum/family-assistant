---
name: Council Deliberation
description: Run independent multi-model investigation followed by evidence-backed peer review and synthesis. Use for an explicit council request or when coordinating the council profile; not for routine questions or a council-member assignment.
visibility_labels: [council]
---

# Council Deliberation

The default council is **three members, one independent-proposal phase, two peer-review phases, then
your synthesis**. Respect an explicitly requested smaller or larger limit, but never silently
recruit more members or extend the debate. The roster and each member's `model_tier` are in your
system prompt.

## How the runtime drives the phases

- Start every member of a phase **in one response**, as parallel `delegate_to_service` calls to
  `council_member`, each with its own `model_tier` and `delivery_hint: "background"`. Then end your
  turn with one line saying which phase is under way. That line is not the council's answer.
- You are woken **once**, when every delegation you started has finished. The wake lists each
  finished delegation with its status, and each result is in your history as data. Do not call
  `get_delegation_status` in a loop, and do not end a turn waiting for a member you have not
  started.
- A turn that starts no delegation is your final answer: it is returned to whoever convened the
  council. Write the synthesis only in that turn, and never write it in a turn that also starts
  members.
- A member's reference and preset appear in `list_delegations` (its `model_tier`), so you can always
  recover which label a delegation belongs to. Keep a short table of label, preset and latest
  reference in your own turn text as you go.

## 1. Prepare a shared brief

Preserve the original request, the user's constraints, relevant supplied material, prior decisions
and the requested output. Separate established facts, preferences and tentative assumptions. Treat
an existing proposal as something to evaluate, not an endorsed answer. Do not put a preferred
solution of your own into the brief.

Give every member the same brief and the same relevant attachments, passed as `attachment_ids`
rather than filenames. Members may find different sources on their own. Use the stable labels A, B
and C in everything members exchange, with no prestige claims and no model stereotypes.

## 2. Independent proposals

Start fresh delegations (no `resume_delegation_id`). Begin each request with a header such as
`[Council: <topic>; member A; independent proposal]` so the delegation history reads clearly, then
the brief, then this instruction:

> Investigate the question independently. Develop the approach you consider strongest and discuss
> materially different alternatives when useful. Check decision-changing factual assumptions with
> your tools. Explain the key rationale, important assumptions, strongest weakness in your approach,
> and what would change your conclusion. Distinguish sourced facts from inference and speculation.
> Return a self-contained report with references another member can inspect.

No member sees a peer's proposal in this phase.

## 3. Collect a complete phase

When you are woken, match each result to its label. Advance only when every member has either
reported or failed. If `list_delegations` shows a member of the phase still running, which happens
only when the council was chosen directly rather than convened by the Assistant, end the turn with
one line naming who is still working, and start nothing.

- A failed member is reported honestly. With at least two successful members, continue with a
  reduced panel and say so in the synthesis. With only one, stop and return a partial investigation,
  not a council result.
- Never substitute another model for a failed member, and never start a duplicate of a member that
  is still running.

## 4. First peer review: critique and revise

Keep the initial reports. Resume each member's **latest finished delegation** with
`resume_delegation_id` **and the same `model_tier` again** (a resume does not inherit the tier).
Give every member the complete set of proposals under their labels, full and concise rather than
your summary of them, with evidence references intact. One request per member:

> Read the other proposals and identify the observations that would most change the answer. A useful
> contribution may be a correction, an overlooked alternative, a simplification, or a strengthened
> justification for an existing proposal. Investigate consequential factual disagreements where
> feasible. State which ideas you would adopt, which objections matter, and how you would now answer
> the original question. Identify the report and claim you are discussing. Do not manufacture
> disagreement or repeat an objection without adding evidence or reasoning.

A citation is a lead to inspect, not proof. Collect the whole phase before moving on.

## 5. Second peer review: answer objections, publish final positions

Resume each member again, with its latest reference and the same `model_tier`, giving it the
complete first-review set, including criticism of its own proposal:

> Address the material objections and new ideas. Revise, defend or replace your approach on its
> merits. Explain what changed and why; retaining your position is acceptable when criticism does
> not hold up. Publish your best current answer, the uncertainties that still affect it, and the
> most useful next check or experiment. Do not add objections merely to fill another round.

Stop at the phase limit. Missing evidence becomes an explicit uncertainty or a proposed experiment,
not invented consensus or another round.

## 6. Synthesis

Read the original question, the initial proposals, the reviews and the final positions. You have no
proposal of your own to defend. Keep useful early alternatives even when later reports stopped
mentioning them.

- Lead with the substantive answer and its reasoning. Explain materially different alternatives and
  when they become preferable. Name the unresolved assumptions and the most useful next
  investigations. Say what review changed where that helps the reader.
- Weigh arguments and evidence, **not votes**. Several members citing one source are not independent
  confirmation. A sound objection can outweigh agreement; unsupported dissent does not earn equal
  weight. Do not invent a compromise between incompatible designs.
- Cite original, inspectable sources. Distinguish evidence you checked yourself from evidence a
  member reported, and hypotheses from established results. If a decisive uncertainty remains, state
  the conclusion conditionally; for speculative work, competing hypotheses plus a discriminating
  experiment can be the right result.
- Name the participating models and any member failures, without treating a provider name as
  evidence.

Return a substantial but readable answer, not the transcript, unless the transcript was asked for.

## Follow-ups

A follow-up that asks you to expand on or explain the result is answered from the council's existing
record, without convening again. Reconvene only when asked to, or when new evidence or changed
constraints materially reopen the question.

## Limits

The council investigates and advises. It does not publish code, deploy anything or change anything
in the household, and it does not save its intermediate conjectures anywhere. Never convene a
council from inside a council.
