# Claude Agent SDK Profile

## Context

Most of what Family Assistant spends on inference goes to Anthropic's API: Opus 5.5 on the `deep`
tier and Fable 5.2 on `frontier`, which `complex_tasks` runs long tool loops on. A Fable turn on
`complex_tasks` measured $3.81 in November. The same models are available on the household's Claude
subscription, which is a flat fee, through Claude Code. The
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview) is Claude Code packaged as a
library: it runs the Claude Code CLI as a subprocess and drives its agent loop. Claude Code
authenticates to a subscription with a long-lived OAuth token from `claude setup-token`
(`CLAUDE_CODE_OAUTH_TOKEN`), which its documentation offers for "CI pipelines, scripts, or other
environments where interactive browser login isn't available".

The Antigravity `coder` profile (\[gemini-antigravity-managed-agent.md\](gemini-antigravity-managed-
agent.md)) is the precedent for handing a whole turn to a provider-side agent. It is a [C] profile
with no Family Assistant tools, because its agent runs in Google's sandbox. Moving the expensive
Claude work is a different requirement: that work is `complex_tasks`-shaped, and it needs the
household's tools — notes, calendar, documents, Home Assistant, delegation — or it is not a
replacement for anything.

## Decision

A new profile, **`claude_agent`**, runs its turn on the Agent SDK, with Family Assistant's own tools
made available to Claude Code as an in-process MCP server. It is a **[BC]** profile, the same class
as `complex_tasks`: what changes is who runs the loop and who pays for it, not what the turn may
reach.

### The Agent SDK runs the loop; Family Assistant runs every tool

The alternative was to wrap the SDK as an `LLMInterface` and keep `LLMStreamingLoop` in charge,
using a `PreToolUse` hook's `"defer"` decision to stop Claude Code at every tool call and hand the
call back. That makes every model call a fresh CLI process resuming a session transcript that Family
Assistant does not own, so there are two copies of the conversation that have to agree — after
compaction, after the history window slides, after a turn is retried. It also gives up what makes
Claude Code worth using: its own context management and prompt caching within a long run.

So one turn is one Agent SDK session, and a **`ClaudeAgentLoop`** replaces `LLMStreamingLoop` for
the profile. It yields the same `(LLMStreamEvent, LLMMessage)` stream, so persistence, streaming to
the web and Telegram, delegation and telemetry are unchanged. Every tool call Claude Code makes
reaches `ToolExecutor.execute` — the same entry point the ordinary loop uses — so tool policy,
confirmation, taint tracking, tool-call review and attachment handling apply to each call exactly as
they do on `complex_tasks`. The Agent SDK's MCP handler is not told which `tool_use` it is serving;
a `PreToolUse` hook, which is, records the id so the handler can execute the call under it, and the
history Family Assistant saves carries the model's own ids.

### Claude Code holds nothing of its own

The CLI runs inside the application's container, so anything Claude Code can do natively it does
with the application's privileges. It therefore gets none of it:

- **No built-in tools** except `ToolSearch`, which searches the tools it has already been given and
  has no other effect. Bash, file access, web fetch and subagents are absent from the session, not
  merely denied.
- **No filesystem configuration.** `setting_sources=[]`, `skills=[]` and `strict_mcp_config` mean no
  settings file, hook, plugin, skill, `CLAUDE.md` or MCP server from the container's disk is loaded;
  the working directory is an empty temporary directory; `--no-session-persistence` keeps the
  conversation off disk.
- **Prompts delivered verbatim.** The opening message carries rendered history, which other people
  wrote. `verbatim_prompts` stops Claude Code expanding an `@/path` in it into a local file's
  contents, or reading a leading `/` as one of its own slash commands.
- **Permission mode `dontAsk`**, with only the Family Assistant MCP server allowed, so a tool that
  somehow appears is refused rather than prompted for.

`ToolSearch` also stands in for on-demand tool activation. The ordinary loop hides on-demand tools
behind `activate_tools` to protect the context window; Claude Code already defers MCP tools and
loads them by search, so the session is offered every tool that passes the profile's policy and
`activate_tools` is not one of them. On-demand gating was never a permission (the script engine sees
every tool that passes policy), so nothing is widened.

### The subscription is enforced, not assumed

The application's environment carries `ANTHROPIC_API_KEY` for the `anthropic` provider, and Claude
Code prefers an API key to a subscription token. Left alone, every run would be billed to the API at
list price under a profile whose reason to exist is not to be. The loop blanks the API-key variables
in the subprocess's environment, and refuses the turn unless the session reports that no API key is
in use (`apiKeySource: "none"`). A misconfigured deployment fails visibly instead of quietly
spending.

### The model is a tier

As on `coder` ([antigravity-model-tiers.md](antigravity-model-tiers.md)), the model a run uses is
the profile's model tier: every tier the profile can reach must be exactly one `anthropic` model,
with no fallback, because Claude Code takes a model and an effort, not a recipe. The tier's thinking
effort is passed through. The tier's ordinary client remains the profile's client for the few
auxiliary calls a turn can make outside the loop — selecting among many queued attachments, for
example — which go to the API as they do today.

`claude_agent` ships on `claude_opus_5_5` by default, with `claude_fable_5_2` and
`claude_sonnet_5_5` selectable by a person and by a delegating model.

### Conversation history is rendered, not resumed

Each turn starts a fresh session. Earlier turns in the history window reach it as a transcript in
the first message rather than as a resumed Claude Code session, so the history Family Assistant
saves stays the only copy, and windowing and compaction work unchanged. Within a turn the
conversation is native: real tool calls, real results, Claude Code's own caching.

### Reported usage is not spend

Each saved message carries the usage Claude Code reports, under provider `claude_agent_sdk`, so cost
reporting that prices `anthropic` calls does not count subscription runs as API spend. What a run
costs is subscription quota, which these numbers measure but do not price.

## Terms of use

Anthropic's Agent SDK documentation says: "Unless previously approved, Anthropic does not allow
third party developers to offer claude.ai login or rate limits for their products, including agents
built on the Claude Agent SDK." This profile does not offer anyone a claude.ai login: the operator
configures their own token for their own household deployment. Whether a deployment's use falls
within the subscription's terms — in particular when household members other than the subscriber
reach the profile — is the operator's judgement, and the profile is opt-in per deployment for that
reason: it runs only where a token has been configured.

## Deliberate simplifications

- **No resume across turns.** A fresh session per turn re-reads the rendered history, which costs
  subscription quota rather than money. Resuming Claude Code sessions would need their transcripts
  persisted alongside Family Assistant's history and kept consistent with it.
- **No mid-turn input.** A message sent while a turn runs waits for the next turn instead of being
  steered into this one. Claude Code accepts queued messages, so this is follow-up work, not a
  limitation of the runtime.
- **Attachments on the opening message reach the model as images only.** Other attachment types are
  named in the prompt by id, for the model to read with Family Assistant's attachment tools.
- **One process per turn.** Starting the CLI adds a second or two to each turn, acceptable on a
  profile chosen for long tasks.
- **No per-call LLM metrics.** A session's model calls happen inside Claude Code, so they do not
  pass through the client wrapper that records per-call latency metrics; each saved message still
  carries its call's usage, and the turn is still counted.

## Not in this PR

- Routing existing `deep`/`frontier` traffic to the profile automatically. The profile is reachable
  by `/claude`, the web profile picker and delegation; whether `complex_tasks` should become it is a
  decision for after it has run.
- A `taint_sink_class` for the profile. Its only egress is Family Assistant's tools, which are gated
  per call, so it declares none, and startup refuses one rather than ignoring it.

## Verification

- Unit tests drive `ClaudeAgentLoop` through the Agent SDK's own `Query` machinery against a fake
  CLI transport that speaks the stream-json control protocol, in the frame shapes of a recorded real
  session (kept beside the tests): tool calls reach the executor under the model's ids, results
  return to the session, the saved history is valid tool-call protocol, a session that reports an
  API key is refused, and the iteration limit and a quiet end close the turn.
- Startup tests over the tier rules and the shipped profile, including a full startup whose session
  is offered the household's tools and not `activate_tools`.
- A manual run of the shipped profile against a real subscription, recorded in the PR.
