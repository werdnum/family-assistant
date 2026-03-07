# Processing Module Cleanup Ideas

This document outlines areas for improvement within the `src/family_assistant/processing` module,
focusing on error handling, architectural clarity, and interface design based on the
`docs/development/error-handling.md` guidelines.

## 1. Excessive Fallbacks & Silent Failures

- **`AttachmentProcessor.process_content_parts` (attachments.py):**
  - Catches generic `Exception` when processing an attachment and simply `continue`s (silently skips
    the attachment):
    ```python
    except Exception as e:
        logger.error(f"Error processing attachment content part {attachment_id}: {e}", exc_info=True)
        continue
    ```
    *Improvement:* Catch specific storage/registry exceptions. If an attachment is explicitly
    requested but fails to load, this might be a fatal error for that turn, or should result in an
    explicit error message injected for the LLM rather than silent omission.
- **`AttachmentProcessor.convert_urls_to_data_uris` (attachments.py):**
  - Catches generic `Exception` when reading a file or converting it, and falls back to keeping the
    original (unreachable) URL:
    ```python
    except Exception as e:
        logger.error(f"Failed to convert attachment URL to data URI: {e}")
        converted_parts.append(part)
    ```
    *Improvement:* If conversion fails, the LLM will likely hallucinate or fail since it can't reach
    the local server URL. This should probably raise a specific `AttachmentConversionError`.
- **`AttachmentProcessor.select_for_response` (attachments.py):**
  - Catches generic `Exception` during LLM selection and silently falls back to the last N
    attachments.
  - If the LLM doesn't call `attach_to_response`, it silently falls back.
- **`AttachmentProcessor.handle_large_result` (attachments.py):**
  - Catches generic `Exception` when auto-converting a large result to an attachment and falls back
    to returning the full, large string:
    ```python
    except Exception as e:
        logger.error(f"Failed to auto-convert large tool result to attachment: {e}", exc_info=True)
        return content, None
    ```
    *Improvement:* This defeats the purpose of the threshold and might cause subsequent
    `ContextLengthError`. It should probably fail explicitly or truncate with a clear error
    injected.
- **`ContextPreparer.aggregate_context` (context.py):**
  - Catches generic `Exception` from `provider.get_context_fragments()` and ignores it, omitting
    that provider's context silently.
- **`ProcessingService.handle_chat_interaction` (service.py):**
  - In the history fetching block, it catches generic `Exception` and falls back to
    `raw_history_messages = []`. This means a transient database error results in the LLM seeing a
    completely blank history, which is a confusing user experience (Cascading Silent Failure).
    ```python
    except Exception as hist_err:
        logger.error(f"Failed to get message history... {hist_err}", exc_info=True)
        raw_history_messages = []
    ```
  - In the system prompt template formatting:
    ```python
    except ValueError as e:
        logger.error(f"Failed to format system prompt template: {e}. Using template without substitution.")
        final_system_prompt = system_prompt_template.strip()
    ```
    This is a silent fallback that leaves literal `{placeholders}` in the prompt.
- **`ProcessingService.handle_chat_interaction_stream` (service.py):**
  - Same history fetching silent fallback: `raw_history_messages = []`.
- **`ToolExecutor.execute` (tool_execution.py):**
  - Catches `json.JSONDecodeError` for function arguments and returns a successful
    `ToolExecutionResult` containing a `ToolMessage` with an error string. This is good (letting the
    LLM see the error), but the surrounding code also catches generic `Exception` and does the same.

## 2. Inappropriate Catching of Exceptions

- Throughout `attachments.py`, `context.py`, `service.py`, and `tool_execution.py`, there is heavy
  reliance on `except Exception as e:`.
- According to the error handling guidelines: "GOOD: Catch specific exceptions you know how to
  handle".
- Many blocks should let exceptions propagate up to the top-level `handle_chat_interaction` (or
  `stream` equivalent) which *does* have a catch-all to return a graceful
  `_user_friendly_error_message` to the user. Catching them midway and substituting empty
  lists/strings masks bugs.

## 3. Mushy, Excessively Permissive Interfaces

- **`ProcessingService.handle_chat_interaction` / `handle_chat_interaction_stream` Arguments:**
  - These methods take an enormous number of arguments, many of which are optional (`None`),
    interrelated, or passed straight through to `LLMStreamingLoop` and `ToolExecutor`.
  - Arguments like `chat_interface: ChatInterface | None = None` and
    `chat_interfaces: dict[str, ChatInterface] | None = None` are confusing. Which one should be
    used? (The code later merges them).
  - `request_confirmation_callback` is passed down 4 layers deep as a complex `Callable` type hint.
  - *Improvement:* Group related arguments into specific context or request objects (e.g., a
    `ChatRequestContext` dataclass).
- **`ToolExecutor.execute` Arguments:**
  - Takes `processing_service: Any = None`, `home_assistant_client: Any = None`,
    `camera_backend: Any = None`, `event_sources: dict[str, Any] | None = None`.
  - Using `Any` to avoid circular imports or optional dependencies is a sign of a "mushy" interface.
    It completely bypasses type checking for these critical runtime dependencies.
  - *Improvement:* Define Protocols (Interfaces) in a separate `types.py` or `protocols.py` file to
    break circular imports and provide strict typing, rather than using `Any`.
- **`LLMStreamingLoop.run` / `run_stream` Arguments:**
  - Same issue as above. Passes `Any` typed arguments through to the `ToolExecutor`.
- **`ProcessingServiceConfig` (types.py):**
  - Has `model_parameters: dict[str, dict[str, Any]] | None = None` and
    `fallback_model_parameters: dict[str, dict[str, Any]] | None = None`. The use of `Any` here
    makes the configuration permissive and unvalidated.

## 4. Excessive Mixing of Concerns

- **`ToolExecutor` doing Attachment Storage:**
  - In `ToolExecutor.execute`, there is a large block of logic dedicated to deciding if a
    `ToolResult` contains a new attachment, calculating its file extension based on MIME type,
    storing it via `AttachmentRegistry`, and queuing it.
  - *Improvement:* This logic belongs in `AttachmentProcessor` or `AttachmentRegistry` itself.
    `ToolExecutor` should just execute the tool and pass the raw result to an attachment handler.
- **`ProcessingService` doing System Prompt Formatting Regex:**
  - In `ProcessingService.handle_chat_interaction`, there is a complex, 20-line block of code using
    regex and string replacement to safely format the system prompt while protecting JSON-like
    braces (`{}`).
  - *Improvement:* This formatting logic belongs in a dedicated prompt utility or class (e.g.,
    inside `ContextPreparer` or a new `PromptFormatter`).
- **`LLMStreamingLoop` doing Attachment Selection:**
  - `LLMStreamingLoop.run_stream` directly calls `AttachmentProcessor.select_for_response` when
    `pending_attachment_ids` exceeds a threshold, and it parses the original user query out of the
    message history to pass to it.
  - *Improvement:* The loop shouldn't know how to extract the user query for attachment relevance.
- **Circular Dependency (`ProcessingService` \<-> `ToolExecutor`):**
  - `ProcessingService` initializes `ToolExecutor`.
  - `ProcessingService.handle_chat_interaction` passes `self` (as `processing_service`) down through
    `LLMStreamingLoop` into `ToolExecutor.execute`, which then puts it into a
    `ToolExecutionContext`.
  - This is why `processing_service: Any` is used (to break the import cycle).
  - *Improvement:* Tools should not need a reference to the entire `ProcessingService`. They should
    be provided with specifically scoped interfaces (Protocols) for the capabilities they need
    (e.g., a `MessageSender` interface, an `AttachmentCreator` interface).
