# Voice Mode On-Demand Tools

## Problem

A Gemini Live session fixes its tool declarations in the `setup` message. There is no mid-session
equivalent of appending a declaration, so the mechanism the chat path uses for progressive
disclosure — `activate_tools`, which hands the LLM loop a fresh, larger tool list on the next
iteration (see `tools/on_demand.py`) — cannot work in a Live session.

Both live paths therefore declare the profile's entire advertisable tool surface up front:

- the browser/iOS path, which receives declarations from `POST /api/gemini/ephemeral-token` and
  executes calls against `POST /api/tools/execute/{name}`, and
- the Asterisk telephony path, which holds the Live session server-side and executes calls directly
  against the profile's `ToolsProvider`.

That is the whole registry including everything a profile lists in
`tools_config.on_demand_local_tools` / `on_demand_mcp_server_ids` — tools the chat path deliberately
keeps out of the prompt.

## Approach

Keep the hidden set exactly as it is: whatever the profile already configures as on-demand. Change
only how it is reached. A Live session declares

- the eager tools (what the chat path shows before any activation), and
- two meta tools whose declarations never change:
  - **`search_tools(query, limit)`** — returns matching tools with name, description and the **full
    JSON Schema of their arguments**, and
  - **`call_tool(name, arguments_json)`** — runs one of them.

Because the declaration list is static, nothing needs to be injected mid-session: the schema the
model needs arrives as tool *output* rather than as a new declaration. The names and one-line
summaries of the hidden tools go into the system instruction as a catalog, the same information the
chat path puts there, so the model knows what is worth searching for.

## Enforcement chokepoint

`LiveMetaToolsProvider` is a `ToolsProvider` that wraps the profile's existing provider chain (taint
tracking → policy enforcement → root providers). Both live paths already funnel every call through
`ToolsProvider.execute_tool`, so intercepting the two meta names there gives:

- `call_tool` dispatching the inner call **into the same chain**, so policy evaluation, taint
  tracking, tool-call review, confirmation rules and metrics apply exactly as they would have if the
  model had called the tool directly. There is no second execution path to keep in sync, and no way
  for `call_tool` to reach a tool the model could not otherwise have reached.
- Everything that is not a meta tool delegating through untouched.

The web path picks the same wrapper up because `POST /api/tools/execute/{name}` resolves the
provider from the processing service; for every non-meta name the wrapper is transparent, so that
endpoint's behaviour for existing callers is unchanged.

## Details

- **No confirmation.** A live session has no confirmation UI (`request_confirmation_callback` is
  `None` on both paths). Search results and `call_tool` are both restricted to what the policy layer
  advertises with `can_confirm=False`, and `call_tool` on a confirmation-gated tool returns a short
  refusal the model can speak instead of a hang or a stack trace. This also fixes the telephony
  path, which previously advertised confirmation-gated tools it could never run.
- **Arguments as a JSON string.** Gemini function declarations require a typed schema for every
  property, so an open-ended argument object is not expressible. `arguments_json` is a string
  holding a JSON object; a malformed value comes back as an error the model can correct.
- **Input schemas only.** The registry declares argument schemas (OpenAI function-calling format)
  and describes results in prose. `search_tools` returns the `parameters` schema verbatim alongside
  the description — precisely what a declared tool would have given the model.
- **Recursion.** `call_tool` refuses the meta tool names.
- **Kill switch.** `gemini_live_config.tools.on_demand` (default `true`) turns the whole mechanism
  off, restoring flat declaration of every advertisable tool;
  `gemini_live_config.tools.search_result_limit` caps a search response.

## Deliberate simplifications

- **No activation state in a live session.** A hidden tool stays hidden for the session's life;
  every call to it goes through `call_tool`. Activation would buy nothing, because the declaration
  list it would have to change is frozen at setup.
- **The transcript shows `call_tool`.** Web and iOS transcripts render the meta call with the inner
  tool name in its arguments rather than as the inner tool. Voice transcripts are a debug
  affordance; special-casing the display is not worth a second rendering path.
- **Search is lexical.** Matching is token overlap against tool names and summaries, ranked with
  name matches first. The catalog in the system instruction means the model normally searches for a
  name it has already seen, so an embedding search would add a dependency and a failure mode for no
  practical gain.

## Work plan

1. **Meta tools and dispatch.** `search_tools`/`call_tool` definitions, `LiveMetaToolsProvider`, and
   the `OnDemandToolsView` refactor that lets both presentations share the descriptor filtering.
   Verified by unit tests over a fake provider chain: hidden tools absent from declarations, search
   returning full schemas, `call_tool` reaching the wrapped provider, policy-denied and
   confirmation-gated names refused, malformed JSON reported.
2. **Wiring.** `ProcessingService.live_tools_provider`; both live paths advertise from it and append
   the catalog to their system instruction; `/api/tools/execute` resolves it. Verified by the
   existing live-API tests extended to assert the meta tools are declared and the on-demand ones are
   not, and by a functional test that runs a hidden tool end to end through
   `/api/tools/execute/call_tool`.
3. **Configuration and documentation.** `gemini_live_config.tools`, defaults, configuration
   reference and the voice section of the user interface guide.
