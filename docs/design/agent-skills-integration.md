# Agent Skills Integration Design

## 1. Introduction

This document explores integrating the [Agent Skills](https://agentskills.io/) open standard into
Family Assistant. Agent Skills is a portable, vendor-neutral format for giving AI agents specialized
capabilities and procedural knowledge. The standard was developed by Anthropic and released as an
open specification, now maintained by the Linux Foundation's Agentic AI Foundation (AAIF) with
adoption from Microsoft, OpenAI, Atlassian, Figma, Cursor, and GitHub.

### 1.1. What Are Agent Skills?

Agent Skills are **folders of instructions, scripts, and resources** that AI agents can load when
relevant to perform specialized tasks. The core insight is that agents are increasingly capable, but
often lack the **procedural knowledge** and **context-specific information** needed to perform real
work reliably.

A skill consists of:

- **SKILL.md** - Required file with YAML frontmatter (name, description) and markdown instructions
- **scripts/** - Optional executable code (Python, Bash, JavaScript)
- **references/** - Optional detailed documentation loaded on demand
- **assets/** - Optional static resources (templates, schemas, data files)

### 1.2. Key Design Principles

The Agent Skills specification emphasizes:

1. **Progressive Disclosure**: Load information in stages as it becomes relevant

   - Metadata (~100 tokens) loaded at startup for all skills
   - Instructions (\<5000 tokens) loaded when skill is activated
   - Resources loaded only when specifically needed

2. **Semantic Matching**: The agent itself decides when to activate skills based on task relevance,
   using pure LLM reasoning rather than algorithmic routing

3. **Portability**: Skills work across different agent platforms (Claude, Codex, Cursor, etc.)

4. **Modularity**: Skills are self-contained and can be composed

### 1.3. Motivation for Family Assistant

Family Assistant already has excellent abstractions that align well with Agent Skills concepts:

| Agent Skills Concept   | Family Assistant Equivalent            |
| ---------------------- | -------------------------------------- |
| SKILL.md instructions  | Notes with `include_in_prompt=True`    |
| Progressive disclosure | ContextProvider pattern                |
| Script execution       | Tools system (LocalToolsProvider, MCP) |
| Semantic activation    | Could use embeddings + lightweight LLM |
| Security boundaries    | Processing profiles with Rule of Two   |

The opportunity is to:

1. **Formalize** our existing patterns using the open standard format
2. **Enable portability** of skills between Family Assistant and other agents
3. **Optimize context usage** through intelligent skill selection
4. **Reduce costs** by using lightweight models for skill routing

## 2. Proposed Architecture

### 2.1. Hybrid Activation: Agent Self-Selection with Preflight Hints

The architecture supports **two complementary activation modes**:

1. **Agent Self-Activation**: The main model always sees skill metadata in the system prompt and can
   decide to load any skill's full instructions via a tool call
2. **Preflight Hints**: A lightweight model pre-selects likely-relevant skills, loading their full
   instructions before the main model starts, improving reliability and speed

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           User Request                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
┌───────────────────────────────────┐  ┌──────────────────────────────────────┐
│  PREFLIGHT (optional optimization)│  │  SYSTEM PROMPT (always present)      │
│                                   │  │                                      │
│  gemini-2.5-flash-lite analyzes   │  │  Skill catalog with metadata:        │
│  request + last N turns           │  │  - Skill names and descriptions      │
│                                   │  │  - "Use load_skill tool for details" │
│  Output: pre-load these skills    │  │                                      │
└───────────────────────────────────┘  └──────────────────────────────────────┘
                    │                               │
                    └───────────────┬───────────────┘
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  MAIN MODEL PROCESSING                                                      │
│                                                                             │
│  Context includes:                                                          │
│  - Skill catalog (always)                                                   │
│  - Pre-loaded skill instructions (from preflight, if enabled)              │
│  - load_skill tool (for on-demand activation)                              │
│                                                                             │
│  Model can:                                                                 │
│  - Use pre-loaded skills immediately                                        │
│  - Load additional skills via tool if needed                               │
│  - Ignore skills entirely if not relevant                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2. Why Preflight? Reliability and Speed

Preflight routing is **not primarily about cost savings** (context costs are acceptable). The main
benefits are:

1. **Reliability**: The main model starts with relevant instructions already loaded, reducing the
   chance it forgets to load a skill or loads the wrong one
2. **Speed**: Skill instructions are available from the first token, avoiding a tool-call round-trip
3. **Focus**: Pre-loading signals relevance, helping the model prioritize

The main model **always has the option** to load skills directly via tool call. Preflight is a hint
system, not a gatekeeper.

### 2.3. Structured Output for Skill Selection

The preflight model should return structured output to ensure reliable parsing:

```json
{
  "selected_skills": [
    {
      "skill_id": "home-automation",
      "confidence": 0.95,
      "reason": "User mentioned lights and temperature control"
    },
    {
      "skill_id": "calendar-management",
      "confidence": 0.72,
      "reason": "User mentioned scheduling"
    }
  ],
  "no_skill_needed": false
}
```

Using constrained decoding (Gemini's `response_schema` or similar) ensures the output is always
valid JSON matching the expected schema.

## 3. Integration with Family Assistant

### 3.1. Skill Storage: Notes as the Primary Store

Skills in Family Assistant are stored as **notes with YAML frontmatter**. This leverages existing
infrastructure and allows users to create skills through natural conversation.

**Why notes instead of files?**

| Aspect           | Notes                                 | Files                        |
| ---------------- | ------------------------------------- | ---------------------------- |
| Creation         | Via assistant conversation            | Manual filesystem access     |
| Editing          | Via assistant or web UI               | External editor              |
| Version control  | Database with timestamps              | Requires git                 |
| Profile affinity | Uses existing notes-v2 profile system | Would need separate config   |
| Portability      | Export to SKILL.md format             | Native format                |
| Vector search    | Already indexed                       | Would need separate indexing |

**Skill sources (in priority order):**

1. **User skill notes**: Notes with valid skill frontmatter (primary)
2. **Built-in skills**: `src/family_assistant/skills/` directory (for defaults shipped with app)

### 3.2. Identifying Skill Notes via Frontmatter

A note is a skill if its content starts with valid YAML frontmatter containing required skill
fields. **No schema changes needed** - we parse frontmatter on read.

```python
def is_skill_note(note_content: str) -> bool:
    """Check if a note is a skill by parsing its frontmatter."""
    try:
        frontmatter, _ = parse_frontmatter(note_content)
        # Required fields for a valid skill
        return "name" in frontmatter and "description" in frontmatter
    except Exception:
        return False
```

**Example skill note:**

```markdown
---
name: Meeting Notes Helper
description: Help format meeting notes with attendees, agenda, decisions, and action items.
proactive_for_profile_ids: ["default_assistant"]
---

# Meeting Notes Skill

When the user asks to take meeting notes or summarize a meeting...
```

### 3.3. Profile Affinity for Skills

Skills integrate with the **notes-v2 profile affinity system** (see `docs/design/notes-v2.md`). This
allows skills to be automatically loaded for specific processing profiles.

**Frontmatter fields for profile control:**

```yaml
---
name: Home Automation
description: Control smart home devices...
# Profile affinity (from notes-v2 design)
include_in_prompt_default: false # Not loaded by default
proactive_for_profile_ids: ["default_assistant", "automation_creation"] # Auto-load for these
exclude_from_prompt_profile_ids: ["untrusted_readonly"] # Never load for these
---
```

**Resolution order** (same as notes-v2):

1. If current profile in `exclude_from_prompt_profile_ids` → skill not available
2. Else if current profile in `proactive_for_profile_ids` → skill auto-loaded (preflight hint)
3. Else use `include_in_prompt_default` to determine if in catalog

### 3.4. Skill Data Models

```python
@dataclass
class SkillMetadata:
    """Parsed from note frontmatter."""
    id: str                          # Derived from note title (slugified)
    name: str                        # From frontmatter
    description: str                 # Used for skill selection
    # Profile affinity (from notes-v2)
    include_in_prompt_default: bool = True
    proactive_for_profile_ids: list[str] | None = None
    exclude_from_prompt_profile_ids: list[str] | None = None
    # Skill-specific
    allowed_tools: list[str] | None = None
    compatibility: str | None = None

@dataclass
class Skill:
    """Full skill with loaded content."""
    metadata: SkillMetadata
    instructions: str                 # Content after frontmatter
    note_id: int | None = None       # Database note ID (for notes)
    source_path: Path | None = None  # Filesystem path (for built-in)
```

### 3.5. SkillsContextProvider

A new `ContextProvider` that provides both the skill catalog (always) and pre-loaded skill
instructions (from preflight):

```python
class SkillsContextProvider(ContextProvider):
    """Provides skill catalog and pre-loaded instructions."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        skill_router: SkillRouter | None = None,  # Optional preflight
        profile_id: str = "default_assistant",
    ):
        self._registry = skill_registry
        self._router = skill_router
        self._profile_id = profile_id
        self._preloaded_skills: list[Skill] = []

    @property
    def name(self) -> str:
        return "skills"

    async def prepare_for_request(
        self,
        user_message: str,
        conversation_history: list[Message],
    ) -> None:
        """Optionally run preflight to pre-select skills."""
        if self._router:
            self._preloaded_skills = await self._router.select_skills(
                user_message=user_message,
                history=conversation_history[-4:],
                profile_id=self._profile_id,
            )

    async def get_context_fragments(self) -> list[str]:
        """Return skill catalog + pre-loaded instructions."""
        fragments = []

        # Always include skill catalog (metadata only)
        available = self._registry.get_available_skills(self._profile_id)
        if available:
            fragments.append("## Available Skills\n")
            fragments.append("Use `load_skill` tool to activate a skill's full instructions.\n")
            for skill in available:
                fragments.append(
                    f"- **{skill.metadata.name}**: {skill.metadata.description}\n"
                )

        # Include pre-loaded skill instructions
        if self._preloaded_skills:
            fragments.append("\n## Pre-loaded Skills\n")
            for skill in self._preloaded_skills:
                fragments.append(f"### {skill.metadata.name}\n{skill.instructions}\n")

        return fragments
```

### 3.6. Preflight Skill Router

```python
class SkillRouter:
    """Pre-selects skills using lightweight LLM for reliability/speed."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        router_llm: LLMInterface,  # e.g., gemini-2.5-flash-lite
    ):
        self._registry = skill_registry
        self._llm = router_llm

    async def select_skills(
        self,
        user_message: str,
        history: list[Message],
        profile_id: str,
        max_skills: int = 3,
    ) -> list[Skill]:
        """Pre-select skills that are likely relevant."""

        # Get skills available for this profile
        available = self._registry.get_available_skills(profile_id)
        if not available:
            return []

        # Build catalog from available skills only
        catalog = self._build_skill_catalog(available)

        # Create routing prompt
        prompt = self._build_routing_prompt(user_message, history, catalog)

        # Call lightweight model with structured output
        response = await self._llm.generate(
            messages=[UserMessage(content=prompt)],
            response_schema=SkillSelectionSchema,
        )

        # Return full skill objects for selected IDs
        selection = parse_skill_selection(response)
        selected_ids = {s.skill_id for s in selection.selected_skills[:max_skills]}
        return [s for s in available if s.metadata.id in selected_ids]

    def _build_skill_catalog(self, skills: list[Skill]) -> str:
        """Format skill metadata for routing prompt."""
        lines = ["Available skills:\n"]
        for skill in skills:
            lines.append(f"- {skill.metadata.id}: {skill.metadata.description}")
        return "\n".join(lines)

    def _build_routing_prompt(
        self,
        user_message: str,
        history: list[Message],
        catalog: str,
    ) -> str:
        return f"""Analyze the user's request and select relevant skills to pre-load.

{catalog}

Recent conversation:
{format_history(history)}

Current request: {user_message}

Select 0-3 skills that would help with this request. Return only skills that are clearly relevant.
The user can still load other skills manually, so only select high-confidence matches."""
```

### 3.7. Integration with Processing Profiles

Skills integrate with processing profiles via the notes-v2 profile affinity system. Configuration in
`config.yaml` controls preflight behavior:

```yaml
service_profiles:
  - id: "default_assistant"
    skills_config:
      preflight_model: "gemini-2.5-flash-lite"
      preflight_enabled: true
      max_preloaded_skills: 3

  - id: "untrusted_readonly"
    skills_config:
      preflight_enabled: false  # No external model calls
      # Skills with exclude_from_prompt_profile_ids: ["untrusted_readonly"]
      # are automatically unavailable

  - id: "automation_creation"
    skills_config:
      preflight_enabled: true
      # Skills with proactive_for_profile_ids: ["automation_creation"]
      # are auto-loaded even without preflight matching
```

### 3.8. load_skill Tool for On-Demand Activation

The main model can load any skill from the catalog on demand:

```python
@tool(
    name="load_skill",
    description="Load a skill's full instructions. Use when you need detailed guidance for a task.",
)
async def load_skill_tool(
    skill_id: str,
    context: ToolExecutionContext,
) -> str:
    """Load a skill's instructions into context."""
    registry = context.skill_registry
    skill = registry.get_skill(skill_id)

    if not skill:
        return f"Skill '{skill_id}' not found. Available: {registry.list_skill_ids()}"

    # Check profile permissions
    if not registry.is_skill_available(skill_id, context.profile_id):
        return f"Skill '{skill_id}' is not available for this profile."

    # Return instructions (they'll be in the tool result, visible to the model)
    return f"# {skill.metadata.name}\n\n{skill.instructions}"
```

### 3.9. Skill Registry

```python
class SkillRegistry:
    """Manages skill discovery and access."""

    def __init__(self, db_context: DatabaseContext, builtin_path: Path | None = None):
        self._db = db_context
        self._builtin_path = builtin_path
        self._cache: dict[str, Skill] = {}

    async def refresh(self) -> None:
        """Reload skills from all sources."""
        self._cache.clear()

        # Load built-in skills from filesystem
        if self._builtin_path:
            for skill in self._load_builtin_skills():
                self._cache[skill.metadata.id] = skill

        # Load skill notes from database
        for skill in await self._load_skill_notes():
            self._cache[skill.metadata.id] = skill  # Notes override built-in

    async def _load_skill_notes(self) -> list[Skill]:
        """Load all notes that are valid skills."""
        all_notes = await self._db.notes.get_all_notes()
        skills = []
        for note in all_notes:
            if skill := self._parse_note_as_skill(note):
                skills.append(skill)
        return skills

    def get_available_skills(self, profile_id: str) -> list[Skill]:
        """Get skills available for a profile (respecting affinity rules)."""
        return [
            skill for skill in self._cache.values()
            if self._is_available_for_profile(skill, profile_id)
        ]

    def _is_available_for_profile(self, skill: Skill, profile_id: str) -> bool:
        """Check if skill is available for profile (notes-v2 rules)."""
        meta = skill.metadata
        # Exclusion takes precedence
        if meta.exclude_from_prompt_profile_ids:
            if profile_id in meta.exclude_from_prompt_profile_ids:
                return False
        # Profile-specific inclusion
        if meta.proactive_for_profile_ids:
            if profile_id in meta.proactive_for_profile_ids:
                return True
        # Default behavior
        return meta.include_in_prompt_default
```

## 4. Example Skills for Family Assistant

### 4.1. Home Automation Skill

```markdown
---
name: home-automation
description: Control smart home devices, check sensor states, and create automations. Use when the user mentions lights, temperature, locks, sensors, or home automation.
compatibility: Requires Home Assistant MCP server
allowed-tools: Bash(ha:*) mcp_home_assistant
---

# Home Automation Skill

You have access to Home Assistant for smart home control.

## Available Capabilities

- **Device Control**: Turn lights on/off, adjust brightness/color, control switches
- **Climate**: Set thermostat temperature, check current temperature/humidity
- **Sensors**: Check door/window states, motion sensors, energy usage
- **Automations**: Query existing automations, suggest new ones

## Best Practices

1. **Always verify entity IDs** before attempting control
2. **Use friendly names** when confirming actions with the user
3. **Check current state** before toggling (avoid "turn off" when already off)
4. **Suggest automations** for repeated manual actions

## Common Patterns

### Turning on lights
1. Find the entity: `ha entity list | grep -i "light.*{room}"`
2. Check current state: `ha state get light.{entity}`
3. Control: `ha service call light.turn_on -d '{"entity_id": "light.{entity}"}'`

### Creating automations
Delegate to the `automation_creation` profile for complex automations.
```

### 4.2. Calendar Management Skill

```markdown
---
name: calendar-management
description: Manage calendar events, check schedules, and find free time. Use when the user mentions meetings, appointments, schedules, or availability.
---

# Calendar Management Skill

## Capabilities

- View upcoming events for any date range
- Create new calendar events with attendees
- Find free time slots for scheduling
- Check for conflicts before booking

## Guidelines

1. **Always confirm details** before creating events (time, duration, attendees)
2. **Check for conflicts** when scheduling
3. **Use relative times** ("tomorrow at 2pm") when possible
4. **Include timezone** for events with remote attendees

## Time Handling

- User's timezone is provided in context
- Convert all times to UTC for storage
- Display times in user's local timezone
```

### 4.3. Research Assistant Skill

```markdown
---
name: research-assistant
description: Help with research tasks including web searches, document analysis, and information synthesis. Use when the user asks to research, investigate, or find information about a topic.
allowed-tools: web_search document_search
---

# Research Assistant Skill

## Research Process

1. **Clarify the question**: Ensure you understand what information is needed
2. **Search existing knowledge**: Check notes and documents first
3. **Web search if needed**: Use targeted queries
4. **Synthesize findings**: Combine information from multiple sources
5. **Cite sources**: Always indicate where information came from

## Search Strategies

### For factual questions
- Use specific, targeted queries
- Prefer authoritative sources
- Cross-reference multiple sources

### For exploratory research
- Start broad, then narrow
- Look for review articles or overviews
- Identify key terms for deeper searches

## Output Format

Present research findings with:
- **Summary**: Key findings in 2-3 sentences
- **Details**: Organized by subtopic
- **Sources**: Links or references
- **Confidence**: How certain are these findings?
```

## 5. Implementation Plan

### Phase 1: Core Infrastructure

1. **Skill data models**: `SkillMetadata`, `Skill`, `SkillSelection` dataclasses
2. **Skill registry**: Load and index skills from filesystem
3. **SKILL.md parser**: Parse frontmatter and body from markdown files
4. **Basic SkillsContextProvider**: Without preflight routing (all skills always loaded)

### Phase 2: Preflight Routing

1. **SkillRouter implementation**: Lightweight LLM integration
2. **Structured output schema**: For skill selection responses
3. **Router prompt engineering**: Optimize for accuracy and speed
4. **Caching layer**: Cache routing decisions for repeated queries

### Phase 3: Notes Integration

1. **NotesSkillProvider**: Extract skills from tagged notes
2. **Skill creation tool**: Allow assistant to create skills via notes
3. **Skill validation**: Verify skill format and required fields
4. **UI for skill management**: View, edit, enable/disable skills

### Phase 4: Advanced Features

1. **Skill dependencies**: Skills that require other skills
2. **Skill versioning**: Track changes to skill content
3. **Usage analytics**: Track which skills are used and when
4. **External skill sources**: Load skills from git repos or registries

## 6. Alternatives Considered

### 6.1. Embedding-Based Routing

**Status: Probably overkill for our use case.**

Embedding-based routing would use vector similarity to match user requests to skill descriptions:

```python
async def select_skills_by_embedding(
    user_message: str,
    skill_embeddings: dict[str, list[float]],
    threshold: float = 0.7,
) -> list[str]:
    """Select skills using semantic similarity."""
    query_embedding = await embedding_model.generate(user_message)
    scores = [
        (skill_id, cosine_similarity(query_embedding, skill_emb))
        for skill_id, skill_emb in skill_embeddings.items()
    ]
    return [sid for sid, score in sorted(scores, key=lambda x: -x[1]) if score >= threshold][:3]
```

**Analysis:**

- **When it makes sense**: 100+ skills, need sub-10ms routing, local-only operation
- **Our situation**: Likely \<20 skills, 100ms preflight acceptable, LLM already available
- **Verdict**: Skip for now. Lightweight LLM routing is simpler and more accurate for our scale.
  Revisit if skill count grows significantly.

### 6.2. Keyword/Rule-Based Fast Path

For obvious cases, we could bypass the preflight LLM entirely:

```python
# Skills can declare trigger keywords in frontmatter
# ---
# trigger_keywords: ["light", "temperature", "thermostat"]
# ---

def fast_path_selection(user_message: str, skills: list[Skill]) -> list[Skill]:
    """Quick keyword match before falling back to LLM preflight."""
    message_lower = user_message.lower()
    matches = []
    for skill in skills:
        if keywords := skill.metadata.trigger_keywords:
            if any(kw in message_lower for kw in keywords):
                matches.append(skill)
    return matches
```

**Recommendation**: Could be useful as an optimization layer, but not required initially. The LLM
preflight is cheap enough (~100ms, ~$0.0001) that the complexity isn't justified yet.

### 6.3. No Preflight (Agent-Only Selection)

The simplest approach: include skill catalog in system prompt, let main model decide via tool call.

**This is actually our baseline**, not an alternative. The proposed architecture always includes the
skill catalog, and preflight is an optional enhancement for reliability/speed.

**When to disable preflight:**

- Untrusted profiles (no external LLM calls)
- Very low latency requirements
- Cost-sensitive deployments with many requests

## 7. Security Considerations

### 7.1. Skill Source Trust

Skills from different sources have different trust levels:

| Source         | Trust Level | Restrictions        |
| -------------- | ----------- | ------------------- |
| Built-in       | High        | None                |
| User notes     | Medium      | Audited before use  |
| External repos | Low         | Sandboxed execution |

### 7.2. Tool Allowlisting

Skills can declare `allowed-tools` in frontmatter, but the processing profile has final say:

```python
def get_allowed_tools_for_skill(
    skill: Skill,
    profile: ServiceProfile,
) -> list[str]:
    """Intersect skill's requested tools with profile's allowed tools."""
    skill_tools = set(skill.metadata.allowed_tools or [])
    profile_tools = set(profile.tools_config.enable_local_tools or [])

    # Skill can only use tools the profile allows
    return list(skill_tools & profile_tools)
```

### 7.3. Skill Injection Attacks

Since skills are loaded into the system prompt, malicious skills could attempt prompt injection.
Mitigations:

1. **Source verification**: Only load skills from trusted sources
2. **Content scanning**: Check for suspicious patterns (role-play prompts, jailbreaks)
3. **Sandboxed profiles**: Run untrusted skills in restricted profiles
4. **Human review**: Require approval for external skills

## 8. Open Questions

1. **Preflight model selection**: Is gemini-2.5-flash-lite the best choice? Need to benchmark
   accuracy vs latency for alternatives (claude-3-haiku, gpt-4o-mini).

2. **Skill caching**: Should we cache parsed skills? The registry currently loads all notes on
   `refresh()`. Consider lazy loading or incremental updates for many skills.

3. **Skill creation UX**: How should the assistant create skill notes?

   - Direct note creation with frontmatter?
   - Guided wizard-style conversation?
   - Template-based with fill-in-the-blanks?

4. **Skill testing**: How do users validate a skill works before relying on it?

   - Dry-run mode?
   - Example requests in frontmatter?
   - Automated validation?

5. **Portability**: Should we support importing/exporting skills in Agent Skills standard format?

   - Export note → SKILL.md file
   - Import SKILL.md file → note
   - Sync with external skill repos?

6. **Profile auto-detection**: Should preflight also suggest which profile to use, not just which
   skills? (e.g., "This looks like an automation request, should I use automation_creation
   profile?")

## 9. References

- [Agent Skills Specification](https://agentskills.io/specification)
- [Agent Skills GitHub](https://github.com/agentskills/agentskills)
- [Example Skills Repository](https://github.com/anthropics/skills)
- [LLM Routing Survey](https://arxiv.org/html/2502.00409v1)
- [Gemini CLI Model Router](https://medium.com/google-cloud/practical-gemini-cli-intelligent-model-router-e01e543ec438)
- [Claude Skills Deep Dive](https://leehanchung.github.io/blogs/2025/10/26/claude-skills-deep-dive/)
