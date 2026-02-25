# Skills Implementation Plan: Phase 1 Deferred + Phase 2 Catalog

## Goal

Implement the deferred skills infrastructure from the
[agent-skills-integration design](agent-skills-integration.md): frontmatter parsing, file-based
skill loading, skill catalog in the system prompt, and `get_note` access to file-based skills.

## Scope

This covers the deferred items from Phases 1-2 of the design doc:

- **Frontmatter parser**: Parse YAML frontmatter from markdown content
- **DB-based skills**: DB notes with frontmatter (`name` + `description`) are detected as skills and
  shown in the catalog instead of the regular notes section
- **File-based skill loading**: Load `.md` files with frontmatter from directories
- **Skill catalog in system prompt**: Show available skills (both DB and file-based) to the LLM
- **`get_note` for file-based skills**: LLM can load skill content on demand
- **Configuration**: Skill directory paths in config
- **Built-in skills**: A few initial skill files shipped with the app

Explicitly **not** in scope: SkillRouter/preflight routing (Phase 4), Web UI changes (Phase 5).

### DB-Based Skills

Per the design doc: "A note is detected as a skill when its content (DB note) or frontmatter
(file-based note) contains both `name` and `description` fields."

DB notes with skill frontmatter:

- Appear in the **skill catalog** (metadata only), not in the regular notes section
- `include_in_prompt` is ignored for skills — progressive disclosure is the default
- Full content is loadable via `get_note` (already works, no change needed)
- Users create DB skills via `add_or_update_note` with frontmatter in the content

This means `NotesContextProvider` must parse frontmatter on all DB notes to partition them into
regular notes vs skills before formatting.

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

**Write-time detection**: Rather than parsing frontmatter on every read, skill metadata is detected
at write time and stored in dedicated DB columns (`is_skill`, `skill_name`, `skill_description`).
This was chosen over read-time parsing for performance and query simplicity — the DB can filter
skills vs regular notes directly in SQL.

A note is detected as a skill when its content contains YAML frontmatter with both `name` and
`description` fields. This follows the [Agent Skills](https://agentskills.io/specification)
convention.

### File Layout

```
src/family_assistant/skills/
├── __init__.py          # Public API exports
├── frontmatter.py       # parse_frontmatter() utility
├── loader.py            # load_skills_from_directory()
├── registry.py          # NoteRegistry class
├── types.py             # ParsedSkill dataclass
src/family_assistant/skills/builtin/
├── README.md            # Explains what goes here
└── (initial skill files)
```

## Implementation Milestones

### Milestone 1: Frontmatter Parser -- DONE

Created `src/family_assistant/skills/frontmatter.py`:

```python
def parse_frontmatter(content: str) -> tuple[dict[str, Any] | None, str]:
    """Parse YAML frontmatter from markdown content.

    Returns (frontmatter_dict, body) if frontmatter found,
    or (None, original_content) if not.
    """
```

- Handles: `---` delimiters, malformed YAML (returns None), no frontmatter, empty body
- Uses `yaml.safe_load` (no arbitrary code execution)
- Unit tests in `tests/unit/skills/test_frontmatter.py` (14 tests)

### Milestone 2: Skill Loading + ParsedSkill -- DONE

Created `src/family_assistant/skills/types.py` and `src/family_assistant/skills/loader.py`:

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

- Filters files: requires both `name` and `description` in frontmatter
- Supports visibility labels from frontmatter
- Sorts files alphabetically for consistent loading order
- Logs a warning when a configured directory does not exist (helps debug configuration issues)
- Unit tests in `tests/unit/skills/test_loader.py` (8 tests)

### Milestone 3: NoteRegistry -- DONE

Created `src/family_assistant/skills/registry.py`:

```python
class NoteRegistry:
    """Registry of file-based skills, loaded at startup."""

    def __init__(self, skills: list[ParsedSkill]) -> None: ...

    def get_skill_catalog(self, visibility_grants: set[str] | None) -> list[ParsedSkill]:
        """Get all skills accessible to a profile (for catalog listing)."""

    def get_skill_by_name(self, name: str, visibility_grants: set[str] | None) -> ParsedSkill | None:
        """Get a skill by name, respecting access control."""
```

Simple and focused: holds pre-loaded skills, provides access-controlled lookups. Unit tests in
`tests/unit/skills/test_registry.py` (7 tests).

### Milestone 4: Configuration -- PARTIAL

`SkillsConfig` exists in `config_models.py` with `user_dir` field only:

```python
class SkillsConfig(BaseModel):
    user_dir: str | None = None       # User-mounted volume
```

**Not yet done**: `builtin_dir` field, default pointing to shipped skills directory.

### Milestone 5: Extend NotesContextProvider -- DONE

`NotesContextProvider` in `context_providers.py`:

- Accepts `note_registry: NoteRegistry | None` parameter
- DB skill detection uses write-time columns (`is_skill`, `skill_name`, `skill_description`) — no
  read-time frontmatter parsing needed
- `get_prompt_notes()` excludes skills (SQL: `is_skill=False`)
- `get_skills()` returns only skills (SQL: `is_skill=True`)
- Merges DB skills + file-based skills into unified "Available Skills" catalog section
- Respects visibility grants for both sources

Functional tests in `tests/functional/notes/test_skill_catalog.py`.

### Milestone 6: Extend get_note Tool -- DONE

`get_note_tool` in `tools/notes.py`:

- Primary lookup: DB notes via repository
- Fallback: file-based skills via `NoteRegistry`
- Respects visibility grants at both layers
- Returns structured data with `exists`, `title`, `content`, `source` (for file skills)
- DB notes take priority over file skills with same name

Functional tests in `tests/functional/notes/test_get_note_skills.py`.

### Milestone 7: Wiring in Assistant -- DONE

In `Assistant.setup_dependencies()`:

1. Loads file-based skills from configured `skills_config.user_dir`
2. Creates `NoteRegistry` if any skills are loaded
3. Passes registry to `NotesContextProvider` and `ProcessingServiceConfig`

### Milestone 8: Built-in Skills -- DONE

Create initial skill files in `src/family_assistant/skills/builtin/`. These serve as examples and
demonstrate the feature working end-to-end.

### Milestone 9: Tests + Verification -- DONE

Test coverage:

- `tests/unit/skills/test_frontmatter.py` — 14 tests (frontmatter parsing edge cases)
- `tests/unit/skills/test_loader.py` — 8 tests (file loading, filtering, sorting)
- `tests/unit/skills/test_registry.py` — 7 tests (catalog access, visibility filtering)
- `tests/functional/notes/test_skill_catalog.py` — DB skills in catalog, file skills, mixed
  catalogs, visibility filtering
- `tests/functional/notes/test_skill_write_detection.py` — write-time skill detection, metadata
  extraction, filtering
- `tests/functional/notes/test_get_note_skills.py` — tool fallback behavior, override behavior,
  visibility enforcement

All tests pass with both SQLite and PostgreSQL backends.

______________________________________________________________________

## Remaining Work

### Status Summary

| Milestone                      | Status   |
| ------------------------------ | -------- |
| 1. Frontmatter Parser          | **Done** |
| 2. Skill Loading + ParsedSkill | **Done** |
| 3. NoteRegistry                | **Done** |
| 4. Configuration               | **Done** |
| 5. NotesContextProvider        | **Done** |
| 6. get_note Tool               | **Done** |
| 7. Wiring in Assistant         | **Done** |
| 8. Built-in Skills             | **Done** |
| 9. Tests + Verification        | **Done** |

### Cross-reference with Design Doc Phases

| Design Phase | Description                             | Status      |
| ------------ | --------------------------------------- | ----------- |
| Phase 1      | Core Infrastructure                     | **Done**    |
| Phase 2      | Profile-Aware Context                   | **Done**    |
| Phase 3      | On-Demand Access (`get_note`)           | **Done**    |
| Phase 4      | Preflight Routing (`SkillRouter`)       | Not started |
| Phase 5      | Polish (built-in skills, docs, prompts) | **Done**    |

______________________________________________________________________

## Next Milestones

### Milestone 10: Built-in Skills + Configuration -- DONE

Built-in skill files created in `src/family_assistant/skills/builtin/`:

- `browser-automation.md` — Guide for `/browse` command and web workflows
- `camera-integration.md` — Camera feeds, event search, investigation workflow
- `image-tools.md` — AI image generation, transformation, and annotation
- `scheduling.md` — Reminders, callbacks, recurring tasks, RRULE format

`builtin_dir` added to `SkillsConfig`. Default loading path wired in
`Assistant.setup_dependencies()` using `PACKAGE_ROOT / "skills" / "builtin"` fallback. User skills
from `user_dir` override builtins with the same name.

### Milestone 11: Documentation + Prompt Instructions -- DONE

1. **USER_GUIDE.md** updated with Skills section covering: what skills are, built-in skills,
   creating DB-based skills with frontmatter, file-based skills, and visibility labels.
2. **prompts.yaml** updated with skill creation instructions in the system prompt reminders.
3. **`add_or_update_note` tool description** updated to explain frontmatter-based skill creation.

### Milestone 12: Preflight Routing (Phase 4 from Design Doc)

**Goal**: Optionally pre-load relevant skills before the main model starts, for reliability.

This is an optimization — the system works without it since the main model can always load skills
via `get_note`. Implement when there are enough skills that catalog-only metadata is insufficient
for the model to reliably self-select the right one.

**Steps**:

1. **Create `SkillRouter`** class using a lightweight LLM (e.g., Gemini Flash Lite)
2. **Structured output schema** for reliable skill selection parsing
3. **`prepare_for_request()` hook** in `NotesContextProvider`
4. **Configuration**: `skills.preflight_model` and `skills.preflight_enabled`
5. **"Pre-loaded Skills" section** in system prompt with full instructions for selected skills

**Deferred until**: There are 5+ skills and empirical evidence that the model struggles to
self-select.

______________________________________________________________________

## Files Changed

New files:

- `src/family_assistant/skills/__init__.py`
- `src/family_assistant/skills/frontmatter.py`
- `src/family_assistant/skills/loader.py`
- `src/family_assistant/skills/registry.py`
- `src/family_assistant/skills/types.py`
- `src/family_assistant/skills/builtin/*.md` (Milestone 10)
- `tests/unit/skills/test_frontmatter.py`
- `tests/unit/skills/test_loader.py`
- `tests/unit/skills/test_registry.py`

Modified files:

- `src/family_assistant/config_models.py` — add `SkillsConfig`
- `src/family_assistant/context_providers.py` — extend `NotesContextProvider`
- `src/family_assistant/tools/notes.py` — extend `get_note_tool`
- `src/family_assistant/tools/types.py` — add `note_registry` to context
- `src/family_assistant/assistant.py` — wire NoteRegistry
- `docs/user/USER_GUIDE.md` — skills documentation (Milestone 11)
- `prompts.yaml` — skill creation instructions (Milestone 11)
