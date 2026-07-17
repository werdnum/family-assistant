# Native Gemini Computer Use for the Visual Browser Profile

## Motivation

The `browser_visual_profile` drives a browser with coordinate-based actions against screenshots.
Until now it ran plain `gemini-3.5-flash` with hand-written function declarations that *mimic* the
legacy Gemini 2.5 computer-use action space (`click_at`, `type_text_at`, …). The Gemini API now
supports native computer use on `gemini-3.5-flash`, which brings:

- A model actually trained for the action loop (better grounding/reliability).
- Built-in **prompt-injection detection** over screenshot content.
- A **safety-decision protocol**: the model can flag an action (e.g. "click Confirm Payment") as
  requiring explicit human confirmation before it is executed.

This design wires the native capability into the existing profile and implements the client-side
machinery the API expects.

## API contract (verified against google-genai SDK)

- Declared via `types.Tool(computer_use=types.ComputerUse(...))` on the standard `generate_content`
  path (the Interactions API is the other option; see decisions).
- `types.ComputerUse` fields: `environment`, `excluded_predefined_functions`,
  `enable_prompt_injection_detection`, `disabled_safety_policies`.
- **Gemini 3.5 Flash action space** (all args include an `intent: str` the model generates;
  coordinates are normalized 0–999, denormalized by `/1000 * screen_dim`): `click`, `double_click`,
  `triple_click`, `middle_click`, `right_click`, `mouse_down(x, y)`, `mouse_up(x, y)`, `move`,
  `type(text, press_enter=False)`, `drag_and_drop(start_x, start_y, end_x, end_y)`,
  `press_key(key)`, `key_down`, `key_up`, `hotkey(keys: list[str])`,
  `scroll(x, y, direction, magnitude_in_pixels=300)`, `navigate(url)`, `go_back`, `go_forward`,
  `take_screenshot`, `wait(seconds=1)`.
- The legacy Gemini 2.5 computer-use preview model (and its `click_at`-style action space) is being
  shut down; this change **deletes** that action space outright rather than keeping dual
  registration.
- Function responses carry `{"url": <current url>}` in `FunctionResponse.response` plus the
  screenshot as an inline `FunctionResponsePart` — exactly what the existing `GoogleGenAIClient`
  multimodal tool-response path already produces.
- **Safety decisions**: the model may add a `safety_decision` object to a function call's arguments:
  `{"decision": "require_confirmation", "explanation": "..."}`. The client must obtain user approval
  and, when approved, include `"safety_acknowledgement": true` (JSON boolean) in the function
  response payload. Decisions other than `require_confirmation` or an explicit allow (e.g. a blocked
  action) must not execute. Prompt injection detection surfaces through the same mechanism.

## Decisions

1. **Stay on `generate_content`.** The whole LLM stack (history persistence, provider-agnostic
   `LLMInterface`, retry/fallback, taint tracking) is built around client-managed history. The
   Interactions API keeps history server-side via `previous_interaction_id`, which conflicts with
   all of that. Native computer use is fully supported on `generate_content`.

2. **Explicit opt-in flag, not model-name sniffing.** Plain `gemini-3.5-flash` is also the default
   assistant model, so the computer-use tool must not be attached based on model name. A new
   `enable_computer_use: true` key on a profile's `processing_config` is plumbed through to
   `GoogleGenAIClient` and is the only thing that attaches the tool (the old
   `computer-use`-in-model-name sniffing is removed along with the retired 2.5 preview support).
   Setting the flag with a non-Google provider or combining it with `retry_config` is a startup
   configuration error (fail fast).

3. **Prompt-injection detection is always on** whenever the computer-use tool is attached
   (`enable_prompt_injection_detection=True`). There is no waiver knob; a detection surfaces as a
   `safety_decision` requiring user confirmation, which is already the safe default.
   `disabled_safety_policies` is deliberately not exposed.

4. **`excluded_predefined_functions` is operator-configurable** (`computer_use_excluded_functions`
   on `processing_config`), defaulting to none. Deployments that route the visual profile through
   browser-server can exclude the few actions that backend cannot faithfully execute (see 6).

5. **Safety-decision handling lives in `ToolExecutor.execute`.** It is the one seam that sees parsed
   arguments before dispatch and builds the `ToolMessage` afterwards:

   - `safety_decision` is always popped from the arguments (tool signatures don't accept it).
   - `decision == "require_confirmation"` triggers the existing `request_confirmation_callback`
     (same UX as policy-confirm). No callback available → the action is refused with an explanatory
     tool result, not executed.
   - Approved → the tool runs and `"safety_acknowledgement": true` is merged into the `ToolResult`
     data. The Gemini client already JSON-parses tool content into `FunctionResponse.response`, so
     the acknowledgement reaches the API without provider-client changes.
   - Rejected/timed out → the action is not executed and the model receives a declined-message tool
     result so it can adapt or wrap up (we keep the loop alive rather than aborting the turn).
     Unexpected confirmation-infrastructure failures propagate (fail fast) rather than being folded
     into a tool result.
   - Any other decision value (`blocked`, unknown future values, malformed payloads) refuses
     execution outright with an explanatory tool result — safety decisions fail closed.
   - A dedicated confirmation renderer shows the action name, its arguments, and the model's
     `explanation` prominently.

6. **Backend degradation is explicit, never silent.** `BrowserBackend` gains `button`/`click_count`
   on `mouse_click` and new `keyboard_down`/`keyboard_up` methods. `LocalPlaywrightBackend`
   implements them natively. The browser-server REST API only supports plain left single clicks and
   has no key-down/up commands, so `RemoteBrowserBackend` raises a clear `BrowserBackendError` for
   `button != "left"`, `click_count != 1`, and key-down/up — the model sees the error text and
   adapts. Everything else in the new action space maps onto existing commands and works on both
   backends unchanged.

7. **The legacy action space is deleted, not deprecated.** The Gemini 2.5 computer-use preview model
   is shut down, so `click_at`, `type_text_at`, `scroll_at`, `open_web_browser`, `search`,
   `wait_5_seconds`, `scroll_document`, `hover_at`, and `key_combination` are removed from the
   registry, tool definitions, and profile policy in the same change (no backwards-compatibility
   layer, per project policy). The new implementations all accept the model-generated `intent`
   argument.

## Accepted residual behavior

- `double_click`/`triple_click` on browser-server are refused (no `click_count` server-side); the
  model can fall back to `click` or DOM delegation.
- If the model emits a safety decision on a *policy-confirmed* tool, the user may be asked twice
  (safety ack + policy confirm). Rare and harmless.
- A safety confirmation approved through the *durable* (task-worker) confirmation path executes with
  `safety_decision` stripped but without merging `safety_acknowledgement` — that flow already
  resumes in a later turn where the original function-call/response pairing no longer exists.

## Milestones

1. **Client + config plumbing** — `GoogleGenAIClient` opt-in flag, tool injection with
   prompt-injection detection, request-side filtering of both action spaces;
   `ProcessingConfig`/`config_loader`/`assistant.py` plumbing with provider validation.
2. **Backends + action tools** — `BrowserBackend` protocol extensions, replacement of the legacy
   tool implementations/definitions/registration with the Gemini 3.5 action space.
3. **Safety confirmation flow** — `ToolExecutor` safety-decision handling + confirmation renderer.
4. **Profile + docs** — `defaults.yaml` (`browser_visual_profile` opt-in, tool policy for new names,
   system prompt), USER_GUIDE, AGENTS.md.
5. **Verification** — full suite, codex review, PR.
