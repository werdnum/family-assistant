# Built-in Skills + include_system_docs Convergence

## Context

There are currently **three parallel systems** for delivering documentation to the LLM:

### 1. `include_system_docs` (config_loader.py)

Loads `docs/user/*.md` files and appends their **full content** to the system prompt at config
resolution time. Static, per-profile.

Used by:

- `data_visualization` profile → `data_visualization.md`, `vega_lite_reference.md`

### 2. `get_user_documentation_content` tool (tools/documents.py)

An LLM tool that scans `docs/user/` at startup and lists available filenames in its tool description
(`{available_doc_files}`). The LLM calls it to read any doc file on demand.

Available to any profile that enables the tool. The LLM sees the file list but not the contents
until it calls the tool.

### 3. Skills system (skills/)

File-based skills loaded from directories, shown as **catalog entries** (name + description only) in
system prompt. Full content loadable on demand via `get_note`.

Currently has `user_dir` config but no `builtin_dir` and no actual built-in skill files (Milestone 8
from the implementation plan is "NOT STARTED").

### The Overlap

All three do the same conceptual thing: make procedural documentation available to the LLM. They
differ in:

| Dimension           | `include_system_docs` | `get_user_documentation_content` | Skills               |
| ------------------- | --------------------- | -------------------------------- | -------------------- |
| When loaded         | Build-time (always)   | On-demand (tool call)            | On-demand (get_note) |
| What LLM sees first | Full content          | Filename list                    | Name + description   |
| Access control      | Per-profile (config)  | Per-profile (tool enable)        | Visibility labels    |
| Where files live    | `docs/user/`          | `docs/user/`                     | Skills directories   |
| Discovery mechanism | N/A (injected)        | Tool description                 | Skill catalog        |

## Proposal

### Phase A: Ship Built-in Skills (immediate)

Convert the four "orphaned" user docs that are currently only discoverable via
`get_user_documentation_content` into built-in skills:

1. `scheduling.md` → `src/family_assistant/skills/builtin/scheduling.md`
2. `browser_automation.md` → `src/family_assistant/skills/builtin/browser-automation.md`
3. `camera_integration.md` → `src/family_assistant/skills/builtin/camera-integration.md`
4. `image_tools.md` → `src/family_assistant/skills/builtin/image-tools.md`

Each gets frontmatter with `name` and `description`. The original files in `docs/user/` stay as-is
(they serve double duty as user-facing documentation).

Implementation:

1. Add `builtin_dir` field to `SkillsConfig` (defaulting to the bundled directory)
2. Wire builtin loading in `Assistant.setup_dependencies()` (builtin first, then user — user skills
   override builtin skills with the same name)
3. Verify built-in skills appear in the catalog

### Phase B: `auto_load_skills` replaces `include_system_docs` (convergence)

Add an `auto_load_skills` field to profile processing config:

```yaml
profiles:
  - id: "data_visualization"
    processing_config:
      auto_load_skills:
        - "Data Visualization"
        - "Vega-Lite Reference"
```

Semantics: for these skills, inject their **full content** into the system prompt (same as what
`include_system_docs` does today), rather than just showing the catalog entry. This is essentially
static preflight routing — "always preload these skills for this profile."

Implementation:

1. Convert `data_visualization.md`, `vega_lite_reference.md`, `scripting.md` to built-in skills
2. Add `auto_load_skills` to `ProcessingConfig`
3. In `NotesContextProvider`, when rendering the skill catalog: if a skill is in `auto_load_skills`,
   include its full content in a "Pre-loaded Skills" section instead of just the catalog line
4. Migrate existing profiles from `include_system_docs` to `auto_load_skills`
5. Deprecate and remove `include_system_docs`

### Phase C: Deprecate `get_user_documentation_content` (cleanup)

Once all procedural docs are available as skills via `get_note`, the
`get_user_documentation_content` tool becomes redundant:

- Skills provide better discovery (name + description vs bare filename)
- `get_note` already handles the on-demand loading
- Visibility labels provide finer-grained access control

Steps:

1. Ensure all docs currently in `docs/user/` that the LLM needs are either built-in skills or
   referenced in the system prompt directly
2. Remove `get_user_documentation_content` tool
3. Remove `_scan_user_docs()` and `{available_doc_files}` wiring

### Phase D: Preflight Routing (future, already planned)

The `auto_load_skills` approach is static — the profile config decides what gets preloaded.
Preflight routing (Phase 4 from the skills design doc) makes this dynamic: a lightweight model
selects relevant skills per-request.

`auto_load_skills` and preflight routing compose naturally:

- `auto_load_skills` = "always load these for this profile"
- Preflight routing = "additionally load these based on this specific request"

## Migration Path Summary

```
Current state:
  include_system_docs → full content in system prompt (3 docs, 2 profiles)
  get_user_documentation_content → filename list in tool desc, content on demand
  Skills → catalog + get_note (infrastructure done, no built-in files)

Phase A (this task):
  + 4 built-in skills in catalog
  + builtin_dir config + wiring
  (no changes to existing systems)

Phase B:
  + auto_load_skills in profile config
  + 3 more built-in skills (data_viz, vega, scripting)
  - include_system_docs deprecated

Phase C:
  - get_user_documentation_content removed
  (all doc access via skills system)

Phase D:
  + Preflight routing for dynamic skill selection
```
