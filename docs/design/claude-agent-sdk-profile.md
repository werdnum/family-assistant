# Claude Agent SDK Runtime

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

A model tier may name **`runtime: "claude_code"`**: a turn on that tier runs in a Claude Agent SDK
session on the tier's primary model, with Family Assistant's own tools made available to Claude Code
as an in-process MCP server. The shipped `deep` (Opus 5.5) and `frontier` (Fable 5.2) tiers do, and
so do the Claude presets (`claude_opus_5_5`, `claude_sonnet_5_5`, `claude_fable_5_2`), so a Claude
model a person, an experiment or a delegating model picks runs on the subscription too. The removed
`claude_agent` profile's conversations still name those presets, and stay in Claude Code because of
it. The runtime changes who runs the loop and who pays for it, not what the turn may reach, so the
profile a turn is on keeps its Rule of Two class.

This started as a profile, `claude_agent`, carrying `complex_tasks`' job. Running it showed that the
profile was only ever a way to say "this model, run this way": the workflow, tool surface and
prompts were `complex_tasks`'. Saying it on the tier lets every profile that reaches Deep or Max use
the subscription, lets an experiment compare a Claude Code arm against an API arm on the same
profile, and keeps the API as the fallback.

### The Agent SDK runs the loop; Family Assistant runs every tool

The alternative was to wrap the SDK as an `LLMInterface` and keep `LLMStreamingLoop` in charge,
using a `PreToolUse` hook's `"defer"` decision to stop Claude Code at every tool call and hand the
call back. That makes every model call a fresh CLI process resuming a session transcript that Family
Assistant does not own, so there are two copies of the conversation that have to agree — after
compaction, after the history window slides, after a turn is retried. It also gives up what makes
Claude Code worth using: its own context management and prompt caching within a long run.

So one turn is one Agent SDK session, run by a **`ClaudeAgentLoop`** in place of `LLMStreamingLoop`.
It yields the same `(LLMStreamEvent, LLMMessage)` stream, so persistence, streaming to the web and
Telegram, delegation and telemetry are unchanged. Every tool call Claude Code makes reaches
`ToolExecutor.execute` — the same entry point the ordinary loop uses — so tool policy, confirmation,
taint tracking, tool-call review and attachment handling apply to each call exactly as they do on
`complex_tasks`. The Agent SDK's MCP handler is not told which `tool_use` it is serving; a
`PreToolUse` hook, which is, records the id so the handler can execute the call under it, and the
history Family Assistant saves carries the model's own ids.

### Claude Code holds nothing of its own

The CLI runs inside the application's container, so anything Claude Code can do natively it does
with the application's privileges. It therefore gets none of it:

- **No built-in tools** except `ToolSearch`, which searches the tools it has already been given and
  has no other effect. Bash, file access, web fetch and subagents are absent from the session, not
  merely denied.
- **No filesystem configuration.** `setting_sources=[]`, `skills=[]` and `strict_mcp_config` mean no
  settings file, hook, plugin, skill, `CLAUDE.md` or MCP server from the container's disk is loaded;
  the working directory is an empty directory; the session's local transcript is removed when it
  ends, and the copy kept for resuming a delegation wake lives in the database (see
  [claude-agent-session-resume.md](claude-agent-session-resume.md)).
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
list price by a runtime whose reason to exist is not to be. The loop blanks the API-key variables in
the subprocess's environment, and refuses the session unless it reports that no API key is in use
(`apiKeySource: "none"`). The refusal comes before any output, so the turn falls back to the API
openly: counted as `billing_refused`, logged, and priced as the API spend it is.

### The runtime is a tier property, and the service picks the loop per turn

A tier already says which model runs and how; where it runs is the same kind of fact. A
`claude_code` tier's primary must be an `anthropic` model, and its parameters must be ones Claude
Code accepts (adaptive thinking, an effort, custom instructions), so the session runs the recipe the
tier names. The chain stays an ordinary chain, fallback included, because it is still used: by
deployments without a token, for the turn's auxiliary calls, and as the fallback below.

Each profile's service holds the ordinary loop and, where any tier it can reach is `claude_code` and
the deployment has a token, a `ClaudeAgentLoop` for those tiers. The turn's tier is resolved once
before the loop runs, as it always was; the service runs the turn on the loop that tier names. Both
loops write history in the same form, plain text and tool calls, so a conversation can move between
them: an experiment arm, a person picking Deep, or a fallback all just change which loop the next
turn runs on.

This reverses, for this runtime only, the rule that server-side agent runtimes are not tiers.
Antigravity and Interactions agents stay off tiers because they drop the profile's tools and
history. Claude Code keeps both, which is the condition: a tier may name an agent runtime only if
the runtime runs Family Assistant's tools and reads its history.

### A session that fails before producing anything falls back to the API

The subscription has rate-limit windows shared with the household's other Claude use, and a token
can lapse. When a session fails before it has yielded anything, nothing has been shown, saved or
executed, so the turn reruns on the tier's chain through the ordinary loop and a warning is logged.
Once a session has produced output, a failure fails the turn: tool calls are saved before they run,
and rerunning the turn could repeat one that took effect.

### Conversation history is rendered, not resumed

Each turn starts a fresh session. Earlier turns in the history window reach it as a transcript in
the first message rather than as a resumed Claude Code session, so the history Family Assistant
saves stays the only copy, and windowing and compaction work unchanged. Within a turn the
conversation is native: real tool calls, real results, Claude Code's own caching.

A turn interrupted by a graceful shutdown is the exception: its session is interrupted cleanly and
the next process resumes it (see [claude-agent-session-resume.md](claude-agent-session-resume.md)).
Where that is not possible, after a crash or a change of setup, the turn is rebuilt: a fresh session
opens with the turn's request, followed by the steps its interrupted run saved, rendered as a
transcript of calls that have already run, and continues from there.

### It is observable from outside

Each model call inside a session is counted in the ordinary LLM call and token metrics, under
provider `claude_agent_sdk`, so existing dashboards show it beside the API providers. A session
counter records how each session ended, including the refusals and authentication failures that mean
the deployment is misconfigured; Claude Code's own reports of the subscription's rate-limit windows
are exported as gauges; and a startup check, which spends no quota, exports whether the binary runs
and would use the subscription rather than an API key. Sessions log their start, the credential and
Claude Code version they ran with, and how they ended.

### Reported usage is not spend

Each saved message carries the usage Claude Code reports, under provider `claude_agent_sdk`, so cost
reporting that prices `anthropic` calls does not count subscription runs as API spend. What a run
costs is subscription quota, which these numbers measure but do not price.

## Terms of use

Anthropic's Agent SDK documentation says: "Unless previously approved, Anthropic does not allow
third party developers to offer claude.ai login or rate limits for their products, including agents
built on the Claude Agent SDK." This runtime does not offer anyone a claude.ai login: the operator
configures their own token for their own household deployment. Whether a deployment's use falls
within the subscription's terms — in particular when household members other than the subscriber
reach Deep or Max — is the operator's judgement, and the runtime is opt-in per deployment for that
reason: it runs only where a token has been configured.

## Deliberate simplifications

- **No resume across ordinary turns.** A fresh session per turn re-reads the rendered history, which
  costs subscription quota rather than money. A delegation wake is the exception: it resumes the
  session that delegated, when that session provably agrees with the history (see
  [claude-agent-session-resume.md](claude-agent-session-resume.md)).
- **No mid-turn input.** A message sent while a turn runs waits for the next turn instead of being
  steered into this one, which on `default_assistant` is a visible difference from Standard. Claude
  Code accepts queued messages, so this is follow-up work, not a limitation of the runtime.
- **Attachments on the opening message reach the model as images only.** Other attachment types are
  named in the prompt by id, for the model to read with Family Assistant's attachment tools.
- **One process per turn.** Starting the CLI adds a second or two to each turn, acceptable on the
  tiers chosen for hard requests.
- **No fallback mid-turn.** A session that fails after a tool call fails the turn rather than
  continuing on the API from the rows saved so far. The common failures (an exhausted window, a
  rejected token) happen before anything is produced.
- **A fallen-back turn counts twice in the turn metrics**, once as an error on the session and once
  on the API loop; session outcomes say which it was.
- **The startup check cannot validate the token.** `claude auth status` reads local state only, so
  it shows the binary runs and that a subscription credential is the one in use, not that the token
  is accepted. A rejected token shows as `auth_failed` sessions.

## Not in this design

- A `taint_sink_class` on a profile that reaches a `claude_code` tier. The session's only egress is
  Family Assistant's tools, which are gated per call; startup refuses a profile-level sink rather
  than ignoring it.

## Verification

- Unit tests drive `ClaudeAgentLoop` through the Agent SDK's own `Query` machinery against a fake
  CLI transport that speaks the stream-json control protocol, in the frame shapes of a recorded real
  session (kept beside the tests): tool calls reach the executor under the model's ids, results
  return to the session, the saved history is valid tool-call protocol, a session that reports an
  API key is refused, and the iteration limit and a quiet end close the turn.
- Startup tests over the tier rules and the shipped tiers, including a full startup whose session is
  offered the household's tools and not `activate_tools`, and one without a token whose tiers run on
  their chain.
- Service tests that a turn on a tier with no session model runs on the ordinary loop, that a
  session refused before output reruns on the API, and that one failing after a tool call fails.
- A manual run against a real subscription, recorded in the PR that introduced the runtime.
