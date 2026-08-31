# Review-eval history extraction runbook

This is a runbook for the maintainer (and the `/engineer` profile they hand it to) covering the
private data path that feeds the tool-call review evaluation harness: **history extraction** into
committable task templates. It produces material that must never reach a committed path. The design
rationale lives in [../design/tool-call-review-eval.md](../design/tool-call-review-eval.md); this
file is the operational procedure.

Extraction is entirely offline: it queries the database with a script, and nothing in the running
application participates. The harness therefore replays *reconstructions* of reviewer inputs rather
than inputs recorded as they were judged — see the design doc's **Data sources** for what that
trades away.

## The private tree

Everything on this path lives under `.review-eval-local/`, which the repository gitignores. Nothing
under it is ever committed. The committed datasets are only the public-corpus, synthetic, and manual
cases; the history quarry is per-deployment and unshared.

The tree is one directory per artifact kind, each with its own consumer, and they are not
interchangeable:

| Directory          | Holds                                    | Consumer                                        |
| ------------------ | ---------------------------------------- | ----------------------------------------------- |
| `public/<corpus>/` | adapted public-corpus cases              | `--dataset` of the eval harness                 |
| `templates/`       | enumerated `TaskTemplate` records        | the maintainer skim, then stage-2 instantiation |
| `runs/`            | run and stamp records the harness writes | read by a human; diffed against a later run     |

`--dataset` scans a directory of **cases**, so point it at a `public/<corpus>/` directory, never at
the tree root: a scanned directory holds cases and nothing else, and a template or a run record
found in one aborts the load naming the file. That is deliberate — the alternative is guessing from
a file's contents which files are cases, which would let a genuinely malformed case disappear
silently.

## Quoting a private case

Evaluation always runs on the **raw** corpus — byte fidelity is what the destination-echo signal
depends on. When you need to quote or share a case that carries household-derived content (a bug
report, a design discussion), generate a pseudonymized copy on demand with the harness's
deterministic pseudonymizer (`family_assistant.eval.tool_call_review.scrub.pseudonymize_case`). It
replaces emails, URLs, phone numbers, and long numeric identifiers with stable pseudonyms — in
mapping keys as well as values, since an additional-property key can itself be an address or an
account id — and accepts an explicit literals map for names and addresses. It fails closed: anything
it cannot rewrite into a still-valid case raises instead of returning a copy that claims to be
pseudonymized. Never paste raw private material outside the private tree.

## History extraction (committable templates)

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
  validation. A schema is the only thing that can vouch for a key, so a template that declares
  argument shapes and resolves no tool is rejected outright rather than passed.
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
poe review-eval-extract-history --database-url "sqlite+aiosqlite:///family_assistant.db" --dry-run

# Write committable templates into the private dir.
poe review-eval-extract-history \
    --database-url "sqlite+aiosqlite:///family_assistant.db" \
    --out-dir .review-eval-local/templates

# Recent history only, which is usually what you want: the task shapes a
# template set is meant to describe are the ones in use now.
poe review-eval-extract-history \
    --database-url "sqlite+aiosqlite:///family_assistant.db" \
    --since 2026-06-01 --out-dir .review-eval-local/templates
```

`--since` takes an ISO 8601 date or datetime and is inclusive; a bare date means midnight UTC and a
naive datetime is read as UTC, because `timestamp` is timezone-aware and a naive bound is an error
on PostgreSQL and a silently wrong comparison on SQLite.

**Pass `--tool-registry` or you will lose every MCP tool call.** The extractor resolves tool names
against the tools compiled into this source tree; a deployment's MCP tools — transport, search,
maps, time — exist only in a process that has connected to the configured servers, so without a
snapshot every call to one is rejected as an unknown tool. Take the snapshot from the deployment
itself:

```bash
# In the deployment container, where the MCP servers are configured:
python scripts/dump_tool_registry.py --out /tmp/registry.json --allow-external-out

# Then extract against it:
poe review-eval-extract-history \
    --database-url "$DATABASE_URL" --since 2026-06-01 \
    --tool-registry .review-eval-local/registry/deployment.json \
    --out-dir .review-eval-local/templates
```

A server that does not come back connected **with at least one tool** aborts the dump rather than
writing a partial registry: its tools would simply be missing, and a missing tool is
indistinguishable from one that never existed, so the next extraction would reject those calls and
report a smaller corpus instead of a broken input. The overlay is resolved as `$CONFIG_FILE` then
`config.yaml`, the same way the application entry point resolves it, so the snapshot describes the
deployment the dump runs inside; `--config-file` overrides it. Local descriptors are built through
the same deployment-effective path as startup, including configuration-driven schemas and OAuth
availability. `--local-only` skips MCP connections but preserves those local customizations, which
makes it useful for checking the local half of a deployment registry.

A `stamp` run records a `registry_hash` alongside its `dataset_hash`. The registry is an input to
every reviewer prompt — tags, destination paths and the MCP server id all render into the tool
context — so two runs over one dataset under two snapshots are two measurements, and the record says
so.

Keep the snapshot with the templates. `scripts/tool_call_review_eval.py` takes the same
`--tool-registry`, and a template set extracted under one registry needs that registry to replay;
schemas from a deployment's MCP servers can enumerate household vocabulary, so the destination
resolves inside the private tree unless `--allow-external-out` names somewhere else private.

The dry run is the default posture for inspection: it reports how many templates are committable and
how many were rejected (with reasons) — by the privacy chokepoint, or because the row itself could
not be abstracted, as when its recorded tool-call arguments are not a JSON object. A rejected row is
reported and dropped, never abstracted into an argument-less template that would look well formed
and quietly thin the task-shape quarry.

**The output directory must be empty.** A template set is the whole answer to one set of parameters,
but it is written as one file per template, so a re-run after changing `--interface-type` or
`--limit` would leave the previous run's templates beside the new ones while the command reported
only the count it just wrote — and both the skim below and the stage-2 pass read the whole
directory. The command therefore refuses a non-empty `--out-dir`, before it reads the database,
rather than clearing it for you: the files there may be part-reviewed work. Remove the directory or
name a different one.

The dry run writes nothing — not to disk, and not to the database. It needs one already at a
compatible schema revision: the extractor never initializes or migrates the database
`--database-url` names, and connects with a plain engine rather than the application's, so it cannot
convert a SQLite file's journal mode either. Point it at an empty or fresh database and the
`message_history` query fails, which is the intended answer for a tool aimed at the wrong place.

**Known limitation.** A `--database-url` you supply already in SQLite URI form —
`sqlite+aiosqlite:///file:…?mode=rwc&uri=true` — is used as written, so an explicitly create-capable
URI stays create-capable and a typo in it can still create an empty database. The rewrite covers the
ordinary path; overriding a mode you asked for by name would be a different policy from not writing
by default. This deployment runs PostgreSQL, where read-only access comes from the role the
connection uses rather than from the URL.

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
4. **Only then commit** the instantiated cases, never the raw templates.

A public repository must never learn private data through its fixture directory. When in doubt, keep
it in `.review-eval-local/`.

## History generation (private M3 run)

The generation command consumes the scrubbed templates and the exact deployment registry snapshot
used to resolve them. It has three explicit phases; each phase refuses to overwrite an existing
artifact or continue with a changed input digest. The model sees only deduplicated security-relevant
shapes. Template ids and frequencies remain in private lineage artifacts. Validated structured
classification and draft JSON are retained privately; raw Pi event streams and stderr are never
persisted.

Use a fresh run directory below `.review-eval-local/runs/`:

```bash
python scripts/generate_review_history_cases.py prepare \
    --templates .review-eval-local/templates \
    --tool-registry .review-eval-local/registry/deployment.json \
    --out-dir .review-eval-local/runs/m3-2026-08-31 \
    --dry-run

python scripts/generate_review_history_cases.py prepare \
    --templates .review-eval-local/templates \
    --tool-registry .review-eval-local/registry/deployment.json \
    --out-dir .review-eval-local/runs/m3-2026-08-31

python scripts/generate_review_history_cases.py classify \
    --templates .review-eval-local/templates \
    --tool-registry .review-eval-local/registry/deployment.json \
    --out-dir .review-eval-local/runs/m3-2026-08-31

python scripts/generate_review_history_cases.py instantiate \
    --templates .review-eval-local/templates \
    --tool-registry .review-eval-local/registry/deployment.json \
    --out-dir .review-eval-local/runs/m3-2026-08-31
```

The default model is `openrouter/z-ai/glm-5.3-flash`; `--model openrouter/deepseek/deepseek-v4-flash-0731`
is also supported. Classification batches contain at most 25 shapes and instantiation batches at
most 5. `--max-shapes 10` is useful for a paid pilot;
the option belongs on `prepare` and is carried by the run manifest. `--dry-run` performs input and
state checks without invoking Pi. Each malformed batch gets one retry,
then its shapes are recorded in quarantine. Unsupported boundaries and multi-tool shapes are
quarantined before a model call.
Delegation shapes with a placeholder sink are also quarantined: a registry snapshot contains tool
metadata but not the deployment's `delegation_sink_classes` mapping, which is required for correct
sink resolution. A concrete historical sink remains usable and is preserved rather than
re-derived.
Pi stdout is drained concurrently with stderr and capped at 4 MiB per attempt; an over-limit
response is quarantined without retaining the stream.
Low-confidence classifications are conservatively retained as review-pending and are not sent to
instantiation. There is no automatic second-model escalation in this command; a maintainer may
manually select a different model for a separately reviewed run.

The generated YAML files under the run's `cases/` directory are review drafts, not a runnable
dataset. Review the private `run.json`, `lineage.jsonl`, classification and case quarantine files,
and every attack/benign pair for a clearly authorized trusted request, a genuinely untrusted attack
context, correct destinations, and realistic synthetic text. Deterministic schema and taint checks
do not establish semantic label quality. Generated contexts always carry the `unknown_external`
taint tier; the historical tier is retained only as private shape lineage and never makes generated
text trusted. The generated benign and attack argument maps must differ so they represent distinct
proposed tool actions; a pair with identical maps is quarantined. Only after human review should a maintainer copy selected drafts into a separately
named private corpus directory and run the normal eval loader; never point
`--dataset` at a run directory and never mix artifacts from different manifests.
