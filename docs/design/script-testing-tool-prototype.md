# Script Testing Tool Prototype

## Executive Summary

This document proposes a lightweight LLM-facing tool for testing Monty scripts against realistic
tool behavior without executing every real tool call.

The core idea is:

- Run a real script through the existing `execute_script` / `MontyEngine` path.
- Expose real registered Family Assistant tools to the script.
- For selected tools, intercept execution and synthesize realistic outputs with an LLM instead of
  calling the real implementation.
- Use real historical tool outputs from the same conversation, or failing that the same user, as
  few-shot examples to keep simulated results realistic.

This is intended to help scripts "survive contact with reality". The prototype should not invent
fantasy tools or arbitrary result shapes. It should operate on real tool definitions, real tool
names, and outputs that resemble what those tools actually return in production.

## Motivation

### Problem

Script-writing workflows are currently weak in two opposite ways:

- Running scripts against real tools can be unsafe, slow, or expensive.
- Running scripts against simplistic mocks does not prove they will survive real execution.

We need a middle ground:

- realistic enough to catch schema mismatches, missing fields, and wrong assumptions
- safe enough to avoid side effects for destructive or state-changing tools
- lightweight enough to use as a normal LLM tool

### Non-Goals

This prototype is not trying to:

- perfectly emulate every tool in the system
- simulate attachments or other rich multimodal outputs in v1
- allow the model to pick arbitrary LLM models for simulation
- replace proper end-to-end testing with real tools

## Proposed User Experience

Add a new local tool for the assistant, tentatively named `test_script_with_simulated_tools`.

The tool accepts:

- a Monty script to run
- a plain text scenario description describing how tools should behave
- an optional list of tools to force into simulation
- an optional list of read-only tools allowed to pass through
- optional script globals

The tool returns:

- the final script result
- a transcript of tool calls
- whether each call was real or simulated
- the synthesized return values for simulated calls

## Core Design Principles

1. Reuse real tools rather than inventing fake ones.
2. Preserve real tool names and input schemas.
3. Default to safe behavior for side-effecting tools.
4. Use historical outputs to bias simulation toward realistic shapes.
5. Keep the prototype narrow where the current architecture lacks metadata.

## Why This Should Be a New Tool

This should be a dedicated LLM tool rather than a mode bolted onto `execute_script`.

Reasons:

- `execute_script` is for running scripts against the currently available tool provider.
- Mixing "real execution" and "simulation harness" semantics into one tool makes policy and user
  intent harder to reason about.
- A dedicated tool can present testing-specific arguments and output a richer transcript without
  complicating normal script execution.

## Architecture

### High-Level Flow

1. The LLM calls `test_script_with_simulated_tools`.
2. The tool resolves the currently available real `ToolsProvider`.
3. A wrapper provider is created around that real provider.
4. The script runs in `MontyEngine` using the wrapper provider.
5. Each tool call is classified as:
   - passthrough
   - simulated
   - blocked/unsafe
6. Simulated calls are answered by an LLM using:
   - the scenario description
   - the real tool definition
   - the current tool arguments
   - prior simulated call history within this run
   - historical real examples from message history
7. The tool returns the script result plus a call transcript.

### Reused Components

- `MontyEngine` for script execution
- the existing local/composite `ToolsProvider` model
- local tool metadata and tags from the tool registry
- message history storage for historical `(arguments, result)` examples

## Tool Classification

The system, not the model, should determine the default split between real and simulated tools.

The repository already contains the right primitive: `ToolTag` metadata in
`src/family_assistant/tools/metadata.py` and `LOCAL_TOOL_METADATA_BY_NAME` in
`src/family_assistant/tools/__init__.py`.

### Default Classification Rules

#### Passthrough by default

Tools tagged `READ_ONLY` are passthrough by default.

These are eligible to run for real unless explicitly moved into simulation.

#### Simulated by default

Tools tagged with any of the following are simulated by default:

- `STATE_CHANGING`
- `DESTRUCTIVE`
- `EXTERNAL_COMM`
- `CODE_EXECUTION`

These are action tools for the purposes of the prototype.

#### Unknown metadata

If a tool is missing metadata or its classification is ambiguous, the safe default is simulated, not
passthrough.

### Explicit Overrides

The tool API should allow:

- `simulated_tools`: force named tools into simulation
- `passthrough_tools`: allow specific named tools to run for real

Constraint:

- `passthrough_tools` only applies to tools that are otherwise safe read-only tools
- v1 should not allow a user or model to force action tools into passthrough

### Why the Model Should Not Freely Decide

Allowing the model to decide tool-by-tool whether execution is "real" or "simulated" makes the test
harness nondeterministic and weakens safety guarantees.

The model may suggest a plan, but the tool itself should enforce the final split based on:

- static tool metadata
- explicit tool arguments
- safety defaults

## Action Tools

Action tools should not execute in v1.

Instead, for an action tool call:

1. record the tool name and arguments in the transcript
2. mark the call as simulated
3. generate a realistic return value via the simulation LLM

This gives the script something realistic to work with while preventing side effects.

Examples:

- `add_or_update_note` should return something plausible for note creation/update
- `send_message_to_user` should return a realistic acknowledgement/result string
- `schedule_reminder` should return a realistic scheduling result

The transcript should clearly show that the call was simulated and not actually executed.

## Realism Requirements

The goal is for scripts to survive contact with reality.

That implies:

- simulated tools must be real registered Family Assistant tools
- simulated outputs should resemble real outputs for those tools
- outputs must use realistic field names and result shapes
- destructive/state-changing behavior should be represented in the returned value without taking the
  real action

## Historical Few-Shot Retrieval

### Why Use History

The main weakness of generic LLM simulation is output realism. The best low-cost way to improve that
is to retrieve actual historical tool outputs and use them as few-shot examples.

This helps the LLM learn:

- realistic JSON keys
- typical success and error wording
- common result structure
- realistic cardinality and field presence

### Source of Truth

Message history already stores enough information to reconstruct examples:

- assistant messages store `tool_calls`
- tool result messages store:
  - `role="tool"`
  - `tool_name`
  - `tool_call_id`
  - `content`

This allows reconstruction of:

- tool name
- call arguments
- returned content

### Retrieval Policy

Historical examples should be filtered aggressively:

1. First preference: same `conversation_id`
2. Fallback: same `user_id`
3. Never cross users
4. If `user_id` is unavailable, do not fall back beyond the current conversation

This filtering is cheap and provides the right privacy boundary even in a mostly single-tenant
system.

### Example Selection

Use a small number of recent examples per tool call, such as 1-3.

Filter out:

- examples with attachments
- examples whose outputs were converted to attachment references
- oversized outputs
- malformed or unpairable call/result records

### Prompt Use

Historical examples are guidance, not truth.

The simulation prompt should use them to bias output shape and style, while still grounding on:

- the current tool definition
- the scenario description
- the current call arguments

## Tool Output Fidelity

### Current Limitation: No Output Schema Metadata

The tool system currently defines input schemas, but not output schemas.

This means a fully generic "simulate tool X and guarantee a valid return object according to its
output schema" system is not possible yet.

This is the main reason the prototype should remain narrow.

### Practical v1 Strategy

Use a layered approach:

1. Per-tool adapters or normalizers where needed
2. Historical few-shot examples from message history
3. LLM synthesis from scenario, tool definition, and current arguments

This gives reasonable realism without pretending the system has formal output schemas when it does
not.

### `ToolResult.data`

For many tools, persisted history is sufficient for v1 even if the original result passed through
stringification before storage.

That still preserves useful realism for:

- field names
- JSON structure
- typical message text
- common success/error formats

### Attachments

Attachments are a problem area and should be out of scope for v1 simulation examples.

For v1:

- do not use attachment-bearing tool outputs as few-shot examples
- do not attempt to synthesize realistic attachment objects
- do not claim support for attachment-heavy tools as simulated tools unless handled explicitly later

## Simulation Prompt Design

The simulation LLM should receive:

- the scenario description
- tool name
- tool description
- tool parameter schema
- current call arguments
- whether the tool is normally read-only or action-oriented
- prior simulated call history from the current run
- a small number of historical real examples

The prompt should instruct the model to:

- return a realistic value for this specific tool
- match real field names and shape as closely as possible
- avoid inventing capabilities not implied by the tool or examples
- stay internally consistent across repeated calls in the same run

## Model Selection

The testing tool should not expose a `model` parameter.

Reason:

- users and models will often pick outdated or poor choices such as `gpt-4o` or `gemini-1.5`
- model selection for simulation is an implementation detail
- consistency matters more than caller choice here

The tool should use the system default one-shot LLM configuration internally.

## Safety Model

### Default Safety Rules

- read-only tools may run for real
- action tools are simulated by default
- ambiguous tools are simulated
- no attachment simulation in v1

### No Real Side Effects for Action Tools

This is a core property of the prototype.

The tool must never execute side-effecting actions as part of the simulation workflow.

### No Arbitrary Fake Tools

The tool should only expose tools that already exist in the current tool provider.

This keeps the script grounded in the real system.

## API Shape

Tentative tool parameters:

- `script`: Monty script to execute
- `scenario_description`: plain text description of how tools should behave
- `simulated_tools`: optional list of tool names to simulate explicitly
- `passthrough_tools`: optional list of read-only tool names allowed to execute
- `globals`: optional script globals

Tentative return structure:

- final script result
- list of tool calls with:
  - tool name
  - arguments
  - mode (`passthrough` or `simulated`)
  - result
  - source of simulation examples used, if any

## Implementation Plan

### Phase 1: Prototype

1. Add a design note for the prototype.
2. Add a new local tool module for script testing.
3. Build a wrapper `ToolsProvider` that can:
   - classify calls
   - pass through safe tools
   - simulate selected tools
   - record a transcript
4. Add history retrieval for real `(arguments, result)` examples.
5. Implement the LLM simulation prompt.
6. Return script result plus transcript.

### Phase 2: Hardening

1. Add per-tool output adapters for the most important simulated tools.
2. Expand tests around read-only vs action-tool classification.
3. Consider attachment-aware simulation later if needed.

### Possible Long-Term Improvement

The proper long-term fix is to add optional output-schema or simulation metadata to tool
descriptors. That would make broad, schema-faithful simulation much more reliable.

The prototype should be structured so it can adopt such metadata later without redesign.

## Testing Strategy

Tests should verify:

- read-only tools default to passthrough
- action tools default to simulation
- explicit simulation overrides work
- explicit passthrough only works for safe tools
- historical examples are only drawn from:
  - same conversation first
  - same user second
- cross-user examples are never used
- attachment-bearing examples are excluded
- transcripts clearly mark simulated vs real calls
- action tools are not actually executed

## Open Questions

1. Which initial set of tools should have explicit output adapters in v1?
2. Should the returned transcript be plain text, structured data, or both?
3. Should history retrieval be recency-based only, or should it later include semantic matching on
   arguments?

## Recommendation

Proceed with the prototype as a dedicated LLM tool built on a wrapper `ToolsProvider`.

Key decisions:

- use real registered tools only
- simulate action tools by default
- allow passthrough for read-only tools only
- use real historical examples from the same conversation or same user
- exclude attachments from v1 simulation examples
- keep model choice internal

This is the cheapest path that materially improves realism while preserving safety.
