# Integrations as Plugins

## Status

Milestone 1 implemented: the plugin seam, with Home Assistant as its first plugin. Later milestones
are proposals.

## Problem

Each bespoke integration was spread across the core. Before this change Home Assistant alone reached
into about twenty files outside its own tool module:

- **Config:** four `home_assistant_*` fields on every profile's `processing_config`, plus an
  `event_system.sources.home_assistant` switch.
- **Construction:** `assistant.py` built and cached a client per profile, a context provider per
  profile and one event source per unique client.
- **Plumbing:** a `home_assistant_client` parameter threaded from `ProcessingService` through the
  LLM loop and tool execution into `ToolExecutionContext`, and through the task worker, the tools
  API and the voice API. Reolink cameras do the same with `camera_backend`.
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

Unchanged in this milestone: plugin tools declare the same tags as before. The intended next step
for per-result grading is a typed return value on plugin tools, capped by the tool's declared floor,
with the host recording it; today native tools do it by reaching into the execution context.

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

## Later milestones

1. **Reolink cameras.** Verified by removing `camera_config` from `processing_config` and
   `camera_backend` from the processing layer and `ToolExecutionContext`.
2. **AI workers.** Needs task handlers and a startup hook on `PluginInstance`, and the shared
   workspace path moved out of the worker config first. Verified by removing the worker special
   cases from the tool registry, task worker and startup.
3. **Per-result grading, then Trino** as a native plugin that grades each result from the tables a
   query reads. Verified by a turn reading only Home Assistant tables carrying no `unknown_external`
   source, while a `lake.messages` query still does.
4. Google data and UCP if still worthwhile by then.
