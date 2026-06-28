# Tools-config inheritance & on-demand tool loading

## Problem

Processing-profile configuration for *which tools load eagerly vs. on demand* was
tangled and produced silent, hard-to-debug regressions.

Two concrete symptoms (observed in production via the engineer profile):

- **`email_intake`** set only `tools_config.confirmation_timeout_seconds` (a 24h
  window). Because the loader replaced `tools_config` **wholesale**, this silently
  wiped the inherited `on_demand_mcp_server_ids` / `on_demand_local_tools`, so every
  on-demand MCP server (including ones the profile's policy *denies*, e.g. Vikunja)
  loaded eagerly on every email turn.
- **`complex_tasks`** restated `on_demand_mcp_server_ids: ["homeassistant"]`. The
  moment an operator added a new MCP server in `config.yaml` (vikunja, trino,
  transportnsw, …), the restated list went stale and those servers loaded eagerly —
  ~4.7k tokens of unused tool descriptions per turn.

### Root cause

In `resolve_service_profile` (`config_loader.py`), nested config objects were
handled inconsistently:

| Section | Behaviour |
| --- | --- |
| `processing_config.prompts` | deep-merge |
| `chat_id_to_name_map` | deep-merge |
| `tools_policy` | replace (intentional security invariant) |
| **`tools_config`** | **replace (bug)** |

`tools_config` is a multi-knob bag (`on_demand_local_tools`,
`on_demand_mcp_server_ids`, `confirmation_timeout_seconds`,
`mcp_initialization_timeout_seconds`, delegation tuning). Replacing it wholesale
means a profile that overrides *one* knob silently discards the inherited values of
*all the others*. Unlike `tools_policy` — where wholesale replacement is a
deliberate, documented security choice so a profile cannot accidentally inherit
allow-rules — there is no reason for `tools_config` to behave this way.

A second, deeper smell: "is this server expensive enough to load lazily?" is a
property of the *server*, yet it was only expressible as a per-profile enumerated
list. Adding a server meant remembering to add its id to every profile's list.

## Fix

### 1. Deep-merge `tools_config`

`tools_config` now deep-merges onto `default_profile_settings.tools_config`, exactly
like `prompts`. `default_profile_settings.tools_config` becomes the single source of
truth that every profile inherits; a profile only overrides the specific keys it
sets. Setting `confirmation_timeout_seconds` no longer disturbs the on-demand lists.

Consequence for list-valued keys: deep-merge replaces lists wholesale (only dict
values recurse). So a profile that wants a *different* on-demand list still states
the full list it wants; a profile that wants the *inherited* list simply omits the
key. The redundant `tools_config` block on `complex_tasks` is removed so it inherits
the canonical lists.

### 2. Declare MCP-server on-demand-ness at the server

`MCPServerConfig` gains an `on_demand: bool = False` flag. A server flagged
`on_demand: true` is loaded lazily in **every** profile, with no per-profile list
edits required. The effective on-demand MCP-server set for a profile is the union of:

- servers flagged `on_demand: true` in `mcp_config`, and
- the profile's `tools_config.on_demand_mcp_server_ids` (inherited from
  `default_profile_settings` unless the profile overrides it).

Union semantics keep the model predictable and backwards-compatible: existing
operator configs that list server ids keep working, while new servers can opt into
lazy loading where they are defined. (Local tools remain enumerated in
`on_demand_local_tools`, now correctly inherited via deep-merge.)

## Why not just re-paste the lists into every profile

That was the tactical option (and what the diagnostic report first proposed). It
fixes today's symptom but leaves both root causes in place: the next profile that
touches one `tools_config` knob, and the next MCP server added to `config.yaml`,
reintroduce the bug. The deep-merge + server-flag approach removes the footguns
instead of duplicating state that must be kept in sync.

## Compatibility

- `tools_policy` wholesale replacement is unchanged (security invariant preserved).
- Operator `config.yaml` that sets
  `default_profile_settings.tools_config.on_demand_mcp_server_ids` continues to work
  and now correctly propagates to all profiles.
- Shipped MCP servers (`time`, `brave`, `scrape`, `google-maps`) keep their current
  eager behaviour (`on_demand` defaults to `false`).
