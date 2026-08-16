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

### 2. The profile evaluates its own sink, as the choke point

The tool-level classification only covers delegation. A profile is also reachable by slash command,
by an A2A inbound request, and by a `wake_llm` automation — and an automation fired by an incoming
email carries exactly the taint this is meant to catch. So `InteractionsAgentProcessingService`
evaluates its declared sink against the turn's incoming taint before submitting, on both the
delegated and the interactive path.

This is not redundant with the tool gate; they cover different entry points, and what each does with
a `confirm` outcome differs because only one of them has somebody to ask:

- **Tool gate** — runs before a delegation run row exists and offers the *confirmation*, because it
  has the confirmation machinery and a live caller. This is where `confirm` is resolved.
- **Profile gate** — the security boundary, and the only gate on entry points that are not tool
  calls.
  - On a **delegated submit** it refuses `deny` only. `delegate_to_service` is the sole creator of a
    delegation run, and its gate classifies the call by this same declared sink, so a `confirm`
    reaching submit has already been approved upstream — refusing it again would fail every approved
    delegation. `deny` is never confirmable, so no upstream approval for it can exist and it is
    still refused here.
  - On a **direct turn** (slash command, A2A request, `wake_llm` automation) it refuses `confirm` as
    well: nothing offered a confirmation on those paths, and a poll-driven or automated turn has
    nobody to ask.

The gate lives on `ProcessingService`, not on the Interactions subclass, and runs at the top of both
`handle_chat_interaction` and `handle_chat_interaction_stream`. The web path streams, so a gate on
the non-streaming call alone would be no gate at all; and putting it on the base class means a
`taint_sink_class` declared on any future profile is enforced rather than silently ignored.

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
3. **`tools/infrastructure.py`** — `TaintTrackingToolsProvider` carries that map and passes it to
   `evaluate_tool`.
4. **`assistant.py`** — builds the map from config before the per-profile loop (it spans profiles)
   and passes the merged taint policy into the processing service so the profile gate has an
   evaluator.
5. **`processing/service.py`** — `sink_refusal_reason()` on the base class, called from both chat
   entry points, with `allow_confirmable` distinguishing a pre-gated delegated submit from a direct
   turn.
6. **`processing/interactions_agent_service.py`** — `_refuse_denied_sink()` applies it to the
   delegated submit; `_build_environment_sources()` replaces the blanket refusal, giving each
   mounted file a unique `target` (a repeated filename gains a `-2` suffix, since the sandbox holds
   one file per path).
7. **`llm/providers/google_genai_client.py`** — `start_agent_interaction(..., environment_sources=)`
   attaches `environment: {"type": "remote", "sources": [...]}`.
8. **`defaults.yaml`** — `coder` declares `taint_sink_class: sandbox_network`.

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
