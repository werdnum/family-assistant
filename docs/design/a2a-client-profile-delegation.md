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

    @property
    def service_config(self) -> ProcessingServiceConfig: ...

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
        service_config: ProcessingServiceConfig,
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

| FA Concept                              | A2A Concept                                       |
| --------------------------------------- | ------------------------------------------------- |
| `trigger_content_parts` (text)          | `TextPart` in A2A `Message`                       |
| `trigger_content_parts` (attachment)    | `FilePart` in A2A `Message`                       |
| `conversation_id`                       | A2A `contextId`                                   |
| `subconversation_id`                    | A2A `taskId` (new task per delegation)            |
| `ChatInteractionResult.text_reply`      | Text from A2A `Artifact` or final agent `Message` |
| `ChatInteractionResult.attachment_ids`  | `FilePart`s from A2A Artifacts (stored locally)   |
| `ChatInteractionResult.error_traceback` | A2A task state `failed` + error info              |

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

In `assistant.py::setup_dependencies()`, after building local profiles:

```python
for profile_def in resolved_profiles:
    if profile_def.remote_a2a:
        # Build remote service
        auth_config = A2AAuthConfig(**profile_def.remote_a2a.auth) if profile_def.remote_a2a.auth else None
        client = A2AClientWrapper(
            agent_url=profile_def.remote_a2a.agent_url,
            auth_config=auth_config,
            timeout=profile_def.remote_a2a.timeout_seconds,
        )
        service = RemoteA2AService(
            service_config=build_service_config(profile_def),
            client=client,
        )
        processing_services_registry[profile_def.id] = service
    else:
        # Existing local profile setup
        ...
```

### Content Part Mapping

#### FA -> A2A (outbound)

```python
async def fa_content_to_a2a_parts(
    content_parts: list[ContentPartDict],
    attachment_registry: AttachmentRegistry,
    db_context: DatabaseContext,
) -> list[Part]:
    parts = []
    for part in content_parts:
        if part["type"] == "text":
            parts.append(TextPart(text=part["text"]))
        elif part["type"] == "attachment":
            attachment = await attachment_registry.get_attachment(db_context, part["attachment_id"])
            parts.append(FilePart(
                file=FileContent(
                    bytes=base64.b64encode(attachment.data).decode(),
                    mimeType=attachment.mime_type,
                    name=attachment.filename,
                ),
            ))
    return parts
```

#### A2A -> FA (inbound)

```python
async def a2a_response_to_fa_result(
    task: Task,
    attachment_registry: AttachmentRegistry,
    db_context: DatabaseContext,
) -> ChatInteractionResult:
    text_parts = []
    attachment_ids = []

    # Extract from artifacts (primary output)
    for artifact in task.artifacts or []:
        for part in artifact.parts:
            if isinstance(part, TextPart):
                text_parts.append(part.text)
            elif isinstance(part, FilePart):
                att_id = await attachment_registry.store_attachment(...)
                attachment_ids.append(att_id)
            elif isinstance(part, DataPart):
                text_parts.append(json.dumps(part.data, indent=2))

    # Fall back to the terminal agent message if no artifacts
    if not text_parts and task.history:
        for msg in reversed(task.history):
            if msg.role == Role.agent:
                for part in msg.parts:
                    if isinstance(part, TextPart):
                        text_parts.append(part.text)
                break

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
- **Agent card fetch failure**: Fail at startup during assembly, not at delegation time. This
  ensures misconfigured remote profiles are caught early.
- **Task timeout**: Configurable per-profile. Default 300s. Cancel the A2A task on timeout.

## Implementation Plan

### Milestone 1: Core A2A Client

1. Add `a2a-sdk` dependency
2. Create `src/family_assistant/a2a/` package with:
   - `auth.py` — Auth config model
   - `client.py` — `A2AClientWrapper` with OpenTelemetry spans for discovery and message calls
   - `content_mapping.py` — FA \<-> A2A content conversion
3. Unit tests with mocked HTTP responses

### Milestone 2: Remote Service + Registry Integration

1. Extract `DelegatableService` protocol
2. Implement `RemoteA2AService`
3. Update registry type from `dict[str, ProcessingService]` to `dict[str, DelegatableService]`
4. Update `delegate_to_service_tool` if needed (should be minimal since it already goes through the
   registry)
5. Integration tests with a mock A2A server

### Milestone 3: Configuration + Assembly

1. Add `RemoteA2AConfig` to config models
2. Update config loader to parse `remote_a2a` profile sections
3. Update `assistant.py::setup_dependencies()` to create `RemoteA2AService` instances
4. Add config validation (agent URL reachable at startup, auth configured)
5. End-to-end test with a real A2A sample server

### Milestone 4: Streaming + Multi-turn (Future)

1. Add streaming support via `message/stream`
2. Handle `input_required` state with multi-turn A2A conversations
3. Push notification support for long-running tasks

## Resolved Questions

1. **Agent card caching**: Load at startup, with a simple TTL cache for refresh. No need for
   anything more sophisticated.

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

## Open Questions

1. **Attachment size limits**: A2A uses inline base64 for file parts. Large attachments (images,
   PDFs) could bloat the JSON-RPC payload. Should we enforce size limits or prefer URL-based file
   references?

## Dependencies

- `a2a-sdk` (PyPI) — Official Python SDK for A2A protocol
- `httpx` — Already a transitive dependency via a2a-sdk; also used elsewhere in the project
