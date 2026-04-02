# Design: Skill-Based Tool Unlock

## Problem

The Family Assistant has 50+ tools registered across many categories. Sending all tool definitions
to the LLM on every turn has several downsides:

1. **Token waste**: Most conversations only use a handful of tools. Sending all definitions consumes
   significant context window space.
2. **Decision fatigue**: The LLM has to scan through dozens of tool definitions to pick the right
   one, increasing the chance of picking wrong or hallucinating parameters.
3. **No forced instruction reading**: Complex tools (automations, scripts, camera analysis) have
   nuanced usage patterns. Currently the LLM sees the JSON schema but may skip the detailed
   instructions in the system prompt.
4. **Flat namespace**: All tools appear equally prominent. There's no progressive disclosure — no
   way to say "these tools exist but you need to learn about them before using them."

## Concept: Skills as Tool Bundles

A **skill** is a named bundle that contains:

- **Brief description**: A one-line summary visible before unlock (so the LLM knows it exists)
- **Instructions**: Detailed usage guidance revealed on unlock (the "box" you open)
- **Tool names**: The tools that become available after unlocking the skill

### Example

```yaml
skills:
  camera_investigation:
    description: "Tools for searching and analyzing security camera footage"
    instructions: |
      When investigating camera footage:
      1. Start with search_camera_events to find relevant events by type/time
      2. Use get_camera_frame to examine specific frames
      3. Use scan_camera_frames for systematic visual analysis
      4. Always report timestamps in the user's timezone
      ...
    tools:
      - list_cameras
      - search_camera_events
      - get_camera_frame
      - get_camera_frames_batch
      - get_camera_recordings
      - get_live_camera_snapshot
      - scan_camera_frames
```

### Flow

1. **Conversation start**: The LLM receives a compact list of available skills (name + brief
   description) in the system prompt, plus any "always-on" tools that aren't behind skills.
2. **LLM decides it needs a skill**: It calls `unlock_skill(skill_id="camera_investigation")`.
3. **`unlock_skill` returns**: The skill's detailed instructions as the tool result text.
4. **Tools become available**: On the *next iteration* of the tool-call loop, the newly unlocked
   tools appear in the tool definitions sent to the LLM.
5. **LLM uses the tools**: With full instructions fresh in context, the LLM can now use the camera
   tools effectively.

## Design

### Configuration

Skills are defined in `defaults.yaml` (or `config.yaml` override), within profile settings:

```yaml
default_profile_settings:
  skills:
    camera_investigation:
      description: "Search and analyze security camera footage"
      instructions: |
        When investigating camera footage:
        1. Start with search_camera_events to find relevant events
        2. Use get_camera_frame to examine specific frames
        ...
      tools:
        - list_cameras
        - search_camera_events
        - get_camera_frame
        - get_camera_frames_batch
        - get_camera_recordings
        - get_live_camera_snapshot
        - scan_camera_frames

    automation_management:
      description: "Create, edit, and manage automated tasks and schedules"
      instructions: |
        Automations support two types: schedule-based and event-based.
        ...
      tools:
        - create_automation
        - update_automation
        - delete_automation
        - list_automations
        - get_automation
        - enable_automation
        - disable_automation
        - get_automation_stats

    data_visualization:
      description: "Create charts and visualize data using Vega-Lite"
      instructions: |
        Use create_vega_chart to render Vega-Lite specifications as images.
        ...
      tools:
        - create_vega_chart
        - jq_query

    image_generation:
      description: "Generate and transform images using AI"
      instructions: |
        ...
      tools:
        - generate_image
        - transform_image
```

Tools NOT listed in any skill remain "always-on" (e.g., notes, calendar, search_documents,
delegate_to_service — the core everyday tools).

### Config Model

```python
class SkillConfig(BaseModel):
    """A skill that bundles tools behind a discoverable unlock mechanism."""
    description: str  # Brief description shown before unlock
    instructions: str  # Detailed instructions revealed on unlock
    tools: list[str]  # Tool names that become available after unlock
```

Added to profile settings:

```python
class DefaultProfileSettings(BaseModel):
    ...
    skills: dict[str, SkillConfig] = Field(default_factory=dict)
```

And per-profile override:

```python
class ServiceProfile(BaseModel):
    ...
    skills: dict[str, SkillConfig] | None = None  # None = inherit from defaults
```

### The `unlock_skill` Tool

A new meta-tool that:

1. Validates the skill ID exists
2. Marks the skill as unlocked in conversation state
3. Returns the skill's instructions as the tool result

```python
SKILL_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "unlock_skill",
            "description": "Unlock a skill to access its tools and instructions. "
                "Available skills are listed in the system prompt. "
                "Call this before using any tools from a skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The skill ID to unlock",
                    },
                },
                "required": ["skill_id"],
            },
        },
    },
]
```

The implementation stores unlocked skill IDs in the `ToolExecutionContext` (or a new `SkillState`
object on the tools provider).

### System Prompt Integration

The system prompt includes a section listing available skills:

```
## Available Skills

You have access to core tools (notes, calendar, tasks, documents, etc.) directly.
Additional tool sets are available as skills. To use them, call `unlock_skill` first.

| Skill | Description |
|-------|-------------|
| camera_investigation | Search and analyze security camera footage |
| automation_management | Create, edit, and manage automated tasks and schedules |
| data_visualization | Create charts and visualize data using Vega-Lite |
| image_generation | Generate and transform images using AI |

Call `unlock_skill(skill_id="<skill>")` to activate a skill's tools and read its instructions.
```

This is generated dynamically from the skills config in the context preparation step.

### Dynamic Tool Availability (Key Architectural Change)

Currently, tool definitions are fetched **once** at the start of a conversation turn
(`llm_loop.py:179-186`), before the iteration loop. To support mid-turn tool unlocking, we need
tools to be re-fetched after each tool execution.

**Option A: Re-fetch tools every iteration** (simple, slight overhead)

Move `get_tool_definitions_for_advertisement()` inside the `while` loop. The tools provider already
caches results, so we just need to invalidate the cache when a skill is unlocked.

**Option B: Conditional re-fetch** (optimized)

Add a `tools_changed` flag to the tools provider. After executing tools, check the flag. If set,
re-fetch tool definitions. `unlock_skill` sets this flag.

**Recommendation**: Option B — minimal overhead, clean signal.

### SkillAwareToolsProvider

A new provider wrapper (following the existing decorator pattern) that:

1. **Wraps** the existing `PolicyEnforcingToolsProvider`
2. **Holds skill state**: which skills are unlocked for this conversation turn
3. **Filters tool definitions**: Only returns tools from unlocked skills (plus always-on tools)
4. **Filters tool execution**: Only allows execution of tools from unlocked skills
5. **Provides `unlock_skill`**: Adds the `unlock_skill` tool definition and handles its execution
6. **Signals tool changes**: Sets a flag when a skill is unlocked so llm_loop knows to re-fetch

```
CompositeToolsProvider
├── SkillAwareToolsProvider (NEW - skill filtering layer)
│   └── PolicyEnforcingToolsProvider (existing security layer)
│       └── LocalToolsProvider (existing base)
└── MCPToolsProvider (unchanged)
```

The `SkillAwareToolsProvider`:

```python
class SkillAwareToolsProvider(ToolsProvider):
    def __init__(
        self,
        wrapped_provider: ToolsProvider,
        skills_config: dict[str, SkillConfig],
    ):
        self._wrapped = wrapped_provider
        self._skills_config = skills_config
        self._unlocked_skills: set[str] = set()
        self._tools_changed = False

        # Compute which tool names are behind skills
        self._skill_tools: set[str] = set()
        for skill in skills_config.values():
            self._skill_tools.update(skill.tools)

    @property
    def tools_changed(self) -> bool:
        """Check and reset the tools_changed flag."""
        changed = self._tools_changed
        self._tools_changed = False
        return changed

    async def get_tool_definitions(self, **kwargs) -> list[ToolDefinition]:
        all_defs = await self._wrapped.get_tool_definitions(**kwargs)

        # Filter: keep tools that are NOT behind any skill, OR whose skill is unlocked
        unlocked_tool_names = set()
        for skill_id in self._unlocked_skills:
            unlocked_tool_names.update(self._skills_config[skill_id].tools)

        filtered = [
            d for d in all_defs
            if d["function"]["name"] not in self._skill_tools
            or d["function"]["name"] in unlocked_tool_names
        ]

        # Add the unlock_skill tool itself
        filtered.extend(SKILL_TOOLS_DEFINITION)

        return filtered

    async def execute_tool(self, tool_name, args, context):
        if tool_name == "unlock_skill":
            return self._handle_unlock(args)
        return await self._wrapped.execute_tool(tool_name, args, context)

    def _handle_unlock(self, args: dict) -> str:
        skill_id = args["skill_id"]
        if skill_id not in self._skills_config:
            available = ", ".join(self._skills_config.keys())
            return f"Unknown skill '{skill_id}'. Available skills: {available}"

        self._unlocked_skills.add(skill_id)
        self._tools_changed = True

        skill = self._skills_config[skill_id]
        tool_list = ", ".join(skill.tools)
        return (
            f"Skill '{skill_id}' unlocked. The following tools are now available: {tool_list}\n\n"
            f"## Instructions\n\n{skill.instructions}"
        )
```

### LLM Loop Change

In `llm_loop.py`, after tool execution, check if tools changed:

```python
# After processing tool results
# Note: CompositeToolsProvider must propagate tools_changed from wrapped providers
if getattr(self.tool_executor.tools_provider, 'tools_changed', False):
    tools_for_llm = await get_tool_definitions_for_advertisement(
        self.tool_executor.tools_provider,
        can_confirm=request_confirmation_callback is not None,
    )
```

### Interaction with Existing Systems

- **Policy engine**: Skills sit *above* the policy engine in the provider chain. A tool must pass
  both the skill gate AND the policy gate to be available. This means admin can still deny specific
  tools within a skill via policy rules.
- **Processing profiles**: Each profile can define its own skills (or inherit defaults). A profile
  with no skills config gets all tools directly (backwards compatible).
- **Delegation**: When delegating to another profile, that profile has its own skill state (starts
  fresh). The delegated profile's skills are independent.
- **Conversation persistence**: Skill unlock state is per-turn (resets each conversation turn). This
  is intentional — the LLM should re-evaluate which skills it needs for each new user message. If we
  want persistence, we could store unlocked skills in conversation metadata, but per-turn keeps
  things simple and ensures instructions are fresh in context.

### Token Savings Estimate

A rough estimate based on current tool counts:

| Category           | Tools   | Est. Tokens (definitions) |
| ------------------ | ------- | ------------------------- |
| Camera             | 7       | ~2,500                    |
| Automations        | 8       | ~3,000                    |
| Data viz           | 2       | ~1,000                    |
| Image gen          | 2       | ~800                      |
| Computer use       | 13      | ~5,000                    |
| Scripts            | 2       | ~1,500                    |
| Engineering        | 6       | ~2,500                    |
| **Total saveable** | **~40** | **~16,000**               |

For a typical conversation that uses 0-2 of these categories, we'd save ~12,000-16,000 tokens per
turn on tool definitions alone.

## Milestones

### Milestone 1: Core Infrastructure

- Add `SkillConfig` to config models
- Create `SkillAwareToolsProvider`
- Implement `unlock_skill` tool
- Add conditional tool re-fetch to llm_loop
- Unit tests for provider filtering and unlock logic

### Milestone 2: Configuration and Integration

- Define initial skill groupings in `defaults.yaml`
- Integrate `SkillAwareToolsProvider` into the provider chain in `assistant.py`
- Add skill listing to system prompt via context preparation
- Update profile tool lists (remove skill-gated tools from always-on)

### Milestone 3: Polish and Testing

- Functional tests: conversation that unlocks a skill and uses its tools
- Edge cases: unlocking non-existent skill, using tool without unlocking, multiple skills
- Update user documentation and CLAUDE.md
- Measure actual token savings

## Alternative Designs

### Alternative A: Tag-Based Skills (No Explicit Tool Lists)

Instead of listing tool names per skill in YAML, skills match tools by their existing `ToolTag`
metadata. Tools already have consistent categorical tags (`CAMERA`, `AUTOMATION`, `BROWSER`, etc.),
so skills can reference those.

**Configuration:**

```yaml
skills:
  camera_investigation:
    description: "Search and analyze security camera footage"
    instructions: |
      When investigating camera footage: ...
    match:
      tags_any: [CAMERA]

  automation_management:
    description: "Create, edit, and manage automated tasks"
    instructions: |
      Automations support two types: ...
    match:
      tags_any: [AUTOMATION]

  data_analysis:
    description: "Charts, data queries, and visualization"
    instructions: |
      ...
    match:
      tags_any: [DATA]
```

**Pros:**

- No duplication: tools don't need to be listed in both `enable_local_tools` and `skills.tools`.
  Adding a new camera tool with the `CAMERA` tag automatically puts it behind the camera skill.
- Reuses existing `ToolMatcher` infrastructure (already supports `tags_any`, `tags_all`, `names`,
  `mcp_server_ids` with glob patterns). No new matching logic needed.
- Naturally stays in sync as tools are added/removed — if it's tagged `CAMERA`, it's in the camera
  skill.
- Can combine matchers for cross-cutting skills (e.g., `tags_any: [MEDIA]` captures both camera and
  image tools).

**Cons:**

- Less explicit: you need to know the tagging conventions to understand what's in a skill. With
  explicit tool lists, the config is self-documenting.
- Tag granularity may not match desired skill boundaries. E.g., `search_camera_events` might be
  tagged both `CAMERA` and `HOME_AUTOMATION` — which skill should it be in? (Answer: both, if using
  `tags_any`.)
- Harder to put a single tool into a skill that doesn't match its existing tags without adding a new
  tag.

**Hybrid option:** Support both `match` and `tools` in skill config. Use `match` as the primary
mechanism, with `tools` as an explicit override/addition:

```yaml
skills:
  camera_investigation:
    description: "Search and analyze security camera footage"
    instructions: ...
    match:
      tags_any: [CAMERA]
    additional_tools:  # Explicit additions beyond tag match
      - get_camera_snapshot  # HA tool also useful here
```

### Alternative B: Tool Metadata Declares Skill Membership

Instead of configuring skills separately, each tool's metadata declares which skill it belongs to.
The skill definitions (description + instructions) live in config, but the tool→skill mapping lives
in code alongside the tool registration.

**Code-side (in `__init__.py`):**

```python
LOCAL_TOOL_METADATA_BY_NAME: dict[str, LocalToolMetadata] = {
    "list_cameras": _metadata(ToolTag.CAMERA, skill="camera_investigation"),
    "search_camera_events": _metadata(ToolTag.CAMERA, skill="camera_investigation"),
    "create_automation": _metadata(ToolTag.AUTOMATION, skill="automation_management"),
    "add_or_update_note": _metadata(ToolTag.NOTES),  # No skill = always-on
    ...
}
```

**Config-side (in `defaults.yaml`):**

```yaml
skills:
  camera_investigation:
    description: "Search and analyze security camera footage"
    instructions: |
      When investigating camera footage: ...
  automation_management:
    description: "Create, edit, and manage automated tasks"
    instructions: |
      Automations support two types: ...
```

**Pros:**

- Tool→skill mapping lives next to other tool metadata (tags, implementations). Single source of
  truth for "what is this tool?"
- Adding a new tool to a skill is done in the same place you register the tool — can't forget.
- Config stays clean: only descriptions and instructions, no tool lists to maintain.
- Type-safe: `skill` field on `LocalToolMetadata` can be validated against known skill IDs.

**Cons:**

- Splits skill definition across two files (code for membership, config for descriptions). You need
  to look in both places to understand a skill fully.
- Less flexible for per-profile skill customization. If profile A wants `search_camera_events` in
  the camera skill but profile B wants it always-on, the code-side declaration doesn't support that.
- Tighter coupling between code and config — changing skill boundaries requires code changes, not
  just config changes.

### Alternative C: Convention-Based (Tool Groups = Skills)

Tools are already grouped into definition lists: `CAMERA_TOOLS_DEFINITION`,
`AUTOMATIONS_TOOLS_DEFINITION`, etc. These groups could *automatically become skills* with minimal
config.

Each tool module exports a skill descriptor alongside its definitions:

```python
# In camera.py
CAMERA_SKILL = SkillDescriptor(
    id="camera",
    description="Search and analyze security camera footage",
    instructions="""
    When investigating camera footage:
    1. Start with search_camera_events...
    """,
)

CAMERA_TOOLS_DEFINITION: list[ToolDefinition] = [...]
```

The `__init__.py` aggregation would then collect skills alongside tools:

```python
_LOCAL_SKILLS = [
    camera.CAMERA_SKILL,
    automations.AUTOMATIONS_SKILL,
    # Modules without a SKILL export have always-on tools
]
```

Config only needs to say which skills exist and whether they're gated:

```yaml
skill_gating:
  camera: true          # Behind unlock
  automation: true      # Behind unlock
  notes: false          # Always-on (or just omit)
```

**Pros:**

- Instructions live next to tool implementations — the people writing tools write the skill
  instructions. No context switching to YAML.
- Tool→skill mapping is implicit from the module structure. `CAMERA_TOOLS_DEFINITION` tools are in
  the `camera` skill. Zero configuration for this mapping.
- Minimal config surface: just "gate this skill or not."
- Easy to understand: one module = one skill (or no skill).

**Cons:**

- One module = one skill is a rigid constraint. What if you want a skill that spans multiple modules
  (e.g., a "media" skill combining camera + image generation + video generation)?
- Instructions as Python strings are harder to edit and review than YAML. Multiline strings in
  Python are awkward for long-form prose.
- Loses the ability to have profile-specific skill customization without code changes.
- Adds a new export convention that all tool modules need to follow.

### Alternative D: Skills as Policy Rules (No New Provider)

Instead of a new `SkillAwareToolsProvider`, implement skills entirely through the existing
`PolicyEngine`. Unlocking a skill dynamically adds ALLOW rules for its tools.

**How it works:**

1. Skill-gated tools start with a `DENY` policy rule at a low priority.
2. `unlock_skill` adds an `ALLOW` rule at higher priority for the skill's tools.
3. The existing `PolicyEnforcingToolsProvider` handles the filtering — no new provider needed.

```python
# Initial state: camera tools denied
PolicyRule(match=ToolMatcher(tags_any=[ToolTag.CAMERA]), decision="deny", priority=50)

# After unlock_skill("camera_investigation"):
PolicyRule(match=ToolMatcher(tags_any=[ToolTag.CAMERA]), decision="allow", priority=150)
```

**Pros:**

- Reuses existing infrastructure entirely. No new provider class.
- Policy engine already handles priority-based rule resolution, caching, advertisement filtering.
- Consistent with existing security model — skills are just another policy layer.
- Easy to reason about: "why can't I use this tool?" → check policy rules.

**Cons:**

- Mixes concerns: policy engine is about security (who can do what), skills are about UX
  (progressive disclosure). Overloading policy with UX concerns may make both harder to reason
  about.
- Dynamic rule mutation is a new pattern for the policy engine, which currently uses static rules.
  Need to handle cache invalidation carefully.
- Still need *something* to hold skill descriptions, instructions, and the unlock_skill tool logic.
  The policy engine doesn't have a concept of "instructions returned on unlock."
- Debugging: policy evaluation logs would show skill-related rules mixed with security rules.

### Comparison Matrix

| Aspect                    | Original (YAML lists) | Alt A (Tag-based)    | Alt B (Metadata)  | Alt C (Convention) | Alt D (Policy)      |
| ------------------------- | --------------------- | -------------------- | ----------------- | ------------------ | ------------------- |
| Config surface            | Verbose               | Compact              | Split code/config | Minimal config     | Moderate            |
| Tool→skill sync           | Manual                | Automatic (via tags) | Manual (in code)  | Automatic (module) | Manual or tag-based |
| Per-profile customization | Easy (YAML)           | Easy (YAML)          | Hard (code)       | Limited            | Easy (policy rules) |
| New provider needed       | Yes                   | Yes                  | Yes               | Yes                | No                  |
| Reuses existing infra     | Partially             | ToolMatcher reuse    | Partially         | Partially          | Fully               |
| Instructions location     | YAML                  | YAML                 | YAML              | Python code        | YAML + custom logic |
| Cross-module skills       | Easy                  | Easy (tag matching)  | Easy (metadata)   | Hard               | Easy                |
| Self-documenting config   | Yes (explicit lists)  | Moderate             | No (need both)    | No (need code)     | No (mixed concerns) |

### Recommendation

**Alt A (Tag-based) with hybrid override** is the strongest option:

1. It eliminates the biggest pain point of the original design (manual tool lists that duplicate
   `enable_local_tools` and go stale).
2. It reuses the existing `ToolMatcher` — a battle-tested matching engine that already handles tags,
   name patterns, and MCP server IDs.
3. The hybrid option (`match` + `additional_tools`) handles edge cases where tag granularity doesn't
   match skill boundaries.
4. Instructions and descriptions stay in YAML where they're easy to review and edit.
5. Per-profile customization remains straightforward.

The one new piece of infrastructure is the `SkillAwareToolsProvider`, which is needed regardless of
config approach (except Alt D, which trades provider complexity for policy engine complexity).

## Open Questions

1. **Should skills persist across turns?** Current design resets per-turn. Could store in
   conversation metadata for persistence. Per-turn is simpler and ensures fresh instructions.

2. **Should the LLM be able to "peek" at tool schemas without unlocking?** Could add a
   `describe_skill` tool that returns tool names and brief descriptions without unlocking. Adds
   complexity; probably not needed since the skill description should be enough.

3. **Should skills be hierarchical?** E.g., a "home" skill that contains "camera" and
   "home_assistant" sub-skills. Adds complexity; start flat and revisit if needed.

4. **Should MCP tools also be gatable behind skills?** The current design only gates local tools.
   MCP tools could be skill-gated too by matching on server ID. Worth considering for future.

5. **Auto-unlock heuristic?** Could the system auto-unlock skills based on user intent keywords
   (e.g., "show me camera footage" auto-unlocks camera_investigation)? This defeats the purpose of
   forcing instruction reading, but could be a UX improvement. Could be a future enhancement where
   the *system prompt* instructs the LLM to unlock proactively based on context.
