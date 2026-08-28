# A2A Client for Profile Delegation

**Status**: Draft **Date**: 2026-04-03

## Problem

Currently, profile delegation (`delegate_to_service`) only works with local `ProcessingService`
instances running in the same process. All profiles share the same Python runtime, database, and
tool infrastructure.

We want to delegate to **remote agents** over the
[A2A (Agent-to-Agent) protocol](https://google.github.io/A2A/) — Google's open standard for
agent-to-agent communication. This enables:

- Delegating to agents running on different machines or in different runtimes
- Connecting to third-party A2A-compatible agents (e.g., specialized coding agents, research agents)
- Separating compute-heavy profiles (e.g., `complex_tasks` with Opus) into their own services
- Federation: multiple Family Assistant instances collaborating

## Background

### Current Delegation Architecture

The delegation system works as follows:

1. **Profile Registry**: All profiles are `ProcessingService` instances stored in a shared
   `processing_services_registry: dict[str, ProcessingService]`
2. **`delegate_to_service` tool**: The LLM calls this tool with a `target_service_id`. The tool
   looks up the target in the registry, checks `delegation_security_level`, optionally confirms with
   the user, then calls `target_service.handle_chat_interaction()` directly
3. **Security levels**: Each profile declares `delegation_security_level`
   (blocked/confirm/unrestricted)
4. **Tool policy**: Each profile has a `PolicyEngine` that filters which tools the LLM can see and
   call
5. **Result**: `ChatInteractionResult` with `text_reply`, `attachment_ids`, and error info

### A2A Protocol Summary

A2A is JSON-RPC 2.0 over HTTP. Key concepts:

- **Agent Card**: Discovery document at `/.well-known/agent-card.json` describing the agent's
  capabilities, skills, auth requirements, and service URL
- **Task**: Stateful unit of work with lifecycle: submitted -> working ->
  completed/failed/input_required
- **Message**: One conversational turn (role: user/agent) containing Parts (text, file, structured
  data)
- **Artifact**: Deliverable output of a task (distinct from conversational messages)
- **Streaming**: SSE via `message/stream` method
- **Python SDK**: `a2a-sdk` package provides `A2ACardResolver`, `BaseClient`, `ClientFactory`

### Existing A2A Server in This Project

This project already has a full A2A server implementation:

- **`src/family_assistant/a2a/`** — Types (`types.py`) and bidirectional converters
  (`converters.py`) between A2A parts and FA `ContentPartDict`
- **`src/family_assistant/web/routers/a2a_api.py`** — JSON-RPC endpoint at `/api/a2a` implementing
  `message/send`, `message/stream`, `tasks/get`, `tasks/cancel`; agent card at
  `/.well-known/agent-card.json`
- **`src/family_assistant/storage/repositories/a2a_tasks.py`** — Task persistence in the database
- **`tests/functional/web/api/test_a2a_api.py`** — Comprehensive test coverage

The existing converters (`content_parts_to_a2a_parts()`, `a2a_parts_to_content_parts()`,
`chat_result_to_artifact()`) handle the FA \<-> A2A content translation that the client will also
need. The client should reuse these rather than duplicating the logic.

## Design

### Core Idea

Introduce **remote profiles** — profile entries in `config.yaml` that point to an A2A agent URL
instead of defining a local LLM + tools configuration. From the LLM's perspective, delegation works
identically: it calls `delegate_to_service` with a `target_service_id`, and the system handles
routing to either a local `ProcessingService` or a remote A2A agent transparently.

### Architecture

```
delegate_to_service tool
        |
        v
processing_services_registry
        |
        +--> LocalProcessingService (existing ProcessingService)
        |         calls handle_chat_interaction() directly
        |
        +--> RemoteA2AService (new, implements same interface)
                  sends message/send or message/stream via A2A client
                  maps A2A Task/Artifacts back to ChatInteractionResult
```

### Component Design

#### 1. `DelegatableService` Protocol

Extract a protocol from `ProcessingService` that both local and remote services implement. The
`delegate_to_service` tool and the registry already only use `handle_chat_interaction()` and
`service_config` — formalize this.

```python
# src/family_assistant/processing/protocol.py

class DelegatableService(Protocol):
    """A service that can receive delegated requests."""

    kind: Literal["local", "remote"]

    @property
    def service_config(self) -> ProcessingServiceConfig | RemoteServiceConfig: ...

    async def handle_chat_interaction(
        self,
        *,
        db_context: DatabaseContext,
        interface_type: str,
        conversation_id: str,
        trigger_content_parts: list[ContentPartDict],
        trigger_interface_message_id: str | None,
        user_name: str,
        replied_to_interface_id: str | None,
        chat_interface: ChatInterface | None,
        request_confirmation_callback: RequestConfirmationCallback | None,
        subconversation_id: str | None = None,
    ) -> ChatInteractionResult: ...
```

The registry type changes from `dict[str, ProcessingService]` to `dict[str, DelegatableService]`.

##### Registry compatibility with other consumers

The `processing_services_registry` is used beyond `delegate_to_service`. Current consumers that
access richer fields:

- `src/family_assistant/web/routers/chat_api.py` — routes chat interactions to a selected profile
  and (in one path) iterates all services to list profiles with their full configs in the UI
- `src/family_assistant/web/routers/context_viewer.py` — iterates all services and reads
  `service.service_config` fields for a debug/context view
- `src/family_assistant/web/routers/gemini_live_api.py` — routes live voice interactions to a
  selected profile
- `src/family_assistant/telegram/handler.py` — routes slash commands and replies to a targeted
  profile
- `src/family_assistant/task_worker.py` — looks up the `reminder` profile for scheduled tasks

Remote A2A profiles cannot be used as the target of interactive chat/voice sessions in the initial
implementation (the remote agent doesn't share our conversation state, streaming loop, or tool
policy layer). They are **delegation-only targets**. To avoid runtime errors in interactive
consumers:

1. **Add `kind: Literal["local", "remote"]`** to the `DelegatableService` protocol so consumers can
   branch defensively.
2. **Interactive routes** (chat_api, gemini_live_api, telegram slash commands) filter the registry
   to `kind == "local"` when selecting a target. Requesting a remote profile as a conversation
   target returns a 400-equivalent error ("profile is delegation-only").
3. **Profile listing endpoints** (chat_api profile list, context_viewer) either (a) skip remote
   profiles entirely, or (b) include them with a minimal shape flagged as `remote: true` and no
   `tools_config` / `prompts` fields. Preferred: include them in listings so users can see that they
   exist, but mark them as delegation-only.
4. **`task_worker.py`** only looks up the `reminder` profile by id; remote profiles can never hold
   that id, so no change needed.

This keeps delegation transparent (the LLM can still call `delegate_to_service` with a remote
target_service_id) while preventing the shared registry from being a footgun for interactive
consumers.

#### 2. `A2AClientWrapper`

Thin wrapper around the `a2a-sdk` client that handles:

- Agent card discovery and caching
- Sending messages and collecting responses
- Mapping between FA content parts and A2A message parts
- Handling task lifecycle (working -> completed, input_required, etc.)

```python
# src/family_assistant/a2a/client.py

class A2AClientWrapper:
    """Wraps the a2a-sdk client for Family Assistant integration."""

    def __init__(
        self,
        agent_url: str,
        auth_config: A2AAuthConfig | None = None,
        timeout: float = 300.0,
    ) -> None: ...

    async def discover(self) -> AgentCard:
        """Fetch and cache the agent card."""
        ...

    async def send_message(
        self,
        text: str,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        attachments: list[AttachmentData] | None = None,
    ) -> A2ATaskResult: ...

    async def send_message_stream(
        self,
        text: str,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
    ) -> AsyncIterator[A2AStreamEvent]: ...

    async def close(self) -> None: ...
```

#### 3. `RemoteA2AService`

Implements `DelegatableService` by translating `handle_chat_interaction()` into A2A `message/send`
calls.

```python
# src/family_assistant/a2a/remote_service.py

class RemoteA2AService:
    """Implements DelegatableService by delegating to a remote A2A agent."""

    def __init__(
        self,
        service_config: RemoteServiceConfig,
        client: A2AClientWrapper,
    ) -> None: ...

    async def handle_chat_interaction(self, ...) -> ChatInteractionResult:
        # 1. Convert FA content parts to A2A message text + file parts
        # 2. Send via A2A client (message/send)
        # 3. Poll/wait for task completion
        # 4. Convert A2A artifacts back to ChatInteractionResult
        # 5. Handle input_required by returning appropriate error
        ...
```

Key translation logic:

| FA Concept                                 | A2A Concept                                                          |
| ------------------------------------------ | -------------------------------------------------------------------- |
| `trigger_content_parts` (text)             | `TextPart` in A2A `Message`                                          |
| `trigger_content_parts` (attachment)       | `FilePart` in A2A `Message`                                          |
| `subconversation_id` + `target_service_id` | A2A `contextId` (per-delegation isolation, matching local semantics) |
| (none)                                     | `taskId` omitted on first send; server-assigned ID persisted         |
| `ChatInteractionResult.text_reply`         | Text from A2A `Artifact` or final agent `Message`                    |
| `ChatInteractionResult.attachment_ids`     | `FilePart`s from A2A Artifacts (stored via attachment registry)      |
| `ChatInteractionResult.error_traceback`    | A2A task state `failed` + error info                                 |

The client omits `taskId` on the first `message/send` and lets the remote server assign one. The
returned `taskId` is persisted for cancellation correlation.

The `contextId` is derived from `(subconversation_id, target_service_id)` (e.g.,
`f"{subconversation_id}:{target_service_id}"`). This matches local delegation semantics, where
`delegate_to_service_tool` generates a fresh `subconversation_id = uuid4()` per delegation so each
delegation is isolated from prior history. Including `target_service_id` also prevents collisions
when multiple remote profiles share an endpoint. Opt-in continuity (reusing a prior `contextId`
across delegations) can be added later if needed.

#### 4. Configuration

Add a new `remote_a2a` section to profile configuration:

```yaml
# config.yaml
service_profiles:
  # Existing local profile
  - id: "default_assistant"
    processing_config:
      llm_model: "claude-sonnet-4-6"
      ...

  # New remote A2A profile
  - id: "remote_coding_agent"
    description: "Remote coding agent via A2A"
    remote_a2a:
      agent_url: "https://coding-agent.example.com"
      auth:
        type: "bearer"        # bearer | api_key | oauth2_client_credentials
        token_env: "CODING_AGENT_TOKEN"  # env var containing the token
      timeout_seconds: 300
      # Optional: override discovered agent card skills for the LLM description
      skills_description: "Specialized coding agent for complex refactoring tasks"
    processing_config:
      delegation_security_level: "confirm"
```

When `remote_a2a` is present, the assembly code creates a `RemoteA2AService` instead of a local
`ProcessingService`.

#### 5. Auth Configuration

```python
# src/family_assistant/a2a/auth.py

class A2AAuthConfig(BaseModel):
    """Authentication configuration for an A2A agent."""
    type: Literal["bearer", "api_key", "oauth2_client_credentials", "none"] = "none"
    token_env: str | None = None        # env var for bearer token or API key
    header_name: str = "Authorization"  # custom header for api_key type
    # OAuth2 client credentials
    client_id_env: str | None = None
    client_secret_env: str | None = None
    token_url: str | None = None
```

Auth credentials are always read from environment variables, never stored in config files.

#### 6. Assembly Changes

Remote profiles do not need most fields of `ProcessingServiceConfig` (LLM model, tools, prompts,
iteration limits, etc.) since the remote agent owns its own configuration. Rather than splitting
`ProcessingServiceConfig` or making fields conditional, introduce a smaller `RemoteServiceConfig`
that holds only the fields `delegate_to_service` actually reads. This is safe because interactive
consumers (chat_api, context_viewer, gemini_live_api) filter the registry to `kind == "local"`
before accessing fields like `tools_provider`, `prompts`, or `timezone` — see the "Registry
compatibility" section above.

```python
@dataclass
class RemoteServiceConfig:
    """Minimal config for a remote A2A profile.

    Mirrors the surface area of ProcessingServiceConfig that
    delegate_to_service and the registry depend on.
    """
    id: str
    description: str
    delegation_security_level: DelegationSecurityLevel
    tools_config: ToolsConfig  # only confirmation_timeout_seconds is used
```

The `DelegatableService` protocol's `service_config` property returns a union of
`ProcessingServiceConfig | RemoteServiceConfig`, and `delegate_to_service` only reads fields that
exist on both.

In `assistant.py::setup_dependencies()`, after building local profiles:

```python
for profile_def in resolved_profiles:
    if profile_def.remote_a2a:
        auth_config = A2AAuthConfig(**profile_def.remote_a2a.auth) if profile_def.remote_a2a.auth else None
        client = A2AClientWrapper(
            agent_url=profile_def.remote_a2a.agent_url,
            auth_config=auth_config,
            timeout=profile_def.remote_a2a.timeout_seconds,
        )
        service = RemoteA2AService(
            service_config=build_remote_service_config(profile_def),
            client=client,
        )
        processing_services_registry[profile_def.id] = service
    else:
        # Existing local profile setup
        ...
```

### Content Part Mapping

Conversion is split by whether it needs the attachment registry.
`src/family_assistant/a2a/converters.py` holds the I/O-free part (text and plain URL references);
everything carrying attachment bytes goes through `A2AAttachmentTransfer` in
`src/family_assistant/a2a/attachments.py`, in both directions and on both the client and the server.
See [a2a-attachment-transfer.md](a2a-attachment-transfer.md); the size guard for inline attachments
(`MAX_INLINE_ATTACHMENT_BYTES = 10 MB`) lives there and is applied by the client before sending.

The client adds one new function for extracting a `ChatInteractionResult` from a completed A2A task:

```python
async def a2a_task_to_chat_result(
    task: Task,
    attachment_registry: AttachmentRegistry,
    db_context: DatabaseContext,
) -> ChatInteractionResult:
    """Convert a completed A2A Task to a ChatInteractionResult.

    Extracts text and files from artifacts first, falling back to the
    terminal agent message if no artifacts are present.
    """
    text_parts: list[str] = []
    attachment_ids: list[str] = []

    async def extract_parts(parts: list[Part]) -> None:
        for part in parts:
            inner = part.root
            if isinstance(inner, TextPart):
                text_parts.append(inner.text)
            elif isinstance(inner, DataPart):
                text_parts.append(json.dumps(inner.data, indent=2))
            elif isinstance(inner, FilePart):
                att_id = await store_a2a_file_as_attachment(
                    inner, attachment_registry, db_context
                )
                attachment_ids.append(att_id)

    # Extract from artifacts (primary output)
    for artifact in task.artifacts or []:
        await extract_parts(artifact.parts)

    # Fall back to the terminal agent message if no artifacts
    if not text_parts and not attachment_ids and task.history:
        for msg in reversed(task.history):
            if msg.role == Role.agent:
                await extract_parts(msg.parts)
                break

    if not text_parts and not attachment_ids:
        return ChatInteractionResult.error(
            text_reply="Remote agent completed but produced no output.",
            error_traceback="A2A task completed with no text, data, or file parts",
        )

    return ChatInteractionResult.success(
        text_reply="\n\n".join(text_parts),
        attachment_ids=attachment_ids or None,
    )
```

### Task Lifecycle Handling

The A2A task can enter several non-terminal states that need handling:

| A2A State        | FA Behavior                                                                                                 |
| ---------------- | ----------------------------------------------------------------------------------------------------------- |
| `submitted`      | Wait/poll                                                                                                   |
| `working`        | Wait/poll (optionally stream status to user via `chat_interface`)                                           |
| `completed`      | Extract artifacts, return `ChatInteractionResult.success()`                                                 |
| `failed`         | Return `ChatInteractionResult.error()` with task error details                                              |
| `canceled`       | Return error with cancellation message                                                                      |
| `input_required` | **First pass**: return error asking LLM to provide more info. **Future**: support multi-turn A2A delegation |
| `auth_required`  | Return error; auth should be pre-configured                                                                 |
| `rejected`       | Return error; agent declined the task                                                                       |

### Streaming Support (Future Enhancement)

For the initial implementation, use synchronous `message/send`. Streaming can be added later:

1. `RemoteA2AService` implements `handle_chat_interaction_stream()` using `message/stream`
2. SSE events map to FA's `LLMStreamEvent` types
3. `TaskStatusUpdateEvent` -> progress messages via `chat_interface`
4. `TaskArtifactUpdateEvent` -> streamed artifact chunks

### Error Handling

- **Network errors** (connection refused, timeout): Return `ChatInteractionResult.error()` with
  descriptive message. The delegation tool already handles this and reports to the LLM.
- **Auth errors** (401/403): Return error suggesting configuration issue. Log details.
- **Agent card fetch failure**: Validate config shape at startup (URL format, auth config present),
  but perform actual agent card discovery lazily at first delegation time with retry/backoff. This
  avoids making app startup depend on remote agent reachability — a transient network issue
  shouldn't prevent the app from booting.
- **Task timeout**: Configurable per-profile. Default 300s. Cancel the A2A task on timeout.

## Implementation Plan

### Milestone 1: Core A2A Client

`a2a-sdk` is already a dependency. Add to the existing `src/family_assistant/a2a/` package:

1. `auth.py` — Auth config model
2. `client.py` — `A2AClientWrapper` with OpenTelemetry spans for discovery and message calls. Uses
   existing `content_parts_to_a2a_parts()` from `converters.py` for outbound conversion.
3. Unit tests with mocked HTTP responses covering:
   - Agent card discovery (success, unreachable, malformed)
   - `message/send` happy path (task -> completed with artifacts)
   - Task states: `failed`, `input_required`, `auth_required`, `canceled`, `rejected`
   - Timeout and network error handling
   - Attachment size limit enforcement (under limit, at limit, over limit)

### Milestone 2: Remote Service + Registry Integration

1. Extract `DelegatableService` protocol
2. Implement `RemoteA2AService`
3. Update registry type from `dict[str, ProcessingService]` to `dict[str, DelegatableService]`
4. Update `delegate_to_service_tool` if needed (should be minimal since it already goes through the
   registry)
5. Integration tests using the project's own A2A server (at `/api/a2a`) as the remote endpoint. Spin
   up the FA app with a test profile, point `A2AClientWrapper` at it, and verify the full round-trip
   through `delegate_to_service` -> `RemoteA2AService` -> A2A server -> `ProcessingService`. Test
   scenarios:
   - Text delegation and response round-trip
   - Delegation security levels (blocked, confirm, unrestricted)
   - Error propagation (target service fails)
   - Task cancellation

### Milestone 3: Configuration + Assembly

1. Add `RemoteA2AConfig` to config models
2. Update config loader to parse `remote_a2a` profile sections
3. Update `assistant.py::setup_dependencies()` to create `RemoteA2AService` instances
4. Add config validation: validate URL format and auth env vars at startup; do NOT verify remote
   reachability at startup (see Error Handling for lazy discovery)
5. End-to-end test: configure a remote profile pointing at the local A2A server, verify delegation
   works through the full config -> assembly -> delegation -> A2A -> response pipeline

### Milestone 4: Streaming + Multi-turn (Future)

1. Add streaming support via `message/stream`
2. Handle `input_required` state with multi-turn A2A conversations
3. Push notification support for long-running tasks

## Resolved Questions

1. **Agent card caching**: Lazy discovery on first delegation with retry/backoff (see Error
   Handling), then cached in memory with a simple TTL for refresh. Startup only validates config
   shape, not remote reachability, to avoid making app boot depend on remote agents.

2. **Multi-turn delegation**: Out of scope for this design. When an A2A task returns
   `input_required`, return an error to the calling LLM so it can reformulate and re-delegate.
   Multi-turn A2A delegation will be designed alongside multi-turn local profile delegation as a
   separate effort.

3. **Observability**: Yes — add OpenTelemetry spans for A2A calls (discovery, message/send, task
   polling). Remote calls have meaningful latency worth tracking.

4. **Fallback**: No fallback. If a remote agent is unreachable, report the error. The delegation
   tool already handles errors gracefully and reports them to the LLM.

5. **Security**: Use the same security model as local delegation targets
   (`delegation_security_level` on the profile). The risks are not meaningfully different from local
   profiles — both execute arbitrary LLM-driven logic. The existing blocked/confirm/unrestricted
   model is sufficient. Endpoint allowlisting is implicit: remote agent URLs are configured in
   `config.yaml` by the operator, not discovered dynamically or specified by the LLM. The
   `delegate_to_service` tool only accepts a `target_service_id` that maps to a pre-configured
   profile — the LLM cannot redirect delegation to an arbitrary endpoint. This is the same trust
   model as MCP server configurations.

## Design Decisions

### Attachment Size Limits

A2A uses inline base64 for file parts, which bloats payload size by ~33%. To prevent memory spikes
and oversized JSON-RPC requests, enforce a `MAX_INLINE_ATTACHMENT_BYTES` limit (default 10 MB) on
outbound attachments. Attachments exceeding the limit are rejected with an error surfaced to the
calling LLM. URL-based file transfer can be added later if needed for larger payloads.

## Dependencies

No new dependencies required. `a2a-sdk` and `httpx` are already in the project.
