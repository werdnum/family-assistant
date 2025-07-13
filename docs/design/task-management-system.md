# Task Management System Design

## Overview

This document outlines the design considerations for implementing a task/to-do management system
within the family assistant. The system needs to handle various types of home-related tasks while
integrating seamlessly with the existing conversational assistant infrastructure.

## Requirements

### Task Types

1. **Chores/Recurring Tasks**

   - Daily, weekly, monthly, or seasonal recurrence
   - Flexible timing (no strict due dates)
   - Examples: clean cat box, change AC filter, birthday shopping

2. **Event-Preparation Tasks**

   - Tied to specific events
   - Become obsolete after the event
   - Examples: pack for trip, buy Christmas presents

3. **Deadline-Driven Tasks**

   - Strict due dates that persist even if missed
   - Examples: file taxes, renew licenses

### Task Properties

- **Assignees**: Some tasks have specific people responsible
- **Contexts**: When/where tasks should be performed
  - Time contexts: business hours, after kids' bedtime
  - Location contexts: at store, on way home from work
- **Hierarchy/Tags**: Loose organization and relationships
  - Example: "Call carpenter" → "Kitchen remodel" → "Before birthday deadline"

### Additional Requirements

1. **Dependencies**: Some tasks block others (e.g., patch walls before painting)
2. **Effort Tracking**: Quick tasks vs. multi-hour projects
3. **Seasonal/Weather Dependencies**: Outdoor tasks tied to conditions
4. **Resource/Budget Tracking**: Tasks requiring purchases or shared resources
5. **Documentation Storage**: Service records, warranty info, etc.

## Design Paradigms

### Paradigm 1: Pure Conversational Memory

**Concept**: Tasks exist only in conversational context and notes with no formal structure.

**Implementation**:

- Natural language storage in notes
- NLP extraction of task meaning
- Scheduled callbacks for reminders
- No formal task states

**Pros**:

- Zero new infrastructure
- Maximum flexibility
- Natural interaction

**Cons**:

- No structured queries
- Difficult to track completion
- Vulnerable to LLM interpretation errors

### Paradigm 2: Smart Markdown Lists

**Concept**: Tasks stored as markdown checklists with metadata conventions.

```markdown
# TODO: Home Maintenance
- [ ] Clean gutters @monthly @outdoor @mike
- [x] Replace AC filter @3months last:2024-01-15
- [ ] Call plumber #urgent @business-hours
```

**Implementation**:

- Markdown notes with special syntax
- Assistant parses checkbox states
- Tags for context and metadata
- Scripts for bulk operations

**Pros**:

- Human-readable
- Works offline
- Version control friendly

**Cons**:

- Limited to flat lists
- Parsing complexity
- Potential corruption from failed updates

### Paradigm 3: Event-Driven Task Engine

**Concept**: Tasks as events flowing through the system, triggering other events.

**Implementation**:

- Tasks as event listener configurations
- State changes trigger follow-ups
- Home Assistant for physical triggers
- Calendar integration for time triggers

**Pros**:

- Powerful automation
- Reactive system
- Natural integration with existing events

**Cons**:

- Complex chains hard to visualize
- Overkill for simple tasks
- Fragile if chain breaks

### Paradigm 4: Federated Task Sources

**Concept**: No central storage; assistant aggregates tasks from multiple sources.

**Sources**:

- Calendar events
- Email scanning
- Home sensors
- Recurring notes
- Weather conditions

**Pros**:

- No duplicate data entry
- Leverages existing information
- Always up-to-date

**Cons**:

- Can miss tasks not in sources
- Computationally expensive
- No single source of truth

### Paradigm 5: Collaborative Scripts + Notes

**Concept**: Structured data in notes with Starlark scripts for logic.

```json
{
  "tasks": [
    {
      "id": 1,
      "title": "Clean gutters",
      "recur": "monthly",
      "context": ["outdoor", "weekend"],
      "assignee": "mike"
    }
  ]
}
```

**Implementation**:

- JSON/YAML in special notes
- Starlark scripts for operations
- Natural language interface
- Scripts for reports and automation

**Pros**:

- Balance of structure and flexibility
- Extensible via scripts
- Fallback to direct data access

**Cons**:

- More complex data model
- Scripts need maintenance
- Still vulnerable to parsing errors

### Paradigm 6: Dedicated Task Entities

**Concept**: Tasks as first-class database entities with structured storage.

**Schema**:

```python
class Task:
    id: UUID
    title: str
    description: Optional[str]
    status: Enum["pending", "in_progress", "completed", "cancelled"]
    due_date: Optional[datetime]
    recurrence: Optional[RecurrenceRule]
    assignee: Optional[str]
    tags: List[str]
    context_triggers: List[ContextRule]
    parent_task: Optional[UUID]
    created_at: datetime
    completed_at: Optional[datetime]
```

**Implementation**:

- PostgreSQL tables
- RESTful API endpoints
- LLM translates natural language to API calls
- Web UI fallback for direct manipulation

**Pros**:

- Bulletproof storage
- Complex queries possible
- Complete audit trail
- LLM-failure resistant
- Proper multi-user support

**Cons**:

- Requires schema design
- More code to maintain
- Less flexible than pure notes
- Higher implementation effort

## Robustness Analysis

### LLM Failure Impact by Paradigm

| Paradigm        | Failure Mode       | Impact               | Recovery               |
| --------------- | ------------------ | -------------------- | ---------------------- |
| Conversational  | Misinterpretation  | Tasks lost/corrupted | Manual note editing    |
| Markdown        | Parse errors       | List corruption      | Direct file editing    |
| Event-Driven    | Chain breaks       | Automation fails     | Manual intervention    |
| Federated       | Aggregation fails  | Tasks invisible      | Check sources directly |
| Scripts + Notes | Script errors      | Operations fail      | Direct data access     |
| **Entities**    | **API call fails** | **No data loss**     | **Web UI fallback**    |

### Key Robustness Considerations

1. **Data Persistence**: Only dedicated entities guarantee data survives LLM failures
2. **Graceful Degradation**: Systems should provide fallback interfaces
3. **Audit Trail**: Important for understanding what went wrong
4. **Idempotency**: Operations should be safe to retry

## Recommended Approach: Hybrid Entity System

### Core Components

1. **Database Layer**

   - Task entities in PostgreSQL
   - Proper constraints and relationships
   - Migration support for schema evolution

2. **API Layer**

   - RESTful endpoints for CRUD operations
   - Batch operations for efficiency
   - Webhook support for integrations

3. **Conversational Interface**

   - Natural language to API translation
   - Context-aware suggestions
   - Graceful error handling

4. **Web UI Fallback**

   - Direct task manipulation
   - Bulk operations
   - Visual task hierarchies

5. **Integration Points**

   - Calendar sync
   - Home Assistant triggers
   - Note references
   - Script automation

### Implementation Phases

**Phase 1: Core Task Storage**

- Basic task entity model
- CRUD API endpoints
- Simple web UI

**Phase 2: Conversational Interface**

- Natural language processing
- Context extraction
- Basic reminders

**Phase 3: Advanced Features**

- Recurrence patterns
- Dependencies
- Location/time contexts
- Family sharing

**Phase 4: Automation**

- Event listener integration
- Script-based workflows
- Predictive suggestions

## Conclusion

The dedicated entity approach with conversational interface provides the best balance of:

- **Robustness**: Data persists regardless of LLM performance
- **Flexibility**: Natural language interaction when possible
- **Functionality**: Complex features like dependencies and contexts
- **Integration**: Works with existing assistant features

This design ensures the task management system enhances the family assistant without introducing
fragility or complexity that could compromise the user experience.

## Critical Feedback (via Gemini 2.5 Pro)

After reviewing this design in the context of a 2-person household with a conversational-first
assistant, several critical observations emerged:

### Core Critique: Over-Engineering

The original design is "a masterclass in Enterprise thinking" when it should embrace "Family
thinking." The fundamental issue is solving for problems that don't exist in a 2-person household:

1. **Data Loss Paranoia**: For a family to-do list, occasional LLM misinterpretation is an
   annoyance, not catastrophic. The cost of preventing this with databases, APIs, and UI fallbacks
   is disproportionate.

2. **Rigid Schema Obsession**: Family tasks are fluid. A task might be "in progress" for weeks or
   "blocked" for fuzzy reasons. Rigid enums create friction where simple checkboxes suffice.

3. **Unnecessary Abstraction**: A RESTful API between the LLM and data adds complexity when the only
   "client" is the assistant itself. Direct function calls or scripts would be simpler.

### The Missing Metaphor: "Shopping List on the Fridge"

The most powerful, simple model was overlooked. A physical shopping list works because:

- It's always visible and accessible
- Anyone can add to it or cross things off
- No training required
- If it gets messy, you just rewrite it
- It's inherently shared

The digital equivalent: A markdown note that's easy to read/edit with natural language commands.

### Enterprise vs. Family Thinking

| Feature        | Enterprise (Original Design)   | Family (Better Approach)       |
| -------------- | ------------------------------ | ------------------------------ |
| Data Store     | PostgreSQL for ACID compliance | Folder of markdown files       |
| Data Integrity | Foreign keys, rigid schema     | "It's just text" - flexible    |
| Failure Mode   | API fails, data safe in DB     | Script fails, user gets alert  |
| Interface      | RESTful API                    | Collection of internal scripts |
| User Fallback  | Dedicated Web UI               | "Just open the text file"      |

### Key Insights

1. **Paradigm 6 (Dedicated Entities) is overkill** for this use case
2. **Federated Sources (Paradigm 4)** is too magical - users need to understand where tasks come
   from
3. **Best approach**: Creatively merge Paradigms 2 (Smart Markdown), 3 (Event-Driven), and 5
   (Scripts + Notes)
4. **Leverage existing features** instead of building new infrastructure
5. **Start ultra-simple** and grow organically based on actual needs

## Revised Design: The "Shopping List" Approach

Based on the feedback, here's a radically simplified design that embraces the conversational nature
of the system while maintaining practical functionality.

### Core Philosophy

- **Text files over databases**
- **Scripts over APIs**
- **Conventions over configuration**
- **Graceful degradation over bulletproof systems**
- **Flexibility over rigid schemas**
- **Incremental complexity over upfront design**

### What is a Task?

A task is not a new entity type. It's simply:

1. **For simple tasks**: A line in a shared markdown checklist
2. **For complex tasks**: A note with special structure and metadata

### Implementation Architecture

#### Storage Layer: Smart Notes

**Simple Tasks** - stored in `Family TODO.md`:

```markdown
# Family TODO

## Urgent
- [ ] Call plumber about leak @mike
- [ ] File taxes by April 15

## Home Maintenance  
- [ ] Clean gutters @monthly @outdoor
- [x] Replace AC filter @3months last:2024-01-15

## Shopping
- [ ] Milk
- [ ] Birthday present for Jamie
```

**Complex Tasks** - individual note files in `tasks/` folder:

```yaml
---
title: Kitchen Remodel Planning
status: in-progress
assignee: both
deadline: 2024-08-01
tags: [home-improvement, major-project]
---

## Current Status
Waiting for contractor quotes

## Subtasks
- [x] Measure kitchen dimensions
- [x] Create wishlist of features
- [ ] Get 3 contractor quotes
- [ ] Choose contractor
- [ ] Schedule work

## Notes
- Budget: $15,000-20,000
- Must be done before Jamie's birthday party
```

#### Logic Layer: Starlark Scripts

Instead of APIs, use scripts that directly manipulate notes:

- `task_add.star` - Parses natural language, adds to appropriate list/note
- `task_complete.star` - Marks tasks complete, logs completion
- `task_list.star` - Queries tasks by various criteria
- `task_remind.star` - Checks for due tasks and sends reminders

#### Automation Layer: Event Listeners

Leverage existing event system for all automation:

**Recurring Tasks**:

```python
# Event listener configuration
{
    "name": "ac_filter_reminder",
    "type": "scheduled",
    "schedule": "0 9 1 */3 *",  # First day of every 3rd month at 9am
    "action": "script",
    "script": "task_remind.star",
    "args": {"task_pattern": "AC filter"}
}
```

**Contextual Reminders**:

```python
# Triggered when user arrives at store
{
    "name": "shopping_reminder",
    "type": "state_changed",
    "entity_id": "zone.grocery_store",
    "condition": "person.user in zone.grocery_store",
    "action": "script", 
    "script": "task_context_remind.star",
    "args": {"tag": "shopping"}
}
```

#### Conversational Interface

The LLM translates natural language into script calls:

```
User: "Add buy milk to the shopping list"
Assistant: [Calls task_add.star with {list: "shopping", task: "buy milk"}]
"Added to shopping list"

User: "What do I need to do today?"  
Assistant: [Calls task_list.star with {filter: "today"}]
"Here's what's on your list for today:
- Call plumber about leak (urgent)
- Buy milk (when you're at the store)"

User: "Remind me to pack when I get home"
Assistant: [Creates event listener for zone.home + calls task_add.star]
"I'll remind you to pack when you get home"
```

### Incremental Implementation Path

#### Phase 1: The Shared List (Day 1)

- Create `Family TODO.md`
- Add two LLM tools: `append_to_todo` and `read_todo`
- That's it - immediate value, zero infrastructure

#### Phase 2: Smart Parsing (Week 1)

- Add `task_add.star` script that understands tags like @person, #urgent
  - Script maps list names to markdown sections (e.g., "shopping" → "## Shopping" header)
  - Tasks without specified list go to a default section
- Add `task_complete.star` for marking done
- Simple search/filter in `task_list.star`

#### Phase 3: Basic Automation (Week 2)

- Daily scheduled script that reads TODO.md
- Sends morning summary via Telegram
- Checks for items marked @today or overdue deadlines

#### Phase 4: Context & Recurrence (Week 3-4)

- Event listeners for location/time contexts
- Simple recurrence: When a recurring task is marked complete, create the next instance
  - Calculate next due date from the completion date (for floating recurrence)
  - Or from the original due date (for fixed recurrence)
  - Store recurrence pattern in task metadata (e.g., @every_3_months)
- Individual task notes for complex items

#### Phase 5+: Organic Growth

- Add features only as family actually needs them
- Each addition should be a small script or note convention
- Never add a database table when a text file would work

### Handling Edge Cases

**Concurrent Edits**:

- For single-node deployment: Use file locking in scripts (`flock`)
- For Kubernetes/distributed environment: 
  - Option 1: Ensure scripts run on single pod (e.g., leader election)
  - Option 2: Use atomic file operations (write to temp file, then rename)
  - Option 3: For high concurrency, consider Redis/etcd for distributed locking
- For 2-person household, collisions are rare
- Worst case: "Oops, please tell me again"

**Parsing Errors**:

```python
try:
    task = parse_task_note(content)
except ParseError:
    return "I couldn't understand that task note. Can you check the formatting?"
```

**Finding Tasks**:

- Use existing vector search on note contents
- "Find that thing about the kitchen" works naturally

### Why This Approach Wins

1. **Zero New Infrastructure**: Uses only existing features
2. **Human-Readable**: Family members can directly edit TODO.md if needed
3. **Graceful Degradation**: If LLM fails, it's still just a text file
4. **Conversational**: No rigid schemas to map language onto
5. **Incremental**: Each phase delivers value independently
6. **Maintainable**: New features are just new scripts, not schema migrations

### Success Metrics

Instead of "data integrity" or "query performance", measure:

- Can both family members use it without training?
- Does it reduce mental load around household tasks?
- Is it still working smoothly after 6 months?
- Can tasks be added/completed in under 10 seconds?

This revised design embraces the assistant's conversational nature while providing practical task
management for a real family's needs.
