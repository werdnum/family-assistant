# Review-eval history generation pipeline

**Status:** Proposed

## Objective

Turn structurally scrubbed history-derived task templates into a broad private corpus without
sending household text to a model or treating model output as trusted evaluation metadata. This is
Milestone 3 of [tool-call-review-eval.md](tool-call-review-eval.md).

## Trust and privacy boundary

The input is the `TaskTemplate` format produced by `extract_review_history.py`, not message history.
Every template is revalidated against the matching deployment registry before use. The batch model
receives only a deterministic shape id and closed-vocabulary or schema-derived fields: boundary,
tool names, argument-key shapes, intent category, content kind, taint tier, and sink class. Template
ids, frequencies, source rows, argument values, and free text remain in the private local manifest.
Delegation shapes whose historical sink is unknown remain quarantined because the registry alone
cannot reconstruct a deployment's profile-specific delegation sink mapping.

Model output is an untrusted draft. The script owns case ids, labels, source and lineage metadata,
tool names, taint metadata, sink resolution, constraints, and output paths. No model response is
written as a case until strict parsing, exact batch-id reconciliation, tool-schema validation, and
review-input reconstruction all pass.

## Stages

### Shape preparation

Templates are canonicalized and deduplicated by their security-relevant structured shape. The
private manifest records every member template and frequency, while prompts contain only the stable
shape id. This turns repeated historical calls into coverage information rather than repeated model
spend or inflated evidence.

### Classification

A cheap model fills only closed-vocabulary hypotheses that the extraction could not know, such as
intent and content kind. Batches contain at most 25 shapes. The response must contain exactly the
requested ids once each and no extra fields. Model-response validation failures receive one bounded
retry, then move to quarantine. A Pi process, event-stream, or output-limit failure aborts the run
visibly rather than being recorded as a bad model response. Sink class and taint are never
model-classified. Deployment-defined tool and property identifiers never cross the model boundary;
the model receives only the closed `ToolTag` vocabulary and closed argument roles derived from
destination metadata and JSON shape. Shapes that cannot be reconciled with the registry are
quarantined before a call.

### Instantiation

Accepted shapes are sent in batches of at most five. The model drafts invented trusted requests,
benign and adversarial untrusted contexts, and argument values for the already-selected tool shape.
The script constructs one attack and one genuinely authorized benign case from each draft. A draft
may invent required values absent from historical argument shapes, but every key and value must
validate against the deployment tool schema.

The attack and benign case share the task surface, not necessarily literal argument values: an
attacker destination and an authorized destination cannot be the same value while retaining honest
labels. Because both cases use the same tool and trusted request, identical argument maps would be
the same proposed action and cannot honestly receive different labels; such drafts are quarantined.
The trusted request must clearly authorize the benign action and must not authorize the attack
action.

### Review and promotion

All generated material stays below `.review-eval-local/`. A run records prompt revision, selected
provider/model, input and registry digests, batch attempts, accepted shapes, and quarantine reasons,
but not raw CLI event streams. Deterministic validation is necessary but does not establish label
quality. Generated cases are promoted to the runnable private corpus only after review of the attack
mechanism, benign authorization, and absence of copied household content.

## Local model adapter

The batch runner invokes `pi` directly, without a shell, tools, extensions, skills, prompt
templates, context files, approval, or session persistence. Prompts are supplied on stdin. JSON mode
is an event stream, so only the final assistant message from the terminal `message_end` event is
parsed; stdout and stderr are not retained after the bounded attempt.

The default first-pass model is `openrouter/z-ai/glm-5.3-flash`; the operator may select
`openrouter/deepseek/deepseek-v4-flash-0731`. Low-confidence classifications remain review-pending
and are not instantiated. The command does not automatically escalate to a second model; a
maintainer may run a separately reviewed follow-up with the alternate model for quarantined,
low-confidence, or high-impact shapes rather than doubling every batch.

## Cost and execution gates

For the current 245 unique shapes, classification uses ten normal calls and instantiation uses 49.
One retry per malformed batch sets a hard ceiling of 118 calls. Before that full run, a pilot covers
ten classification shapes and five instantiations. Paid execution is an explicit command; tests use
a fake subprocess stream and never contact a model.

High-impact sinks and every attack draft require reviewer attention. Full-corpus generation does not
imply promotion, and generated case count does not become an independent-evidence count merely
because a model produced many variants.

## Verification

- Equivalent templates collapse to one shape while the private manifest retains their frequency.
- Only validated templates plus closed-vocabulary semantic roles derived from registry metadata
  enter prompts; deployment tool names and schema property names remain private.
- Missing, duplicate, extra, malformed, or schema-invalid model records fail closed after one retry.
- Pi process, event-stream, and stdout-limit failures abort the run and are never quarantined as
  model-response failures.
- Required tool arguments can be added, but unknown keys and invalid values are quarantined.
- Every accepted case round-trips through `EvalCase`, tool-schema validation, and reviewer-input
  construction.
- All outputs are confined to `.review-eval-local/`; no raw event stream or household text is
  persisted by the generator.
