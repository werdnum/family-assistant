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

## AI workers

Implemented. The worker tools, config, backends, cleanup task and startup reconciliation live in
`plugins/ai_workers/`; the tool registry, task worker and startup no longer mention workers. Tool
names, tags and policy behaviour are unchanged.

- **The shared workspace moved out first.** Its path is the top-level `shared_workspace_path`,
  because the workspace file tools use it as much as workers do.
- **Plugins contribute task handlers.** They are declared on the plugin beside its tools and, like
  tools, registered with every task worker whether or not the plugin is configured, so a recurring
  task seeded while it was configured still has a handler once it is removed. A handler finds an
  instance it needs the way a tool does. A task type claimed twice is a startup error.
- **Instances get a startup hook.** It runs once, in the background, after the task worker pool is
  up, with the database and the shared workspace path; a failure is logged and does not stop other
  instances' hooks. `close()` remains the shutdown hook. Home Assistant needs neither and is
  unchanged.
- **Plugins shape the tools they serve.** The catalogue still holds every plugin tool, so policy can
  name them, but the tools a deployment offers come from each plugin given its configured instances.
  Workers offer nothing without an instance, as the old `enabled: false` did, and fill
  `spawn_worker`'s agent choices from config.
- **A plugin tool's confirmation travels on its registration**: the prompt renderer and the check
  that refuses arguments no prompt could show faithfully.
- `ai_worker_config` became `plugins.ai_workers.<instance>`, and an instance is what enables the
  plugin. The `AI_WORKER_*` environment variables went with it; no deployment set them.

### Deliberate simplifications

- **One sandbox.** A worker task does not record which instance ran it, so reconciling a second
  backend would fail the first one's live tasks; configuring two is a config error.
- **Worker lifecycle webhooks stay in the generic webhook router.** Workers report on the generic
  event endpoint under its existing authentication, and handling that from the plugin needs a
  webhook hook nothing else wants yet. The `worker_tasks` table and repository stay in core storage,
  with the other tables and their migrations.
- **The cleanup sweeps the workspace only when its payload names it**, which the startup hook does.
  The daily task a deployment seeded before removing workers keeps collecting task rows and
  listeners but leaves the shared workspace alone.

### Temporary config migration

Deployed config still carries a disabled `ai_worker_config`. Until it is rewritten, loading moves
its workspace path to `shared_workspace_path`, turns an enabled block into the `default` instance
and drops a disabled one, with a deprecation warning. The migration is removed once deployed config
has moved.

## Later milestones

1. **Reolink cameras.** Verified by removing `camera_config` from `processing_config` and
   `camera_backend` from the processing layer and `ToolExecutionContext`.
2. **AI workers.** Implemented; see [AI workers](#ai-workers).
3. **Per-result grading, then Trino** as a native plugin that grades each result from the tables a
   query reads. Verified by a turn reading only Home Assistant tables carrying no `unknown_external`
   source, while a `lake.messages` query still does.
4. Google data and UCP if still worthwhile by then.
