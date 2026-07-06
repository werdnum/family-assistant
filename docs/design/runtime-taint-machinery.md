# Runtime Taint Machinery

## Status

Proposed detailed design. This expands the roadmap items from issues #991, #992, and #993 into an
implementation plan for runtime taint state, source trust tiers, taint-aware sink policy, and
automatic provenance propagation to written artifacts.

This design assumes the repository-level note write confinement from
`docs/design/profile-confined-note-writes-and-automation-approvals.md`: `NoteWritePolicy` is the
write-side chokepoint for note labels, `wake_llm` carries execution profile provenance, and confined
profiles can use label policy as an actual boundary. The same approach is extended here: enforcement
lives at chokepoints, labels and taint propagate automatically, and profiles declare policy rather
than reimplementing confinement logic.

## Problem

The codebase has most of the static pieces needed for prompt-injection containment:

- Rule-of-Two processing profiles.
- Tool metadata tags, including `OUTPUT_UNTRUSTED`.
- Per-profile tool policy and durable confirmation.
- Visibility labels on notes and indexed documents.
- Repository-level note write policy.

The missing piece is runtime state. `OUTPUT_UNTRUSTED` currently describes a tool, but no turn-level
object records that an untrusted result has entered the conversation. Without that object, the
assistant cannot answer the practical questions that matter after ambient ingestion is widened:

- Which source trust tier is currently in context?
- Did untrusted text arrive before or after a sensitive corpus read?
- Which sinks should remain free, require confirmation, be audited, or be blocked?
- Which labels should be stamped onto notes, tickets, automations, and other persisted artifacts?
- How can we observe the policy's impact before creating user-facing friction?

A boolean "tainted or not" answer is too coarse. If all email, web, and tool output is collapsed
into one untrusted class, the only safe matrix tends toward "confirm everything after any external
content," which trains rubber-stamping and makes ambient ingestion unusable. The machinery needs
graduated tiers and sink classes so friction concentrates on attacker-addressable, high-bandwidth
egress and post-taint corpus broadening.

## Goals

- Consume `OUTPUT_UNTRUSTED` and source provenance tags at runtime.
- Classify ingested and retrieved content into graduated source trust tiers.
- Maintain an append-only turn taint state that records the maximum taint present in context and
  enough provenance to explain why.
- Enforce a configurable taint-by-sink matrix at chokepoints:
  - tool execution,
  - context assembly and query-aware retrieval,
  - artifact writes,
  - browser snapshots and navigation,
  - sandbox/network egress.
- Support observe-first rollout: record what would have been gated before enforcing it.
- Propagate taint into written artifacts so taint survives storage and re-read.
- Keep the next confined ambient agent to a data connector plus YAML profile configuration.

## Non-Goals

- Perfect content truthfulness. Taint controls what can be done with content, not whether a summary
  derived from that content is correct.
- Per-token taint tracking inside LLM responses. Turn-level and artifact-level taint are the
  enforcement units.
- Blocking all low-bandwidth covert channels. The design focuses on realistic scalable prompt
  injection: attacker-addressable egress, arbitrary web navigation, network-capable code, and
  query-steering into private corpora.
- Replacing processing profiles. Profiles remain the coarse capability boundary; runtime taint makes
  that boundary more granular within a profile.
- Building mailbox sync or authenticated browsing. Those are major consumers of this design, but
  separate designs should cover their connector-specific behavior.

## Terminology

### Source Trust Tier

`SourceTrustTier` is a monotonic classification of content source trust. Higher values are less
trusted and dominate lower values when they appear in the same turn.

| Tier | Name                 | Meaning                                                                                                   | Examples                                                                                   |
| ---- | -------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 0    | `trusted_user`       | Direct input from an authenticated user, or system-authored control text.                                 | Web chat input, Telegram user message, profile preamble, fixed-template scheduler trigger. |
| 1    | `known_contact`      | Human or household source known to the deployment, but not authenticated as the active user.              | Family email sender, known school contact, known forwarded message source.                 |
| 2    | `recognized_machine` | Machine-generated content from a recognized sender or authenticated integration.                          | Receipts, newsletters, delivery notifications, Home Assistant event payloads.              |
| 3    | `unknown_external`   | Unknown humans, arbitrary web content, unauthenticated inbound content, and generic external tool output. | Web pages, search results, unknown email, MCP output without stronger provenance.          |

The turn's taint level is the maximum tier present in context. The max rule is deliberately simple:
it is explainable, monotonic, and cheap to enforce at every chokepoint.

`KNOWN_CONTACT` is intentionally below `RECOGNIZED_MACHINE`: known human relationships are easier to
reason about for a family assistant, while machine mail is often bulk content with templated
tracking links and less predictable body text. The initial matrix treats both tiers identically for
most sinks; the ordering mainly preserves room for future policy distinctions.

### Taint Source

A `TaintSource` explains why taint changed:

- `source_type`: `user_message`, `email`, `document`, `note`, `tool_output`, `browser_snapshot`,
  `attachment`, `automation_trigger`, `event`, `sandbox_output`, or `manual`.
- `source_id`: stable local identifier where available, such as email id, document id, note title,
  attachment id, tool call id, or automation id.
- `tier`: the source trust tier.
- `labels`: artifact labels inherited from storage or ingestion.
- `reason`: short human-readable explanation, safe for audit logs.

### Sink Class

`SinkClass` describes what a requested operation can do with tainted context:

| Sink class                    | Meaning                                                                                             | Examples                                                                            |
| ----------------------------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `user_local`                  | Sends output only to the active authenticated user or current local UI.                             | Chat reply, web stream text, voice reply.                                           |
| `home_local`                  | Acts inside the household trust boundary without arbitrary destination selection.                   | Home Assistant action, MQTT to configured broker.                                   |
| `artifact_write`              | Persists content into the assistant's own stores.                                                   | Note write, task/ticket write, automation write, attachment registration.           |
| `low_bandwidth_external`      | External communication with fixed recipient or fixed template.                                      | Fixed-template push notification, owner-addressed durable confirmation.             |
| `known_user_message`          | Free-form message to a server-validated configured user; destination is not attacker-selectable.    | Future server-validated known-user messaging.                                       |
| `arbitrary_external_message`  | Free-form external communication where destination is model-controlled or outside configured users. | Email sending, shared tracker body, calendar invite with guests.                    |
| `attacker_addressable_egress` | High-bandwidth outbound data to attacker-selectable destinations.                                   | URL fetch, browser navigation, browser form submit, arbitrary webhook call.         |
| `sandbox_network`             | Code or CLI with network access.                                                                    | Worker agent network, Monty extension command, future skills-plus-CLI egress.       |
| `sensitive_read_broadening`   | New access to private corpus after high-tier taint entered context.                                 | Semantic document search, note search, message-history search, full document fetch. |

Sink class is not identical to tool tag. A single tool may map to different sink classes by
arguments or runtime state. For example, browser navigation to a configured origin and browser
navigation to an LLM-provided arbitrary URL need different policy outcomes.

### Policy Outcome

`TaintPolicyOutcome` is one of:

- `allow`: execute normally.
- `audit`: execute normally and record that taint was present.
- `confirm`: request durable confirmation before execution.
- `redact`: execute with server-side redaction or templating.
- `deny`: refuse execution.

`audit` is not a weaker `allow`; it is the observe-first bridge. In observe mode, outcomes that
would later be `confirm`, `redact`, or `deny` are logged as `audit` with `would_have_outcome`.

## Data Model

### Runtime Types

Add a new module, likely `src/family_assistant/security/taint.py`, with small immutable value types:

```python
class SourceTrustTier(IntEnum):
    TRUSTED_USER = 0
    KNOWN_CONTACT = 1
    RECOGNIZED_MACHINE = 2
    UNKNOWN_EXTERNAL = 3


class SinkClass(StrEnum):
    USER_LOCAL = "user_local"
    HOME_LOCAL = "home_local"
    ARTIFACT_WRITE = "artifact_write"
    LOW_BANDWIDTH_EXTERNAL = "low_bandwidth_external"
    KNOWN_USER_MESSAGE = "known_user_message"
    ARBITRARY_EXTERNAL_MESSAGE = "arbitrary_external_message"
    ATTACKER_ADDRESSABLE_EGRESS = "attacker_addressable_egress"
    SANDBOX_NETWORK = "sandbox_network"
    SENSITIVE_READ_BROADENING = "sensitive_read_broadening"


class TaintPolicyOutcome(StrEnum):
    ALLOW = "allow"
    AUDIT = "audit"
    CONFIRM = "confirm"
    REDACT = "redact"
    DENY = "deny"


class TaintSourceType(StrEnum):
    USER_MESSAGE = "user_message"
    EMAIL = "email"
    DOCUMENT = "document"
    NOTE = "note"
    TOOL_OUTPUT = "tool_output"
    BROWSER_SNAPSHOT = "browser_snapshot"
    ATTACHMENT = "attachment"
    AUTOMATION_TRIGGER = "automation_trigger"
    EVENT = "event"
    SANDBOX_OUTPUT = "sandbox_output"
    MANUAL = "manual"


@dataclass(frozen=True)
class TaintSource:
    source_type: TaintSourceType
    source_id: str | None
    tier: SourceTrustTier
    labels: frozenset[str]
    reason: str


@dataclass(frozen=True)
class SensitiveReadScope:
    kind: Literal["notes", "documents", "message_history", "attachments"]
    qualifier: str
    surfaced_ids: frozenset[str]


@dataclass(frozen=True)
class SensitiveReadRecord:
    sequence: int
    scope: SensitiveReadScope
    query_origin: Literal["direct_user", "model_generated", "tool_or_history"]


@dataclass(frozen=True)
class TurnTaintState:
    max_tier: SourceTrustTier
    sources: tuple[TaintSource, ...]
    sensitive_reads: tuple[SensitiveReadRecord, ...]
    fresh_high_taint_seen_at_sequence: int | None
    history_high_taint_present: bool


class TurnTaintTracker(Protocol):
    def snapshot(self) -> TurnTaintState: ...
    def replace(self, state: TurnTaintState) -> None: ...
    def add_source(self, source: TaintSource) -> TurnTaintState: ...
```

`TurnTaintState` should be updated functionally: methods return a new instance rather than mutating
in place. That keeps it safe to pass through async contexts and simple to snapshot into audit logs.
The processing loop owns a small turn-local `TurnTaintTracker` that holds the latest state by
replacement. `ToolExecutionContext` receives the tracker, not a detached state copy. That gives tool
execution, context assembly, confirmations, and final message persistence a shared view while still
keeping the state object immutable and avoiding mutable global state.

The current LLM loop executes top-level tool calls from a single model response concurrently. Each
top-level call in that batch therefore evaluates policy against the same pre-batch snapshot, while
result taint is merged into the tracker as each result completes. This is correct for top-level
batch siblings because one sibling cannot use another sibling's result until that result has been
returned to the model in a later LLM step. Sequence numbers are assigned in three ranges: pre-batch
state, deterministic per-call batch sequence ordered by tool-call index, and post-call merged
states. The sensitive-read-broadening rule uses those sequence numbers so reads and taint introduced
by top-level sibling calls do not pretend to have seen each other.

Batch execution needs two explicit mechanics:

- The LLM loop captures one pre-batch taint snapshot before dispatch and passes that snapshot into
  every top-level tool-call evaluation in the batch. The wrapper must not read the live tracker for
  pre-execution policy during a concurrent top-level batch.
- Result sources are merged through a synchronous tracker method with no `await` between reading the
  old state and replacing it. Audit writes happen after the in-memory merge. This prevents lost
  updates when two tool calls finish close together.

This snapshot rule does not apply inside a script. A Monty script is one top-level tool call, but
its internal tool calls are sequential and script code can feed the result of call N into call N+1.
Script-internal tool calls must therefore evaluate against the live script/turn tracker after each
previous internal result has been merged. A script that fetches unknown external content and then
performs a broad semantic read is evaluated against the post-fetch taint state, not the clean
top-level pre-batch snapshot.

`replace()` is for turn initialization and restoring stored taint state, such as deferred
confirmation execution. Runtime result accumulation should use `add_source()` so concurrent
tool-result merges do not reintroduce lost updates.

### Persisted Metadata

Add source trust/provenance fields to document and artifact metadata before widening ingestion:

- `source_trust_tier`: string enum name.
- `source_trust_reason`: short explanation.
- `provenance_labels`: labels that should contribute to future turn taint.
- `origin_profile_id`: profile that wrote or ingested the artifact, where applicable.
- `origin_turn_id`: turn that wrote the artifact, where applicable.
- `origin_tool_call_id`: tool call that produced the artifact, where applicable.

For indexed documents, these fields should live alongside `visibility_labels` in the vector document
metadata and storage row.

For message history, Phase 1 uses dedicated application-owned columns rather than provider metadata:

- `taint_metadata_json`: compact max tier, source summaries, and metadata version.
- `taint_metadata_version`: schema/version marker, including `runtime_v1` and `legacy_inferred`.

For notes, automations, attachments, and future tickets, use artifact-owned provenance metadata:

- Attachments can reuse their existing metadata JSON.
- Notes and automation tables need new `provenance_metadata_json` columns; do not overload note
  `visibility_labels` or automation `action_config`.
- Future ticket/task integrations should add equivalent artifact-owned provenance metadata at the
  storage boundary.
- Keep high-volume audit details in `taint_audit_events`, not on the artifact row.

Do not overload `visibility_labels` with every audit detail. Visibility labels remain the read
filter; provenance metadata explains why labels exist and how rereads affect taint.

For durable confirmation rows, add confirmation-owned taint metadata:

- `taint_state_json`: the stored state used to render the prompt.
- `sink_class`: resolved sink class for the approved action.
- `static_policy_reason` and `taint_policy_reason`: explanations shown or summarized at approval.
- `approval_policy_fingerprint`: stable fingerprint used to avoid a second confirmation loop when
  the approved action is executed.

## Source Tier Assignment

Tier assignment happens once, at ingestion or result creation, not every time content is read.

### Direct User Input

Authenticated direct user messages enter the turn as `TRUSTED_USER`. Attachments uploaded by the
same user in the same interaction inherit `TRUSTED_USER` for source trust, but their parsed content
can still carry file-type risk. File parser failures must fail loudly; silent parser fallback would
make provenance misleading.

### Email and Mailbox Content

Mailbox sync and existing Mailgun intake should assign tiers using authenticated source data:

- `KNOWN_CONTACT`: sender identity matches a configured family/known-contact allowlist and the
  channel authentication passes the deployment's email-auth rules.
- `RECOGNIZED_MACHINE`: sender/domain/message pattern matches a configured machine-sender allowlist
  and passes authentication. Examples: receipts, statements, school newsletter platform, delivery
  notifications.
- `UNKNOWN_EXTERNAL`: everything else, including failed authentication, unknown senders, forwarded
  content with uncertain origin, and arbitrary HTML.

Sender text inside the email must never self-assert the tier. The classifier may use headers,
authentication results, configured lists, and connector identity; it must not trust claims in the
body such as "this is from Andrew."

### Web, Browser, MCP, and External Tools

Default tier is `UNKNOWN_EXTERNAL` for external content. A tool can return a lower tier only when
the runtime has authenticated, connector-level evidence:

- Home Assistant state from the configured HA instance may be `RECOGNIZED_MACHINE`.
- Calendar data from configured private calendars may be `RECOGNIZED_MACHINE` or `KNOWN_CONTACT`
  depending on source.
- MCP servers default to `UNKNOWN_EXTERNAL` unless the MCP server config explicitly declares a
  trusted local source class.

The existing `OUTPUT_TRUSTED`, `OUTPUT_UNTRUSTED`, and `OUTPUT_UNSPECIFIED` tags map into this:

- `OUTPUT_UNTRUSTED` -> add a `UNKNOWN_EXTERNAL` source unless the tool result supplies a more
  specific authenticated tier.
- `OUTPUT_TRUSTED` -> do not raise taint by itself.
- `OUTPUT_UNSPECIFIED` -> add `UNKNOWN_EXTERNAL` in observe mode until the tool is classified.

`OUTPUT_UNSPECIFIED` should become noisy in audit logs. The rollout is not finished until important
tools are explicitly classified.

## Turn State Lifecycle

### Initialization

Each processing turn starts with a fresh `TurnTaintState` built from:

- direct user message source,
- explicit trigger source, such as automation/event/reminder,
- context provider fragments injected before the model call,
- message history included in the prompt,
- attachments included in the prompt.

The state must be created before the first LLM call, not after the first tool call. Otherwise an
untrusted email fragment injected by a context provider would be invisible to the first requested
sink.

### Context Assembly

Context providers need to return structured fragments, not only strings. Introduce a shape like:

```python
@dataclass(frozen=True)
class ContextFragment:
    provider_name: str
    text: str
    taint_sources: tuple[TaintSource, ...]
    visibility_labels: frozenset[str]
    document_ids: frozenset[str]
```

During migration, legacy providers can be wrapped as `TRUSTED_USER` or profile-configured
`RECOGNIZED_MACHINE`, but the wrapper should emit audit warnings for providers without provenance.

The future query-aware retrieval provider must also return `ContextFragment` so retrieval can both
respect visibility grants and update turn taint when it injects a document.

### Tool Results

The taint-aware provider wrapper is the central result-taint chokepoint. This must sit in the
`ToolsProvider` chain, not only in `ToolExecutor`, because Monty scripts call
`tools_provider.execute_tool()` directly. After the wrapped tool returns but before the result is
given back to the caller, the wrapper resolves result taint:

1. Look up the executed `ToolDescriptor`.
2. Derive result tier from tags and optional structured result metadata.
3. Register a `TaintSource` with the current `turn_id`, tool name, and tool call id.
4. Replace the turn-local tracker state with the new state.
5. Return the original tool result plus side-channel result-taint metadata for callers that persist
   messages or attachments.
6. Persist audit log entries for tier changes.

Tool implementations should not generally mutate taint state directly. They may return richer
metadata when the generic tag is insufficient, such as "this result came from configured Home
Assistant" or "this browser snapshot contains sensitive credential fields." The executor remains the
wrong layer for this conversion because not every tool call is executed through it.

Error results derive taint the same way as successful results. A failed HTTP fetch, browser
navigation, or MCP call can return attacker-controlled error text; once that text is fed back to the
model, it must carry the tool's output taint.

`ToolExecutor` still has work to do: when it converts a tool result into a `ToolMessage`, it copies
the side-channel result-taint metadata into message-history taint storage so future turns can
reconstruct taint from history. Script callers that do not create chat history still update the
turn-local tracker through the provider wrapper.

### Assistant Replies

The final assistant reply is also a sink, but it is normally `user_local`: the response goes back to
the authenticated user or active UI that initiated the turn. Runtime taint should not confirm-gate a
normal answer to that user.

The reply still needs stored taint metadata. If the assistant summarizes an unknown external email,
and that assistant response is loaded into a later turn's history, the later turn should know the
response was derived from unknown external content. The simplest rule is:

- Every assistant message stores the current `TurnTaintState.max_tier` and compact source summary at
  the moment the message is persisted.
- Tool messages store their own result taint as described above.
- History reconstruction takes the max of persisted message and tool taint.

This is slightly conservative because an assistant may ignore the untrusted text, but that is
acceptable. It avoids relying on the model to self-report which context influenced its answer.

### Message History

History is context. If a previous assistant response or tool result contains content derived from
`UNKNOWN_EXTERNAL`, and that content is loaded into a later turn, the later turn must inherit the
stored taint. This requires storing taint metadata with message history, or at minimum storing it
for `ToolMessage` rows and assistant messages that follow them.

Pragmatic first version:

- Store taint metadata on `ToolMessage` rows.
- Store max-tier taint metadata on assistant message rows.
- When formatting history, propagate the max tier of every included message and tool result. A
  direct user message does not cleanse older untrusted context while that older context remains in
  the prompt.

This is conservative enough for prompt injection because tool outputs are the main source of
untrusted content, while assistant messages cover summaries and transformations that would otherwise
lose provenance.

This creates an important usability problem: if an old unknown-external email remains in the history
window, every later turn starts with `max_tier=UNKNOWN_EXTERNAL`. The design handles that by
distinguishing context taint from fresh taint:

- `max_tier` always reflects all included context and governs egress/artifact-write policy.
- `history_high_taint_present` records that high-tier taint came only from included history.
- `fresh_high_taint_seen_at_sequence` records high-tier taint introduced by this turn's new input,
  context injection, or tool results.
- The post-taint read-broadening rule confirms by default after fresh high-tier taint. For
  history-carried high-tier taint, broad reads are:
  - `audit` when anchored in the current direct user request or a narrow already surfaced id,
  - `confirm` when the query is `tool_or_history`, because that is the query-steering case,
  - `confirm` when the query is unanchored `model_generated`.

That mitigation is intentionally narrower than dropping history taint. Old external text still
taints egress and written artifacts, but it does not make every explicitly user-requested search in
a long-running conversation require confirmation forever. Observe-mode audit data should calibrate
the query-origin heuristic before enforcement.

### Scripts, Delegation, and Workers

Subconversations and delegated work must receive explicit taint state. Otherwise a trusted parent
turn could read unknown external content, delegate the risky part, and the child would start clean.

Rules:

- `delegate_to_service` passes the parent's current taint summary into the child request. The target
  profile may add stricter policy, but it cannot lower the inherited tier.
- Async delegation result messages return with the child's max tier. The parent turn absorbs that
  tier when the result is attached to the conversation.
- Monty script execution receives the taint state of the turn or automation trigger that launched
  it. Script tool calls use the same taint evaluator as normal tool calls, but they evaluate against
  the live script-local tracker after each previous script result has been merged.
- `llm_json` and any model call inside a script returns output tainted at least as high as the
  script's input state. If the script included unknown external log/email content, the JSON summary
  is still unknown-external-derived data. The current `llm_json` path is `llm_call_json_async` in
  the scripting LLM API, exposed by `MontyEngine`; it needs the same taint inheritance wrapper as
  tool calls.
- Worker-agent results are `UNKNOWN_EXTERNAL` unless the worker runtime can prove a stricter
  provenance contract. The default worker assumption should be untrusted output plus sandbox-network
  policy for any network access.

## Taint Policy Matrix

The default matrix should be data-driven config with code defaults. Profiles can override it only
within operator-defined minimum strictness; an untrusted profile should not be able to relax its own
egress policy.

Suggested default enforcement after observe mode:

| Max tier in context  | User/local | Home/local | Artifact write | Low-bandwidth external | Known-user message | Arbitrary external message | Attacker-addressable egress | Sandbox network | Sensitive read broadening |
| -------------------- | ---------- | ---------- | -------------- | ---------------------- | ------------------ | -------------------------- | --------------------------- | --------------- | ------------------------- |
| `TRUSTED_USER`       | allow      | allow      | allow          | allow                  | policy             | policy                     | policy                      | policy          | allow                     |
| `KNOWN_CONTACT`      | allow      | allow      | audit          | allow                  | audit              | confirm                    | confirm                     | confirm         | allow                     |
| `RECOGNIZED_MACHINE` | allow      | allow      | audit          | allow                  | audit              | confirm                    | confirm                     | confirm         | allow                     |
| `UNKNOWN_EXTERNAL`   | allow      | allow      | audit          | audit                  | confirm            | confirm                    | confirm                     | deny            | confirm                   |

`policy` means the existing static tool policy decides. Runtime taint should not make trusted turns
more permissive than they are today.

Profiles may make `UNKNOWN_EXTERNAL` attacker-addressable egress stricter, usually `deny` for
non-interactive confined agents. The shipped default should be `confirm` for interactive profiles so
observe data can distinguish useful browsing from prompt-injection-shaped browsing before operators
choose stricter policy.

`redact` is reserved for sink-specific adapters, not a default matrix cell. Browser snapshot
redaction and server-side ticket templating are the intended first users.

The distinction between `known_user_message` and `arbitrary_external_message` matters, but it
requires server-side recipient validation. The current `send_message_to_user` schema tells the model
to use chat ids from the Known Users prompt section, but the tool does not enforce that the supplied
chat id is in the configured list. Until that validation exists, classify `send_message_to_user` as
`arbitrary_external_message`. After validation, it can move to `known_user_message`: still
free-form, but no longer attacker-addressable by destination. Fixed-template notifications are lower
bandwidth and should remain audit only even for unknown-external context, otherwise non-interactive
digesters could not send routine "new item available" notices without turning confirmation into
denial.

This means the mailbox-digester acceptance scenario uses two unattended delivery paths: a confined
note write for the summary, and optional fixed-template notification for "new digest available" or
"critical item found." Free-form messages to known users remain confirm-gated when unknown external
mail is in context.

### Why User and Home Local Stay Free

The current authenticated user already sees the answer. Requiring confirmation before replying to
them adds friction without preventing exfiltration. Home-local actions are bounded to configured
household systems; they can still be state-changing, so the static profile policy and existing
confirmation rules remain in force, but runtime taint does not automatically block them.

The sink resolver must classify Home Assistant actions by service, not blanket every HA call as
`home_local`. Lights, sensors, and local scenes fit `home_local`; HA services that deliver messages,
invoke webhooks, call `rest_command`, or otherwise move data outside the household should map to
`low_bandwidth_external`, `arbitrary_external_message`, or `attacker_addressable_egress` as
appropriate.

### Why Arbitrary Egress Concentrates Friction

Generic prompt injection needs a destination controlled by the attacker. URL fetches, browser
navigation, browser form submission, and network-capable code are high-bandwidth, attacker
addressable, and easy to exploit at scale. These are the sinks where confirmation and denial should
concentrate.

### Post-Taint Read Broadening

The matrix needs a temporal rule in addition to max tier:

If `UNKNOWN_EXTERNAL` has entered the turn, a later request that broadens access to private semantic
memory or sensitive documents requires confirmation. Data already loaded before the high taint
arrived remains usable.

This blocks query steering such as "search your notes for passwords and summarize them." It also
avoids punishing the common useful flow where the assistant first retrieves relevant family context
for a user request and then later fetches a web page.

Implementation:

- Context/retrieval tools are tagged or mapped as `SENSITIVE_READ_BROADENING`.
- `TurnTaintState` records the sequence number when fresh high-tier taint first entered.
- Each sensitive read records a sequence number, stable read scope, and query origin.
- The evaluator confirms reads whose sequence is after `fresh_high_taint_seen_at_sequence` and whose
  scope is broader than data already loaded.
- If high-tier taint came only from history, broad reads are audited only when anchored in the
  current direct user request or a narrow already surfaced id. Queries classified as
  `tool_or_history` or unanchored `model_generated` are confirm-gated. Profiles can choose a
  stricter "confirm on any history taint" mode.

V1 scope comparison should be deliberately small:

- A full fetch of an already surfaced `document_id`, `note_id`, message id, or attachment id is
  narrow.
- Re-reading the active conversation history already included in context is narrow.
- Any semantic search, keyword search across a collection, list-all operation, or full fetch of an
  id that has not been surfaced in the current turn is broad.

This avoids inventing a complicated lattice before there is traffic data. The structured
`SensitiveReadScope` can grow later if the audit log shows useful middle cases.

V1 query-origin classification is likewise conservative:

- `direct_user` when the current user message explicitly asks for the read class ("search my notes",
  "find the email", "open that document") and the query terms are substantially drawn from the user
  message or the read is a narrow fetch of an already surfaced id.
- `tool_or_history` when the query terms are substantially drawn from a high-tier source or an
  external tool result.
- `model_generated` otherwise.

This classifier is only a starting point for observe mode. It must log the evidence used for the
classification so false positives and false negatives can be reviewed.

The v1 classifier must be deterministic and non-LLM-based:

- Normalize current user text, high-tier source snippets, and query arguments to lowercase content
  tokens with stopwords removed.
- Mark `direct_user` only when the user message contains an explicit read/search verb and either the
  query overlaps at least two user content tokens or the read is a narrow already surfaced id.
- Mark `tool_or_history` when at least two query content tokens overlap a high-tier source and do
  not overlap the current user message, or when the query target id came only from high-tier
  history/tool output.
- Mark `model_generated` for the remaining broad reads.

No LLM call participates in this classification; otherwise tainted context would be judging whether
tainted context influenced the query.

The `direct_user` classifier is intentionally narrow because it can be attacker-influenced: an old
injected message can try to get the model to phrase a search using the user's own words. The audit
record must include the matched user terms and any high-tier terms that also appeared in the query
so this failure mode is measurable before enforcement.

Read scopes should be explicit:

- `notes:semantic:*` is broad.
- `documents:full:<document_id>` is narrow if `<document_id>` was already surfaced before high
  taint.
- `message_history:conversation:<conversation_id>` is broad unless it is the active conversation
  already included in history.

## Enforcement Chokepoints

### Tool Advertisement

Do not hide tools solely because of runtime taint. Taint changes during a turn after tools are
advertised, and hiding can make the model reason incorrectly about what is possible. Keep
advertisement based on static policy. Enforce taint at execution time and return a clear tool error
or confirmation result.

Exception: if a profile is statically incapable of confirmation and the taint matrix would require
confirmation for a sink, the tool can be omitted in a later optimization. The first implementation
should prefer execution-time enforcement for correctness.

### Tool Execution

Add a `TaintEnforcingToolsProvider` or extend `PolicyEnforcingToolsProvider` with runtime policy. It
should run after static policy resolution and around the wrapped tool execution:

1. Static policy denies/confirmation still apply first.
2. Resolve `SinkClass` for the tool call from descriptor, tags, arguments, and profile config.
3. Evaluate the taint matrix against the current `TurnTaintState`, or against the pre-batch snapshot
   supplied by the LLM loop for concurrent tool-call batches.
4. In observe mode, log `would_have_outcome` and continue.
5. In enforce mode:
   - `allow`/`audit`: execute.
   - `confirm`: use the existing durable confirmation path.
   - `redact`: execute through the sink-specific redaction adapter.
   - `deny`: raise `ToolPolicyDeniedError`.
6. After execution, derive result taint from the descriptor/result metadata and merge it into the
   turn-local tracker before returning the result. The in-memory merge is synchronous; audit writes
   happen after the tracker update.

Static confirmation and taint confirmation must not create duplicate prompts. If either layer
requires confirmation, produce one confirmation payload that explains both reasons.

### Deferred Confirmations

Durable confirmations split policy evaluation from execution time, so they need explicit taint
handling. This intentionally changes the existing approval semantics: approval no longer means
"rerun whatever current policy allows"; it means "execute the action that was shown, under at least
the policy strictness that caused the prompt."

1. The confirmation row stores the taint state, sink class, static policy reason, taint policy
   reason, and effective outcome that caused the prompt.
2. The renderer shows a redacted taint explanation together with the normal tool arguments.
3. When approved, execution rebuilds context from the stored processing profile and the stored taint
   state. It then re-runs both static and taint policy before executing.
4. If current policy is stricter than the stored prompt, fail closed and ask for a new confirmation.
5. If current policy is looser, use the stored prompt's stricter outcome for that execution.
   Approval should mean "approve what was shown," not "approve whatever policy now permits."

The stored taint state must include enough source summary for audit without preserving full
untrusted payloads inside the confirmation row.

Re-running policy on approval must not create a second confirmation loop for the same reason. The
confirmation executor should pass the stored approval id into the policy layer; if the current
decision is `confirm` for the same tool, arguments, sink class, and taint reason, that confirmation
is considered satisfied. A new or stricter reason fails closed instead of prompting recursively from
inside the approval execution.

### Context Providers and Retrieval

Context assembly must update `TurnTaintState` for injected fragments. Query-aware retrieval and
semantic search tools must also pass through the sensitive-read-broadening check before executing
the search.

The policy evaluator should live below the individual retrieval tools where possible. A shared
`SensitiveReadPolicy` helper can be called by `search_documents`, `get_note`, message-history
search, attachment text reads, and the future reflexive retrieval provider. The key is one helper,
not bespoke conditionals in each tool.

### Artifact Writes

Artifact writes use two mechanisms:

1. Existing write policies, such as `NoteWritePolicy`, enforce what the profile may write.
2. A new `ArtifactProvenancePolicy` derives labels and metadata from `TurnTaintState`.

For notes, the derivation should happen in `ToolExecutionContext.note_write_policy()` or its
successor:

- Static profile required labels remain a floor.
- Runtime taint labels are unioned into `required_labels`.
- `allowed_labels` must include any runtime labels the profile is allowed to produce; otherwise the
  write is denied.
- Provenance metadata records the actual taint sources.

For other artifacts:

- Automation records should store origin profile, origin turn, and provenance labels.
- Tickets/tasks should store provenance labels and refuse to feed automation triggers unless policy
  explicitly allows that provenance.
- Attachment registration should carry provenance metadata when attachment content came from a
  tainted tool result.

Acceptance test: adding a mailbox digester profile should not require Python changes beyond the
mailbox connector. Its notes/tickets inherit mailbox provenance automatically.

### Browser and Credential State

Browser tools need two related but distinct controls:

- Content taint: snapshots, extracted text, downloaded pages, and DOM-derived data are normally
  `UNKNOWN_EXTERNAL`.
- Credential capability: password-in-field and authenticated-session states are non-transferable
  capabilities, stricter than text taint.

The taint matrix controls navigation, extraction, and egress. Credential state should be represented
as a separate capability marker on browser contexts:

- `public`: no credential capability.
- `credential_in_field`: password or secret is present in a page; snapshots redact; only broker
  finalization/discard is allowed.
- `authenticated:<origin>`: cookies/session for an origin; navigation policy is origin-scoped.
- `discarded`: context closed; no further use.

Global snapshot redaction is a chokepoint and should ship before credential broker complexity. Taint
policy then decides whether an authenticated browser context may receive content influenced by
high-tier external text or navigate cross-origin.

### Sandbox and Skills-Plus-CLI

Network egress from code and CLI skills should be mediated by an egress proxy, not per-command
prompt text. Runtime taint supplies the input:

- No high-tier taint: static profile egress policy applies.
- `UNKNOWN_EXTERNAL` present: deny arbitrary network by default; allow configured origins with audit
  or confirmation depending on profile.
- Any response body from the network re-enters the turn as `UNKNOWN_EXTERNAL` unless the origin is a
  configured authenticated integration.

This is what lets skills-plus-CLI become the default extension path without exploding the tool
policy surface.

## Configuration

Add a top-level config section:

```yaml
taint_policy:
  mode: observe  # observe | enforce
  high_taint_tier: unknown_external
  default_unspecified_tool_output_tier: unknown_external
  operator_minimum:
    unknown_external:
      sandbox_network: deny
      attacker_addressable_egress: confirm
  matrix:
    unknown_external:
      attacker_addressable_egress: confirm
      sandbox_network: deny
      sensitive_read_broadening: confirm
      arbitrary_external_message: confirm
      known_user_message: confirm
      low_bandwidth_external: audit
      artifact_write: audit
  source_tiers:
    known_contacts:
      email_addresses: []
      domains: []
    recognized_machine_senders:
      email_addresses: []
      domains: []
      header_patterns: []
  artifact_labels:
    unknown_external: ["source_unknown_external"]
    recognized_machine: ["source_recognized_machine"]
    known_contact: ["source_known_contact"]
```

Profiles can add stricter rules:

```yaml
service_profiles:
  - id: mailbox_digester
    visibility_grants: ["mailbox_digest"]
    processing_config:
      required_note_visibility_labels: ["mailbox_digest"]
      allowed_note_visibility_labels:
        - "mailbox_digest"
        - "source_known_contact"
        - "source_recognized_machine"
        - "source_unknown_external"
    taint_policy:
      matrix_overrides:
        unknown_external:
          arbitrary_external_message: deny
          attacker_addressable_egress: deny
```

Operator policy must be able to set minimum strictness that profile overrides cannot relax. A
restricted profile may make `confirm` into `deny`, but not `deny` into `allow`.

Minimum strictness lives in the top-level `taint_policy.operator_minimum` map. Merge order is: code
defaults, top-level deployment matrix, profile overrides, then operator minimum clamp. The clamp
uses an ordered strictness lattice: `allow < audit < confirm < deny`. Profile config that would
select an outcome below the minimum is rejected during config load rather than silently rewritten.
`redact` is not part of this lattice: it is a sink-specific adapter modifier. A profile cannot
satisfy a `confirm` minimum by selecting `redact`; it must use `confirm` plus redaction, or a
stricter `deny`.

The example minimum intentionally makes `sandbox_network: deny` an unrelaxable deployment rule while
leaving attacker-addressable egress at `confirm` for interactive profiles.

## Audit and Observability

Add a `taint_audit_events` table or equivalent structured log with:

- `event_id`
- `created_at`
- `conversation_id`
- `turn_id`
- `processing_profile_id`
- `subconversation_id`
- `max_tier`
- `sources`
- `sink_class`
- `tool_name`
- `tool_call_id`
- `requested_outcome`
- `effective_outcome`
- `mode`
- `arguments_summary`
- `artifact_id` when relevant

In observe mode:

- Never block or confirm solely due to taint.
- Log the outcome the matrix would have selected.
- Include enough argument summary to tune false positives without storing secrets. Use existing
  confirmation renderers or redaction helpers to avoid raw argument dumps.

Add a review automation that periodically summarizes:

- top tools/sinks that would have been gated,
- most common sources of high-tier taint,
- egress confirmations caused by fresh taint vs history-carried taint,
- Home Assistant/event-triggered flows that would have gated network-capable scripts or browser
  egress,
- `OUTPUT_UNSPECIFIED` tools that need classification,
- note/artifact rereads whose stored provenance disagrees with their tag-derived tier,
- confirmations that would have fired in non-interactive profiles,
- artifacts written with high-tier taint.

The transition to enforcement should be per sink class, not all at once.

## Confirmation UX

Taint-triggered confirmations should be specific and short. They should say:

- what source introduced the taint,
- what sink is being requested,
- why it is risky,
- what will happen if approved.

Example:

```text
This turn includes content from an unknown external email. The assistant wants to navigate the
browser to a URL from the conversation. Approve only if this destination is expected.
```

Do not expose internal jargon such as "tier 3" as the primary user-facing explanation. Keep the
structured tier in audit logs.

## Implementation Plan

### Phase 1: Foundation and Observe-Only State

- Add `security/taint.py` value types.
- Add `TurnTaintState` and a turn-local tracker to processing turn state and `ToolExecutionContext`.
- Add `taint_metadata_json` and `taint_metadata_version` to message-history storage and round-trip
  them through history formatting.
- Map existing tool output tags to result taint in the taint-aware provider wrapper.
- Create audit logging in observe mode.
- Treat `OUTPUT_UNSPECIFIED` as `UNKNOWN_EXTERNAL` for audit purposes.
- Tests:
  - `OUTPUT_UNTRUSTED` tool result raises turn max tier.
  - `OUTPUT_TRUSTED` does not raise tier.
  - unspecified output logs a classification warning.
  - tool result and assistant-message taint survive history formatting into a later turn.
  - concurrent tool-call batches evaluate against the pre-batch snapshot and merge result taint
    before the next LLM step.
  - direct user messages do not cleanse taint from older included history.
  - Monty script calls to an `OUTPUT_UNTRUSTED` tool raise turn taint through the provider wrapper,
    even though they bypass `ToolExecutor`.
  - a Monty script that reads unknown external content and then performs a broad sensitive read is
    evaluated against the post-read taint state, not the top-level pre-batch snapshot.
  - delegated, script, and worker-result paths cannot drop inherited taint.

### Phase 2: Source Tiers at Ingestion

- Add source tier config for email, mailbox, and trusted integrations.
- Stamp tier/provenance metadata into indexed documents.
- Return structured `ContextFragment` from context providers, and ship a legacy adapter that wraps
  existing `list[str]` providers with configured/default provenance during the migration.
- Update email indexing and document search to surface source tier and labels.
- Tests:
  - known-contact email stamps `KNOWN_CONTACT`.
  - recognized machine sender stamps `RECOGNIZED_MACHINE`.
  - unknown or failed-auth email stamps `UNKNOWN_EXTERNAL`.
  - retrieved indexed documents update turn taint.

### Phase 3: Taint Matrix Evaluator in Observe Mode

- Implement `TaintPolicyEvaluator`.
- Add Pydantic config models and config-loader merge support for top-level and profile-level
  `taint_policy`. Nested profile overrides must be covered by tests so they are not silently dropped
  during profile resolution.
- Add sink-class resolver for local tools and MCP tools.
- Wrap tool execution with taint evaluation after static policy.
- Implement post-taint read-broadening checks for document/note/message-history reads.
- Store taint state on durable confirmation rows and re-evaluate policy on approval.
- Keep mode `observe` by default.
- Tests:
  - arbitrary egress after unknown external content logs would-confirm.
  - sandbox network after unknown external content logs would-deny.
  - user-local reply remains allowed.
  - profile-level `taint_policy` overrides cannot relax top-level operator minimums.
  - legacy message-history rows are backfilled or marked before enforcement is enabled.
  - sensitive read before unknown external content is allowed; after unknown external content is
    would-confirm.
  - history-carried high-tier taint audits explicitly user-requested reads instead of confirming
    every read in a long-running conversation.
  - deferred confirmation approval executes with the stored taint state and fails closed if policy
    became stricter.

### Phase 4: Artifact Provenance Propagation

- Add `ArtifactProvenancePolicy`.
- Derive runtime labels from `TurnTaintState`.
- Union runtime labels into `NoteWritePolicy.required_labels`.
- Store provenance metadata on notes, automations, attachments, and future tickets using existing
  metadata JSON where available or `provenance_metadata_json` where not.
- Refuse automation input from disallowed provenance labels by policy.
- Tests:
  - note written after unknown external context gets the configured source label.
  - confined profile whose allowed label ceiling excludes the runtime label is denied.
  - rereading the note restores the stored taint.
  - automation cannot consume a ticket/note with disallowed provenance.

### Phase 5: Enforce High-Confidence Sinks and Split Follow-Ups

- Enable enforcement first for `sandbox_network` and attacker-addressable browser/network egress.
- Keep arbitrary external messages at confirm until audit data proves lower friction is safe.
- Add browser snapshot redaction as a global chokepoint.
- Split sandbox egress proxy and per-origin authenticated browser policy into their own follow-up
  designs before implementation. The capability-state sketch above is enough to keep this design
  compatible with those follow-ups, but not enough to implement them directly.
- Tests:
  - high-tier taint denies sandbox network in non-interactive profiles.
  - browser navigation to an attacker-provided arbitrary URL confirms or denies.
  - credential-in-field snapshots redact regardless of taint mode.

## Migration Strategy

- Ship types and audit logging with no behavior change.
- Classify tools incrementally; audit `OUTPUT_UNSPECIFIED` loudly.
- Treat early Phase 1 audit data as tag-coverage data, not policy-calibration data. Several current
  tools tagged `OUTPUT_UNTRUSTED` are actually retrieval or pure-transform tools:
  - document/note retrieval should derive result taint from each returned document's stored source
    tier once Phase 2 metadata exists,
  - pure transforms such as `jq_query` should preserve the max tier of their input rather than
    manufacturing `UNKNOWN_EXTERNAL`,
  - attachment read-back, especially `read_text_attachment`, should derive result taint from stored
    attachment provenance rather than from a blanket output tag,
  - until those cases are classified, "would-confirm" counts for reads/transforms will be noisy.
- Backfill existing indexed documents as `UNKNOWN_EXTERNAL` unless source-specific evidence exists.
- Before artifact provenance lands, note/artifact reads without provenance metadata must not be
  treated as trusted merely because the read tool has a trusted tag. In observe mode, log these as
  `missing_artifact_provenance`; in enforcement for taint-gated sinks, default missing provenance to
  `UNKNOWN_EXTERNAL` or block enforcement until a backfill has run. Reclassify `list_notes` and
  related note-read tools as part of the tag-cleanup work so note writes cannot launder taint.
- Backfill legacy message-history taint before enabling enforcement:
  - user messages default to `TRUSTED_USER`,
  - tool messages derive tier from the tool descriptor tag where the tool name is known, otherwise
    `UNKNOWN_EXTERNAL`,
  - assistant messages inherit the max tier of preceding included context in the same assistant
    response window when reconstructable, otherwise `UNKNOWN_EXTERNAL` in observe data and excluded
    from enforcement until reviewed. Rows inferred this way should be marked
    `taint_metadata_version=legacy_inferred` so audit reports can separate legacy uncertainty from
    new runtime evidence.
- Do not grant new source labels to the default assistant until observe data is reviewed.
- Convert one ambient agent first, preferably mailbox digesting, as the acceptance test.

## Failure Modes and Required Defaults

- Missing source tier must default to `UNKNOWN_EXTERNAL`, not trusted.
- Missing sink-class mapping must default to static policy plus audit warning in observe mode, and
  to `confirm` or `deny` in enforce mode depending on whether the profile can confirm.
- Missing audit sink must fail closed only when enforcement is enabled for a sink; in observe mode
  it should log an application error but not block user work.
- Taint metadata parse failure from history must default to the highest tier present in raw legacy
  markers, or `UNKNOWN_EXTERNAL` for malformed markers.
- Missing taint metadata on included message-history rows blocks enforcement for taint-gated sinks
  until the migration/backfill above has run. Observe mode may continue and should log
  `legacy_missing_taint_metadata`.
- Profile overrides must not relax operator minimum strictness.

## Open Questions

- How fine-grained browser origin policy should be before authenticated browsing enforcement begins.
- Whether `KNOWN_CONTACT` and `RECOGNIZED_MACHINE` should have different default behavior for
  arbitrary external messages. The initial matrix treats them the same to keep rollout simple.
- Whether history-carried high-tier taint should keep `known_user_message` and browser egress at
  `confirm` for the whole history window, or whether a narrower fresh-vs-history egress rule is
  justified after observe-mode data.
- How to model remote A2A delegation as a sink when the target profile is backed by an external
  endpoint rather than an in-process local profile.
- How to expose taint audit review in the web UI vs as a periodic note/report automation.

## Acceptance Criteria

- A tool tagged `OUTPUT_UNTRUSTED` changes the turn state before any later LLM step or non-batch
  tool execution; same-batch concurrent calls share the pre-batch snapshot and merge results as they
  complete.
- An unknown external source can no longer steer a later broad semantic read without an audit or
  confirmation event.
- Attacker-addressable egress after unknown external content is observed first and then enforceable
  through one policy matrix.
- Notes and other artifacts written during tainted turns inherit configured provenance labels and
  restore taint on reread.
- Adding a mailbox digester requires only the mailbox connector and profile/config declarations; no
  bespoke Python confinement code beyond generic taint/provenance machinery.
