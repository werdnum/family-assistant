# ToolsProvider Refactoring Plan

## Phase 1: Minimal Fix for Tools UI Visibility (Current Focus)

### Problem
The tools UI at `/tools` only shows tools available to the default profile because it gets its tools provider from `app.state.processing_service.tools_provider`.

### Minimal Solution
Create a single root ToolsProvider with ALL tools, then use FilteredToolsProvider to create profile-specific views for ProcessingService instances.

### Implementation Steps

1. **Create FilteredToolsProvider class** in `tools/infrastructure.py`:
   ```python
   class FilteredToolsProvider(ToolsProvider):
       """Provides a filtered view of another ToolsProvider based on allowed tool names."""
       
       def __init__(self, wrapped_provider: ToolsProvider, allowed_tool_names: set[str] | None):
           """
           Args:
               wrapped_provider: The provider to filter
               allowed_tool_names: Set of allowed tool names. If None, all tools are allowed.
           """
           self._wrapped_provider = wrapped_provider
           self._allowed_tool_names = allowed_tool_names
           self._filtered_definitions: list[dict[str, Any]] | None = None
       
       async def get_tool_definitions(self) -> list[dict[str, Any]]:
           if self._filtered_definitions is None:
               all_definitions = await self._wrapped_provider.get_tool_definitions()
               if self._allowed_tool_names is None:
                   self._filtered_definitions = all_definitions
               else:
                   self._filtered_definitions = [
                       d for d in all_definitions 
                       if d.get("function", {}).get("name") in self._allowed_tool_names
                   ]
           return self._filtered_definitions
       
       async def execute_tool(self, name: str, arguments: dict[str, Any], context: ToolExecutionContext) -> str:
           if self._allowed_tool_names is not None and name not in self._allowed_tool_names:
               raise ToolNotFoundError(f"Tool '{name}' is not available in this profile")
           return await self._wrapped_provider.execute_tool(name, arguments, context)
       
       async def close(self) -> None:
           # Don't close the wrapped provider - it's shared
           pass
   ```

2. **In Assistant.setup_dependencies()**, create a single root provider:
   ```python
   # Create root providers with ALL tools (before profile loop)
   root_local_provider = LocalToolsProvider(
       definitions=base_local_tools_definition,  # ALL local tools
       implementations=local_tool_implementations,  # ALL implementations
       embedding_generator=self.embedding_generator,
       calendar_config=None,  # Will be handled per-context
   )
   
   all_mcp_servers = self.config.get("mcp_config", {}).get("mcpServers", {})
   root_mcp_provider = MCPToolsProvider(
       mcp_server_configs=all_mcp_servers,
       initialization_timeout_seconds=60,
   )
   
   self.root_tools_provider = CompositeToolsProvider(
       providers=[root_local_provider, root_mcp_provider]
   )
   
   # Store for UI/API access
   fastapi_app.state.tools_provider = self.root_tools_provider
   fastapi_app.state.tool_definitions = await self.root_tools_provider.get_tool_definitions()
   ```

3. **Update profile creation** to use FilteredToolsProvider:
   ```python
   # In the profile loop, replace provider creation with:
   local_tools_list = profile_tools_conf_dict.get("enable_local_tools")
   
   # Build set of enabled tools
   if local_tools_list is None:
       # All tools enabled
       enabled_tool_names = None
   else:
       enabled_tool_names = set(local_tools_list)
       # TODO: Add MCP tool filtering when we can identify tools by server
   
   # Create filtered view
   filtered_provider = FilteredToolsProvider(
       wrapped_provider=self.root_tools_provider,
       allowed_tool_names=enabled_tool_names
   )
   
   # Wrap with confirming provider as before
   confirming_provider = ConfirmingToolsProvider(
       wrapped_provider=filtered_provider,
       tools_requiring_confirmation=profile_confirm_tools_set,
   )
   
   # Use confirming_provider in ProcessingService
   ```

### Benefits
- **Clean architecture**: Single source of truth for all tools
- **Efficient**: No duplicate tool instances or MCP connections
- **Profile isolation**: Each profile only sees its allowed tools
- **UI gets all tools**: Tools UI shows everything available

### Remaining Issues (for Phase 2)
- Tools executed via API still won't have ProcessingService dependencies
- Calendar config needs special handling
- ~~MCP tool filtering by server ID not yet implemented~~ ✅ IMPLEMENTED

---

## Phase 2: Comprehensive Refactoring (Future Work)

### Current Architecture Issues

The current implementation creates separate ToolsProvider instances for each ProcessingService profile:
- Each profile creates its own LocalToolsProvider with filtered tools based on `enable_local_tools`
- Each profile creates its own MCPToolsProvider with filtered servers based on `enable_mcp_server_ids`
- These are wrapped in CompositeToolsProvider and ConfirmingToolsProvider
- The tools UI gets the tools from the default ProcessingService's provider
- This means the tools UI only sees the filtered tools from the default profile

### Problems to Address

1. **Law of Demeter violation**: Tools UI accesses tools through `app.state.processing_service.tools_provider`
2. **Incomplete tool visibility**: Tools UI only sees tools available to the default profile
3. **Redundant tool instances**: Each profile creates its own copy of the same tools
4. **Profile restrictions leak**: Tool filtering meant for LLM safety affects the UI
5. **Service dependency coupling**: Tools depend on ProcessingService for:
   - `processing_service.tools_provider` (circular dependency in execute_script)
   - `processing_service.app_config` (documents.py needs storage path)
   - `processing_service.processing_services_registry` (services.py)
   - `processing_service.home_assistant_client` (home_assistant.py)
6. **Broken tools in API**: Tools executed via `/api/tools/execute/{tool_name}` can't access these dependencies
7. **ToolExecutionContext overloading**: Context carries both execution info AND service dependencies

### Proposed Architecture

#### Core Principles

1. **Single root ToolsProvider** with all available tools
2. **ProcessingService instances** get filtered views based on their profile's `enable_local_tools`
3. **Tools UI/API** gets direct access to the root provider (no filtering)
4. **Profile restrictions** only apply to LLM calls through ProcessingService

### Implementation Design

#### 1. Create ServiceContainer for Shared Dependencies

Create a new class to hold service dependencies that tools need:

```python
@dataclass
class ServiceContainer:
    """Container for services that tools may need access to."""
    app_config: dict[str, Any]
    embedding_generator: EmbeddingGenerator | None
    clock: Clock
    home_assistant_client: Any | None
    processing_services_registry: dict[str, ProcessingService] | None
    indexing_source: IndexingSource | None
    # Add calendar_config here or keep it per-profile?
```

This will be stored in FastAPI state and passed to ToolExecutionContext.

#### 2. Update ToolExecutionContext

Add service_container field:

```python
@dataclass
class ToolExecutionContext:
    # ... existing fields ...
    service_container: ServiceContainer | None = None
    # Remove individual service fields (processing_service, embedding_generator, etc.)
```

#### 3. Create FilteredToolsProvider Wrapper Class

```python
class FilteredToolsProvider(ToolsProvider):
    """Provides a filtered view of another ToolsProvider based on allowed tool names."""
    
    def __init__(self, wrapped_provider: ToolsProvider, allowed_tool_names: set[str] | None):
        """
        Args:
            wrapped_provider: The provider to filter
            allowed_tool_names: Set of allowed tool names. If None, all tools are allowed.
        """
        self._wrapped_provider = wrapped_provider
        self._allowed_tool_names = allowed_tool_names
        self._filtered_definitions: list[dict[str, Any]] | None = None
    
    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        if self._filtered_definitions is None:
            all_definitions = await self._wrapped_provider.get_tool_definitions()
            if self._allowed_tool_names is None:
                # No filtering - return all tools
                self._filtered_definitions = all_definitions
            else:
                # Filter to only allowed tools
                self._filtered_definitions = [
                    d for d in all_definitions 
                    if d.get("function", {}).get("name") in self._allowed_tool_names
                ]
        return self._filtered_definitions
    
    async def execute_tool(self, name: str, arguments: dict[str, Any], context: ToolExecutionContext) -> str:
        if self._allowed_tool_names is not None and name not in self._allowed_tool_names:
            raise ToolNotFoundError(f"Tool '{name}' is not available in this profile")
        return await self._wrapped_provider.execute_tool(name, arguments, context)
    
    async def close(self) -> None:
        # Don't close the wrapped provider - it's shared
        pass
```

#### 4. Update Tool Implementations

Tools need to be updated to use ServiceContainer instead of ProcessingService:

```python
# execute_script.py
if exec_context.service_container:
    # Get root tools provider from service container
    tools_provider = exec_context.service_container.root_tools_provider
    
# documents.py  
if exec_context.service_container:
    document_storage_path_str = exec_context.service_container.app_config.get(
        "document_storage_path"
    )

# services.py
if exec_context.service_container and exec_context.service_container.processing_services_registry:
    registry = exec_context.service_container.processing_services_registry

# home_assistant.py
if exec_context.service_container and exec_context.service_container.home_assistant_client:
    ha_client = exec_context.service_container.home_assistant_client
```

#### 5. Refactor Assistant.setup_dependencies()

Changes needed:

1. **Create single root providers early**:
   ```python
   # After embedding generator setup
   # Create root providers with ALL tools
   root_local_provider = LocalToolsProvider(
       definitions=base_local_tools_definition,  # ALL tools
       implementations=local_tool_implementations,  # ALL implementations
       embedding_generator=self.embedding_generator,
       calendar_config=None,  # Will be set per-profile during execution
   )
   
   # Create root MCP provider with ALL configured servers
   all_mcp_servers_config = self.config.get("mcp_config", {}).get("mcpServers", {})
   root_mcp_provider = MCPToolsProvider(
       mcp_server_configs=all_mcp_servers_config,
       initialization_timeout_seconds=60,
   )
   
   # Create root composite provider
   self.root_tools_provider = CompositeToolsProvider(
       providers=[root_local_provider, root_mcp_provider]
   )
   
   # Initialize the root provider
   await self.root_tools_provider.get_tool_definitions()
   ```

2. **Create and store ServiceContainer**:
   ```python
   # Create service container with shared dependencies
   self.service_container = ServiceContainer(
       app_config=self.config,
       embedding_generator=self.embedding_generator,
       clock=self.clock,
       home_assistant_client=self.home_assistant_client,
       processing_services_registry=None,  # Will be set after creating all services
       indexing_source=self.indexing_source,
       root_tools_provider=self.root_tools_provider,  # Add this!
   )
   
   # Store in FastAPI state
   fastapi_app.state.tools_provider = self.root_tools_provider
   fastapi_app.state.tool_definitions = await self.root_tools_provider.get_tool_definitions()
   fastapi_app.state.service_container = self.service_container
   ```

3. **Create filtered providers for each profile**:
   ```python
   # In the profile loop
   # Get enabled tools for this profile
   local_tools_list_from_config = profile_tools_conf_dict.get("enable_local_tools")
   mcp_server_ids_from_config = profile_tools_conf_dict.get("enable_mcp_server_ids")
   
   # Build set of all enabled tool names for this profile
   enabled_tool_names = set()
   
   # Add local tools
   if local_tools_list_from_config is None:
       # All local tools enabled
       enabled_tool_names.update(local_tool_implementations.keys())
   else:
       enabled_tool_names.update(local_tools_list_from_config)
   
   # Add MCP tools (need to get their names from definitions)
   if mcp_server_ids_from_config is not None:
       # Filter MCP tools by server ID
       all_tool_defs = await self.root_tools_provider.get_tool_definitions()
       for tool_def in all_tool_defs:
           tool_name = tool_def.get("function", {}).get("name", "")
           # Check if this is an MCP tool from an enabled server
           # (This requires MCPToolsProvider to expose which server each tool comes from)
           # For now, include all MCP tools if any servers are enabled
           if tool_name not in local_tool_implementations:  # It's an MCP tool
               enabled_tool_names.add(tool_name)
   
   # Create filtered provider
   filtered_provider = FilteredToolsProvider(
       wrapped_provider=self.root_tools_provider,
       allowed_tool_names=enabled_tool_names if local_tools_list_from_config is not None or mcp_server_ids_from_config is not None else None
   )
   
   # Wrap with confirming provider as before
   confirming_provider_for_profile = ConfirmingToolsProvider(
       wrapped_provider=filtered_provider,
       tools_requiring_confirmation=profile_confirm_tools_set,
   )
   ```

4. **Update the delegate_to_service tool description**:
   - Move this logic before creating the root provider
   - Apply it to the base_local_tools_definition before creating root_local_provider

#### 3. Update LocalToolsProvider

The LocalToolsProvider needs a small change to handle calendar_config being None at initialization:

```python
# In LocalToolsProvider.execute_tool()
if needs_calendar_config:
    # Try to get calendar_config from context first
    calendar_config = getattr(context, 'calendar_config', None) or self._calendar_config
    if calendar_config:
        call_args["calendar_config"] = calendar_config
    else:
        logger.error(...)
```

#### 6. Update ProcessingService

When creating ToolExecutionContext, pass the service container:

```python
# In processing.py
tool_execution_context = ToolExecutionContext(
    interface_type=interface_type,
    conversation_id=conversation_id,
    user_name=user_name,
    turn_id=turn_id,
    db_context=db_context,
    chat_interface=chat_interface,
    timezone_str=self.timezone_str,
    request_confirmation_callback=request_confirmation_callback,
    service_container=self.service_container,  # Pass the container
    # Remove: processing_service=self, clock=self.clock, etc.
)
```

ProcessingService needs to receive service_container in its constructor.

#### 7. Update tools_api.py

Pass service container when creating execution context:

```python
# Get service container from app state
service_container = getattr(request.app.state, "service_container", None)

execution_context = ToolExecutionContext(
    interface_type="api",
    conversation_id=f"api_call_{uuid.uuid4()}",
    user_name="APIUser",
    turn_id=f"api_turn_{uuid.uuid4()}",
    db_context=db_context,
    chat_interface=None,
    timezone_str=timezone_str,
    request_confirmation_callback=None,
    service_container=service_container,  # Now tools will work!
)
```

#### 8. Update tools_ui.py

No changes needed! It already accesses `request.app.state.tools_provider` and `request.app.state.tool_definitions`, which will now contain the root provider with all tools.

#### 9. Update TaskWorker

Similar to tools_api.py, pass service container:

```python
# In task_worker.py
exec_context = ToolExecutionContext(
    interface_type=final_interface_type,
    conversation_id=final_conversation_id,
    user_name="TaskWorkerUser",
    turn_id=str(uuid.uuid4()),
    db_context=db_context,
    chat_interface=self.chat_interface,
    timezone_str=self.timezone_str,
    service_container=self.service_container,  # From constructor
)
```

### Migration Strategy

1. **Backward Compatibility**: The changes are mostly internal and maintain the same external interfaces
2. **Testing**: Existing tests should continue to work with minimal changes
3. **Rollback**: Easy to revert by keeping the old per-profile provider creation logic

### Benefits

1. **Clean Architecture**: Single source of truth for all tools
2. **Proper Separation**: UI sees all tools, LLM access is properly restricted
3. **Performance**: Single initialization of MCP connections instead of per-profile
4. **Maintainability**: Clearer code flow and responsibilities

### Implementation Order

1. Create ServiceContainer class in `tools/types.py`
2. Update ToolExecutionContext to use ServiceContainer
3. Create FilteredToolsProvider class in `tools/infrastructure.py`
4. Update Assistant.setup_dependencies() to create root providers and ServiceContainer
5. Update all tool implementations to use ServiceContainer
6. Update ProcessingService, tools_api.py, and TaskWorker to pass ServiceContainer
7. Test with existing profiles to ensure filtering works correctly
8. Verify tools UI shows all available tools
9. Verify tools work via API endpoint

### Potential Issues

1. **MCP Tool Filtering**: Currently, we can't easily filter MCP tools by server ID. We might need to enhance MCPToolsProvider to track which server provides each tool.
2. **Calendar Config**: Tools that need calendar_config will need to get it from the execution context rather than provider initialization. This is profile-specific so can't go in ServiceContainer.
3. **Memory**: Keeping a single root provider means all tool definitions are kept in memory even if not used by any profile.
4. **Circular Dependency**: execute_script.py needs access to root_tools_provider, which we'll add to ServiceContainer to break the cycle.
5. **Backward Compatibility**: Need to maintain old ToolExecutionContext fields during transition.

### Tool Updates Required

Based on analysis of current usage:

1. **execute_script.py**: 
   - Currently: `exec_context.processing_service.tools_provider`
   - Update to: `exec_context.service_container.root_tools_provider`

2. **documents.py**:
   - Currently: `exec_context.processing_service.app_config.get("document_storage_path")`
   - Update to: `exec_context.service_container.app_config.get("document_storage_path")`

3. **services.py**:
   - Currently: `exec_context.processing_service.processing_services_registry`
   - Update to: `exec_context.service_container.processing_services_registry`

4. **home_assistant.py**:
   - Currently: `exec_context.home_assistant_client`
   - Update to: `exec_context.service_container.home_assistant_client`

5. **tasks.py**:
   - Currently: `exec_context.clock or SystemClock()`
   - Update to: `exec_context.service_container.clock if exec_context.service_container else SystemClock()`

---

## Summary

**Phase 1 (Current Focus)**: Create a separate root ToolsProvider with all tools for the UI/API to fix the immediate visibility issue with minimal code changes.

**Phase 2 (Future Work)**: Comprehensive refactoring using ServiceContainer pattern to properly separate service dependencies from profile restrictions and ensure all tools work from all entry points.

