# iOS Intelligence (Model Tier) Selection

## Status

Implemented. Extends [model-tiers.md](model-tiers.md) — whose UX section already states that the
same semantics "eventually appear across web, iOS, and Telegram" — to the native iOS client. Stage 2
shipped the selection envelope, the admission gate, the web selector and the Telegram tier commands,
and recorded "no iOS control yet" as the surface that was left. This closes that.

Nothing about the tier contract changes here. `POST /api/v1/chat/turns` already accepts
`model_tier`, `GET /api/v1/profiles` already reports each profile's selectable tiers and its
default, and persisted assistant messages already carry `reasoning_info.model_tier*`. The work is
entirely client-side: read those three, and give the user the same one-shot-with-optional-pin
control the web composer has.

## What the user gets

A tier control in the Chat toolbar beside the profile picker — the placement the design doc
specifies — offering the tiers the _current_ profile permits, with the profile's default marked. The
usual state is no selection at all, which sends no `model_tier` and lets the backend apply the
profile default. Choosing a tier applies to the next message and is then spent; a pin in the same
menu keeps it for the rest of the conversation. Replies carry a tier capsule beside the existing
profile capsule, so what actually served a turn is visible in the thread.

Profiles that offer fewer than two selectable tiers show no control at all, rather than a menu with
one dead entry. That is derived from `/v1/profiles`, so a profile becoming pinned (or gaining tiers)
is a config change with no client release.

## Decisions

**The control lives in the toolbar, not the composer.** The iOS composer row already carries camera,
photo, file, paste, the text field and send/stop/steer; a sixth control there would crowd a row
whose every element is per-message. The toolbar already holds the profile picker, and "which agent"
and "how much thinking" are the two questions the design pairs.

**Visible only when it is doing something.** The menu label is icon-only while no tier is selected
and expands to the tier's name (with a pin glyph when pinned) once one is. The web control always
shows a label because it always shows the effective tier; on a phone toolbar that would spend
permanent width restating a default. A selection, which is the state the design requires to be
visible, is what earns the label.

**One-shot is enforced at the send, not by the menu.** The choice is consumed where the turn is
built, so every path that starts a turn spends it exactly once, and a submission that is rejected
before a turn exists (an empty draft, an attachment still uploading) does not silently eat it.

**A tier choice is scoped to the conversation and profile it was made in.** Switching profile,
opening another conversation, and starting a new chat all clear it. This is the same rule the web
applies, and it matters more here: `changeProfile` starts a fresh conversation, so a surviving pin
would carry a spend decision into a conversation the user did not make it in.

**The selection travels with the turn, not with the UI.** `ActiveTurnSession` and `FailedSend`
already carry the profile a turn was sent under so a retry reissues the same turn rather than
reconstructing it from current UI state; the tier joins them for the same reason. A retry of a
message sent at Deep is sent at Deep, whatever the picker now says.

**The badge reads persisted history, not the stream.** iOS replaces its optimistic bubbles with
persisted rows as soon as a turn completes (`mergeNewMessages`), and those rows carry
`reasoning_info`. So the tier badge needs only the messages endpoint — no `turn_ended` plumbing,
which the web needed because assistant-ui keeps the streamed message. One decode site, and a badge
that cannot disagree with what was recorded.

## Deliberate simplifications

- **No Auto entry.** Auto is stage 3; until it exists there is nothing to select. The absent-choice
  state already means "the profile decides", which is where Auto will land.
- **A refused tier surfaces as an ordinary failed send.** The gate returns 400 with a message naming
  the eligible tiers, and the existing send-failure path shows the server's detail inline with a
  Retry. Reaching it requires the operator to narrow a profile's tiers between the profile list load
  and the send, and the retry re-sends the same tier because that is what the user asked for. An
  uncommon path getting correct, explicable behaviour.
- **Voice, the Watch app and App Intents get no tier control.** Voice runs on a Live API session
  whose model is fixed; the Watch and Siri surfaces are deliberately zero-decision. They keep
  sending no `model_tier` and run at the profile default.
- **The pin is not persisted across launches.** Neither is the web's. Spending more on a request is
  a decision about that conversation, not a setting.

## Verification

Unit tests, in the bundle CI already runs:

- The profile list decodes tiers and the default, and a profile with none decodes to an empty list
  (no control).
- A send with a selection puts `model_tier` on the turn body; a send with none omits the key
  entirely, so the backend applies its own default rather than being told what it already is.
- One-shot: the choice is cleared by the send that spent it; a pinned choice survives and is sent
  again by the following turn.
- Reset: switching profile and opening another conversation both clear the choice, pinned or not.
- Retry: a failed send retried reissues the tier it was sent under.
- The tier recorded on a persisted assistant message reaches the rendered bubble, and survives the
  tool-call grouping the thread renders through.
