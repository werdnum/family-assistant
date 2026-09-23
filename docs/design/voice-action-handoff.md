# Voice action handoff

## Goal

While speaking to the assistant, a user can ask for information to be waiting on their phone. A
directions handoff should be tappable directly from its notification; the same link and surrounding
text should remain in the voice conversation in Chat.

## Approach

Native voice allocates its web conversation ID when the session begins and carries it with direct
tool calls and the final transcript save. On the first tool call, the server binds a new ID to the
authenticated caller with an internal history row. Existing conversations must already have that
caller as their sole owner. The row is hidden from Chat; it only establishes the ownership predicate
before the transcript is uploaded at the end of the call.

`send_to_my_chat` has no recipient argument. It saves text to this conversation through the web chat
delivery path, which also requests a push notification and wakes clients following the conversation.
The tool is available to native voice only. It supports two primary actions:

| Action              | Notification tap            | Saved message   |
| ------------------- | --------------------------- | --------------- |
| `open_url`          | Open a validated HTTPS URL  | Include the URL |
| `open_conversation` | Open this Chat conversation | Keep the text   |

The notification retains the conversation ID as a fallback. iOS routes `open_url` through the system
URL opener instead of interpreting it as an in-app path. Web Push uses the same primary action.
Other notification types continue using their existing navigation behavior.

The transcript carries each line's first transcription time and the device time when the upload
begins. The server translates transcript times into its own clock domain before saving, so device
clock skew does not shift the whole transcript away from a handoff. Upload latency may still move
lines close to the handoff by a few seconds. The handoff remains in Chat if the final transcript
upload fails.

## Boundaries

- The server derives the recipient from authentication. The model cannot name a user, conversation,
  device, or notification token.
- Only HTTPS URLs with a host are accepted for direct opening. Other schemes and malformed URLs fail
  before anything is saved.
- A successful tool result means the handoff was saved and notification dispatch was requested. APNs
  and Web Push do not provide an end-user delivery receipt.
- This first version does not invoke arbitrary iOS App Intents. Additional action kinds can be added
  when they have a concrete client behavior.

## Verification

Test first-call ownership binding, refusal of another user's conversation, action validation,
same-conversation transcript persistence and ordering, and notification payloads. Test iOS
notification taps for both direct HTTPS opening and ordinary in-app navigation, then run focused
backend and iOS checks.
