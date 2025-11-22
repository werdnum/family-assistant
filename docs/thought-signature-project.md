Here is a comprehensive summary of the refactoring and architectural changes.

The overarching theme of these changes is a shift from **"defensive coding"** (accepting mixed types
and coercing them) to **"strict type enforcement"** (validating data shapes at the boundaries). This
was necessitated by the need to support Google Gemini's "Thinking" models, which require
cryptographic thought signatures to be preserved exactly (byte-for-byte) across conversation turns,
a process that was failing due to repeated serialization/deserialization.

### 1. Core Architecture Pattern: "Typed Core, Serialized Edges"

The application now enforces a strict boundary between business logic and I/O:

- **Internal State (Typed):** Within the `ProcessingService` loop and the LLM Clients, messages are
  exclusively Pydantic objects (`AssistantMessage`, `ToolMessage`, `UserMessage`).
- **External State (Serialized):** Serialization to dictionaries or JSON only occurs at the absolute
  edges of the system:
  1. **DB I/O:** The Repository handles converting SQL rows to Objects.
  2. **Network I/O:** The LLM Clients convert Objects to SDK-specific formats immediately before
     transmission.

### 2. Primary Component Changes

#### A. Repository Layer (`message_history.py`)

The repository interface was bifurcated to resolve ambiguity between "data for the LLM" and "data
for the App":

- **`get_recent()` → `list[LLMMessage]`**: Returns strictly typed objects. It deserializes JSON
  (like tool calls) and hydrates complex types (like `GeminiProviderMetadata`) specifically for the
  LLM context window.
- **`get_recent_with_metadata()` → `list[dict]`**: A new method added to retrieve raw dictionary
  rows including database-metadata (timestamps, `internal_id`, `user_id`) required by the Frontend
  and Business Logic, but irrelevant to the LLM.
- **Serialization Responsibility**: The `add()` method now accepts typed `ToolCallItem` objects and
  takes full responsibility for serializing them to JSON for storage, ensuring consistent formatting
  in the database.

#### B. LLM Clients (Google & OpenAI)

- **Strict Input Contract**: `generate_response` now strictly accepts `list[LLMMessage]`. Backward
  compatibility code that sniffed for dictionaries was removed.
- **Google Gemini Protocol Adaptations**:
  - **System Message Merging**: The `_convert_messages_to_genai_format` method was updated to map
    `SystemMessage` objects to `types.Content(role="user", ...)` with a "System:" prefix. This
    avoids API errors with newer Gemini models that have strict constraints on `system` role usage
    or ordering.
  - **Manual Function Calling**: The configuration `automatic_function_calling` is now explicitly
    set to `disable`. This forces the application to handle the tool execution cycle manually, which
    is required to correctly capture and re-inject the thought signatures.
  - **Byte-Level Fidelity**: The client now extracts raw bytes from `GeminiThoughtSignature` objects
    and passes them directly to the SDK, bypassing any internal base64 string conversions that were
    corrupting the cryptographic signatures.

#### C. Processing Service (`processing.py`)

- **Removal of Shims**: The `_convert_dict_messages_to_typed` helper was deleted. The service
  assumes incoming history is already valid typed objects.
- **Signature-Aware Context**: The logic for generating the system prompt was updated to detect if a
  conversation contains Thought Signatures. If present, it avoids modifying or stripping context
  from previous turns (which would invalidate the signature).
- **Serialization Cleanup**: The `message_to_dict()` function (which was ambiguous) was replaced
  with `message_to_json_dict()`, which is explicitly designed for JSON serialization at the API
  boundary.

### 3. Ancillary and Test Infrastructure Changes

#### A. Testing Strategy Overhaul

- **`LLM_RECORD_MODE`**: Introduced a dedicated environment variable for LLM integration tests,
  separating them from standard VCR recording. This supports three modes: `replay` (default/safe),
  `record` (overwrite), and `auto` (append).
- **Documentation**: Added `docs/design/home-assistant-integration-testing.md` and
  `vcr-cassette-rerecording-needed.md` to document the new testing protocols and the necessity of
  re-recording cassettes due to the message format changes.

#### B. Bug Fixes & Cleanup

- **User Message Deduplication**: Fixed a logic error in `processing.py` where the current user
  trigger message was being added to the context twice (once from the database history fetch, and
  once manually).
- **Tool Call Type Safety**: The `ToolCallItem` class now enforces that `provider_metadata` is a
  typed object (e.g., `GeminiProviderMetadata`) rather than a loose dictionary, ensuring type
  checkers can catch serialization errors at compile time.

### 4. The "Why"

The changes were driven by the principle that **implicit conversion is the root of data
corruption**.

By making the interfaces strict (`list[LLMMessage]` vs `list[dict]`), we removed the need for
"defensive" checks scattered throughout the code (e.g., `if isinstance(msg, dict)...`). This ensured
that the opaque binary data required by Gemini's "Thinking" process was handled as an immutable
object reference throughout the lifecycle, only being serialized at the moment of storage or
transmission. This guarantees that the byte sequence received from Google is the exact same byte
sequence sent back in the next turn.

### 5. Test Execution Analysis & Failure Diagnostics

Following the refactoring to strict types and the regeneration of VCR cassettes for the LLM
integration suite, the test landscape has improved significantly. The "Connection Errors" previously
seen in OpenAI tests have been resolved, confirming they were indeed VCR artifacts. However,
distinct failure patterns remain.

#### A. OpenAI Tool Protocol Violation

**Tests:** `tests/integration/llm/test_tool_calling.py::test_tool_response_handling` **Error:**
`InvalidRequestError: Error code: 400 ... An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'.`

- **Symptoms:** 61/62 LLM integration tests now pass. This single failure occurs when manually
  constructing a conversation history involving tool usage.
- **Root Cause:** OpenAI enforces a strict topology: if an Assistant message invokes a tool (defines
  a `tool_call_id`), the immediate next message(s) *must* be Tool messages referencing those exact
  IDs.
  - The test `test_tool_response_handling` manually constructs a history list to simulate a
    mid-conversation state.
  - Likely, the refactoring of `ToolMessage` or `AssistantMessage` serialization has caused a
    mismatch in how `tool_call_id`s are exposed or serialized in this specific manual test setup,
    causing OpenAI to believe the conversation history is incomplete or orphaned.

#### B. Persistent Thought Signature Corruption (Critical)

**Tests:** `test_thought_signature_persistence.py` (SQLite & Postgres) **Error:**
`RuntimeError: LLM streaming error: 400 Bad Request... "message": "Corrupted thought signature."`

- **Symptoms:** Tool execution succeeds (`Script output: 10`), but the subsequent API call fails
  immediately.
- **Root Cause Speculation:** Despite the "Typed Core" refactor, the persistence layer round-trip
  remains flawed.
  1. **Serialization Mismatch:** When `GeminiProviderMetadata` is serialized to JSON for the
     database (base64 string) and deserialized back to `bytes`, there is an encoding mismatch.
  2. **History Reconstruction:** The Google Client's `_convert_messages_to_genai_format` may be
     incorrectly re-wrapping the `function_call` and `thought_signature` parts. The logs show
     warnings about "non-text parts," suggesting the client handling of complex multi-part content
     (Thought + Tool Call) needs tightening.

#### C. Calendar & State Machine Failures

**Tests:** `test_modify_pending_callback`, `test_cancel_pending_callback` **Error:**
`AssertionError: Callback status not updated to 'failed'` and `Callback time not updated correctly`.

- **Symptoms:** Tests fail to see DB state changes (e.g., status remains 'pending').
- **Root Cause:** The logs contain a critical warning:
  `Could not fully resolve type hints... name 'ToolExecutionContext' is not defined`.
  - The Refactor created a circular import or namespace issue involving `ToolExecutionContext`.
  - Consequently, dependency injection fails; tools execute without the context required to perform
    DB writes, resulting in "successful" tool runs (no crash) but no side effects.

#### D. Playwright/UI Timeouts

**Tests:** `test_live_message_updates` **Error:** `TimeoutError: Page.wait_for_selector...`

- **Symptoms:** The frontend never receives or renders the message.
- **Root Cause:** Downstream effect of the API layer refactor. The SSE endpoint relies on
  `get_messages_after_as_dict`. If the serialization in `chat_api.py` does not perfectly match the
  JSON shape expected by the frontend (specifically regarding the new `tool_calls` structure or
  timestamp formatting), the frontend fails to update the DOM.
