# Task Management Tool Implementation Design

## Overview

This document details the tool design for implementing task management within the family assistant. Based on the analysis in `task-management-analysis-summary.md`, we're implementing a markdown-based approach with carefully designed tools that balance simplicity with functionality.

## Design Principles

1. **Simple operations should use simple tools** - No Starlark generation for common tasks
2. **LLM cognitive load matters** - Avoid requiring complex script generation for each operation
3. **Compose simple tools for complex operations** - Better than monolithic complex tools
4. **Pattern matching over regex** - More LLM-friendly and predictable
5. **Granular edits over full rewrites** - Reduce conflicts and improve performance

## Task Management Tools

### Storage Architecture

Tasks are stored in topic-based notes:
- `TODO: Shopping` - Shopping lists
- `TODO: Home` - Home maintenance and chores
- `TODO: Projects` - Multi-step projects
- `TODO: Recurring` - Templates for recurring tasks
- `TODO: Archive/YYYY-MM` - Completed tasks by month

### Core Task Operations

#### 1. add_task
```python
@tool
async def add_task(
    task: str,
    category: str = "General",  # Shopping, Home, Projects
    metadata: Optional[str] = None  # @person, #tags, due:date
) -> str:
    """Add a single task without requiring script generation"""
```

**Rationale**: 
- Covers 60% of operations with zero cognitive load
- Natural conversation: "Add milk to shopping list"
- Auto-creates sections and categories as needed

#### 2. complete_task
```python
@tool
async def complete_task(
    task_pattern: str,
    category: Optional[str] = None  # Search all if None
) -> str:
    """Mark a task as complete - simple checkbox update"""
```

**Rationale**:
- Most common operation after adding
- Pattern matching more forgiving than exact match
- Returns clear success/failure

#### 3. find_tasks
```python
@tool
async def find_tasks(
    filter_type: Literal["person", "tag", "category", "status", "text"],
    filter_value: str,
    include_completed: bool = False
) -> List[Dict[str, str]]:
    """Find tasks matching criteria - returns structured data"""
```

**Rationale**:
- Composable - results can be formatted or acted upon
- Covers most query needs without complex syntax
- Returns structured data for further processing

#### 4. move_tasks
```python
@tool
async def move_tasks(
    from_category: str,
    to_category: str,
    filter: Optional[str] = None  # "@sarah", "#urgent", etc
) -> str:
    """Move tasks between categories"""
```

**Rationale**:
- Common reorganization operation
- Batch operations without scripts
- Preserves task metadata

### Complex Operations Support

#### task_script
```python
@tool
async def task_script(
    description: str,
    script: str
) -> str:
    """Execute complex task operation via script"""
```

**Use cases**:
- Creating recurring task templates
- Cross-note task dependencies
- Custom workflow automation

**Safety features**:
- Validates dangerous operations
- Requires confirmation for deletions
- Clear description for audit trail

## Granular Note Editing Tools

### Problem Statement

Current note operations are too coarse:
- `add_or_update_note()` - Replaces entire note (conflict-prone)
- `append_to_note()` - Only adds to end (limited flexibility)

### Proposed Granular Tools

#### 1. update_note_section
```python
@tool
async def update_note_section(
    note_name: str,
    section_path: str,  # "Shopping/Today" or just "Shopping"
    new_content: str,
    create_if_missing: bool = True
) -> str:
    """Replace entire section content while preserving rest of note"""
```

**Benefits**:
- Updates only relevant part of note
- Understands markdown hierarchy
- Reduces merge conflicts

#### 2. update_note_lines
```python
@tool
async def update_note_lines(
    note_name: str,
    start_pattern: str,  # Find line containing this
    end_pattern: Optional[str] = None,  # Single line if None
    new_lines: Union[str, List[str]],
    occurrence: int = 1  # Which match to update
) -> str:
    """Update specific lines by pattern matching"""
```

**Benefits**:
- Surgical precision for edits
- Pattern matching more LLM-friendly than line numbers
- Handles single lines or ranges

#### 3. insert_in_note
```python
@tool
async def insert_in_note(
    note_name: str,
    position: Literal["before", "after", "end_of_section"],
    reference: str,  # Pattern to find position
    content: str
) -> str:
    """Insert content at specific position"""
```

**Benefits**:
- Maintains document structure
- Auto-handles indentation
- Clear position semantics

### Task-Specific Edit Operations

#### modify_task
```python
@tool
async def modify_task(
    note_name: str,
    task_pattern: str,  # Partial match on task text
    operation: Literal["complete", "uncomplete", "delete", "update"],
    new_text: Optional[str] = None,  # For update
    metadata_changes: Optional[Dict] = None  # {"assignee": "@mike"}
) -> str:
    """Precise task modifications without rewriting whole note"""
```

**Implementation**:
- Built on top of `update_note_lines`
- Preserves checkbox syntax and metadata
- Atomic operations on single tasks

#### move_task
```python
@tool
async def move_task(
    task_pattern: str,
    from_section: str,  # "Shopping/Today"  
    to_section: str,    # "Shopping/Weekly"
    from_note: Optional[str] = None,  # Can move between notes
    to_note: Optional[str] = None
) -> str:
    """Move task between sections or notes"""
```

**Features**:
- Atomic operation prevents task loss
- Creates destination section if needed
- Preserves all task metadata

### Section Management Tools

#### organize_note_section
```python
@tool
async def organize_note_section(
    note_name: str,
    section_path: str,
    organization: Literal["alphabetical", "by_assignee", "by_tag", "by_status"]
) -> str:
    """Reorganize tasks within a section"""
```

**Use cases**:
- Alphabetize shopping lists
- Group tasks by assignee
- Sort by priority tags

#### archive_completed_tasks
```python
@tool
async def archive_completed_tasks(
    from_note: str,
    to_note: str = "TODO: Archive",
    older_than_days: Optional[int] = None
) -> str:
    """Move completed tasks to archive"""
```

**Features**:
- Maintains task history
- Can be scheduled monthly
- Keeps active lists clean

## Implementation Considerations

### Concurrency Safety

1. **Line-level operations** - Minimize conflict window
2. **Pattern-based targeting** - Find current state before modifying
3. **Atomic operations** - Complete edits in single transaction
4. **Version checking** - Detect concurrent modifications

### Error Handling

1. **Pattern not found** - Clear error messages
2. **Multiple matches** - Require disambiguation
3. **Malformed markdown** - Graceful degradation
4. **Note not found** - Option to create

### Performance

1. **Lazy parsing** - Only parse sections being modified
2. **Incremental updates** - Don't rewrite unchanged content
3. **Caching** - Cache parsed structure for multiple operations
4. **Batch operations** - Group related edits

## Usage Examples

### Simple Daily Flow
```python
# Morning: Add tasks
await add_task("Buy milk", "Shopping", "@sarah")
await add_task("Call plumber", "Home", "#urgent")

# During day: Check status
tasks = await find_tasks("person", "sarah")

# Complete tasks
await complete_task("milk", "Shopping")

# Evening: Review and organize
await organize_note_section("TODO: Shopping", "Today", "alphabetical")
await archive_completed_tasks("TODO: Shopping", older_than_days=7)
```

### Complex Scenarios
```python
# Bulk reassignment
tasks = await find_tasks("person", "mike")
for task in tasks:
    await modify_task(
        task["note"], 
        task["text"], 
        "update",
        metadata_changes={"assignee": "@sarah"}
    )

# Create weekly template
await task_script(
    "Generate weekly chore template",
    '''
    # Get template from recurring note
    template = get_note("TODO: Recurring")["content"]
    # Parse and instantiate for this week
    # ... script logic ...
    '''
)
```

## Migration Path

1. **Phase 1**: Implement core task tools (add, complete, find)
2. **Phase 2**: Add granular editing tools (update_section, modify_task)
3. **Phase 3**: Add organization tools (move, archive, organize)
4. **Phase 4**: Processing profiles and advanced features

## Success Metrics

- Task operations complete in < 1 second
- Zero data loss from concurrent edits
- 90% of operations use simple tools (no scripts)
- Natural conversation patterns maintained
- Family members can explain system to others

## Future Enhancements

1. **Task dependencies** - Link related tasks
2. **Recurring task automation** - Smart template expansion
3. **Context-aware suggestions** - Based on location/time
4. **Family analytics** - Task completion patterns (if desired)

## Conclusion

This tool design provides the right balance of simplicity and power for a family task management system. By avoiding both the "replace entire note" bluntness and the "generate Starlark for everything" complexity, we achieve a system that is both LLM-friendly and family-friendly.