# Processing Service Profiles Design

## 1. Introduction

This document outlines the design for supporting multiple "Processing Service Profiles" within the
Family Assistant application. The goal is to allow different instances of the `ProcessingService` to
operate with distinct configurations, particularly regarding available tools, system prompts, and
tool confirmation policies. This enables better security segmentation, reduces cognitive load on the
LLM for general tasks, and allows for specialized assistant behaviors.

The primary motivation is to isolate sensitive or complex tools (e.g., web browsing, calendar
modifications) into specific profiles. The main assistant profile can then delegate tasks to these
specialized profiles, or users might invoke them directly.

## 2. Core Concepts

- **Multiple `ProcessingService` Instances**: The system will instantiate multiple
  `ProcessingService` objects. Each instance will be a standard `ProcessingService` but configured
  differently.

- **Configuration-Driven Differentiation**: Differences between service profiles (toolsets, prompts,
  history length, etc.) will be defined via configuration (`config.yaml`) rather than through
  specialized Python subclasses of `ProcessingService`. This promotes code reusability and
  flexibility.

- **Profile-Specific `ToolsProvider`**: Each `ProcessingService` instance will have its own
  `ToolsProvider` stack (e.g., `LocalToolsProvider`, `MCPToolsProvider`, `CompositeToolsProvider`,
  `ConfirmingToolsProvider`) configured according to its profile's specifications.

- **Profile-Specific LLM Configuration**: Each `ProcessingService` instance can be configured with a
  specific LLM model. This allows, for example, using a powerful reasoning model for complex tasks
  and a more cost-effective model for simpler tasks or large-volume text processing.

- **Service Registry**: Instantiated `ProcessingService` objects will be stored in a central
  registry (e.g., a dictionary in the application state), keyed by their unique profile ID, allowing
  them to be looked up and invoked.

## 3. Configuration (`config.yaml`)

A new structure will be introduced in `config.yaml` to manage these profiles.

### 3.1. `default_profile_settings`

A top-level key `default_profile_settings` will define the baseline configuration. This section will
contain subsections for `processing_config` and `tools_config`, mirroring the structure of an
individual profile's settings.

```yaml
# --- Default Service Profile Configuration ---
default_profile_settings:
  processing_config:
    prompts: # Loaded from prompts.yaml by default, or inline here
      system_prompt: "You are a helpful assistant. Current time is {current_time}."
      # ... other default prompt keys ...
    calendar_config:
      # ... default calendar_config ...
    timezone: "UTC"
    max_history_messages: 5
    history_max_age_hours: 24
    llm_model: "claude-3-haiku-20240307" # Default LLM model
    delegation_security_level: "confirm" # Default delegation security level: "blocked", "confirm", "unrestricted"
  tools_config:
    enable_local_tools: # Default set of local tools
      - "add_or_update_note"
      # ... other default local tools ...
    enable_mcp_server_ids: # Default set of MCP servers
      - "time_server_1"
      # ... other default MCP servers ...
    confirm_tools: # Default list of tools requiring confirmation
      - "modify_calendar_event"
      # ... other tools ...
```

### 3.2. `service_profiles`

A top-level list `service_profiles` will define individual profiles. Each profile object will have:

- `id` (string): A unique identifier for the profile.
- `description` (string, optional): A human-readable description.
- `processing_config` (object, optional): Overrides for `ProcessingServiceConfig` settings. This
  includes `delegation_security_level` and `include_system_docs`.
- `tools_config` (object, optional): Configuration for the toolset available to this profile.

### 3.2.0. `include_system_docs`

Each profile's `processing_config` can specify an `include_system_docs` list to automatically load
user documentation files from `docs/user/` and append them to the system prompt:

```yaml
processing_config:
  include_system_docs:
    - "USER_GUIDE.md"
    - "scripting.md"
  prompts:
    system_prompt: "You are a helpful assistant."
```

The specified documentation files will be loaded at profile initialization and appended to the
system prompt with markdown headers separating each document. This is useful for providing
context-specific documentation to profiles without having to manually copy content or use the
`get_user_documentation_content` tool.

**Security**: The same path validation and security checks from
`get_user_documentation_content_tool` are applied. Files must be in `docs/user/` (or `DOCS_USER_DIR`
env var), have `.md` or `.txt` extensions, and cannot use directory traversal (`..`).

### 3.2.1. `delegation_security_level`

Each profile's `processing_config` can specify a `delegation_security_level` with one of three
values:

- `"blocked"`: This profile cannot be targeted by the `delegate_to_service` tool. Any attempt to
  delegate to it will be rejected.
- `"confirm"`: Delegation to this profile *always* requires user confirmation. This overrides the
  `confirm_delegation: false` argument if passed to the `delegate_to_service` tool.
- `"unrestricted"`: Delegation to this profile is allowed. The `confirm_delegation` argument of the
  `delegate_to_service` tool will be respected.

If not specified for a profile, it inherits from
`default_profile_settings.processing_config.delegation_security_level`.

```yaml
# --- Service Profiles ---
service_profiles:
  - id: "default_assistant"
    description: "Main assistant using default settings."
    # This profile implicitly uses all settings from 'default_profile_settings'
    # as no specific 'processing_config' or 'tools_config' is defined here.

  - id: "focused_assistant"
    description: "Assistant with a specific system prompt and fewer tools."
    processing_config:
      prompts: # MERGES with default_profile_settings.processing_config.prompts
        system_prompt: "You are a focused assistant. Current time is {current_time}."
      max_history_messages: 3 # REPLACES default
      llm_model: "gpt-4-turbo" # REPLACES default, uses a more powerful model
      delegation_security_level: "unrestricted" # This profile allows unrestricted delegation
    tools_config: # REPLACES default_profile_settings.tools_config entirely for this profile
      enable_local_tools:
        - "add_or_update_note"
      enable_mcp_server_ids: []
      confirm_tools: []
    slash_commands: # List of slash commands that trigger this profile
      - "/focus"
      - "/ask_focused"
```

### 3.3. Configuration Merging Strategy

When loading a profile:

1. Start with a deep copy of `default_profile_settings`.
2. For each key in the profile's `processing_config` or `tools_config`:
   - **Dictionaries** (e.g., `prompts`, `calendar_config`): Perform a deep merge. Keys from the
     profile's dictionary will overwrite keys in the default dictionary. New keys in the profile's
     dictionary will be added.
   - **Lists** (e.g., `enable_local_tools`, `confirm_tools`): The profile's list will *replace* the
     default list entirely.
   - **Scalar Values** (e.g., `timezone`, `max_history_messages`): The profile's value will
     *replace* the default value.

### 3.4. `default_service_profile_id`

An optional top-level key `default_service_profile_id` (e.g., `"default_assistant"`) will specify
which profile to use for interactions that don't explicitly target a specific profile (e.g., a
direct message to the Telegram bot without a special command).

## 4. Application Initialization

The main application startup logic (`src/family_assistant/__main__.py`) will be updated to:

1. Load `default_profile_settings` and the list of `service_profiles` from `config.yaml`.
2. For each profile definition in `service_profiles`:
   - Construct its final configuration by merging its settings with `default_profile_settings`
     according to the strategy above.
   - Create a `ProcessingServiceConfig` object from the resolved processing settings.
   - Create an `LLMInterface` instance (e.g., `LiteLLMClient`) configured with the profile's
     specified `llm_model` (and any other relevant LLM parameters from the configuration).
   - Build a `ToolsProvider` stack:
     - `LocalToolsProvider` configured with only the `enable_local_tools` specified for the profile.
     - `MCPToolsProvider` configured with only the `enable_mcp_server_ids` specified for the
       profile.
     - `CompositeToolsProvider` to combine these.
     - `ConfirmingToolsProvider` wrapping the composite, using the profile's `confirm_tools` list.
   - Instantiate a `ProcessingService` with the profile-specific `ProcessingServiceConfig`, the
     profile-specific `LLMInterface`, and the `ToolsProvider` stack.
   - Store the `ProcessingService` instance in a central registry (e.g., a dictionary in FastAPI app
     state or a dedicated manager class), keyed by its profile `id`.

## 5. Interaction Mechanisms

### 5.1. Delegation via Tool (`delegate_to_service`)

- A new local tool, e.g., `delegate_to_service`, will be made available to certain profiles (likely
  the "default_assistant").

- **Schema**:

  - `target_service_id` (string): The ID of the specialized service profile to delegate to.
  - `user_request` (string): The specific request or prompt for the target service.
  - `confirm_delegation` (boolean, optional): If true, user confirmation will be sought before
    delegating. This parameter may be overridden by the target profile's
    `delegation_security_level`.

- **Execution**:

  1. The tool retrieves the target `ProcessingService` instance and its `delegation_security_level`
     from the registry.

  2. If the target's level is `"blocked"`, the tool returns an error.

  3. A decision to request user confirmation is made:

     - If target's level is `"confirm"`, confirmation is *always* requested.
     - If target's level is `"unrestricted"`, confirmation is requested if the tool's
       `confirm_delegation` argument is `true`.
     - If target's level is `"blocked"`, this step is skipped as delegation is denied.

  4. If confirmation is required and obtained (or not required), it prepares the necessary input for
     the target service's `handle_chat_interaction` method.

  5. It calls `handle_chat_interaction` on the target service.

  6. The textual response from the target service is returned as the result of the
     `delegate_to_service` tool.

- **Security**: This mechanism allows the main LLM to operate with a limited toolset and delegate
  sensitive/complex tasks to specialized, potentially more restricted or monitored, environments.
  Confirmation for *entering* this delegated environment with a specific task is a key security
  control point.

### 5.2. Direct Invocation via Slash Commands

- Users can directly invoke a specialized service profile via Telegram slash commands (e.g.,
  `/secure_action <request>`, `/browse <query>`).

- The `TelegramUpdateHandler` (or specific `CommandHandler`s) will:

  - Identify the target service profile based on the command.
  - Retrieve the corresponding `ProcessingService` instance from the registry.
  - Directly invoke its `process_message` method with the user's input and chat context.
  - For certain slash commands targeting sensitive profiles, the handler itself might initiate a
    confirmation step before invoking the service.

## 6. Benefits

- **Enhanced Security Segmentation**: Sensitive tools are isolated and not directly exposed to the
  primary LLM, reducing misuse risks.

- **Reduced Accidental Triggers**: The main LLM is less likely to accidentally call complex or
  sensitive tools.

- **Reduced LLM Cognitive Load**: The main LLM deals with a smaller, focused toolset for general
  tasks. Specialized profiles can have tailored system prompts.

- **Optimized Model Usage**: Allows selection of the most appropriate LLM model (e.g., balancing
  capability, cost, speed) for each profile's specific tasks. For instance, a high-reasoning model
  for complex decision-making profiles, and a cheaper, faster model for profiles focused on text
  summarization or data extraction.

- **Modularity and Maintainability**: Toolsets and service behaviors for different domains can be
  managed independently.

- **Clearer User Intent**: Explicit slash commands make user intent for specialized tasks
  unambiguous.

- **Controlled Execution Environments**: Delegation allows for "clean room" execution where a
  specialized service might operate with a restricted set of tools (e.g., no web access for a
  service handling PII modification) after explicit user consent for the specific task.

## 7. Challenges

- **Increased Architectural Complexity**: Managing multiple service instances, configurations, and
  routing logic.

- **Context Propagation**: Ensuring necessary conversational history and context are correctly
  passed during delegation and results are integrated back. `turn_id` and `thread_root_id` in
  `message_history` will be important.

- **Latency**: Delegation introduces an additional processing hop.

- **Discovery**: The main LLM needs to understand when and how to use the `delegate_to_service`
  tool.

- **Error Handling**: Errors in specialized services need to be gracefully handled and reported.

## 8. Implemented Profiles

### 8.1. `automation_creation`

**Purpose**: Specialized profile for creating and validating event-based and schedule-based
automations with thorough testing and validation.

**Configuration**:

- **Model**: `gemini-3.1-pro-preview` (Google provider)
- **Max Iterations**: 25 (to support complex validation workflows)
- **Delegation**: `unrestricted` (can delegate to other profiles if needed)
- **Documentation**: Automatically loads `scripting.md` for comprehensive Starlark reference

**Available Tools**:

- **Automation Management**: `create_automation`, `list_automations`, `get_automation`,
  `update_automation`, `enable_automation`, `disable_automation`, `delete_automation`,
  `get_automation_stats`
- **Event Testing & Validation**: `query_recent_events`, `test_event_listener`
- **Script Execution**: `execute_script` (for testing candidate scripts)
- **Context & Validation**: Calendar, notes, documents, Home Assistant state history
- **Communication**: `send_message_to_user`
- **Home Assistant**: Full MCP access for entity validation

**Slash Commands**: `/automate`, `/create-automation`

**System Prompt Features**:

The profile uses a prescriptive system prompt that guides through a systematic automation creation
procedure:

1. **Understand Request & Examine Patterns**: Search existing automations for successful patterns
2. **Identify Trigger Conditions**: Determine event sources, filters, and whether simple
   match_conditions or complex condition_script is needed
3. **Determine Action Type**: Choose between deterministic scripts (faster, cheaper) and wake_llm
   (for judgment-based decisions)
4. **Validate Assumptions**: Use tools to verify entity IDs, event structures, states before
   creating
5. **Test Candidate Scripts**: Execute scripts in isolation with test data before embedding
6. **Test Actions Safely**: Dry-run strategies and validation
7. **Validate wake_llm Context**: Ensure LLM will have sufficient context when awakened
8. **Create Automation**: Use validated parameters
9. **Provide Results**: Show automation ID, UI link, explain triggers and actions

**Best Practices Enforced**:

- Always validate entity IDs and event structures using tools (never guess)
- Test scripts with `execute_script` before creating automations
- Use `test_event_listener` to verify conditions would match real events
- Check historical state data with `download_state_history`
- Prefer scripts over wake_llm for deterministic tasks (faster, cheaper)
- Write defensive scripts with `.get()` and null checks
- Handle Starlark limitations (no float(), no try/except, etc.)

**Common Patterns Provided**:

- Zone entry detection (state transitions)
- Threshold crossing (with proper int() truncation for decimals)
- Pattern matching (entity ID prefixes)

**Example Usage**:

```
User: Create an automation that alerts me when the garage door opens at night
Profile: automation_creation

The profile will:
1. List existing automations to check for similar patterns
2. Query recent events to understand garage door event structure
3. Validate garage door entity ID exists in Home Assistant
4. Test a condition script: time_hour(time_now()) >= 21 or time_hour(time_now()) < 6
5. Create either a script-based automation (for simple notification) or wake_llm (if context-aware decision needed)
6. Provide UI link: http://localhost:8000/automations/event/{id}
```

**Security Considerations**:

This profile operates in **trusted [BC] mode** per the Rule of Two framework:

- Processes trusted user input (authenticated users creating automations)
- Has access to sensitive data and can change state
- Relies on strong authentication at interface boundaries

**Design Notes**:

This profile demonstrates the power of specialized processing profiles for complex workflows. By
loading scripting documentation by default and using a systematic, validation-heavy approach, it
helps users create reliable automations that work correctly on first try.

## 9. Future Considerations

- **Dynamic Profile Loading/Reloading**: For more advanced scenarios.

- **Inter-Service Communication**: More complex interactions beyond simple delegation if needed.

- **Resource Management**: Ensuring efficient use of resources if many profiles are active.
