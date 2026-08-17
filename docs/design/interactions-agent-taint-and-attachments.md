# Taint-gating and attachment routing for Interactions agent profiles

## Context

The `coder` profile ([gemini-antigravity-managed-agent.md](gemini-antigravity-managed-agent.md))
runs a Google-hosted sandbox that executes code and reaches the web. It shipped with two deliberate
holes, both recorded as non-goals:

1. **No trust gate.** The profile is reachable by `delegate_to_service`, so the request text it
   receives can be content the assistant derived from an email or a web page. A sandbox that runs
   attacker-influenced instructions is the `[A]`+`[C]` combination the Rule of Two exists to
   prevent, and nothing stopped it.
2. **No attachments.** `submit_async` refused any delegation carrying `attachment_ids` rather than
   dropping them silently, so "transform this spreadsheet" could not be delegated to the one profile
   built to transform things.

These are the same problem seen from two sides. Attachments are not routable *because* there is no
gate; the gate is what makes routing them safe.

The gate already exists and is already correct. `SinkClass.SANDBOX_NETWORK` ships with this matrix:

| turn's max trust tier | → `SANDBOX_NETWORK` |
| --------------------- | ------------------- |
| `trusted_user`        | allow               |
| `known_contact`       | confirm             |
| `recognized_machine`  | confirm             |
| `unknown_external`    | **deny**            |

`spawn_worker` lands there today via its `code_execution` + `worker` tags, so an email-derived
instruction already cannot reach a worker. Three things are missing for `coder`.

## Decision

### 1. A delegation is classified by its target, not by being a delegation

`delegate_to_service` is tagged `delegation` + `output_unspecified`, so a delegation to `coder`
resolves to an ordinary delegation sink no matter what the target does.

`resolve_tool_sink_class` already accepts `arguments` precisely so a tool spanning several sink
classes can be classified by the call rather than by its registration — that is how a Home Assistant
action is classified by its `domain`. Delegation is the same shape: classified by
`target_service_id`.

A profile declares its sink in config (`processing_config.taint_sink_class`), `coder` declares
`sandbox_network`, and the resolver consults a profile-id → sink map built once at startup. A
profile that declares nothing keeps today's classification, so nothing else moves.

### 2. The profile evaluates its own sink, at the point the whole turn is known

The tool-level classification only covers delegation. A profile is also reachable by slash command,
by an A2A inbound request, and by a `wake_llm` automation — and an automation fired by an incoming
email carries exactly the taint this is meant to catch. And a profile may declare a sink and still
hold tools, in which case the model *is* the sink on every call, not only the first: a tool that
reads the web or a mailbox mid-turn raises the tier of what the next call would carry into it.

**Where the check runs is load-bearing.** The turn's taint is not fully known at an entry point: a
trusted "go ahead" pulls in the aggregated context during preparation, and history taint is merged
later still. Checking the trigger's own sources would let a trusted prompt hand the sandbox an
email-derived attachment or a tainted earlier message — precisely what the gate exists to stop. So
the check lives in `LLMStreamingLoop.run_stream`, against the tracker `merge_history_taint` seeds —
the same state the loop uses for tool gating — and it runs at the top of every iteration, so a tool
result that raises the tier closes the sink before the next model call. That is the single point
where history, context and trigger taint are all present, and it is shared by the streaming and
non-streaming paths (`run` delegates to `run_stream`) and by every profile, not just this subclass.
Refusal raises `TaintedSinkRefusedError`; the two chat entry points catch it and render the reason
rather than letting it read as an internal fault.

The submit-then-poll path never runs the loop, so it evaluates what it has — the parent turn's
sources plus the attachments it is about to mount — in `submit_async`.

What each gate does with a `confirm` outcome differs, because only one of them has somebody to ask —
and the difference is settled by evidence, not by inference:

- **Tool gate** — runs before a delegation run row exists and puts the question to the user, because
  it has the confirmation machinery and a live caller. When a user approves a delegation, it records
  that **on that turn's taint state** (`TurnTaintState.approved_sinks`) — and only then. A
  delegation the matrix waved through records nothing: not needing a confirmation and getting one
  are different facts, and a profile may tighten the matrix, so the target's gate can ask about a
  sink this one allowed. Recording passage as approval would answer that question on the user's
  behalf without anything ever being shown.
- **Profile gate** — the security boundary, and the only gate on entry points that are not tool
  calls. It permits `confirm` exactly when an approval for its sink travelled with the taint, and
  refuses `deny` regardless — `deny` is never confirmable, so no approval for it can exist.

**The declaration follows the rollout; it does not preempt it.** The deployment-wide
`taint_policy.mode` ships as `observe`, which downgrades every `confirm` and `deny` to `audit`, so
under it the matrix above records rather than decides. That is deliberate, and a profile-level
`enforce` override on `coder` is the wrong way to close the gap — it was tried and reverted.

Three reasons, all from
[runtime-taint-enforcement-operational-findings.md](runtime-taint-enforcement-operational-findings.md):

- The rollout is in `observe` because the shipped matrix is too false-positive-prone to enable, not
  because nobody got round to it. Enforcing it on one profile applies exactly the matrix under
  measurement, ahead of the measurement.
- The correction proposed for this very cell is to turn interactive
  `unknown_external → sandbox_network` from hard denial *into* confirmation. An override moves it
  the opposite way.
- Worse, it refuses tiers the matrix only wants confirmed. `known_contact` and `recognized_machine`
  resolve to `confirm`, which the profile gate permits only on a recorded approval — and the
  approval comes from the *calling* profile's tool gate, which is still in `observe` and so never
  asks. An override turns those tiers into refusals with nothing that could satisfy them, leaving
  `trusted_user` as the only tier that reaches the sandbox.

Stored `include_in_prompt` notes at `unknown_external`, the systemic cause that document identifies,
reach this profile only indirectly: `coder` sets `include_aggregated_context: false`, so it never
receives the notes context itself, but a `/coder` turn in a conversation whose earlier turns did
inherits that tier through `merge_history_taint`.

The declaration is still what makes the sink real: it takes effect deployment-wide, in one step,
when the mode is switched. Enforcement posture belongs to the deployment, and stays a single control
point rather than a per-profile one.

The downgrade is one of two ways a gate can pass without asking anybody — the other is a matrix that
simply allows the sink. Neither is an approval, which is why the tool gate records one only from an
approved confirmation.

Putting the approval on the taint is what keeps this from becoming a pile of mechanism. The
alternative — a "this was pre-confirmed" flag threaded from the delegation tool down through six
signatures to the loop — only ever *reconstructed* whether an approval was likely, and got it wrong
whenever the caller's and target's policies differed (an observe-mode caller asks nothing, yet the
flag would claim an approval). The approval is a fact about the same content the taint describes, so
it travels with it: serialized into `taint_state_json` with the run, rehydrated by the worker, and
read by whichever gate needs it. Approvals are deliberately *not* carried across a history read — a
human clearing one turn says nothing about a later turn that merely quotes it.

`submit_async` already receives the parent turn's taint as `initial_taint_sources` — the delegation
run persists `taint_state_json` and the worker rehydrates it. The original implementation discarded
that parameter; it is now what the gate evaluates.

### 3. Attachments mount as files, and bring their taint with them

Delegated attachments become `environment.sources` entries of `type: "inline"` with base64 `content`
and a `target` path under `/workspace`, so the agent opens a real file rather than reading a blob
pasted into its prompt. That is the right shape for a coding agent, and it makes binary attachments
work instead of being refused.

Each attachment contributes a `TaintSource` derived from its stored provenance (the same
`taint_metadata` / `source_trust_tier` fields `merge_artifact_taint_into_context` reads), merged
into the state **before** the sink is evaluated. So routing a spreadsheet from an unknown sender
into a code-execution sandbox is denied by the matrix rather than by a hand-written rule, and
routing the user's own file is allowed without one.

## Implementation

1. **`config_models.py`** — `ProcessingConfig.taint_sink_class: SinkClass | None`, plumbed to
   `ProcessingServiceConfig`.
2. **`security/taint.py`** — `resolve_tool_sink_class` takes an optional
   `delegation_sink_classes: Mapping[str, SinkClass]`; a delegation tool whose `target_service_id`
   is in the map resolves to the declared sink.
3. **`tools/infrastructure.py`** — `TaintTrackingToolsProvider` carries that map, and records an
   approval on the turn's taint when it clears a delegation (outright or after a confirmation).
4. **`assistant.py`** — builds the map from config before the per-profile loop (it spans profiles)
   and passes the merged taint policy into the processing service so the profile gate has an
   evaluator.
5. **`security/taint.py`** — `TurnTaintState.approved_sinks`, with `approve_sink()` /
   `is_sink_approved()`, serialized alongside the sources and merged by
   `merge_taint_state_into_tracker`.
6. **`processing/service.py`** / **`processing/llm_loop.py`** — `sink_refusal_reason()` on the base
   class, called from `run_stream` against the merged turn state; both chat entry points catch
   `TaintedSinkRefusedError` and render it.
7. **`processing/interactions_agent_service.py`** — `_refuse_denied_sink()` applies it to the
   delegated submit; `_build_environment_sources()` replaces the blanket refusal, giving each
   mounted file a unique `target` (a repeated filename gains a `-2` suffix, since the sandbox holds
   one file per path).
8. **`llm/providers/google_genai_client.py`** — `start_agent_interaction(..., environment_sources=)`
   attaches `environment: {"type": "remote", "sources": [...]}`.
9. **`defaults.yaml`** — `coder` declares `taint_sink_class: sandbox_network` and pins
   `taint_policy.mode: enforce`, so the gate holds at any rollout stage.

## Non-goals

- **Mounting attachments on the interactive path.** `/coder` with a file attached does not mount it:
  a text, CSV or JSON attachment still arrives as text (the base adapter injects those as text,
  which this path carries), and anything binary is refused rather than dropped. The interactive path
  runs through `LLMInterface.generate_response_stream`, whose signature is shared by every provider
  and cannot carry an environment; giving it one means either mutable per-turn state on a client
  shared across concurrent turns, or replacing streaming with submit-and-poll for these profiles.
  Delegation is the path that matters — the assistant hands the file over. Worth revisiting if the
  interactive case turns out to be common.
- **Explicit `SANDBOX_OUTPUT` labelling of the result.** The safe behaviour already holds, but by
  fallback rather than by intent: `_delegation_result_taint_metadata` derives result taint from the
  assistant rows in the run's subconversation, the poll path writes none, and the `None` branch is
  conservatively `unknown_external`. A test now pins that, so a future change that starts persisting
  assistant rows for these runs cannot silently downgrade a sandbox result to trusted. Labelling it
  as `sandbox_output` with a real reason string would improve the audit trail, but it needs
  `ChatInteractionResult` to carry taint and `_finalize_delegation_run` to consume it, which is more
  machinery than the accuracy gain justifies today.
- **GitHub credentials via the egress allowlist.** `environment.network` takes `{domain, transform}`
  entries whose headers the egress proxy injects on outbound requests, which is how a sandbox would
  authenticate to push — and notably the token never enters the model's context. That is a separate
  change: it needs decisions about where installation tokens come from and how they are scoped per
  repository. It becomes safe to consider *because* of this one: the sink gate is what stops an
  attacker-derived instruction reaching a credentialed sandbox.

## Open questions

- **`confirm` at the profile gate.** Refusing is right while the only confirmable entry point is the
  delegation tool. If a `known_contact`-tier direct `/coder` turn turns out to be common, the
  interactive path could offer a real confirmation, since it has a user waiting.
- **Attachment size.** Capped so one delegation cannot push an arbitrarily large base64 body through
  `interactions.create`. The cap is a constant rather than config until there is evidence about what
  real files look like.
