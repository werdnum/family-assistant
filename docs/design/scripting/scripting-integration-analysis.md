# Scripting Language Integration Analysis for Family Assistant

## Executive Summary

This document analyzes where a scripting language could be integrated into the family-assistant codebase to enhance flexibility and user customization. The analysis identifies specific integration points, current code patterns that could benefit from scripting, and security considerations.

## Most Promising Integration Points

### 1. Event Listener Actions (Highest Value)

**Current State:**

- Event listeners currently only support `wake_llm` action type
- Actions are hardcoded in `EventProcessor._execute_action_in_context()` (lines 194-242)
- The design document mentions future action types: `tool_call`, `notification`, `script`

**Integration Opportunity:**

- Add a `script` action type that executes user-defined scripts
- Scripts could access event data and perform custom actions
- Would enable complex automation logic without waking the LLM

**Code Reference:**

```python

# src/family_assistant/events/processor.py:203-242
if action_type == "wake_llm":
    # Current implementation
elif action_type == "script":
    # Execute user script with event context
    script_result = await execute_script(
        listener.get("action_config", {}).get("script"),
        {"event": event_data, "listener": listener}
    )

```

**Security Boundary:**Scripts would need sandboxed access to:

- Event data (read-only)
- Tool execution via controlled API
- Database queries via safe repository pattern

### 2. Custom Tools via Scripting

**Current State:**

- Tools are Python functions with rigid structure
- Adding new tools requires code changes and deployment
- Tool definitions use JSON schema

**Integration Opportunity:**

- Allow users to define custom tools using scripts
- Scripts would have access to a safe subset of tool context
- Could enable domain-specific tools without modifying core code

**Code Reference:**

```python

# Potential script tool wrapper
class ScriptedTool:
    def __init__(self, name: str, script: str, schema: dict):
        self.name = name
        self.script = script
        self.schema = schema

    async def execute(self, exec_context: ToolExecutionContext, **kwargs):
        # Execute script with sandboxed context
        return await script_engine.execute(
            self.script,
            context={
                "args": kwargs,
                "db": SafeDatabaseAPI(exec_context.db_context),
                "chat": SafeChatAPI(exec_context.chat_interface),
            }
        )

```

### 3. Context Providers

**Current State:**

- Context providers aggregate information for LLM prompts
- Currently implemented as Python classes
- Limited to predefined providers

**Integration Opportunity:**

- Allow custom context providers via scripts
- Scripts could query APIs or transform data for context
- Would enable user-specific context without code changes

**Code Reference:**

```python

# src/family_assistant/processing.py:138-170
# Current context aggregation could support scripted providers
class ScriptedContextProvider(ContextProvider):
    async def get_context_fragments(self) -> list[str]:
        return await script_engine.execute(
            self.script,
            context={"db": self.safe_db_api}
        )

```

### 4. Data Transformation in Processing Pipeline

**Current State:**

- Document indexing pipeline has fixed processors
- LLM processes messages with static logic
- Limited customization of data flow

**Integration Opportunity:**

- Add scriptable processors to indexing pipeline
- Allow pre/post-processing hooks for LLM interactions
- Enable custom data transformations

**Code Reference:**

```python

# src/family_assistant/indexing/pipeline.py
# Could add ScriptProcessor to the pipeline
class ScriptProcessor(Processor):
    async def process(self, doc: Document) -> Document:
        # Execute user script to transform document
        return await script_engine.execute(
            self.script,
            context={"document": doc}
        )

```

### 5. Conditional Logic in Tools

**Current State:**

- Tools have fixed logic
- Complex conditions require LLM evaluation
- No user customization of tool behavior

**Integration Opportunity:**

- Allow scripts to define conditional tool behavior
- Scripts could validate inputs or modify outputs
- Would reduce LLM calls for simple logic

## Data Structures and APIs to Expose

### Safe Database API

```python
class SafeDatabaseAPI:
    """Read-only database access for scripts"""
    async def query_notes(self, limit: int = 10) -> list[dict]:
        # Safe wrapper around NotesRepository

    async def get_recent_events(self, hours: int = 24) -> list[dict]:
        # Safe wrapper around EventsRepository

    async def search_documents(self, query: str) -> list[dict]:
        # Safe wrapper around VectorRepository

```

### Safe Tool Execution API

```python
class SafeToolAPI:
    """Controlled tool execution for scripts"""
    async def execute_tool(self, name: str, args: dict) -> str:
        # Execute allowed tools with validation
        # Respect confirmation requirements
        # Apply rate limiting

```

### Event Context API

```python
class EventContextAPI:
    """Event data access for event scripts"""
    def get_event_data(self) -> dict:
        # Read-only event data

    def get_listener_config(self) -> dict:
        # Listener configuration

    async def emit_notification(self, message: str):
        # Send notification to user

```

## Security Requirements

1. **Sandboxed Execution**

   - No file system access
   - No network access (except via controlled APIs)
   - No process execution
   - Memory and CPU limits

2. **Capability-Based Security**

   - Scripts can only access explicitly provided APIs
   - No access to Python builtins like `exec`, `eval`, `__import__`
   - Limited to safe subset of standard library

3. **Rate Limiting**

   - Script execution time limits
   - API call rate limits
   - Resource usage tracking

4. **Audit Trail**

   - Log all script executions
   - Track API calls made by scripts
   - Store script errors for debugging

## Implementation Considerations

### Language Choice Options

1. **Lua**(via lupa or similar)

   - Pros: Designed for embedding, small, fast, battle-tested
   - Cons: Another language to learn

2. **Restricted Python**(via RestrictedPython)

   - Pros: Same language as codebase, familiar to users
   - Cons: Harder to secure properly

3. **JavaScript**(via pyduktape or Node.js subprocess)

   - Pros: Widely known, good sandboxing options
   - Cons: Heavier runtime, async complexity

4. **Domain-Specific Language**

   - Pros: Perfect security, tailored to use case
   - Cons: High implementation effort, learning curve

### Recommended Approach

1. Start with event listener scripts as proof of concept
2. Use Lua for simplicity and security
3. Provide minimal API surface initially
4. Expand based on user needs and security review

## Migration Path

1. **Phase 1**: Add `script` action type to event listeners
2. **Phase 2**: Implement safe APIs for database and notifications
3. **Phase 3**: Add script-based custom tools
4. **Phase 4**: Enable scripted context providers
5. **Phase 5**: Full scripting integration across system

## Conclusion

The event listener system presents the most immediate and high-value integration point for scripting. It has clear use cases, well-defined boundaries, and would provide significant user value without major architectural changes. Starting here would allow validating the scripting approach before expanding to other areas of the system.
