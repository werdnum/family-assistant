# Monitoring Guide

This guide covers monitoring, logging, and observability for Family Assistant deployments.

## Overview

Effective monitoring is essential for:

- **Availability**: Ensuring the service is responsive and healthy
- **Performance**: Identifying slow queries, LLM latency, and resource constraints
- **Debugging**: Troubleshooting issues with detailed logs
- **Capacity Planning**: Understanding usage patterns and resource needs

Family Assistant provides health endpoints, structured logging, and hooks for external monitoring
systems.

______________________________________________________________________

## Health Endpoints

### GET /health

The primary health check endpoint for container orchestration and load balancers.

**Response Format**:

```json
{
  "status": "ok",
  "reason": "Telegram polling active"
}
```

**Health States**:

| Status         | HTTP Code | Description                             | Action                            |
| -------------- | --------- | --------------------------------------- | --------------------------------- |
| `ok`           | 200       | Service fully operational               | None                              |
| `healthy`      | 200       | Web service running (Telegram disabled) | None                              |
| `initializing` | 200       | Service starting up                     | Wait for initialization           |
| `unhealthy`    | 503       | Service degraded or failed              | Check logs, restart if persistent |

**Failure Scenarios**:

- **Telegram polling stopped**: The bot lost connection to Telegram API
- **Telegram Conflict error**: Another bot instance is running with the same token
- **Service initialization error**: Telegram service failed to start properly

**Kubernetes Configuration** (from `deploy/deployment.yaml`):

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 15
  periodSeconds: 20
  timeoutSeconds: 5
  failureThreshold: 3
```

**Recommended Readiness Probe**:

```yaml
readinessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 2
```

______________________________________________________________________

## Logging

### Log Format

Logs use a standard format:

```
%(asctime)s - %(name)s - %(levelname)s - %(message)s
```

Example:

```
2025-01-10 14:30:45,123 - family_assistant.web.routers.chat_api - INFO - Processing chat request for conversation abc123
```

### Log Levels

| Level    | Usage                                           |
| -------- | ----------------------------------------------- |
| DEBUG    | Detailed diagnostic information                 |
| INFO     | Normal operational messages                     |
| WARNING  | Unexpected but handled conditions               |
| ERROR    | Errors that prevent specific operations         |
| CRITICAL | Severe errors that may require immediate action |

### Where Logs Are Written

By default, logs are written to **stdout** (standard output), making them compatible with container
logging systems like Docker, Kubernetes, and cloud logging services.

### Logging Configuration

#### Environment Variable

Set `LOGGING_CONFIG` to point to a custom logging configuration file:

```bash
LOGGING_CONFIG=/etc/family-assistant/logging.conf
```

#### Default Configuration File

The default `logging.conf` provides sensible defaults:

```ini
[loggers]
keys=root,httpx,telegram,apscheduler,caldav,task_worker_logger,storage_tasks_logger

[handlers]
keys=consoleHandler

[formatters]
keys=standardFormatter

[logger_root]
level=INFO
handlers=consoleHandler

[logger_httpx]
level=WARNING
handlers=consoleHandler
qualname=httpx
propagate=0

[logger_telegram]
level=INFO
handlers=consoleHandler
qualname=telegram
propagate=0

[handler_consoleHandler]
class=StreamHandler
level=DEBUG
formatter=standardFormatter
args=(sys.stdout,)

[formatter_standardFormatter]
format=%(asctime)s - %(name)s - %(levelname)s - %(message)s
```

#### Adjusting Log Levels

To increase verbosity for specific components, modify or create a custom `logging.conf`:

```ini
[logger_family_assistant_llm]
level=DEBUG
handlers=consoleHandler
qualname=family_assistant.llm
propagate=0
```

### LLM Message Debugging

Set `DEBUG_LLM_MESSAGES=true` to log detailed LLM request/response payloads:

```bash
DEBUG_LLM_MESSAGES=true
```

This enables logging of:

- Full message history sent to LLM
- Tool definitions and tool choice
- Complete LLM responses

**Warning**: This produces verbose output and may log sensitive user data. Use only for debugging in
development or controlled environments.

Example output:

```
2025-01-10 14:30:45,123 - family_assistant.llm - INFO - LLM Request to gemini/gemini-2.5-pro:
[{"role": "system", "content": "..."}, {"role": "user", "content": "What's the weather?"}]
```

### LLM Debug Mode

For debugging LLM provider issues:

```bash
DEBUG_LLM_MESSAGES=true
```

This enables debug logging for LLM API calls to OpenAI, Anthropic, Google, and other providers.

______________________________________________________________________

## Metrics

Family Assistant exports Prometheus metrics on a port of its own — **9090** by default, not the
application port. The application port is typically published by an Ingress in its entirety, and
token spend, model line-up and error rates are not public information; a separate port keeps the
exporter reachable from inside the cluster and nowhere else.

```
curl http://family-assistant:9090/metrics
```

Set `METRICS_PORT` to move it, or `METRICS_ENABLED=false` to turn it off. The rationale and the
chokepoints the numbers come from are in
[docs/design/prometheus-metrics.md](../design/prometheus-metrics.md).

Standard process and Python runtime metrics (`process_resident_memory_bytes`, `python_gc_*`, …) are
exported alongside the application ones.

**What is counted.** Every LLM call on the chat path — streamed or not, successful, failed or
abandoned. Structured-output calls (tool-call review), tools executed from an approved durable
confirmation, and managed-agent runs (`coder`, Deep Research) are not counted; see the design doc's
[deliberate simplifications](../design/prometheus-metrics.md#deliberate-simplifications).

### LLM

Every LLM metric carries the same five labels:

| Label            | Meaning                                                                  |
| ---------------- | ------------------------------------------------------------------------ |
| `profile`        | The processing profile whose turn made the call; `none` outside a turn   |
| `provider`       | `anthropic`, `openai`, `google`, …                                       |
| `model`          | The model as configured                                                  |
| `resolved_model` | The model the provider reports serving (an alias resolves to a snapshot) |
| `operation`      | `chat`, `structured`, `embedding`, `image`, or the managed agent's name  |

| Metric                                              | Type      | Extra labels            |
| --------------------------------------------------- | --------- | ----------------------- |
| `family_assistant_llm_tokens_total`                 | Counter   | `kind`                  |
| `family_assistant_llm_calls_total`                  | Counter   | `outcome`, `error_type` |
| `family_assistant_llm_call_duration_seconds`        | Histogram | `outcome`               |
| `family_assistant_llm_time_to_first_output_seconds` | Histogram | —                       |

`kind` splits tokens into five **disjoint** buckets, normalised so that they mean the same thing on
every provider — providers disagree about whether their own cache and reasoning counts are separate
buckets or subsets, and that correction is applied before export rather than in every query:

| `kind`           | Meaning                                                         |
| ---------------- | --------------------------------------------------------------- |
| `input_uncached` | Prompt tokens neither read from nor written to the prompt cache |
| `cache_read`     | Prompt tokens served from the prompt cache                      |
| `cache_write`    | Prompt tokens written into the prompt cache                     |
| `output`         | Generated tokens, excluding reasoning                           |
| `reasoning`      | Reasoning / thinking tokens                                     |

A bucket a provider does not report is absent rather than zero, so no `cache_read` series means
"this provider or model does not report caching", not "nothing was cached".

`family_assistant_llm_time_to_first_output_seconds` is only observed for streamed calls, where it is
the latency the user actually feels.

### Tools and turns

| Metric                                   | Type      | Labels                       |
| ---------------------------------------- | --------- | ---------------------------- |
| `family_assistant_tool_calls_total`      | Counter   | `profile`, `tool`, `outcome` |
| `family_assistant_tool_duration_seconds` | Histogram | `profile`, `tool`            |
| `family_assistant_turns_total`           | Counter   | `profile`, `outcome`         |
| `family_assistant_turn_duration_seconds` | Histogram | `profile`, `outcome`         |
| `family_assistant_turns_in_progress`     | Gauge     | `profile`                    |

Tool `outcome` is `success`, `denied` (refused by tool policy), `not_found`, `cancelled`, or
`error`. Turn `outcome` is `success`, `error`, or `cancelled` — a browser that navigated away
mid-turn is not a failure.

### Useful queries

Token spend per profile, in tokens per second:

```promql
sum by (profile) (rate(family_assistant_llm_tokens_total[1h]))
```

Which profile is spending on reasoning rather than answers:

```promql
sum by (profile) (rate(family_assistant_llm_tokens_total{kind="reasoning"}[1h]))
  / sum by (profile) (rate(family_assistant_llm_tokens_total{kind=~"output|reasoning"}[1h]))
```

Prompt-cache hit rate — the denominator is the reconstructed full prompt, which is what makes this
comparable across providers:

```promql
sum by (model) (rate(family_assistant_llm_tokens_total{kind="cache_read"}[1h]))
  / sum by (model) (
      rate(family_assistant_llm_tokens_total{kind=~"input_uncached|cache_read|cache_write"}[1h])
    )
```

Whether a profile is being served by its configured model or by a fallback:

```promql
sum by (profile, model, resolved_model) (rate(family_assistant_llm_calls_total[1h]))
```

LLM calls per turn, which is where a runaway tool loop shows up:

```promql
sum by (profile) (rate(family_assistant_llm_calls_total[1h]))
  / sum by (profile) (rate(family_assistant_turns_total[1h]))
```

Error rate by provider:

```promql
sum by (provider) (rate(family_assistant_llm_calls_total{outcome="error"}[15m]))
  / sum by (provider) (rate(family_assistant_llm_calls_total[15m]))
```

Estimated cost, if you keep a price table in a recording rule — prices are deliberately not compiled
into the binary, where they would go stale silently:

```promql
sum by (profile) (
  rate(family_assistant_llm_tokens_total[1h]) * on (model, kind) group_left
    llm_price_per_token
)
```

______________________________________________________________________

## Alerting Recommendations

### Critical Alerts

These require immediate attention:

| Condition                    | Threshold    | Suggested Action              |
| ---------------------------- | ------------ | ----------------------------- |
| Health check failing         | 3+ failures  | Investigate and restart       |
| Telegram Conflict error      | Any          | Check for duplicate instances |
| Database connection failures | 5+ in 5 min  | Check database availability   |
| LLM API errors (5xx)         | 10+ in 5 min | Check provider status         |
| Memory usage                 | > 90%        | Scale up or investigate leaks |

### Warning Alerts

These indicate potential issues:

| Condition             | Threshold     | Suggested Action           |
| --------------------- | ------------- | -------------------------- |
| LLM request latency   | p95 > 30s     | Review model selection     |
| HTTP request latency  | p95 > 5s      | Investigate slow endpoints |
| Error rate            | > 5%          | Review error logs          |
| Background task queue | > 100 pending | Check worker capacity      |
| Disk usage            | > 80%         | Clean up or expand storage |

### Example Prometheus Alert Rules

```yaml
groups:
  - name: family-assistant
    rules:
      - alert: HealthCheckFailing
        expr: probe_success{job="family-assistant-health"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Family Assistant health check failing"

      - alert: HighLLMLatency
        expr: >-
          histogram_quantile(
            0.95,
            sum by (le, profile) (
              rate(family_assistant_llm_call_duration_seconds_bucket[15m])
            )
          ) > 30
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High LLM response latency (p95 > 30s) on {{ $labels.profile }}"

      - alert: LLMCallsFailing
        expr: >-
          sum by (provider) (rate(family_assistant_llm_calls_total{outcome="error"}[15m]))
            / sum by (provider) (rate(family_assistant_llm_calls_total[15m])) > 0.1
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "Over 10% of {{ $labels.provider }} LLM calls are failing"

      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) > 0.05
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "HTTP error rate above 5%"
```

______________________________________________________________________

## Troubleshooting with Logs

### Common Log Patterns

#### Startup Sequence

Normal startup logs:

```
INFO - Initialized config with code defaults.
INFO - Loaded and merged configuration from config.yaml
INFO - Resolved 3 service profiles. Default ID: default_assistant
INFO - Starting application via Assistant class...
INFO - Stored final AppConfig in FastAPI app state.
```

#### Telegram Polling Issues

```
WARNING - Health check failing due to Telegram Conflict: ...
```

**Cause**: Another instance is using the same bot token. **Solution**: Ensure only one instance is
running, or use webhooks for multiple replicas.

#### LLM API Errors

```
ERROR - LLM response structure unexpected or empty for model gemini/gemini-2.5-pro
WARNING - LLM Request failed: API rate limit exceeded
```

**Cause**: Provider issues or quota exhaustion. **Solution**: Check provider dashboard, implement
retry logic, or switch models.

#### Database Connection Issues

```
ERROR - Database connection failed: connection refused
```

**Cause**: Database unreachable. **Solution**: Verify `DATABASE_URL`, check database
container/service status.

#### MCP Server Failures

```
ERROR - MCP server 'brave' failed to initialize within 60 seconds
WARNING - Environment variable expansion failed in MCP config: ...
```

**Cause**: MCP server process failed to start or missing environment variables. **Solution**: Check
MCP configuration, verify required environment variables.

#### MCP Tool List Changes

```
INFO - MCP server 'brave' reported a changed tool list on health check: now 3 tool(s) (added: brave_search; removed: none)
```

**Cause**: the tool list a server reports is re-read on every health check, so the tools the
assistant can reach follow the server rather than being frozen at connect time. A server that came
up reporting the wrong set — most damagingly an empty one — recovers on its own within one health
check interval. **Solution**: none needed for a one-off. A server that flips its list back and forth
every cycle is misbehaving; check that server's own logs.

### Debugging Tips

1. **Enable DEBUG_LLM_MESSAGES** to see full LLM request/response cycles
2. **Check health endpoint** first for quick status assessment
3. **Filter by logger name** to focus on specific components:
   ```bash
   grep "family_assistant.llm" logs.txt
   ```
4. **Look for ERROR and WARNING** patterns:
   ```bash
   grep -E "(ERROR|WARNING)" logs.txt | tail -50
   ```
5. **Correlate by timestamp** when investigating specific incidents

### Where to Look for Issues

| Issue Type              | Logger/Component                   |
| ----------------------- | ---------------------------------- |
| LLM request failures    | `family_assistant.llm`             |
| Telegram bot issues     | `telegram`, health endpoint logs   |
| Database errors         | `family_assistant.storage`         |
| Tool execution problems | `family_assistant.tools`           |
| Calendar integration    | `caldav`, `family_assistant.tools` |
| Background tasks        | `family_assistant.task_worker`     |
| Web API issues          | `family_assistant.web`             |
| MCP server problems     | `family_assistant.tools.mcp`       |

______________________________________________________________________

## Example Setups

### Basic Logging Aggregation

#### Docker Compose with Loki

```yaml
version: "3.8"
services:
  family-assistant:
    image: family-assistant:latest
    logging:
      driver: loki
      options:
        loki-url: "http://loki:3100/loki/api/v1/push"
        loki-batch-size: "400"

  loki:
    image: grafana/loki:latest
    ports:
      - "3100:3100"
    volumes:
      - loki-data:/loki

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    environment:
      - GF_AUTH_ANONYMOUS_ENABLED=true
```

#### Kubernetes with Fluent Bit

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: fluent-bit-config
data:
  fluent-bit.conf: |
    [INPUT]
        Name              tail
        Path              /var/log/containers/family-assistant*.log
        Parser            docker
        Refresh_Interval  10

    [OUTPUT]
        Name          forward
        Match         *
        Host          fluentd
        Port          24224
```

### Prometheus/Grafana Stack

Point Prometheus at the metrics port (9090 by default), not the application port:

```yaml
scrape_configs:
  - job_name: "family-assistant"
    static_configs:
      - targets: ["family-assistant:9090"]
    scrape_interval: 30s
```

```yaml
version: "3.8"
services:
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    ports:
      - "9091:9090"

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
    volumes:
      - grafana-data:/var/lib/grafana
```

### Uptime Monitoring

Use external monitoring services to check the `/health` endpoint:

- **UptimeRobot**: Free tier supports 50 monitors
- **Pingdom**: Enterprise-grade monitoring
- **Better Stack (formerly Better Uptime)**: Incident management included

Example check configuration:

- URL: `https://your-domain.com/health`
- Method: GET
- Expected status: 200
- Interval: 1 minute
- Alert after: 2 failures

______________________________________________________________________

## Related Documentation

- [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md) - Complete environment variable reference
- [../deployment/DEPLOYMENT.md](../deployment/DEPLOYMENT.md) - Deployment guide (if available)
