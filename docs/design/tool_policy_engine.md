# Tool Policy Engine Design

## 1. Introduction

This document describes the design for a unified, rule-based **Tool Policy Engine** that replaces
the current ad-hoc tool access control system (`enable_local_tools`, `confirm_tools`,
`enable_mcp_server_ids`). The new system provides:

- **Rule-based tool access control** with allow/deny/confirm decisions
- **Tool metadata annotations** for sensitivity, capabilities, and output trustworthiness
- **Priority-based rule evaluation** where higher-priority rules override lower-priority ones
- **Operator config layering** where `config.yaml` overrides compose with `defaults.yaml`
- **Per-profile policy rules** that compose with global defaults
- **Dynamic taint-aware policies** that restrict tools when processing untrusted content
- **Unified provider** replacing the separate `FilteredToolsProvider` + `ConfirmingToolsProvider`

### 1.1. Motivation

The current system has several limitations:

1. **No subtraction**: Profiles must re-list every tool they want. There is no way to say "use the
   defaults but remove `delete_calendar_event`." Adding a new tool to the defaults requires updating
   every profile that wants it.

2. **No tool grouping**: Tools have no metadata beyond name and description. You cannot write a rule
   like "confirm all destructive tools" -- you must list each tool by name.

3. **No per-MCP-tool policy**: `enable_mcp_server_ids` controls access to entire MCP servers. You
   cannot allow one tool from a server while blocking another.

4. **Confirmation and filtering are disconnected**: `enable_local_tools` (filtering) and
   `confirm_tools` (confirmation) are separate, flat lists with no shared model. This leads to
   redundancy and makes it hard to reason about the effective policy for a given tool.

5. **No dynamic restriction**: When the processing context becomes tainted by untrusted input (e.g.,
   email content, web-scraped text), there is no mechanism to restrict the available tool set to
   prevent prompt injection exploits.

6. **No output trust model**: Tools that fetch external content (web scraping, email) produce output
   that may contain prompt injection attacks, but the system has no way to track or act on this.

### 1.2. Design Influences

This design draws inspiration from:

- **Gemini CLI Policy Engine**: Layered policies with priorities, tool matching with wildcards, and
  context-dependent approval modes.
- **Meta's Rule of Two**: Agents should satisfy at most two of three properties: [A] processing
  untrusted input, [B] accessing sensitive data, [C] changing state externally.
- **Existing ScriptConfig model**: `allowed_tools` / `deny_all_tools` in the scripting engine
  provides a simple precedent for tool access control.

## 2. Core Concepts

### 2.1. Tool Metadata Registry

Every tool -- both local Python tools and MCP server tools -- has a set of **tags** that describe
its security-relevant properties. Tags are declared in code for local tools and in configuration for
MCP tools.

### 2.2. Policy Rules

A **policy rule** matches tools by name pattern, tags, or MCP server ID, and specifies a
**decision**: `allow`, `deny`, or `confirm`. Rules have a numeric **priority**; when multiple rules
match, the highest-priority rule wins.

### 2.3. Policy Layers

Policies compose across three layers:

1. **Application defaults** (`defaults.yaml`): Base policy rules shipped with the application.
2. **Operator overrides** (`config.yaml`): Deployment-specific rules that override or extend
   defaults. These are the "per-user overrides" where "user" means the operator deploying the
   system.
3. **Per-profile rules**: Each service profile can specify additional rules that apply only when
   that profile is active.

Higher layers override lower layers through priority: operator rules at higher priority than
defaults, profile rules at higher priority than operator rules.

### 2.4. Taint Tracking

When a tool produces output marked as potentially containing untrusted content (e.g., web scraping,
email ingestion), the processing context's **taint level** escalates. Policy rules can be
conditioned on the current taint level, enabling dynamic restriction of sensitive tools.

## 3. Tool Metadata

### 3.1. Tag Taxonomy

```python
class ToolTag(StrEnum):
    """Security-relevant tags for tools."""

    # === Capability tags (what the tool does) ===
    READ_ONLY = "read_only"            # Only reads data, no side effects
    STATE_CHANGING = "state_changing"   # Modifies persistent state (DB, calendar, notes)
    EXTERNAL_COMM = "external_comm"    # Communicates externally (sends messages, emails)
    DESTRUCTIVE = "destructive"        # Deletes data or is hard to reverse
    CODE_EXECUTION = "code_execution"  # Executes arbitrary code (scripts, workers)
    BROWSER = "browser"                # Browser automation tools
    CAMERA = "camera"                  # Camera / surveillance access
    HOME_AUTOMATION = "home_auto"      # Home Assistant / IoT control
    DELEGATION = "delegation"          # Delegates to another processing profile
    FILE_SYSTEM = "file_system"        # Reads/writes local filesystem

    # === Output trust tags (how safe is the output) ===
    OUTPUT_TRUSTED = "output_trusted"          # Output is from own DB or user-created content
    OUTPUT_UNTRUSTED = "output_untrusted"      # Output may contain prompt injection payloads
    TRUST_UNSPECIFIED = "trust_unspecified"    # Trust level unknown (untagged MCP tools)

    # === Functional group tags (for convenience matching) ===
    NOTES = "notes"
    CALENDAR = "calendar"
    DOCUMENTS = "documents"
    SCHEDULING = "scheduling"
    MEDIA = "media"
    AUTOMATION = "automation"
    WORKER = "worker"
    DATA = "data"
```

Tags are not mutually exclusive. A tool can be both `STATE_CHANGING` and `CALENDAR` and
`OUTPUT_TRUSTED`. The capability and output trust tags are the most security-relevant; the
functional group tags exist for ergonomic rule writing.

### 3.2. Local Tool Metadata Declaration

Each tool module declares metadata alongside its tool definitions:

```python
# src/family_assistant/tools/calendar.py

TOOL_METADATA: dict[str, set[ToolTag]] = {
    "add_calendar_event": {
        ToolTag.STATE_CHANGING, ToolTag.CALENDAR, ToolTag.OUTPUT_TRUSTED
    },
    "search_calendar_events": {
        ToolTag.READ_ONLY, ToolTag.CALENDAR, ToolTag.OUTPUT_TRUSTED
    },
    "modify_calendar_event": {
        ToolTag.STATE_CHANGING, ToolTag.CALENDAR, ToolTag.OUTPUT_TRUSTED
    },
    "delete_calendar_event": {
        ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING, ToolTag.CALENDAR, ToolTag.OUTPUT_TRUSTED
    },
}
```

The `__init__.py` aggregates all module metadata into a single registry:

```python
# src/family_assistant/tools/__init__.py

TOOL_METADATA_REGISTRY: dict[str, set[ToolTag]] = {
    **notes.TOOL_METADATA,
    **calendar_mod.TOOL_METADATA,
    **communication.TOOL_METADATA,
    # ... all tool modules ...
}
```

A test validates that every tool in `AVAILABLE_FUNCTIONS` has a corresponding entry in
`TOOL_METADATA_REGISTRY`, and vice versa.

### 3.3. MCP Tool Metadata

MCP tools are not under our control, so their metadata is declared in configuration. Each MCP server
config can specify tags for individual tools or a wildcard default:

```yaml
mcp_config:
  mcpServers:
    homeassistant:
      transport: "sse"
      url: "..."
      tool_metadata:
        # Per-tool overrides
        "get_entity_state": ["read_only", "home_auto", "output_trusted"]
        "call_service": ["state_changing", "home_auto", "output_trusted"]
        # Wildcard: default tags for any tool from this server not listed above
        "*": ["home_auto"]

    brave:
      command: "deno"
      args: [...]
      tool_metadata:
        "*": ["read_only", "output_untrusted"]

    browser:
      command: "deno"
      args: [...]
      tool_metadata:
        "*": ["browser", "state_changing", "output_untrusted"]
```

**Resolution**: When looking up metadata for an MCP tool, the system checks for an exact name match
first, then falls back to the `"*"` wildcard for that server. If neither exists, the tool is treated
as having `OUTPUT_UNTRUSTED` tag implicitly (see below).

### 3.4. Untagged Tools and Fail-Closed Defaults

The system follows a **fail-closed** approach to tool metadata:

**Local tools**: Every tool in `AVAILABLE_FUNCTIONS` **must** have an entry in
`TOOL_METADATA_REGISTRY`. This is enforced at startup -- a missing entry is a hard error that
prevents the application from starting. This ensures developers cannot accidentally ship a tool
without considering its security properties.

**MCP tools**: MCP tools without explicit metadata (no per-tool entry and no `"*"` wildcard for
their server) automatically receive the `TRUST_UNSPECIFIED` tag. This tag honestly represents the
system's state of knowledge: we don't know what this tool does or whether its output is safe.

The `trust_unspecified` tag is then handled by **policy rules**, keeping the security decision in
the policy layer where it belongs. The application defaults include rules for `trust_unspecified`
tools (see section 5.1 for examples), but operators and profiles can override these rules to match
their risk appetite:

```yaml
# Default application policy (in defaults.yaml):
# Treat unspecified-trust tools conservatively
- match: { tags_any: ["trust_unspecified"] }
  decision: "confirm"
  priority: 15
  description: "Unknown MCP tools require confirmation"

# An operator who trusts all their MCP servers could override:
- match: { tags_any: ["trust_unspecified"] }
  decision: "allow"
  description: "Operator trusts all configured MCP servers"

# A high-security profile could be stricter:
- match: { tags_any: ["trust_unspecified"] }
  decision: "deny"
  priority: 50
  description: "Event handler blocks unknown tools entirely"
```

With `trust_unspecified`, untagged MCP tools:

- Are **matchable by policy rules** via `tags_any: ["trust_unspecified"]`, giving operators and
  profiles full control over how to handle them
- Are **not matched** by tag-based rules checking for specific capability tags (e.g.,
  `tags_any: ["read_only"]`), so they will not be accidentally allowed by broad tag rules
- **Are matched** by name-based rules and MCP server ID rules
- Fall through to the `default_decision` of the policy if no rule matches

**Taint behavior**: For taint propagation (section 6.3), `trust_unspecified` tools are treated the
same as `output_untrusted` -- their output escalates the context taint level. An operator who wants
to suppress this for specific servers should tag those servers' tools with `output_trusted` via the
`"*"` wildcard in `tool_metadata`, which removes the `trust_unspecified` tag.

**Rationale**: Local tools are under our control, so we can require exhaustive metadata at
development time. MCP tools are third-party, so we cannot require metadata. Rather than pretending
we know something we don't (by forcing `output_untrusted`), we label the gap in our knowledge
honestly and let the policy engine decide. This is more composable: different profiles can handle
unspecified trust differently (e.g., the main assistant confirms, the event handler denies, a
development profile allows).

## 4. Policy Rule Model

### 4.1. Rule Structure

```python
class ToolMatcher(BaseModel):
    """Matches tools by name patterns, tags, and/or MCP server origin."""

    # Match by tool name -- supports fnmatch-style globs (*, ?, [seq])
    names: list[str] | None = None    # e.g., ["delete_*", "modify_calendar_event"]

    # Match by tag -- tool must have ALL specified tags (AND logic)
    tags_all: list[str] | None = None  # e.g., ["state_changing", "calendar"]

    # Match by tag -- tool must have ANY of specified tags (OR logic)
    tags_any: list[str] | None = None  # e.g., ["destructive", "external_comm"]

    # Match by MCP server ID -- tool must originate from one of these servers
    mcp_server_ids: list[str] | None = None  # e.g., ["browser", "brave"]
```

```python
class PolicyRule(BaseModel):
    """A single policy rule mapping tool matches to access decisions."""

    match: ToolMatcher                              # What tools this rule applies to
    decision: str                                   # "allow", "deny", or "confirm"
    priority: int = 0                               # Higher priority wins
    description: str = ""                           # Human-readable audit trail
    when_tainted: str | None = None                 # Only apply when context taint >= this level
```

```python
class ToolPolicyConfig(BaseModel):
    """Complete policy configuration for a profile or config layer."""

    rules: list[PolicyRule] = Field(default_factory=list)
    default_decision: str = "deny"                  # Decision when no rule matches
```

### 4.2. Matching Semantics

A rule matches a tool if **all specified criteria** in its `ToolMatcher` are satisfied (AND logic
across criteria). Within a criterion:

- `names`: OR logic -- tool name matches **any** pattern in the list (fnmatch glob)
- `tags_all`: AND logic -- tool has **all** tags in the list
- `tags_any`: OR logic -- tool has **any** tag in the list
- `mcp_server_ids`: OR logic -- tool originates from **any** listed server

If a criterion field is `None`, it is not checked (wildcard). A `ToolMatcher` with all fields `None`
matches nothing (safety: an empty matcher should not accidentally match everything).

**Examples**:

```yaml
# Match any tool named "delete_*" that is also tagged "calendar"
match:
  names: ["delete_*"]
  tags_any: ["calendar"]
# -> Matches delete_calendar_event (name matches AND has calendar tag)
# -> Does NOT match delete_note (name matches but no calendar tag)
# -> Does NOT match modify_calendar_event (name doesn't match)

# Match any tool tagged as destructive
match:
  tags_any: ["destructive"]
# -> Matches delete_calendar_event, delete_note, delete_automation, etc.

# Match all tools from the browser MCP server
match:
  mcp_server_ids: ["browser"]
# -> Matches any tool discovered from the "browser" MCP server
```

### 4.3. Evaluation Algorithm

```
function evaluate(tool_name, mcp_server_id, context_taint_level):
    # Sort rules by priority descending
    for rule in rules sorted by -priority:
        # Skip rules that don't apply at current taint level
        if rule.when_tainted and context_taint_level < rule.when_tainted:
            continue

        # Check if tool matches this rule
        if rule.match matches (tool_name, tool_tags, mcp_server_id):
            return rule.decision

    return default_decision
```

When multiple rules have the same priority, the **first matching rule** (in declaration order) wins.
This is deterministic: rules are evaluated in the order they appear in configuration, with priority
as the primary sort key.

### 4.4. Taint-Conditional Rules

Rules can specify `when_tainted` to only apply when the processing context has been tainted by
untrusted content. The taint levels form an ordered hierarchy:

```
trusted < partially_tainted < untrusted
```

A rule with `when_tainted: "untrusted"` only applies when `context_taint_level >= untrusted`. A rule
with `when_tainted: "partially_tainted"` applies when the level is `partially_tainted` or
`untrusted`.

Rules without `when_tainted` (the default) always apply regardless of taint level.

## 5. Configuration Structure

### 5.1. Profile-Level Policy

The `tools_config` section of each profile (and `default_profile_settings`) is replaced with a
policy-based model:

```yaml
default_profile_settings:
  tools_policy:
    default_decision: "deny"    # Deny tools unless explicitly allowed
    rules:
      # --- Allow read-only tools ---
      - match: { tags_any: ["read_only"] }
        decision: "allow"
        priority: 10
        description: "Read-only tools are safe by default"

      # --- Allow most state-changing tools ---
      - match:
          tags_any: ["state_changing"]
        decision: "allow"
        priority: 10

      # --- Require confirmation for destructive tools ---
      - match: { tags_any: ["destructive"] }
        decision: "confirm"
        priority: 20
        description: "Destructive operations always need user confirmation"

      # --- Require confirmation for calendar modifications ---
      - match: { names: ["modify_calendar_event"] }
        decision: "confirm"
        priority: 20

      # --- Require confirmation for delegation ---
      - match: { tags_any: ["delegation"] }
        decision: "confirm"
        priority: 20

      # --- Allow all tools from homeassistant and brave MCP servers ---
      - match: { mcp_server_ids: ["homeassistant", "brave", "time", "google-maps"] }
        decision: "allow"
        priority: 10

      # --- Block browser MCP server by default ---
      - match: { mcp_server_ids: ["browser"] }
        decision: "deny"
        priority: 10

      # --- Require confirmation for untagged MCP tools ---
      - match: { tags_any: ["trust_unspecified"] }
        decision: "confirm"
        priority: 15
        description: "Unknown MCP tools require confirmation by default"

      # --- Taint-aware: block external communication when tainted ---
      - match: { tags_any: ["external_comm"] }
        decision: "deny"
        when_tainted: "untrusted"
        priority: 100
        description: "Block external communication when processing untrusted content"

      # --- Taint-aware: require confirmation for state changes when tainted ---
      - match: { tags_any: ["state_changing"] }
        decision: "confirm"
        when_tainted: "untrusted"
        priority: 90
        description: "Require confirmation for state changes on tainted context"
```

### 5.2. Per-Profile Overrides

Profiles specify their own policy rules that compose with the defaults. Profile rules run at their
declared priority -- if a profile needs to override a default rule, it uses a higher priority.

```yaml
service_profiles:
  - id: "default_assistant"
    description: "Main assistant for general tasks."
    # Inherits default tools_policy. No overrides needed.

  - id: "reminder"
    description: "Formulates reminder messages. Read-only access only."
    processing_config:
      delegation_security_level: "blocked"
    tools_policy:
      default_decision: "deny"
      rules:
        # Only allow specific read-only tools
        - match:
            names:
              - "search_calendar_events"
              - "get_note"
              - "list_notes"
              - "search_documents"
              - "get_full_document_content"
              - "get_user_documentation_content"
          decision: "allow"
          priority: 10
        # Deny everything from MCP servers
        - match: { mcp_server_ids: ["*"] }
          decision: "deny"
          priority: 10

  - id: "browser_profile"
    description: "Web browsing capabilities."
    processing_config:
      delegation_security_level: "unrestricted"
    tools_policy:
      default_decision: "deny"
      rules:
        # Allow only browser tools
        - match: { tags_any: ["browser"] }
          decision: "allow"
          priority: 10
        - match: { names: ["attach_to_response"] }
          decision: "allow"
          priority: 10

  - id: "event_handler"
    description: "Automated event processing. No state changes except notes."
    processing_config:
      delegation_security_level: "blocked"
    tools_policy:
      default_decision: "deny"
      rules:
        # Allow specific read-only and limited write tools
        - match:
            names:
              - "add_or_update_note"
              - "list_notes"
              - "get_note"
              - "search_documents"
              - "send_message_to_user"
              - "search_calendar_events"
              - "query_recent_events"
          decision: "allow"
          priority: 10
        # Allow homeassistant MCP (read-only)
        - match: { mcp_server_ids: ["homeassistant"] }
          decision: "allow"
          priority: 10
        # No confirmation needed for automated operations
```

### 5.3. Operator Config Layering

The operator's `config.yaml` can override `defaults.yaml` policies. The merging strategy for
`tools_policy`:

1. **`default_decision`**: Operator's value replaces the default if specified.
2. **`rules`**: Operator's rules receive an **automatic priority offset** of +1000 and are
   **prepended** to the default rules. This guarantees that operator rules always take precedence
   over application defaults without requiring operators to manually manage priority numbers.

**Automatic priority offsetting**: When merging rules from `config.yaml` into
`default_profile_settings`, the system adds +1000 to each operator rule's declared priority. An
operator rule with `priority: 0` becomes effective priority 1000, which is higher than any
application default rule (0-99). This means operators write rules with simple, low priority numbers
and the system ensures they override defaults:

```python
# During config merge
for rule in operator_rules:
    rule.effective_priority = rule.priority + OPERATOR_PRIORITY_OFFSET  # +1000
```

This means operators can:

- **Add restrictions**: Write deny rules (they automatically override default allow rules)
- **Remove restrictions**: Write allow rules (they automatically override default deny rules)
- **Add confirmation requirements**: Write confirm rules for specific tools
- **Block entire categories**: Write deny rules matching tags
- **All without thinking about priority numbers** -- the offset handles precedence

**Example**: Operator wants to block all code execution and require confirmation for all Home
Assistant tools:

```yaml
# config.yaml (operator overrides)
default_profile_settings:
  tools_policy:
    rules:
      # Block code execution across all profiles
      # Declared priority 0 -> effective priority 1000 (overrides any default)
      - match: { tags_any: ["code_execution"] }
        decision: "deny"
        description: "Operator policy: no code execution"

      # Require confirmation for all Home Assistant operations
      - match: { tags_any: ["home_auto"] }
        decision: "confirm"
        description: "Operator policy: confirm HA operations"
```

The operator does not need to specify `priority` -- the default of 0 becomes effective priority 1000
after offsetting, which overrides all application defaults (0-99). If the operator needs to order
their own rules relative to each other, they can use priority values which will be offset uniformly
(e.g., operator priority 10 becomes 1010, operator priority 20 becomes 1020).

### 5.4. Rule Merging Across Layers

The effective policy for a profile is built by merging rules from three sources with automatic
priority offsetting:

```
effective_rules = merge(
    application defaults (priority as-is: 0-99),
    operator overrides   (priority += 1000),
    profile-specific     (priority as-is: 0-99, scoped to profile),
)
sorted by effective_priority descending, then declaration order for tie-breaking
```

The `default_decision` is taken from the most specific layer that defines it (profile > operator >
defaults).

**Priority offset constants**:

| Layer                | Offset | Effective Range | Description                                                     |
| -------------------- | ------ | --------------- | --------------------------------------------------------------- |
| Application defaults | +0     | 0-99            | Base policies shipped with the app                              |
| Profile-specific     | +0     | 0-99            | Profile's own tool access rules                                 |
| Operator overrides   | +1000  | 1000-1099       | Deployment-specific overrides                                   |
| Taint-conditional    | (any)  | (any)           | Declared at whatever layer; use high priority within that layer |

Profile-specific rules use the same priority range as defaults (0-99) because they are scoped to a
single profile and compose with defaults within that scope. Operator rules always override both due
to the +1000 offset.

**Why prepend + offset instead of append?** Two reasons:

1. **Intuitive behavior**: Operators expect their config to override defaults. Automatic offsetting
   makes this work without priority management.
2. **Deterministic tie-breaking**: For equal effective priorities, rules earlier in the list win.
   Prepending operator rules means they win ties against application defaults. This is a safety net
   -- in practice the +1000 offset should make ties rare.

## 6. Taint Tracking and Dynamic Policies

### 6.1. Taint Levels

```python
class TaintLevel(StrEnum):
    TRUSTED = "trusted"                # Direct user input, own database content
    PARTIALLY_TAINTED = "partially_tainted"  # Processed/sanitized external content
    UNTRUSTED = "untrusted"            # Raw external content (emails, web scraping)
```

Taint levels are ordered: `TRUSTED < PARTIALLY_TAINTED < UNTRUSTED`. Escalation is one-way within a
processing turn -- once tainted, the context stays tainted until the conversation turn ends.

### 6.2. Taint Sources

The taint level of the processing context escalates when:

1. **A tool with `output_untrusted` or `trust_unspecified` tag executes**: The tool's output is
   injected into the LLM context and may contain injection payloads. Context escalates to
   `UNTRUSTED`. Tools with unknown trust (`trust_unspecified`) are treated conservatively.

2. **Processing an email or forwarded message**: The input source is external and not fully vetted.
   Context starts at `UNTRUSTED` for these interactions.

3. **Delegation with taint inheritance**: When a profile delegates to another profile and
   `inherit_taint` is true (default), the target profile inherits the source's taint level.

### 6.3. Taint Propagation in ProcessingService

After each tool execution in the processing loop, the `ProcessingService` checks if the tool's
output is tagged as untrusted:

```python
# In the tool execution loop of ProcessingService
tool_tags = get_effective_tags(tool_name, tool_metadata_registry)
# For MCP tools without metadata, tool_tags will contain {TRUST_UNSPECIFIED}
# Local tools always have explicit metadata (enforced at startup)

# Taint escalation: output_untrusted OR trust_unspecified (unless explicitly trusted)
taints_context = (
    ToolTag.OUTPUT_UNTRUSTED in tool_tags
    or ToolTag.TRUST_UNSPECIFIED in tool_tags
) and ToolTag.OUTPUT_TRUSTED not in tool_tags

if taints_context:
    exec_context.context_taint_level = max(
        exec_context.context_taint_level,
        TaintLevel.UNTRUSTED
    )
```

The `PolicyEngine` receives the current taint level for each evaluation, enabling taint-conditional
rules to activate dynamically.

### 6.4. Rule of Two Integration

Taint-conditional rules implement the **Rule of Two** from Meta's AI agent security framework:

| Scenario                     | Properties                                          | Taint-Conditional Rules                                                              |
| ---------------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------ |
| **Trusted [BC]**             | Trusted input + sensitive data + state changes      | No taint rules active; full tool access                                              |
| **Untrusted-Readonly [AB]**  | Untrusted input + sensitive data + NO state changes | `when_tainted: "untrusted"` deny rules block `state_changing` and `external_comm`    |
| **Untrusted-Sandboxed [AC]** | Untrusted input + NO sensitive data + state changes | `when_tainted: "untrusted"` deny rules block tools tagged with sensitive data access |

The default application rules implement the **[AB] fallback** -- when context is tainted, block
external communication and require confirmation for state changes. Operators can configure stricter
policies (full deny of state-changing tools) or looser ones depending on their threat model.

### 6.5. Limitations of Taint Tracking

- **MCP tools without metadata**: As described in section 3.4, MCP tools without explicit metadata
  receive the `trust_unspecified` tag and escalate taint by default. The policy engine handles
  access control for these tools via rules matching `trust_unspecified`. Operators who want to avoid
  taint escalation from a trusted MCP server should configure `tool_metadata` with `output_trusted`
  tags. This may cause false taint escalation for trusted-but-unconfigured MCP servers, which is
  preferable to missing a genuine injection vector.

- **Context window persistence**: Once the LLM's context contains untrusted content from a previous
  turn, subsequent turns may still be influenced. The taint tracking per-turn model does not address
  persistent context contamination. A more sophisticated approach would track taint at the message
  level, but this is deferred as future work.

- **LLM-level prompt injection defenses**: Taint tracking restricts tool access but does not prevent
  the LLM from being influenced by injected instructions in other ways (e.g., changing its response
  style or revealing information via text responses). Defense in depth with input sanitization and
  output monitoring remains important.

## 7. PolicyEnforcingToolsProvider

### 7.1. Architecture

The new `PolicyEnforcingToolsProvider` replaces both `FilteredToolsProvider` and
`ConfirmingToolsProvider` with a single provider that makes unified access decisions:

```
PolicyEnforcingToolsProvider (unified allow/deny/confirm)
  -> CompositeToolsProvider
    |-- LocalToolsProvider (all local Python tools)
    |-- MCPToolsProvider (all MCP server tools)
```

### 7.2. Behavior

```python
class PolicyEnforcingToolsProvider(ToolsProvider):
    """Enforces tool access policy with allow/deny/confirm decisions."""

    def __init__(
        self,
        wrapped_provider: ToolsProvider,
        policy_engine: PolicyEngine,
        confirmation_timeout: float = 3600.0,
    ) -> None: ...

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        """Return only tool definitions for allowed and confirm-required tools.
        Denied tools are excluded entirely -- the LLM never sees them."""
        all_defs = await self.wrapped_provider.get_tool_definitions()
        return [
            d for d in all_defs
            if self.policy_engine.evaluate(d["function"]["name"], ...)
               != PolicyDecision.DENY
        ]

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict,
        exec_context: ToolExecutionContext,
    ) -> ToolResult:
        """Execute a tool, enforcing policy decisions."""
        decision = self.policy_engine.evaluate(
            tool_name,
            mcp_server_id=self._get_server_id(tool_name),
            taint_level=exec_context.context_taint_level,
        )

        if decision == PolicyDecision.DENY:
            raise ToolNotFoundError(tool_name)

        if decision == PolicyDecision.CONFIRM:
            approved = await self._request_confirmation(tool_name, arguments, exec_context)
            if not approved:
                return ToolResult(text=f"Tool '{tool_name}' was not approved by user.")

        result = await self.wrapped_provider.execute_tool(tool_name, arguments, exec_context)

        # Propagate taint if tool output is untrusted
        self._propagate_taint(tool_name, exec_context)

        return result
```

### 7.3. Confirmation Integration

The existing confirmation infrastructure (confirmation renderers for calendar events, timeout
handling, web/Telegram confirmation UI) is preserved. The `PolicyEnforcingToolsProvider` uses the
same `request_confirmation_callback` from `ToolExecutionContext` that `ConfirmingToolsProvider` uses
today.

The key difference: instead of checking a `confirm_tools` set, it queries the `PolicyEngine`.

### 7.4. Dynamic Re-evaluation

Because the `PolicyEngine` receives the current `context_taint_level` on each `evaluate()` call, the
effective policy can change mid-conversation. A tool that was allowed on the first call may become
denied or require confirmation after a tool with untrusted output executes.

**Important**: `get_tool_definitions()` is called at the start of each LLM turn (not once at
startup). This means the LLM's visible tool set can shrink during a conversation as taint escalates.
The provider must re-evaluate which tools to expose on each call.

## 8. Delegation Policy

### 8.1. Current Model

The current `delegation_security_level` field on `ProcessingConfig` remains conceptually the same
but is now expressed as part of the policy system:

- `"blocked"`: The profile's policy includes a deny rule for the `delegate_to_service` tool
  targeting this profile (enforced at the delegation tool level, not the policy engine).
- `"confirm"`: Delegation requires confirmation.
- `"unrestricted"`: Delegation is allowed without confirmation.

### 8.2. Enhanced Delegation Controls

Two new optional fields on `ProcessingConfig`:

```yaml
processing_config:
  delegation_security_level: "confirm"

  # NEW: Only these profiles can delegate to this one. None = any.
  allowed_delegation_sources: ["default_assistant"]

  # NEW: Whether delegated context inherits source taint level. Default: true.
  inherit_delegation_taint: true
```

**Source restrictions**: `allowed_delegation_sources` prevents unintended delegation chains. For
example, the `telephone` profile (which handles untrusted voice input) should not be able to
delegate to the `automation_creation` profile (which can create arbitrary automations).

**Taint inheritance**: When `inherit_delegation_taint` is true (default), the delegated profile's
`ToolExecutionContext` starts with the source profile's taint level. This prevents circumventing
taint restrictions by delegating to a "clean" profile.

When `inherit_delegation_taint` is false, the delegated profile starts with `TRUSTED` taint. This is
appropriate when the delegation target processes a sanitized/summarized version of the input rather
than raw untrusted content.

## 9. Configuration Model Changes

### 9.1. Removed Fields

The following fields are **removed** from `ToolsConfig`:

- `enable_local_tools: list[str] | None` -- replaced by policy allow rules
- `enable_mcp_server_ids: list[str] | None` -- replaced by policy allow rules with `mcp_server_ids`
- `confirm_tools: list[str]` -- replaced by policy confirm rules

### 9.2. New `tools_policy` Field

A new field `tools_policy` on `ServiceProfile` and `DefaultProfileSettings` replaces `tools_config`
for tool access control:

```python
class ToolMatcherConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    names: list[str] | None = None
    tags_all: list[str] | None = None
    tags_any: list[str] | None = None
    mcp_server_ids: list[str] | None = None

class PolicyRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    match: ToolMatcherConfig
    decision: str                           # "allow", "deny", "confirm"
    priority: int = 0
    description: str = ""
    when_tainted: str | None = None         # "trusted", "partially_tainted", "untrusted"

class ToolPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[PolicyRuleConfig] = Field(default_factory=list)
    default_decision: str = "deny"

class ToolsConfig(BaseModel):
    """Configuration for tool-related settings (non-policy)."""
    model_config = ConfigDict(extra="forbid")
    mcp_initialization_timeout_seconds: int = 60
    confirmation_timeout_seconds: float = 3600.0
```

### 9.3. ServiceProfile Changes

```python
class ServiceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    processing_config: ProcessingConfig = Field(default_factory=ProcessingConfig)
    tools_config: ToolsConfig = Field(default_factory=ToolsConfig)  # Non-policy tool settings
    tools_policy: ToolPolicyConfig | None = None                     # NEW: policy rules
    chat_id_to_name_map: dict[int, str] = Field(default_factory=dict)
    slash_commands: list[str] = Field(default_factory=list)
    visibility_grants: list[str] = Field(default_factory=list)
```

### 9.4. MCP Config Changes

```python
class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # NEW: metadata tags for tools from this server
    tool_metadata: dict[str, list[str]] = Field(default_factory=dict)
```

### 9.5. ProcessingConfig Changes

```python
class ProcessingConfig(BaseModel):
    # ... existing fields ...
    delegation_security_level: str = "confirm"

    # NEW
    allowed_delegation_sources: list[str] | None = None
    inherit_delegation_taint: bool = True
```

## 10. Migration from Current System

### 10.1. Translation Rules

Every existing configuration can be mechanically translated to the new policy model:

| Old Config                                 | New Policy                                                                                                         |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `enable_local_tools: ["tool_a", "tool_b"]` | `rules: [{match: {names: ["tool_a", "tool_b"]}, decision: "allow", priority: 10}]` with `default_decision: "deny"` |
| `enable_local_tools: null` (all tools)     | `default_decision: "allow"` with no deny rules                                                                     |
| `confirm_tools: ["tool_x"]`                | `rules: [{match: {names: ["tool_x"]}, decision: "confirm", priority: 20}]`                                         |
| `enable_mcp_server_ids: ["server1"]`       | `rules: [{match: {mcp_server_ids: ["server1"]}, decision: "allow", priority: 10}]`                                 |
| `enable_mcp_server_ids: []`                | No MCP allow rules; `default_decision: "deny"` blocks them                                                         |

### 10.2. Migration of Existing Profiles

**`default_assistant`** (currently inherits all defaults):

```yaml
# Before:
tools_config:
  enable_local_tools: [... 40+ tools ...]
  enable_mcp_server_ids: ["time", "brave", "homeassistant", "google-maps"]
  confirm_tools: ["delete_calendar_event", "modify_calendar_event"]

# After:
tools_policy:
  default_decision: "deny"
  rules:
    # Allow all standard tools by tag
    - match: { tags_any: ["notes", "scheduling", "documents", "calendar", "automation",
                           "media", "data", "worker", "delegation"] }
      decision: "allow"
      priority: 10
    # Plus specific tools not easily grouped
    - match: { names: ["execute_script", "render_home_assistant_template",
                        "get_camera_snapshot", "download_state_history",
                        "list_home_assistant_entities", "query_recent_events",
                        "attach_to_response"] }
      decision: "allow"
      priority: 10
    # MCP servers
    - match: { mcp_server_ids: ["time", "brave", "homeassistant", "google-maps"] }
      decision: "allow"
      priority: 10
    # Confirmation for destructive + calendar mods
    - match: { tags_any: ["destructive"] }
      decision: "confirm"
      priority: 20
    - match: { names: ["modify_calendar_event"] }
      decision: "confirm"
      priority: 20
```

**`reminder`** (read-only, minimal tools):

```yaml
# After:
tools_policy:
  default_decision: "deny"
  rules:
    - match:
        names: ["search_calendar_events", "get_note", "list_notes",
                 "search_documents", "get_full_document_content",
                 "get_user_documentation_content"]
      decision: "allow"
      priority: 10
```

**`browser_profile`** (browser tools only):

```yaml
# After:
tools_policy:
  default_decision: "deny"
  rules:
    - match: { tags_any: ["browser"] }
      decision: "allow"
      priority: 10
    - match: { names: ["attach_to_response"] }
      decision: "allow"
      priority: 10
```

**`event_handler`** (automated, restricted):

```yaml
# After:
tools_policy:
  default_decision: "deny"
  rules:
    - match:
        names: ["add_or_update_note", "list_notes", "get_note",
                 "search_documents", "send_message_to_user",
                 "search_calendar_events", "query_recent_events"]
      decision: "allow"
      priority: 10
    - match: { mcp_server_ids: ["homeassistant"] }
      decision: "allow"
      priority: 10
```

### 10.3. Benefits of Migration

After migration, common operations become much simpler:

**Adding a new tool to most profiles**: Tag the tool appropriately. All profiles that allow that tag
category automatically get the new tool. No need to edit 10 profile configs.

**Subtracting a tool from defaults**: A profile that wants "everything except image generation" just
adds a deny rule:

```yaml
tools_policy:
  rules:
    - match: { names: ["generate_image", "transform_image", "generate_video"] }
      decision: "deny"
      priority: 30
```

**Operator restrictions**: An operator who doesn't want any code execution across any profile:

```yaml
# In config.yaml
default_profile_settings:
  tools_policy:
    rules:
      - match: { tags_any: ["code_execution"] }
        decision: "deny"
        priority: 500
```

## 11. Implementation Phases

### Phase 1: Tool Metadata Registry

Add tags to all local tools. No behavior changes.

- Create `src/family_assistant/tools/metadata.py` with `ToolTag` enum
- Add `TOOL_METADATA` dicts to each tool module
- Aggregate into `TOOL_METADATA_REGISTRY` in `__init__.py`
- Add `tool_metadata` field to `MCPServerConfig`
- Test: every tool in `AVAILABLE_FUNCTIONS` has metadata entry; every tag is valid

### Phase 2: Policy Engine

Implement the rule evaluation engine with exhaustive tests.

- Create `src/family_assistant/tools/policy.py` with `PolicyEngine`, `PolicyRule`, `ToolMatcher`,
  `PolicyDecision`
- Add config models: `ToolPolicyConfig`, `PolicyRuleConfig`, `ToolMatcherConfig`
- Test: name matching, glob patterns, tag matching (all/any), MCP server matching, priority
  ordering, default decisions, edge cases, taint-conditional rules

### Phase 3: PolicyEnforcingToolsProvider

Unified provider that replaces FilteredToolsProvider + ConfirmingToolsProvider.

- Create `PolicyEnforcingToolsProvider` in `infrastructure.py`
- Preserve existing confirmation infrastructure (renderers, callbacks, timeout)
- Test: denied tools excluded from definitions, confirm tools trigger callback, taint propagation

### Phase 4: Config Migration

Replace `enable_local_tools`/`confirm_tools`/`enable_mcp_server_ids` with `tools_policy`.

- Update `config_models.py`: add `tools_policy` to `ServiceProfile` and `DefaultProfileSettings`,
  remove old fields from `ToolsConfig`
- Update `assistant.py` setup: build `PolicyEngine` from merged config, create
  `PolicyEnforcingToolsProvider`
- Migrate `defaults.yaml` to new policy format
- Remove `FilteredToolsProvider` and `ConfirmingToolsProvider`
- Update all tests

### Phase 5: Taint Tracking

Add taint propagation to the processing loop.

- Add `TaintLevel` and `context_taint_level` to `ToolExecutionContext`
- Add taint propagation in `ProcessingService` after tool execution
- Taint-conditional rules already supported by Phase 2 engine
- Test: taint escalation, conditional rules activating, delegation taint inheritance

### Phase 6: Enhanced Delegation

Richer delegation controls.

- Add `allowed_delegation_sources` and `inherit_delegation_taint` to `ProcessingConfig`
- Update `delegate_to_service` tool to check source restrictions and propagate taint
- Test: source restrictions, taint inheritance, backwards compatibility

## 12. Testing Strategy

### 12.1. Unit Tests

- **`test_tool_metadata.py`**: Every tool has metadata; tag values are valid; metadata and
  `AVAILABLE_FUNCTIONS` are consistent
- **`test_policy_engine.py`**: Rule evaluation logic -- name globs, tag matching, priorities,
  defaults, taint conditions, edge cases. This is the most critical test file.
- **`test_policy_enforcing_provider.py`**: Provider filters definitions, blocks denied tools,
  triggers confirmation, propagates taint

### 12.2. Security-Critical Scenarios

These must have dedicated test cases:

01. A deny rule at priority N blocks a tool despite an allow rule at priority M < N
02. An untagged MCP tool is denied in a `default_decision: "deny"` policy
03. A taint-conditional deny rule activates after an `output_untrusted` tool executes
04. A profile cannot bypass a global operator deny rule (priority enforcement via +1000 offset)
05. `delegate_to_service` respects `allowed_delegation_sources`
06. Taint propagates through delegation when `inherit_delegation_taint` is true
07. A tool that matches both allow and deny at the same priority: first-match semantics
08. Empty matcher (`ToolMatcher()` with all None) matches nothing
09. Application startup fails if a local tool in `AVAILABLE_FUNCTIONS` has no metadata entry
10. An MCP tool without metadata receives `trust_unspecified` tag and escalates taint
11. An MCP tool explicitly tagged `output_trusted` does NOT escalate taint
12. Policy rules can match `trust_unspecified` to allow/deny/confirm untagged MCP tools
13. Operator rules with default priority (0) override application default rules (automatic +1000
    offset verified)

### 12.3. Integration Tests

- End-to-end profile setup from YAML config produces correct tool sets
- Full processing loop with taint tracking and dynamic tool restriction
- Existing profile behaviors are preserved after migration

### 12.4. Property-Based Tests (Hypothesis)

- Priority invariant: if rule A has higher priority than rule B and both match the same tool, rule
  A's decision always wins
- Metadata completeness: every tool name in `AVAILABLE_FUNCTIONS` exists in `TOOL_METADATA_REGISTRY`
- Monotonic taint: taint level never decreases within a processing turn
