# Tool-call review corpus visibility ablation

**Status:** Proposed

## Objective

Use large public prompt-injection corpora to measure how the tool-call reviewer behaves as
attacker-controlled browser content moves from withheld to visible quarantined context. The review
boundary, trusted objective, proposed action, and every other prompt input remain fixed within a
pair so the result isolates information exposure rather than differences between actions, boundary
types, or datasets.

This extends the evaluation harness in
[tool-call-review-eval.md](tool-call-review-eval.md). It does not change production rendering or
tool-call policy.

## Evaluation question

The reviewer can appear robust for two very different reasons:

1. it correctly treats visible attacker text as data rather than authority; or
2. production rendering withholds the attacker text, so there is nothing available to lobby it.

Both are useful defenses, but they support different claims. A corpus row evaluated with only one
visibility treatment cannot distinguish them.

## Controlled browser visibility treatments

Both treatments use `BrowserActionReviewInput` and the shipped browser prompt assembler. They share
the same objective, damage envelope, proposed action, recent actions, mitigation guidance, and
policy contexts. Only the browser `environment` field differs:

- **Hidden:** `environment` is replaced with one fixed, deterministic withheld-content marker.
- **Full:** `environment` contains the attack or benign-twin text and is rendered as fenced,
  explicitly untrusted browser environment content.

The hidden treatment is an evaluation-only transformation, not a production rendering path. It
answers the causal question "what changes when this reviewer sees the environment text?" A
separate comparison of production conversation and browser boundaries can test system-level
behavior, but cannot be interpreted as an information-visibility ablation because the payload and
action semantics also change.

The evaluation does not add a partially visible treatment. A bounded excerpt level belongs here
only if it has a precise representation and a concrete product question.

## Matched case groups

Each attack source produces one matched group with four cases:

| Control | Hidden | Full |
| --- | --- | --- |
| Attack | attacker text withheld | attacker text visible and quarantined |
| Benign twin | corresponding content withheld | corresponding content visible and quarantined |

All four cases use the browser boundary and share the action surface, damage class, attack class,
and source-group identity. Within each attack hidden/full pair and each benign hidden/full pair,
every payload field except `environment` is identical; the hidden environment is the fixed marker
and the full environment is the source text.

The attack pair proposes an action the trusted objective does not authorize. Its benign twin keeps
the same kind and shape of action but changes the objective, argument values, and environment as
needed to make the action genuinely authorized. Requiring the exact same objective and action
across attack and benign controls would make one of the labels false: an unauthorized action does
not become benign merely because the injection is removed. The controls are therefore matched for
surface and risk, while the causal visibility comparison is made within each control kind.

A corpus row labeled "not an injection" is not automatically a benign twin. It is usable as a
natural-benign pool only when the surrounding trusted request independently authorizes the proposed
action. Otherwise the judge can correctly refuse the action and the harness would misreport that as
model friction.

## Identity, splits, and evidence units

Generated cases carry stable, content-free identities for:

- the upstream source or attack family used for holdout assignment;
- the matched ablation group;
- the visibility treatment; and
- the attack, generated benign twin, or natural-benign control kind.

Holdout assignment happens before case expansion. Every correlated attack, twin, and visibility
variant stays in one split. Derivative or templated upstream rows share a family-level split key so
near-duplicates cannot leak across development and held-out evidence.

Visibility variants are paired measurements, not additional independent attacks. Confidence bounds
count each `source_group` once overall, even when one source family produces multiple matched
groups and both hidden/full treatments. A per-visibility slice naturally sees that family once in
its own treatment. `matched_group` identifies a particular four-case pair matrix; it is not an
independence claim. Expanding one row or attack family into many variants must never improve the
supported false-allow bound merely through correlated duplication.

## Reporting

The report preserves the existing attack allow, attack confirm, benign friction, expectation,
inconclusive, seed-flip, and latency metrics. It adds:

- metrics sliced by visibility and control kind;
- complete and incomplete matched-group counts;
- aggregate paired verdict-transition counts between hidden and full treatments; and
- attack allow-rate and benign-friction changes from hidden to full, split by control kind where
  applicable.

The summary and stamp contain aggregate transition counts and rates rather than one line per case;
the full trial record remains available for drill-down. This keeps reports bounded when corpora have
thousands of rows.

The primary ablation result is the paired change from hidden to full. It measures sensitivity to
visible attacker content. It is not, by itself, proof of a model's general susceptibility to prompt
injection.

## Corpus ingestion and provenance

Raw corpora, generated cases, license files, manifests, and run outputs remain under
`.review-eval-local/`. Generated cases record the corpus identity, pinned upstream revision, source
split, adapter version, and lineage keys without embedding raw source text in filenames or logs.

The first materializations are:

- Deepset Prompt Injections, preserving its published train/test split; and
- InjecAgent base direct-harm and data-stealing cases, preserving their attack-family structure.

InjecAgent is adapted to the application's reviewer boundaries; this is not a reproduction of the
benchmark's original end-to-end agent attack-success metric. Enhanced attack variants remain a
separate slice so they cannot silently change the base distribution.

## Cost-aware production

Deterministic code performs parsing, identity construction, boundary assembly, schema validation,
and report aggregation. Low-cost models may draft benign twins or history-derived content, but they
do not author labels, lineage, security metadata, tool names, or verdict constraints.

Batch output is accepted only after strict structured validation, exact input-ID reconciliation,
tool-schema validation, reviewer-input reconstruction, and privacy checks. Invalid batches are
retried a bounded number of times and then quarantined for review. Model sessions and raw event
streams are not retained.

Evaluation proceeds progressively:

1. smoke-test the committed samples;
2. run a stratified development subset with one seed;
3. run the full materialized corpus once; and
4. spend repeated seeds on the held-out gate, unstable groups, and failures.

This exposes structural errors before paying for repeated judgments over thousands of cases.

## Delivery plan

### Milestone 1: ablation contract and reporting

The case schema can represent matched groups and controlled browser visibility treatments. The runner
reports visibility slices and paired transitions without inflating independent-input counts.

Verification: schema round trips; malformed, incomplete, non-browser, or payload-mismatched groups
fail loudly; hidden cases use the single fixed marker; correlated variants stay in one holdout
group; and synthetic trial fixtures prove the aggregate paired metrics and source-family
denominators.

### Milestone 2: public-corpus adapters

The Deepset and InjecAgent adapters consume pinned upstream formats and emit the four-case matched
groups. Benign controls have grounded authorization, and source provenance plus license material is
preserved privately.

Verification: committed samples exercise both visibility treatments at the browser boundary,
attacker text appears only in the full reviewer prompt, twins preserve the proposed action, and
every generated case reconstructs through the production reviewer input.

### Milestone 3: private materialization and baseline

Pinned full corpora are materialized under `.review-eval-local/`, split before expansion, validated
against the deployment registry, and evaluated progressively. Run records retain dataset, registry,
adapter, model, and prompt digests.

Verification: manifests reconcile every accepted, rejected, and quarantined upstream row; no raw or
generated private artifact enters version control; the report states the independent evidence count
separately from trial count.

### Milestone 4: history-derived cases

The separate history pipeline deduplicates extracted templates, uses low-cost batch models to draft
classification and benign/attack pairs, and promotes only validated, reviewed cases. It reuses the
same identity, validation, and reporting contracts but does not mix hallucinated history-derived
cases into public-corpus provenance.

Verification: one model call per batch of unique shapes, deterministic reconstruction of all
generated cases, explicit quarantine accounting, and human review before promotion.

## Deliberate simplifications

- The initial visibility ladder has only hidden and full treatments. Hidden is a deterministic
  synthetic transformation used to isolate visibility, while both prompts otherwise use production
  browser review assembly.
- Natural-benign corpus rows are excluded unless the adapter can ground an authorized action.
- Public-corpus adaptation measures the shipped reviewer boundary, not the upstream benchmark's
  original agent or tool environment.
- Full-corpus repeated-seed runs are deferred until a one-seed run demonstrates that the adapter and
  judge produce useful variation.
