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

### 2.1. Two-Phase Skill Activation with Preflight Routing

The key innovation proposed here is **preflight skill selection** using a lightweight model:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           User Request                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  PHASE 1: Preflight Routing (gemini-2.5-flash-lite or similar)             │
│                                                                             │
│  Input:                                                                     │
│  - User request (+ last N turns of conversation)                           │
│  - Skill metadata catalog (names + descriptions only, ~100 tokens each)    │
│                                                                             │
│  Output (structured/constrained):                                           │
│  - List of relevant skill IDs (0-3 skills typically)                       │
│  - Confidence scores                                                        │
│  - Optional: brief reasoning for selection                                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  PHASE 2: Context Injection & Main Processing                              │
│                                                                             │
│  For each selected skill:                                                   │
│  - Load full SKILL.md body into system prompt                              │
│  - Register skill's tools with ToolsProvider                               │
│  - Make references/ available for on-demand loading                        │
│                                                                             │
│  Main model processes request with enriched context                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2. Why Preflight Routing?

The standard Agent Skills approach loads all skill metadata into the system prompt and lets the main
LLM decide which to activate. This has drawbacks:

1. **Token cost**: Every request pays for skill metadata tokens, even when irrelevant
2. **Distraction**: Large skill catalogs can distract the model from the actual task
3. **Latency**: Main model must process all metadata before starting work

**Preflight routing with a lightweight model** addresses these:

| Aspect             | Standard Approach         | Preflight Routing            |
| ------------------ | ------------------------- | ---------------------------- |
| Metadata tokens    | All skills, every request | Only for preflight (~cheap)  |
| Main model context | Cluttered with metadata   | Clean, only relevant skills  |
| Cost per request   | O(num_skills) overhead    | O(1) preflight + O(selected) |
| Latency            | Single pass, but longer   | Two passes, but optimized    |

**Cost Analysis** (rough estimates):

- Preflight with gemini-2.5-flash-lite: ~$0.0001 per request (50 skills × 100 tokens metadata)
- Avoided main model tokens: ~$0.001-0.01 per request (depending on model)
- Net savings: 10-100x on context costs when skills aren't needed

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

### 3.1. Skill Storage and Discovery

Skills can be stored in multiple locations:

1. **Built-in skills**: `src/family_assistant/skills/` directory
2. **User skills**: Stored as special notes with `skill_type` metadata
3. **External skills**: Loaded from git repos or skill registries

```python
@dataclass
class SkillMetadata:
    """Parsed from SKILL.md frontmatter."""
    id: str                          # e.g., "home-automation"
    name: str                        # Human-readable name
    description: str                 # Used for skill selection
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: list[str] | None = None
    metadata: dict[str, str] | None = None

@dataclass
class Skill:
    """Full skill with loaded content."""
    metadata: SkillMetadata
    instructions: str                 # SKILL.md body
    scripts: dict[str, str]          # filename -> content
    references: dict[str, str]       # filename -> content
    source_path: Path | None = None  # For file-based skills
```

### 3.2. SkillsContextProvider

A new `ContextProvider` implementation that integrates with the existing pattern:

```python
class SkillsContextProvider(ContextProvider):
    """Provides skill instructions as context fragments."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        skill_router: SkillRouter,
        max_skills: int = 3,
    ):
        self._registry = skill_registry
        self._router = skill_router
        self._max_skills = max_skills
        self._selected_skills: list[Skill] = []

    @property
    def name(self) -> str:
        return "skills"

    async def prepare_for_request(
        self,
        user_message: str,
        conversation_history: list[Message],
    ) -> None:
        """Run preflight routing to select relevant skills."""
        self._selected_skills = await self._router.select_skills(
            user_message=user_message,
            history=conversation_history[-4:],  # Last 4 turns
            max_skills=self._max_skills,
        )

    async def get_context_fragments(self) -> list[str]:
        """Return formatted skill instructions for selected skills."""
        if not self._selected_skills:
            return []

        fragments = ["## Active Skills\n"]
        for skill in self._selected_skills:
            fragments.append(f"### {skill.metadata.name}\n{skill.instructions}\n")

        return fragments
```

### 3.3. Preflight Skill Router

```python
class SkillRouter:
    """Routes requests to relevant skills using lightweight LLM."""

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
        max_skills: int = 3,
    ) -> list[Skill]:
        """Use lightweight LLM to select relevant skills."""

        # Build skill catalog (metadata only)
        catalog = self._build_skill_catalog()

        # Create routing prompt
        prompt = self._build_routing_prompt(user_message, history, catalog)

        # Call lightweight model with structured output
        response = await self._llm.generate(
            messages=[UserMessage(content=prompt)],
            response_schema=SkillSelectionSchema,
        )

        # Parse and return selected skills
        selection = parse_skill_selection(response)
        return [
            self._registry.get_skill(s.skill_id)
            for s in selection.selected_skills[:max_skills]
            if s.confidence >= 0.5
        ]

    def _build_skill_catalog(self) -> str:
        """Format skill metadata for routing prompt."""
        lines = ["Available skills:\n"]
        for skill in self._registry.list_skills():
            lines.append(f"- {skill.metadata.id}: {skill.metadata.description}")
        return "\n".join(lines)

    def _build_routing_prompt(
        self,
        user_message: str,
        history: list[Message],
        catalog: str,
    ) -> str:
        return f"""Analyze the user's request and select relevant skills.

{catalog}

Recent conversation:
{format_history(history)}

Current request: {user_message}

Select 0-3 skills that would help with this request. Return only skills that are clearly relevant.
If no skills are needed, return an empty list."""
```

### 3.4. Integration with Processing Profiles

Skills can be enabled/disabled per processing profile, aligning with Rule of Two security:

```yaml
service_profiles:
  - id: "default_assistant"
    skills_config:
      enable_skills: null  # All skills enabled
      skill_sources:
        - "builtin"
        - "user_notes"
      preflight_model: "gemini-2.5-flash-lite"
      preflight_enabled: true
      max_active_skills: 3

  - id: "untrusted_readonly"
    skills_config:
      enable_skills:        # Only safe skills
        - "search-help"
        - "formatting-guide"
      preflight_enabled: false  # No external model calls

  - id: "automation_creation"
    skills_config:
      enable_skills:
        - "starlark-scripting"
        - "home-assistant"
        - "event-listener-patterns"
      include_skill_tools: true  # Register skill scripts as tools
```

### 3.5. Notes as Skills

Family Assistant's notes system can serve as a skill store, enabling users to create skills through
natural conversation:

```python
class NotesSkillProvider:
    """Extracts skills from notes with skill metadata."""

    async def load_skills_from_notes(self, db: DatabaseContext) -> list[Skill]:
        """Load notes that are formatted as skills."""
        notes = await db.notes.get_notes_by_tag("skill")

        skills = []
        for note in notes:
            if skill := self._parse_note_as_skill(note):
                skills.append(skill)
        return skills

    def _parse_note_as_skill(self, note: Note) -> Skill | None:
        """Parse a note as a skill if it has valid frontmatter."""
        try:
            frontmatter, body = parse_frontmatter(note.content)
            if "name" not in frontmatter or "description" not in frontmatter:
                return None

            return Skill(
                metadata=SkillMetadata(
                    id=slugify(note.title),
                    name=frontmatter["name"],
                    description=frontmatter["description"],
                    **{k: v for k, v in frontmatter.items()
                       if k in SkillMetadata.__dataclass_fields__}
                ),
                instructions=body,
                scripts={},
                references={},
                source_path=None,
            )
        except Exception:
            return None
```

This allows users to say:

> "Create a skill for helping me write meeting notes. It should format notes with attendees, agenda,
> decisions, and action items."

The assistant creates a note with proper SKILL.md frontmatter, and it becomes available as a skill.

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

Instead of using a lightweight LLM for preflight routing, we could use embeddings:

```python
async def select_skills_by_embedding(
    user_message: str,
    skill_embeddings: dict[str, list[float]],
    embedding_model: EmbeddingGenerator,
    threshold: float = 0.7,
) -> list[str]:
    """Select skills using semantic similarity."""
    query_embedding = await embedding_model.generate(user_message)

    scores = [
        (skill_id, cosine_similarity(query_embedding, skill_emb))
        for skill_id, skill_emb in skill_embeddings.items()
    ]

    return [
        skill_id for skill_id, score in sorted(scores, key=lambda x: -x[1])
        if score >= threshold
    ][:3]
```

**Pros**:

- Very fast (~5ms vs ~100ms for LLM)
- No external API calls
- Deterministic

**Cons**:

- Less nuanced understanding of context
- Can't reason about multi-step tasks
- Requires embedding model

**Recommendation**: Use embedding-based routing as a fast path, with LLM routing for ambiguous cases
or when high accuracy is needed.

### 6.2. Rule-Based Routing

Simple keyword or pattern matching:

```python
SKILL_PATTERNS = {
    "home-automation": ["light", "temperature", "sensor", "automation", "home assistant"],
    "calendar-management": ["calendar", "meeting", "schedule", "appointment", "event"],
}

def select_skills_by_rules(user_message: str) -> list[str]:
    message_lower = user_message.lower()
    return [
        skill_id for skill_id, keywords in SKILL_PATTERNS.items()
        if any(kw in message_lower for kw in keywords)
    ]
```

**Pros**:

- Extremely fast
- No model dependencies
- Fully transparent

**Cons**:

- Brittle (missed synonyms, context)
- Requires manual maintenance
- No understanding of intent

**Recommendation**: Use as a fast-path optimization for obvious cases, not as primary routing.

### 6.3. No Preflight (Standard Approach)

Load all skill metadata into the system prompt and let the main model decide:

```python
async def get_context_fragments(self) -> list[str]:
    """Standard approach: return all skill metadata."""
    fragments = ["## Available Skills\n"]
    for skill in self._registry.list_skills():
        fragments.append(
            f"### {skill.metadata.name}\n"
            f"{skill.metadata.description}\n"
            f"To use this skill, I'll load its full instructions.\n"
        )
    return fragments
```

**Pros**:

- Simpler architecture
- Main model has full context for decisions
- Works with any model

**Cons**:

- Higher token costs
- Context pollution
- Main model distraction

**Recommendation**: Support this as a fallback when preflight routing is disabled or unavailable.

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
   alternatives (claude-3-haiku, gpt-4o-mini, local models).

2. **Caching strategy**: Should we cache routing decisions? For how long? Based on what key?

3. **Skill composition**: How should skills that depend on each other interact?

4. **Feedback loop**: Should we track which skills are actually used vs. selected, to improve
   routing over time?

5. **User control**: How much control should users have over skill activation? Always-on skills?
   Skill preferences?

## 9. References

- [Agent Skills Specification](https://agentskills.io/specification)
- [Agent Skills GitHub](https://github.com/agentskills/agentskills)
- [Example Skills Repository](https://github.com/anthropics/skills)
- [LLM Routing Survey](https://arxiv.org/html/2502.00409v1)
- [Gemini CLI Model Router](https://medium.com/google-cloud/practical-gemini-cli-intelligent-model-router-e01e543ec438)
- [Claude Skills Deep Dive](https://leehanchung.github.io/blogs/2025/10/26/claude-skills-deep-dive/)
