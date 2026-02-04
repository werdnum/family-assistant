# Notes + Skills: Unified Design

## 1. Introduction

This document proposes a unified architecture for enhancing notes with profile-aware visibility
(notes-v2 Milestone 3+) and integrating [Agent Skills](https://agentskills.io/) concepts. The
central insight is that these are the **same feature**: both need content that appears in the right
context, with non-included content discoverable on demand.

### 1.1. What Exists Today

| Feature                  | Status                                            |
| ------------------------ | ------------------------------------------------- |
| Notes CRUD               | Done                                              |
| `include_in_prompt` flag | Done                                              |
| Vector indexing          | Done                                              |
| Semantic search          | Done                                              |
| `NotesContextProvider`   | Done (simple boolean filtering)                   |
| `include_system_docs`    | Done (loads files into system prompt per profile) |

### 1.2. What's Missing

| Need                             | notes-v2 Milestone | Skills Need       |
| -------------------------------- | ------------------ | ----------------- |
| Profile-based visibility         | Milestone 3        | Profile affinity  |
| Direct note access tool          | Milestone 5        | `load_skill` tool |
| Bundled content from files       | (not planned)      | Built-in skills   |
| Catalog listing in system prompt | (not planned)      | Skill discovery   |
| Preflight routing                | (not planned)      | Skill pre-loading |

### 1.3. The Unifying Insight

Notes and skills differ only in **how content is used**, not in how it's stored or managed:

- **Regular notes**: Content is the value (facts, preferences, family info)
- **Skills**: Content is procedural guidance, plus structured metadata for discovery

A skill is just **a note with frontmatter**. Profile affinity, discoverability, and on-demand access
serve both use cases identically. We don't need separate systems.

## 2. Architecture

### 2.1. The Frontmatter Convention

Notes can optionally begin with YAML frontmatter. Frontmatter controls visibility and, for skills,
provides discovery metadata:

**Regular note with profile affinity** (not a skill, no `name`/`description`):

```markdown
---
proactive_for_profile_ids: ["automation_creation"]
---

Starlark scripts in this project use the `event_listener` pattern.
The standard entry point is `on_event(event_type, payload)`.
```

**Skill note** (has `name` and `description`):

```markdown
---
name: Meeting Notes
description: Format meeting notes with attendees, agenda, decisions, and action items.
---

# Meeting Notes Skill

When the user asks to take meeting notes, use this structure:

## Template
- **Date**: {date}
- **Attendees**: {attendees}
- **Agenda**: ...
- **Decisions**: ...
- **Action Items**: ...
```

**Regular note without frontmatter** (behaves exactly as today):

```
Our family prefers vegetarian meals on weekdays.
```

### 2.2. Visibility Rules

Three layers, evaluated in order:

```
1. Frontmatter exclusion     →  exclude_from_prompt_profile_ids contains current profile?
                                  YES → not visible, not in catalog
                                  NO  → continue

2. Frontmatter inclusion     →  proactive_for_profile_ids contains current profile?
                                  YES → visible (included in prompt / catalog)
                                  NO  → continue

3. Database flag (existing)  →  include_in_prompt = true?
                                  YES → visible
                                  NO  → reactive only (searchable, loadable on demand)
```

This preserves backward compatibility. Notes without frontmatter behave exactly as they do today.
The existing `include_in_prompt` column remains the simple default, with frontmatter providing
per-profile overrides.

**No schema changes needed.** Profile affinity lives in frontmatter, parsed at load time.

### 2.3. Content Sources

Notes come from two sources, merged at load time:

```
┌─────────────────────────────┐    ┌───────────────────────────────┐
│  Database Notes             │    │  File-Based Notes             │
│                             │    │                               │
│  - User-created via chat    │    │  - Built-in skills            │
│  - Managed via web UI       │    │  - Shipped with application   │
│  - include_in_prompt column │    │  - From config directory      │
│                             │    │  - Always include_in_prompt   │
│  Higher priority (override) │    │  Lower priority (defaults)    │
└─────────────────────────────┘    └───────────────────────────────┘
                    │                               │
                    └───────────────┬───────────────┘
                                    ▼
                        ┌───────────────────────┐
                        │  Note Registry         │
                        │                       │
                        │  Merged, parsed,      │
                        │  frontmatter cached   │
                        └───────────────────────┘
```

**File-based notes** extend the `include_system_docs` pattern:

```yaml
# config.yaml (or defaults.yaml)
skills:
  builtin_dir: "src/family_assistant/skills"   # Shipped with app
  user_dir: "/config/skills"                    # User-mounted volume
```

Files are markdown with optional frontmatter (same format as note content). A DB note with a
matching title overrides the file-based version, allowing user customization of built-in skills.

### 2.4. What Appears in the System Prompt

For a given profile, the `NotesContextProvider` produces three sections:

```
## Notes
(notes where visibility=true AND not a skill)
- Family Preferences: Our family prefers vegetarian meals on weekdays.
- School Schedule: Monday: Math, Tuesday: Science...

## Available Skills
(skills where visibility=true, metadata only)
Use the `get_note` tool to load a skill's full instructions.
- **Meeting Notes**: Format meeting notes with attendees, agenda, decisions, and action items.
- **Home Automation**: Control smart home devices, check sensor states, create automations.
- **Research Assistant**: Help with research tasks including web searches and synthesis.

## Pre-loaded Skills
(skills selected by preflight, full instructions included)
### Meeting Notes
When the user asks to take meeting notes, use this structure...

## Other Notes
(titles of notes where visibility=false, for awareness)
Other available notes (not included above): "Tax Records", "Medical Info"
```

The distinction between "Notes" and "Available Skills" is purely presentational: skills have their
`name` and `description` extracted for a concise catalog listing, while regular notes have their
full content included directly.

### 2.5. Hybrid Skill Activation

Skills support two complementary activation modes:

1. **Agent self-activation**: The main model sees the skill catalog and can load any skill via the
   `get_note` tool
2. **Preflight pre-loading**: A lightweight model (e.g., gemini-2.5-flash-lite) optionally
   pre-selects relevant skills before the main model starts

Preflight is an **optimization for reliability and speed**, not a gatekeeper. The main model always
has the full catalog and can load any skill.

```
User Request
      │
      ├──── Preflight (optional) ────── Pre-load 0-3 skills into context
      │     gemini-2.5-flash-lite
      │     structured output
      │
      └──── System Prompt (always) ──── Skill catalog with metadata
                                         + pre-loaded skill instructions
                                         + get_note tool for on-demand loading
```

## 3. Implementation

### 3.1. Note Registry

A single class replaces both the notes-v2 profile logic and the proposed SkillRegistry:

```python
@dataclass
class ParsedNote:
    """A note with parsed frontmatter."""
    title: str
    content: str                      # Content after frontmatter
    raw_content: str                  # Original content (with frontmatter)
    include_in_prompt: bool           # From DB column
    attachment_ids: list[str]
    # Parsed from frontmatter (None if no frontmatter)
    frontmatter: dict[str, Any] | None = None
    # Derived from frontmatter
    is_skill: bool = False            # Has name + description
    skill_name: str | None = None     # From frontmatter "name"
    skill_description: str | None = None  # From frontmatter "description"
    proactive_for_profile_ids: list[str] | None = None
    exclude_from_prompt_profile_ids: list[str] | None = None
    # Source tracking
    source: str = "database"          # "database" or "file"
    note_id: int | None = None        # DB id (for database notes)
    source_path: Path | None = None   # File path (for file notes)


class NoteRegistry:
    """Loads, parses, and indexes all notes from DB and files."""

    def __init__(
        self,
        db_context_func: Callable[[], Awaitable[DatabaseContext]],
        builtin_dir: Path | None = None,
        user_dir: Path | None = None,
    ):
        self._db_context_func = db_context_func
        self._builtin_dir = builtin_dir
        self._user_dir = user_dir
        self._notes: dict[str, ParsedNote] = {}  # title -> ParsedNote

    async def refresh(self) -> None:
        """Reload all notes from all sources."""
        self._notes.clear()

        # Load file-based notes first (lower priority)
        for directory in [self._builtin_dir, self._user_dir]:
            if directory and directory.exists():
                for note in self._load_from_directory(directory):
                    self._notes[note.title] = note

        # Load DB notes (higher priority, overrides files)
        async with await self._db_context_func() as db:
            for note_dict in await db.notes.get_all():
                parsed = self._parse_note(note_dict, source="database")
                self._notes[parsed.title] = parsed

    def get_visible_notes(self, profile_id: str) -> list[ParsedNote]:
        """Get notes visible for a profile (applying visibility rules)."""
        return [n for n in self._notes.values() if self._is_visible(n, profile_id)]

    def get_visible_skills(self, profile_id: str) -> list[ParsedNote]:
        """Get skills visible for a profile."""
        return [n for n in self.get_visible_notes(profile_id) if n.is_skill]

    def get_visible_regular_notes(self, profile_id: str) -> list[ParsedNote]:
        """Get non-skill notes visible for a profile."""
        return [n for n in self.get_visible_notes(profile_id) if not n.is_skill]

    def get_excluded_titles(self, profile_id: str) -> list[str]:
        """Get titles of notes not visible for a profile (for awareness listing)."""
        return [n.title for n in self._notes.values() if not self._is_visible(n, profile_id)]

    def get_by_title(self, title: str) -> ParsedNote | None:
        """Get any note by title (for on-demand access)."""
        return self._notes.get(title)

    def _is_visible(self, note: ParsedNote, profile_id: str) -> bool:
        """Apply visibility rules (Section 2.2)."""
        if note.exclude_from_prompt_profile_ids:
            if profile_id in note.exclude_from_prompt_profile_ids:
                return False
        if note.proactive_for_profile_ids:
            if profile_id in note.proactive_for_profile_ids:
                return True
        return note.include_in_prompt

    def _parse_note(self, note_dict: dict, source: str = "database") -> ParsedNote:
        """Parse a note dict, extracting frontmatter if present."""
        raw_content = note_dict["content"]
        frontmatter, body = parse_frontmatter(raw_content)

        is_skill = bool(frontmatter and "name" in frontmatter and "description" in frontmatter)

        return ParsedNote(
            title=note_dict["title"],
            content=body,
            raw_content=raw_content,
            include_in_prompt=note_dict.get("include_in_prompt", True),
            attachment_ids=note_dict.get("attachment_ids", []),
            frontmatter=frontmatter,
            is_skill=is_skill,
            skill_name=frontmatter.get("name") if frontmatter else None,
            skill_description=frontmatter.get("description") if frontmatter else None,
            proactive_for_profile_ids=frontmatter.get("proactive_for_profile_ids") if frontmatter else None,
            exclude_from_prompt_profile_ids=frontmatter.get("exclude_from_prompt_profile_ids") if frontmatter else None,
            source=source,
            note_id=note_dict.get("id"),
        )
```

### 3.2. Enhanced NotesContextProvider

The existing `NotesContextProvider` is extended (not replaced) to be profile-aware and skill-aware:

```python
class NotesContextProvider(ContextProvider):
    """Provides notes and skills context, profile-aware."""

    def __init__(
        self,
        note_registry: NoteRegistry,
        profile_id: str,
        prompts: PromptsType,
        skill_router: SkillRouter | None = None,
        attachment_registry: Any = None,
    ):
        self._registry = note_registry
        self._profile_id = profile_id
        self._prompts = prompts
        self._router = skill_router
        self._attachment_registry = attachment_registry
        self._preloaded_skills: list[ParsedNote] = []

    async def prepare_for_request(
        self,
        user_message: str,
        conversation_history: list[Message],
    ) -> None:
        """Optionally run preflight to pre-select skills."""
        if self._router:
            available_skills = self._registry.get_visible_skills(self._profile_id)
            if available_skills:
                self._preloaded_skills = await self._router.select_skills(
                    user_message=user_message,
                    history=conversation_history[-4:],
                    available_skills=available_skills,
                )

    async def get_context_fragments(self) -> list[str]:
        fragments: list[str] = []

        # 1. Regular notes (full content)
        regular_notes = self._registry.get_visible_regular_notes(self._profile_id)
        if regular_notes:
            # ... format as today, using prompts templates ...
            pass

        # 2. Skill catalog (metadata only)
        skills = self._registry.get_visible_skills(self._profile_id)
        if skills:
            catalog_lines = ["## Available Skills",
                            "Use `get_note` tool to load a skill's full instructions."]
            for skill in skills:
                catalog_lines.append(f"- **{skill.skill_name}**: {skill.skill_description}")
            fragments.append("\n".join(catalog_lines))

        # 3. Pre-loaded skill instructions
        preloaded_ids = {s.title for s in self._preloaded_skills}
        if self._preloaded_skills:
            fragments.append("\n## Pre-loaded Skills")
            for skill in self._preloaded_skills:
                fragments.append(f"### {skill.skill_name}\n{skill.content}")

        # 4. Excluded notes (awareness listing)
        excluded = self._registry.get_excluded_titles(self._profile_id)
        if excluded:
            titles_str = ", ".join(f'"{t}"' for t in excluded)
            fragments.append(f"Other available notes (not shown): {titles_str}")

        return fragments
```

### 3.3. Unified `get_note` Tool

A single tool serves both notes-v2 direct access (Milestone 5) and skill loading:

```python
@tool(
    name="get_note",
    description=(
        "Retrieve the full content of a note or skill by title. "
        "Use this to load skill instructions from the Available Skills catalog, "
        "or to access notes not included in the current prompt."
    ),
)
async def get_note_tool(
    title: str,
    context: ToolExecutionContext,
) -> str:
    """Load a note or skill's content."""
    registry = context.note_registry
    note = registry.get_by_title(title)

    if not note:
        available = [n.title for n in registry._notes.values()]
        return f"Note '{title}' not found. Available notes: {', '.join(available[:20])}"

    if note.is_skill:
        return f"# {note.skill_name}\n\n{note.content}"
    else:
        return f"# {note.title}\n\n{note.content}"
```

### 3.4. Preflight Skill Router

Unchanged from previous design - a lightweight LLM pre-selects relevant skills:

```python
class SkillRouter:
    """Pre-selects skills using lightweight LLM for reliability/speed."""

    def __init__(self, router_llm: LLMInterface):
        self._llm = router_llm

    async def select_skills(
        self,
        user_message: str,
        history: list[Message],
        available_skills: list[ParsedNote],
        max_skills: int = 3,
    ) -> list[ParsedNote]:
        """Pre-select skills that are likely relevant."""
        if not available_skills:
            return []

        catalog = "\n".join(
            f"- {s.title}: {s.skill_description}" for s in available_skills
        )
        prompt = f"""Select 0-{max_skills} skills relevant to the user's request.

Available skills:
{catalog}

Recent request: {user_message}

Return a JSON array of skill titles that are clearly relevant.
If none are relevant, return an empty array."""

        response = await self._llm.generate(
            messages=[UserMessage(content=prompt)],
            response_schema=SkillSelectionSchema,
        )

        selected_titles = parse_skill_selection(response)
        title_set = set(selected_titles[:max_skills])
        return [s for s in available_skills if s.title in title_set]
```

### 3.5. Bundled Skills from Files

Follows the `include_system_docs` pattern with frontmatter support:

```python
def load_notes_from_directory(directory: Path) -> list[ParsedNote]:
    """Load markdown files as notes. Used for built-in and user skill dirs."""
    notes = []
    if not directory.exists():
        return notes

    for md_file in sorted(directory.glob("*.md")):
        try:
            raw_content = md_file.read_text()
            frontmatter, body = parse_frontmatter(raw_content)

            # Use frontmatter name or filename as title
            title = (frontmatter or {}).get("name", md_file.stem)

            notes.append(ParsedNote(
                title=title,
                content=body,
                raw_content=raw_content,
                include_in_prompt=True,  # File-based notes default to visible
                attachment_ids=[],
                frontmatter=frontmatter,
                is_skill=bool(frontmatter and "name" in frontmatter and "description" in frontmatter),
                skill_name=(frontmatter or {}).get("name"),
                skill_description=(frontmatter or {}).get("description"),
                proactive_for_profile_ids=(frontmatter or {}).get("proactive_for_profile_ids"),
                exclude_from_prompt_profile_ids=(frontmatter or {}).get("exclude_from_prompt_profile_ids"),
                source="file",
                source_path=md_file,
            ))
        except Exception as e:
            logger.warning(f"Failed to load note from {md_file}: {e}")

    return notes
```

**Directory structure:**

```
src/family_assistant/skills/        # Built-in skills (shipped with app)
├── home-automation.md
├── meeting-notes.md
└── research-assistant.md

/config/skills/                      # User skills directory (container volume)
├── my-custom-skill.md
└── family-recipes.md
```

## 4. What This Replaces

### 4.1. notes-v2 Milestones Superseded

| notes-v2 Milestone                       | Status in This Design                           |
| ---------------------------------------- | ----------------------------------------------- |
| Milestone 1: Note indexing               | Done (keep as-is)                               |
| Milestone 2: include_in_prompt           | Done (keep as-is)                               |
| Milestone 2.5: Tool/UI enhancements      | Partially superseded by `get_note` tool         |
| **Milestone 3: Profile-based filtering** | **Replaced by frontmatter** (no schema changes) |
| Milestone 4: Web UI enhancements         | Deferred (not blocking)                         |
| **Milestone 5: Direct note access tool** | **Replaced by `get_note` tool**                 |

### 4.2. Key Simplification

The notes-v2 plan called for:

- New database columns (`proactive_for_profile_ids`, `exclude_from_prompt_profile_ids`)
- Migration to rename `include_in_prompt` to `include_in_prompt_default`
- Profile-aware SQL queries
- Separate tool parameters for profile lists

This design achieves the same outcome with:

- **No schema changes** - frontmatter in existing `content` column
- **No migrations** - existing `include_in_prompt` column used as-is
- **Python-side filtering** - parse frontmatter on load, filter in memory (fine for \<100 notes)
- **One new tool** (`get_note`) that serves both direct access and skill loading

### 4.3. Trade-offs

**Frontmatter instead of columns:**

| Aspect            | Columns (notes-v2)      | Frontmatter (this design)             |
| ----------------- | ----------------------- | ------------------------------------- |
| SQL filtering     | Efficient at DB level   | Must load all, filter in Python       |
| Adding new fields | Migration per field     | Just parse more frontmatter           |
| Portability       | Locked to our schema    | Compatible with Agent Skills standard |
| User editing      | Needs UI for each field | Edit content directly                 |
| Performance       | Better at 1000+ notes   | Fine for \<100 notes                  |

**Python-side filtering is acceptable** because:

- Family Assistant typically has \<50 notes
- Notes are cached in the registry, not re-queried per request
- Frontmatter parsing is fast (~microseconds per note)

## 5. Example Skills

### 5.1. Home Automation

```markdown
---
name: Home Automation
description: Control smart home devices, check sensor states, and create automations. Activate when user mentions lights, temperature, locks, sensors, or home automation.
proactive_for_profile_ids: ["default_assistant", "automation_creation"]
exclude_from_prompt_profile_ids: ["untrusted_readonly"]
---

# Home Automation Skill

You have access to Home Assistant for smart home control.

## Device Control

- Turn lights on/off, adjust brightness/color
- Set thermostat temperature
- Check sensor states (doors, windows, motion)

## Best Practices

1. Always verify entity IDs before control
2. Check current state before toggling
3. Suggest automations for repeated manual actions
```

### 5.2. Research Assistant

```markdown
---
name: Research Assistant
description: Help with research tasks including web searches, document analysis, and information synthesis.
---

# Research Assistant Skill

## Process

1. Clarify the question
2. Search existing knowledge (notes, documents) first
3. Web search if needed
4. Synthesize findings
5. Cite sources

## Output Format

- **Summary**: 2-3 sentences
- **Details**: Organized by subtopic
- **Sources**: Links or references
- **Confidence**: How certain are these findings?
```

## 6. Implementation Plan

### Phase 1: Core Infrastructure

1. **Frontmatter parser**: Extract YAML frontmatter from note content
2. **`ParsedNote` dataclass**: Unified representation with parsed metadata
3. **`NoteRegistry`**: Load from DB + files, parse frontmatter, cache
4. **File loading**: `load_notes_from_directory()` for built-in skills
5. **Configuration**: `skills.builtin_dir` and `skills.user_dir` in config

**Deliverable**: Notes and file-based skills loaded into a unified registry.

### Phase 2: Profile-Aware Context

1. **Refactor `NotesContextProvider`**: Use `NoteRegistry` instead of direct DB queries
2. **Profile visibility**: Apply frontmatter rules in context provider
3. **Skill catalog**: Generate catalog listing for skill notes
4. **Pass profile_id** through context provider initialization

**Deliverable**: Different profiles see different notes/skills. Skills listed in catalog.

### Phase 3: On-Demand Access

1. **`get_note` tool**: Load any note/skill by title
2. **Register tool**: Add to tool definitions
3. **Tests**: Functional tests for note access and skill loading

**Deliverable**: LLM can load skills from catalog and access reactive notes.

### Phase 4: Preflight Routing

1. **`SkillRouter`**: Lightweight LLM skill selection
2. **Structured output schema**: For reliable parsing
3. **`prepare_for_request`**: Hook into context provider
4. **Configuration**: `skills.preflight_model` and `skills.preflight_enabled`

**Deliverable**: Relevant skills pre-loaded before main model starts.

### Phase 5: Polish

1. **Ship built-in skills**: Create initial skill files
2. **Web UI**: Display skill/note type indicators (if desired)
3. **User guide**: Document skill creation for end users
4. **Skill creation via assistant**: Teach the assistant to create skill notes

## 7. Security

### 7.1. Source Trust

| Source              | Trust  | Restrictions        |
| ------------------- | ------ | ------------------- |
| Built-in files      | High   | None                |
| User DB notes       | Medium | Audited before use  |
| User file directory | Medium | User-managed volume |

### 7.2. Profile Enforcement

Skills respect the same processing profile boundaries as the rest of the system. The
`exclude_from_prompt_profile_ids` field provides defense-in-depth: a skill can declare it should
never be available in untrusted profiles, regardless of other settings.

### 7.3. Injection Risk

Skills are loaded into the system prompt, so malicious skills could attempt prompt injection. Since
all skills come from trusted sources (built-in files or user-created notes), this is mitigated by
the same trust model as regular notes.

## 8. Open Questions

1. **Registry refresh frequency**: Refresh on every request, or cache with TTL? Notes change
   infrequently so a 60s cache with manual invalidation on note CRUD seems reasonable.

2. **Preflight model selection**: gemini-2.5-flash-lite is the initial candidate. Need to benchmark
   accuracy and latency. Could also be configurable per profile.

3. **Skill creation flow**: Should the assistant know how to create skills? Could add instructions
   in the system prompt like "To create a skill, use `add_or_update_note` with YAML frontmatter
   containing `name` and `description` fields."

4. **Profile auto-detection**: Should preflight also suggest which profile to use? This extends
   beyond skills into general routing.

## 9. References

- [Agent Skills Specification](https://agentskills.io/specification)
- [Agent Skills GitHub](https://github.com/agentskills/agentskills)
- [notes-v2 Design](notes-v2.md) (superseded by this document for Milestones 3+)
- [Processing Profiles Design](processing_profiles.md)
