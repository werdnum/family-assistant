# Gemini Thought Signatures: Behavior and Constraints

This document captures findings from experimental testing of Gemini thought signature behavior
(tested with `gemini-3-flash-preview` on 2025-12-25).

## Background

Gemini's "Thinking" models return `thoughtSignature` fields attached to function call parts.
Google's documentation states that these signatures should be "passed back exactly as received" when
sending conversation history in subsequent turns.

## Key Finding: Signatures Do NOT Prevent History Modification

Testing revealed that thought signatures are **not strict cryptographic bindings** that prevent
modification of conversation history. The Gemini API accepts all of the following modifications even
when thought signatures are present:

| Modification Type                            | Result                               |
| -------------------------------------------- | ------------------------------------ |
| Add/modify system prompt                     | Accepted                             |
| Add user message in middle of history        | Accepted                             |
| Modify user message before signed response   | Accepted                             |
| Modify tool call arguments (with signature)  | Accepted (model may notice mismatch) |
| Modify tool response content                 | Accepted                             |
| Modify assistant message text content        | Accepted                             |
| Remove thought signature entirely            | Accepted (with skip workaround)      |
| Add user message at end                      | Accepted                             |
| Remove earlier messages (context truncation) | Accepted                             |

### Interesting Observation

When tool call arguments were modified (e.g., changing `location: "Paris"` to `location: "Tokyo"`)
while keeping the original thought signature, the model noticed the inconsistency and responded:

> "I accidentally retrieved the weather for Tokyo in my previous step"

This suggests the model can detect when the signature doesn't match the content, but the API does
not reject the request.

## What Thought Signatures Actually Do

Based on testing, thought signatures appear to serve purposes other than preventing history
modification:

1. **Preserving reasoning quality**: The signature may help maintain coherence in the model's
   extended thinking across turns
2. **Validation requirement**: Gemini 3 models require thought signatures to be present (or use the
   `skip_thought_signature_validator` workaround) but don't cryptographically validate conversation
   history against them
3. **Internal state tracking**: May be used for internal debugging or quality metrics

## Implementation Notes

### The `skip_thought_signature_validator` Workaround

When a function call doesn't have a thought signature (e.g., programmatically created tool calls or
messages retrieved from database without preserved signatures), use the special value:

```python
thought_signature=b"skip_thought_signature_validator"
```

This tells Gemini to skip validation for that function call.

### Preserving Signatures

While modification is technically allowed, preserving signatures when possible is still recommended:

1. **Reasoning quality**: Signatures may help maintain reasoning coherence
2. **Future compatibility**: Google may strengthen validation in future API versions
3. **Best practice**: Preserving original context when possible is generally good practice

### Current Implementation

The codebase preserves thought signatures:

- Signatures are stored as base64-encoded strings in the database
- They're reconstructed to bytes when sending to the Gemini API
- Missing signatures use the `skip_thought_signature_validator` workaround

## Testing

The findings above were verified through integration tests making real API calls to Gemini. Tests
were run in record mode with a valid `GEMINI_API_KEY`.

## References

- [Google Gemini Thought Signatures Documentation](https://ai.google.dev/gemini-api/docs/thought-signatures)
- `docs/thought-signature-project.md` - Implementation history and architecture
- `src/family_assistant/llm/providers/google_genai_client.py` - Signature handling code
