# Review-eval history extraction and capture runbook

This is a runbook for the maintainer (and the `/engineer` profile they hand it to) covering the two
private data paths that feed the tool-call review evaluation harness: **live capture** of reviewed
inputs, and **history extraction** into committable task templates. Both produce material that must
never reach a committed path. The design rationale lives in
[../design/tool-call-review-eval.md](../design/tool-call-review-eval.md); this file is the
operational procedure.

## The private tree

Everything on these paths lives under `.review-eval-local/`, which the repository gitignores.
Nothing under it is ever committed. The committed datasets are only the public-corpus, synthetic,
and manual cases; the friction pool and the history quarry are per-deployment and unshared.

## Path 1 — Live capture (friction set)

### What it does

When `tool_call_review.capture.enabled` is true, the review chokepoint serializes each reviewed
conversation input — the exact typed `ToolCallReviewInput` the judge saw, its constraints, and a
link to the audit row's event id — into `tool_call_review.capture.directory` (default
`.review-eval-local/captures`) as a raw `EvalCase`. This is the only place that data exists: the
audit table keeps identifiers and audit-safe summaries, not the in-memory message window, guidance,
policy contexts, and taint state that were reviewed.

Capture is best-effort and off the review's critical path: the write happens in a detached task
after the verdict is decided and audited, and every failure is logged and swallowed. Capture never
adds latency to, or breaks, a review.

### Enabling it

See
[../operations/CONFIGURATION_REFERENCE.md](../operations/CONFIGURATION_REFERENCE.md#capture-live-capture-friction-set).
Ship it off; turn it on only on a deployment where you are deliberately building the friction set,
and only with `directory` pointing inside `.review-eval-local/`.

### Labels

Captures are stored **raw and unlabeled**, recorded with an interim `label: benign` because the
schema's label is not yet nullable. **This interim label is not a real label.** A benign-by-default
capture would count a correct `deny` as friction and teach tuning to prefer `allow` on it, so:

- Only captures a maintainer has **positively labeled** after skimming enter the friction pool and
  tuning metrics.
- Unlabeled captures replay for observation only.

Until the schema gains a nullable/`unlabeled` label state (a coordinator change), track which
capture ids you have actually reviewed and labeled out of band; do not treat the stored `benign` as
truth.

### Quoting a capture

Evaluation always runs on the **raw** corpus — byte fidelity is what the assembly-parity check and
the destination-echo signal depend on. When you need to quote or share a capture (a bug report, a
design discussion), generate a pseudonymized copy on demand with the harness's deterministic
pseudonymizer (`family_assistant.eval.tool_call_review.scrub.pseudonymize_case`). It replaces
emails, URLs, phone numbers, and long numeric identifiers with stable pseudonyms, and accepts an
explicit literals map for names and addresses. Never paste a raw capture outside the private tree.

## Path 2 — History extraction (committable templates)

### What it does

`scripts/extract_review_history.py` (poe task `review-eval-extract-history`) walks historical turns
through the message-history repository, abstracts each tool call into an **enumerated
`TaskTemplate`**, runs every template through the structural privacy chokepoint, and writes only the
templates that pass into `.review-eval-local/templates`.

A `TaskTemplate` is a structured record of enumerated fields only:

- `intent_category` — a closed vocabulary (or a `<placeholder>` token).
- `tool_names` — resolved against the live tool registry.
- `argument_shapes` — argument keys mapped to JSON **type names**, never values. Only keys the tool
  declares in its parameter schema are recorded, with the type taken from the schema; an unexpected
  key (where household text could otherwise ride across the boundary) is dropped and fails closed at
  validation.
- `sink_class`, `taint_tier` — enumerated from the taint model.
- `content_kind` — a fixed content-kind tag.

No field admits verbatim household text. The extraction pass emits `<unknown>` for intent and `none`
for content-kind (they are not recoverable from history alone); a later classification pass refines
them to closed-vocabulary values before instantiation.

### What the structural chokepoint guarantees

The privacy boundary is **structural, not procedural**: `TaskTemplate.validate_committable()` fails
closed. A template is committable only if every field parses as its enumerated type or a placeholder
token; any free-text or unrecognized value aborts the export. The guarantee is that **private text
has no field to travel in** — not that a reviewer remembered to look. The script revalidates every
template at the write boundary, so nothing reaches disk without passing the chokepoint immediately
before it is written, and it refuses any `--out-dir` outside the `.review-eval-local/` tree.

The maintainer skim is a **second layer on top of** the validator, not the boundary itself.

### Running it

```bash
# Dry run: classify, validate, report counts and rejections, write nothing.
poe review-eval-extract-history -- --database-url "sqlite+aiosqlite:///family_assistant.db" --dry-run

# Write committable templates into the private dir.
poe review-eval-extract-history -- \
    --database-url "sqlite+aiosqlite:///family_assistant.db" \
    --out-dir .review-eval-local/templates
```

The dry run is the default posture for inspection: it reports how many templates are committable and
how many the chokepoint rejected (with reasons), without touching disk. It runs cleanly against an
empty or fresh dev database (it initializes the schema and finds zero turns).

### Human-review step before anything leaves the private tree

Templates are committable in the sense that they carry no private text — but they still describe
this household's task distribution, and committing them is a deliberate act. Before any template
moves from `.review-eval-local/templates` into a committed dataset:

1. **Read every template.** Confirm each field is a genuine enumerated value or an intended
   placeholder, and that no argument key encodes content (the chokepoint admits only keys the tool
   declares in its parameter schema, but a distinctive key set can still be revealing).
2. **Refine placeholders.** Replace `<unknown>` intents and `none` content-kinds with
   closed-vocabulary values via the classification pass; re-run `validate_committable()`.
3. **Instantiate, do not copy.** Committed cases come from stage 2 — a model hallucinating concrete
   names, dates, and bodies from the template. Never hand-copy real content into a case.
   Hallucinated content is sufficient: the judge rules on alignment between fenced content and
   trusted intent, so the content must be realistic, never real.
4. **Only then commit** the instantiated cases (not the raw templates or captures).

A public repository must never learn private data through its fixture directory. When in doubt, keep
it in `.review-eval-local/`.
