# Skills Implementation Plan: Phase 1 Deferred + Phase 2 Catalog

## Goal

Implement the deferred skills infrastructure from the
[agent-skills-integration design](agent-skills-integration.md): frontmatter parsing, file-based
skill loading, skill catalog in the system prompt, and `get_note` access to file-based skills.

## Scope

This covers the deferred items from Phases 1-2 of the design doc:

- **Frontmatter parser**: Parse YAML frontmatter from markdown content
- **File-based skill loading**: Load `.md` files with frontmatter from directories
- **Skill catalog in system prompt**: Show available skills to the LLM
- **`get_note` for file-based skills**: LLM can load skill content on demand
- **Configuration**: Skill directory paths in config
- **Built-in skills**: A few initial skill files shipped with the app

Explicitly **not** in scope: SkillRouter/preflight routing (Phase 4), Web UI changes (Phase 5).

## Design Decisions

### Incremental vs Full NoteRegistry

The design doc proposes a `NoteRegistry` that replaces all direct DB access in
`NotesContextProvider` and tools, becoming the unified data source for both DB notes and file-based
skills. This is the right long-term architecture, but implementing it in one step would require
refactoring `NotesContextProvider`, `get_note_tool`, `list_notes_tool`, and their test suites
simultaneously.

**Proposed approach**: Implement `NoteRegistry` as an additive layer that handles file-based skills
without disrupting the existing DB-backed notes path:

1. `NoteRegistry` owns file-based skill data (loaded once at startup)
2. `NotesContextProvider` continues to query DB for regular notes, and also queries `NoteRegistry`
   for skill catalog
3. `get_note_tool` checks `NoteRegistry` for file-based skills when the DB doesn't have a match
4. Regular note CRUD tools (`add_or_update`, `delete`, `list_notes`) remain DB-only — you can't
   modify file-based content

This preserves all existing behavior and tests while adding skills. A future milestone can
consolidate into the full unified `NoteRegistry` from the design doc if needed.

### Skill Detection

A note is detected as a skill when its content contains YAML frontmatter with both `name` and
`description` fields. This follows the [Agent Skills](https://agentskills.io/specification)
convention.

### File Layout

```
src/family_assistant/skills/
├── __init__.py          # ParsedSkill dataclass, load_skills_from_directory()
├── frontmatter.py       # parse_frontmatter() utility
└── registry.py          # NoteRegistry class
src/family_assistant/skills/builtin/
├── README.md            # Explains what goes here
└── (initial skill files)
```

## Implementation Milestones

### Milestone 1: Frontmatter Parser

Create `src/family_assistant/skills/frontmatter.py`:

```python
def parse_frontmatter(content: str) -> tuple[dict[str, Any] | None, str]:
    """Parse YAML frontmatter from markdown content.

    Returns (frontmatter_dict, body) if frontmatter found,
    or (None, original_content) if not.
    """
```

- Handles: `---` delimiters, malformed YAML (returns None), no frontmatter, empty body
- Uses `yaml.safe_load` (no arbitrary code execution)
- Unit tests in `tests/unit/test_frontmatter.py`

### Milestone 2: Skill Loading + ParsedSkill

Create `src/family_assistant/skills/__init__.py`:

```python
@dataclass(frozen=True)
class ParsedSkill:
    """A file-based skill with parsed metadata."""
    name: str                          # From frontmatter
    description: str                   # From frontmatter
    content: str                       # Body after frontmatter
    source_path: Path                  # Original file path
    visibility_labels: frozenset[str]  # From frontmatter, default empty

def load_skills_from_directory(directory: Path) -> list[ParsedSkill]:
    """Load markdown files with skill frontmatter from a directory."""
```

Why `ParsedSkill` instead of `ParsedNote` from the design doc:

- At this stage, file-based skills and DB notes are different types with different storage
- `ParsedSkill` has exactly the fields needed for file-based skills, no DB-specific fields
- If we unify later, we can merge the types then

Unit tests in `tests/unit/test_skill_loading.py`.

### Milestone 3: NoteRegistry

Create `src/family_assistant/skills/registry.py`:

```python
class NoteRegistry:
    """Registry of file-based skills, loaded at startup."""

    def __init__(self, skills: list[ParsedSkill]) -> None: ...

    def get_skill_catalog(self, visibility_grants: set[str] | None) -> list[ParsedSkill]:
        """Get all skills accessible to a profile (for catalog listing)."""

    def get_skill_by_name(self, name: str, visibility_grants: set[str] | None) -> ParsedSkill | None:
        """Get a skill by name, respecting access control."""
```

Simple and focused: holds pre-loaded skills, provides access-controlled lookups.

### Milestone 4: Configuration

Add to `config_models.py`:

```python
class SkillsConfig(BaseModel):
    builtin_dir: str | None = None    # Default: bundled with app
    user_dir: str | None = None       # User-mounted volume
```

Add to `AppConfig`:

```python
skills_config: SkillsConfig = Field(default_factory=SkillsConfig)
```

### Milestone 5: Extend NotesContextProvider

Add skill catalog section to the system prompt output. After regular notes, append:

```
## Available Skills
Use the `get_note` tool to load a skill's full instructions.
- **Meeting Notes**: Format meeting notes with attendees, agenda, decisions, and action items.
- **Research Assistant**: Help with research tasks...
```

Changes to `NotesContextProvider`:

- Add `note_registry: NoteRegistry | None = None` parameter
- In `get_context_fragments()`, append skill catalog if registry has skills

### Milestone 6: Extend get_note Tool

When `get_note_tool` doesn't find a note in the DB, fall back to `NoteRegistry`:

```python
# Existing DB lookup
note = await db_context.notes.get_by_title(title, visibility_grants=grants)
if note:
    return format_note(note)

# Fall back to file-based skills
if note_registry:
    skill = note_registry.get_skill_by_name(title, visibility_grants=grants)
    if skill:
        return format_skill(skill)

return "Note not found..."
```

Changes to `ToolExecutionContext`: add `note_registry: NoteRegistry | None = None`.

### Milestone 7: Wiring in Assistant

In `Assistant.setup_dependencies()`:

1. Load file-based skills from configured directories
2. Create `NoteRegistry` with loaded skills
3. Pass to `NotesContextProvider` and `ProcessingServiceConfig`

### Milestone 8: Built-in Skills

Create 2-3 initial skill files in `src/family_assistant/skills/builtin/`. These serve as examples
and demonstrate the feature working end-to-end.

### Milestone 9: Tests + Verification

- Unit tests for frontmatter parser
- Unit tests for skill loading
- Unit tests for NoteRegistry
- Functional tests for skill catalog in system prompt
- Functional tests for get_note loading file-based skills
- Full `poe test` pass

## Files Changed

New files:

- `src/family_assistant/skills/__init__.py`
- `src/family_assistant/skills/frontmatter.py`
- `src/family_assistant/skills/registry.py`
- `src/family_assistant/skills/builtin/*.md`
- `tests/unit/test_frontmatter.py`
- `tests/unit/test_skill_loading.py`
- `tests/unit/test_note_registry.py`

Modified files:

- `src/family_assistant/config_models.py` — add `SkillsConfig`
- `src/family_assistant/context_providers.py` — extend `NotesContextProvider`
- `src/family_assistant/tools/notes.py` — extend `get_note_tool`
- `src/family_assistant/tools/types.py` — add `note_registry` to `ToolExecutionContext`
- `src/family_assistant/assistant.py` — wire NoteRegistry
