# Prometheus metrics for LLM usage

## Problem

Family Assistant emits OpenTelemetry spans for every LLM call, and those spans already carry token
usage (`gen_ai.usage.*`). Spans answer "what did *this* turn do"; they do not answer "what has this
deployment spent, on which profile, on which model, over the last month". Jaeger holds a sampled,
short-retention trace store, not a time series.

The questions that go unanswered today are all aggregate and all operational:

- Which processing profile is burning the tokens? (`complex_tasks` at `reasoning_effort: high` and
  `coder` cost very differently from the default assistant.)
- Is prompt caching actually working, and did it stop working after a prompt change?
- How much of the output bill is reasoning rather than answer?
- Which model is actually serving a profile — the configured one, or a fallback?
- Are calls failing, and on which provider?

## Approach

Export Prometheus metrics from a single chokepoint per concept, scraped by the cluster's existing
VictoriaMetrics `vmagent`.

### One chokepoint per concept

| Concept                   | Chokepoint                                              |
| ------------------------- | ------------------------------------------------------- |
| Chat and structured calls | `LLMCallTelemetry` (`llm/utils/call_telemetry.py`)      |
| Image and embedding calls | `instrumented_llm_request` (`observability/metrics.py`) |
| Managed-agent runs        | `Interaction.usage`, on the stream and on the poll      |
| Tool execution            | `ToolExecutor`, and the durable-confirmation worker     |
| Processing turn           | `LLMStreamingLoop.run_stream`                           |

Two request chokepoints rather than one because they answer different questions. `LLMCallTelemetry`
also builds a span and a diagnostics ring-buffer record, worth having for anything conversational;
`instrumented_llm_request` only counts, which is all an embedding batch or an image request needs.

**Every path that reaches a provider goes through one of them.** That is the property worth keeping,
and the one that decays silently: a provider surface added later reaches no counter and nothing
fails. The inventory is a grep over the SDK entry points — `messages.create`, `chat.completions`,
`responses.create`, `generate_content`, `embed_content`, `images.generate`, `interactions.create` —
which is how the image-generation and embedding paths were found.

`LLMCallTelemetry` is already the one place where a provider call's span and its diagnostics record
are assembled, precisely so a provider that reports a new detail reports it everywhere at once.
Metrics are the third record of the same event and belong in the same place: a provider added later
is instrumented by construction rather than by remembering to instrument it.

### The profile label

`LLMCallTelemetry` sits below the provider clients and knows nothing about processing profiles. The
profile reaches it through a `ContextVar` (`llm/call_context.py`), entered once in
`LLMStreamingLoop.run_stream` — which `run()` also funnels through, so both the streaming and the
non-streaming path are covered by a single `with` block. This follows the existing
`request_side_effects` pattern.

LLM calls made outside a profile turn (one-shot helpers, evaluation runners) label `profile="none"`
rather than being dropped: an unattributed cost is still a cost, and a rising `none` series is
itself the signal that something is spending outside a turn.

### Normalising provider token accounting

Providers disagree about what their own numbers mean, and `MessageReasoningInfo` documents the
disagreement rather than resolving it:

- Anthropic reports uncached / cache-read / cache-write as three disjoint buckets, so
  `prompt_tokens` is only the uncached remainder.
- OpenAI and Google report the whole prompt in `prompt_tokens`, with the cache numbers as subsets.
- OpenAI and Anthropic fold reasoning/thinking tokens into `completion_tokens`; Google reports
  `thoughts_token_count` alongside a `candidates_token_count` that excludes it.

A dashboard that summed these raw would double-count on two providers and under-count on a third. So
the metric boundary is where the accounting is normalised, once, into **disjoint** buckets that
every provider maps onto:

| `kind`           | Meaning                                                        |
| ---------------- | -------------------------------------------------------------- |
| `input_uncached` | Prompt tokens that were neither read from nor written to cache |
| `cache_read`     | Prompt tokens served from the prompt cache                     |
| `cache_write`    | Prompt tokens written into the prompt cache                    |
| `output`         | Generated tokens excluding reasoning                           |
| `reasoning`      | Reasoning / thinking tokens                                    |
| `tool_use`       | Tokens the provider spent running its own server-side tools    |

**The buckets are billing tiers, not provider fields.** That is what makes normalising worth doing
rather than merely tidy: each tier is priced differently by every provider, so cost is one join of
`family_assistant_llm_tokens_total` against a price table on `(model, kind)`. A split along any
other axis leaves that arithmetic to every query.

Because the buckets are disjoint, `sum by (kind)` is the total,
`input_uncached + cache_read + cache_write` is the full prompt, and a cache hit rate is
`cache_read / (that sum)` — on every provider. The alternative, exporting each provider's raw fields
and normalising in PromQL, would put the correction in every query and every dashboard instead of in
one function with tests.

Buckets a provider does not report are not emitted at all, so absent means "not reported" rather
than zero — the same distinction the span attributes already preserve.

### Why not OpenTelemetry metrics

The service is already wired for OTel and exports spans over OTLP, so emitting `gen_ai.*` metrics
through the same SDK looks like the smaller change. It is the wrong one here: the cluster runs
`OTEL_METRICS_EXPORTER=none` and collects no OTel metrics at all. Traces go to Jaeger; the metrics
pipeline is VictoriaMetrics scraping Prometheus endpoints, and nothing on the OTLP side would be
read.

Emitting OTel metrics would therefore mean standing up an OTLP metrics receiver and a
Prometheus-remote-write path to feed a store that is already there and already scraping — new
infrastructure to reach the same rows. A `prometheus_client` registry on a scrape endpoint is what
the existing pipeline consumes directly.

### Exposure

Metrics are served on a **separate port** (default 9090, `METRICS_PORT`), not as a route on the main
app. The main app's port is published to the internet through an Ingress, and the whole of `/`
reaches it; a `/metrics` route there would publish the household's token spend, model line-up and
error rates to anyone who asked. A second port is not routed by any Ingress, so the exporter is
reachable only from inside the cluster, which is exactly the audience.

`METRICS_ENABLED=false` turns the exporter off for deployments that do not scrape.

### Scraping

The cluster's `VMAgent` runs `selectAllByDefault: true`, so a `VMPodScrape` in the workload's own
namespace is picked up with no change to the agent. A pod scrape rather than a service scrape: the
`family-assistant` Service exists to back the Ingress, and adding a metrics port to it would widen
the Ingress's reachable surface for no gain.

## Deliberate simplifications

- **Not every source reports tokens, and not every one is billed in them.** Google's embedding API
  reports only `billable_character_count`; the OpenAI image endpoints bill per image. Where a
  provider reports no tokens, no token bucket is emitted and
  `family_assistant_llm_calls_total{operation=...}` is the meter — a zero-valued bucket would read
  as "embedded nothing" rather than "not reported in tokens".

- **No cost metric.** Turning tokens into dollars needs a per-model price table that would go stale
  silently and be wrong for exactly the models that matter (previews, aliases). Prices belong in the
  dashboard's recording rules, where they are visible and editable, not compiled into the binary.

- **`resolved_model` is a label.** It multiplies the series count by the number of dated snapshots a
  configured alias resolves to, which is small and bounded, and it is the only way to see
  provider-side routing and fallback in the time series at all.

- **Process-local state.** The exporter holds counters in the process, so a restart resets them.
  Every metric is a counter or a histogram, so `rate()`/`increase()` handle resets; nothing here
  needs to survive a restart.

## Metrics

All metrics are prefixed `family_assistant_`.

### LLM

Common labels: `profile`, `provider`, `model` (as requested), `resolved_model` (as served),
`operation`.

| Metric                                              | Type      | Extra labels | Notes                     |
| --------------------------------------------------- | --------- | ------------ | ------------------------- |
| `family_assistant_llm_tokens_total`                 | Counter   | `kind`       | The five disjoint buckets |
| `family_assistant_llm_calls_total`                  | Counter   | `outcome`    | `success` / `error`       |
| `family_assistant_llm_call_duration_seconds`        | Histogram | `outcome`    | Whole provider call       |
| `family_assistant_llm_time_to_first_output_seconds` | Histogram | —            | Streamed calls only       |

`family_assistant_llm_errors_total` is not a separate metric: `outcome="error"` on the call counter
carries it, with `error_type` folded in as a label there.

### Tools

| Metric                                   | Type      | Labels                       |
| ---------------------------------------- | --------- | ---------------------------- |
| `family_assistant_tool_calls_total`      | Counter   | `profile`, `tool`, `outcome` |
| `family_assistant_tool_duration_seconds` | Histogram | `profile`, `tool`            |

`outcome` is how the execution ended: `returned`, `denied` (tool policy), `not_found`, `cancelled`,
or `error`. A turn the user abandoned is not a tool failure, and folding it into `error` would put
ordinary browser behaviour into the number the error rate is watched on.

The returning case is `returned` rather than `success` on purpose. A tool reports an expected
failure by returning a `ToolResult`, which has no status field, so the executor cannot tell a
refusal from a result. Calling it `success` would make a tool error rate read as healthy while real
failures flowed through it. Giving tool results a failure status would fix that properly, and is a
change to the tool protocol rather than to this metric — so the metric claims only what it knows.

### Turns

| Metric                                   | Type      | Labels               |
| ---------------------------------------- | --------- | -------------------- |
| `family_assistant_turns_total`           | Counter   | `profile`, `outcome` |
| `family_assistant_turn_duration_seconds` | Histogram | `profile`, `outcome` |
| `family_assistant_turns_in_progress`     | Gauge     | `profile`            |

`outcome` is `success`, `error`, or `cancelled` — a browser that navigated away is not a failure,
and folding it into `error` would fire an error-rate alert on ordinary use.

There is no turn-iterations metric: how many LLM calls a turn took is
`rate(family_assistant_llm_calls_total) / rate(family_assistant_turns_total)` on the same profile,
which the two counters already answer.

The in-progress gauge shows concurrency, and a turn stuck at a nonzero value with no calls flowing
is the shape of a hang.

## Work plan

1. **Exporter and chokepoints.** `observability/metrics.py` with the metric definitions and the
   token normaliser; the `ContextVar`; instrumentation at the three chokepoints; the exporter
   process started from `Assistant`. Verified by unit tests that drive each chokepoint and assert on
   the rendered exposition text, including one test per provider convention proving the five buckets
   are disjoint and sum to what the provider reported.
2. **Documentation.** `docs/operations/MONITORING.md` gains the metric reference and the PromQL for
   the questions in *Problem*; `CONFIGURATION_REFERENCE.md` gains `METRICS_ENABLED` /
   `METRICS_PORT`.
3. **Cluster wiring (kube-config).** The metrics port on the Deployment, a `VMPodScrape`, and a
   `VMRule` with alerts for the failure modes the metrics newly make visible. Verified by
   `pre-commit run --all-files`.
