# Integrations as Plugins

## Status

Milestones 1 and 2 implemented: the plugin seam with Home Assistant as its first plugin, then
Reolink cameras. Later milestones are proposals.

## Problem

Each bespoke integration was spread across the core. Before this change Home Assistant alone reached
into about twenty files outside its own tool module:

- **Config:** four `home_assistant_*` fields on every profile's `processing_config`, plus an
  `event_system.sources.home_assistant` switch.
- **Construction:** `assistant.py` built and cached a client per profile, a context provider per
  profile and one event source per unique client.
- **Plumbing:** a `home_assistant_client` parameter threaded from `ProcessingService` through the
  LLM loop and tool execution into `ToolExecutionContext`, and through the task worker, the tools
  API and the voice API. Reolink cameras did the same with `camera_backend`.
- **Registration:** each tool listed three times in `tools/__init__.py`.

Adding an integration meant touching all of that, which is also why a finer-grained native tool (the
per-result Trino grading discussed in the Trino taint thread) was expensive to consider.

## Why a seam, not one-off code

The seam has several users already in the tree: Home Assistant, Reolink cameras, AI workers, and
less obviously Google data, UCP shopping, MQTT and weather. A native Trino tool would be another.
The generic part is only the seam; each plugin's code is concretely about its own system.

## Shape

A plugin is in-process Python listed in `family_assistant/plugins/registry.py`. There is no dynamic
loading and no isolation: plugin code is trusted exactly as core tool code is.

- **`Plugin`** (`plugins/base.py`) declares an `id`, a pydantic `config_model`, its `tools` as
  `ToolRegistration`s (definition, implementation and tags in one object), and `start()`, which
  builds a runtime instance from one configured entry.
- **`PluginInstance`** is what one configured entry contributes at runtime: context providers for a
  profile, event sources, and `close()`. It holds its own client, so the client no longer travels
  through the core.
- **`PluginRuntime`** (`plugins/runtime.py`) starts every configured instance once at startup.
  `for_profile()` returns a **`ProfilePlugins`**: the instances that profile selected. That one
  object replaces each per-integration parameter in the processing layer and on
  `ToolExecutionContext`; a tool finds its instance with `context.plugins.get(InstanceType)`.

### Config

`plugins.<plugin id>` is a map of named instances, validated by the plugin's own model, so the
`SecretStr` rule and diagnostic masking apply unchanged. A profile gets each plugin's `default`
instance unless its `plugins` map names another instance or `null`. Naming an unknown plugin or
instance is a startup error.

Plugin tools are registered whether or not the plugin is configured, so `tools_policy` can name them
and the tool inventory is the same in every deployment. A tool whose profile has no instance reports
that the integration isn't configured, as before.

### Taint and sinks

Plugin tools declare the same static tags as other tools. A tool may also grade each result itself:
it returns `ToolResult.provenance`, and the host records it, but only for a tool registered with a
`cleanest_result_tier`, and never cleaner than that tier. A grading bug can therefore only err as
far as the tool's declared floor, and a tool that declares no floor keeps its static grade whatever
it returns. The Trino plugin is the first user: it grades a query by the tables `EXPLAIN (TYPE IO)`
says it reads, against an operator-supplied table-to-tier map. Native tools outside plugins can use
the same return value; the older pattern of adding sources to the execution context still works.

## Milestone 1: the seam and Home Assistant

Home Assistant was chosen first because it has the most end-to-end coverage: VCR integration tests
against a real Home Assistant, functional tests through the processing loop, and event-source tests.

- Tools, client, context provider and event source live in `plugins/home_assistant/`.
- `home_assistant_client` is gone from the processing layer and `ToolExecutionContext`, replaced by
  `plugins`.
- `processing_config.home_assistant_*` and `event_system.sources.home_assistant` are gone;
  `HOMEASSISTANT_URL` and `HOMEASSISTANT_API_KEY` populate the `default` instance.
- `defaults.yaml` no longer ships a household-specific context template.

### Deliberate simplifications

- **One Home Assistant event source.** Event source ids are persisted as `home_assistant`, so at
  most one instance may enable events; a second is a startup error.
- **Plugin tools still take `ToolExecutionContext`.** A narrower plugin tool context is worthwhile
  but would rewrite every tool signature; it waits until a plugin needs it.
- **`excluded_context_providers` still governs plugin context providers.** Choosing no instance
  (`home_assistant: null`) is the plugin-native way; both work.

### Known issue carried over

`HomeAssistantSource` is typed for the raw `homeassistant_api.Client` but has always been handed the
wrapper, which has no `get_states` or `get_entity_histories`. Its connection health check and
listener validation calls fail as a result. This milestone preserves the behaviour and marks the
call site.

## Milestone 2: Reolink cameras

- The camera tools, the camera backend protocol, its fake and the Reolink backend live in
  `plugins/reolink/`. The plugin id is `reolink` because everything configurable about it is
  Reolink's; the tools are written against the backend protocol, so another camera system would be
  another plugin supplying the same protocol.
- `camera_backend` is gone from the processing layer and `ToolExecutionContext`; the tools find the
  profile's `ReolinkInstance` through `plugins`.
- `processing_config.camera_config` is gone. An instance is a map of cameras, and `REOLINK_CAMERAS`
  (JSON) fills the `default` instance through the same environment-variable mapping as Home
  Assistant's settings, so camera passwords are `SecretStr` from the start.
- The tool names, tags and shipped tool policy are unchanged.

No config migration was needed: the deployment supplies its cameras only through `REOLINK_CAMERAS`,
which keeps working.

### Deliberate simplifications

- **Every profile gets the default instance, not just `camera_analyst`.** Previously only a profile
  with `camera_config` had a backend. Now reachability is decided where it is for every other tool,
  by `tools_policy`, and shipped defaults grant the camera tools to `camera_analyst` alone. A
  deployment wanting a profile to hold no cameras at all can say `reolink: null`.
- **The environment variable is stricter.** An unknown field in a `REOLINK_CAMERAS` entry is a
  startup error rather than ignored, as for any other config; a value that isn't a JSON object is
  logged and ignored, as before.

## Later milestones

1. **AI workers.** Needs task handlers and a startup hook on `PluginInstance`, and the shared
   workspace path moved out of the worker config first. Verified by removing the worker special
   cases from the tool registry, task worker and startup.
2. **Per-result grading, then Trino** (done) as a native plugin that grades each result from the
   tables a query reads. Verified by a query over household tables grading `recognized_machine`
   while a `lake.messages` query still grades `unknown_external`. Deliberate simplification: the
   plugin grades from the plan Trino reports just before running the query, so a view redefined in
   between is graded by its old definition.
3. Google data and UCP if still worthwhile by then.
