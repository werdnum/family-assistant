# Tool-call review batch runbook

The staged batch runner uses OpenRouter's asynchronous batch endpoint while keeping every request,
result, reason, and manifest private. Preparation is local and refuses to reuse a run directory:

```bash
python scripts/tool_call_review_batch.py prepare \
    --dataset .review-eval-local/public/deepset-browser-ablation-gate \
    --dataset .review-eval-local/public/injecagent-browser-ablation-both-gate \
    --tool-registry .review-eval-local/registry/deployment.json \
    --model google/gemini-3.7-flash \
    --seeds 1 --batch-size 500 --max-tokens 512 \
    --run-dir .review-eval-local/runs/batch-gate-2026-08-31
```

Use `prepare --dry-run` to validate inputs without writing artifacts. Submission is the only
network-spending phase and requires a positive operator-approved amount:

```bash
python scripts/tool_call_review_batch.py submit \
    --run-dir .review-eval-local/runs/batch-gate-2026-08-31 \
    --approved-spend-usd 10 --approve-spend
python scripts/tool_call_review_batch.py poll \
    --run-dir .review-eval-local/runs/batch-gate-2026-08-31
python scripts/tool_call_review_batch.py harvest \
    --run-dir .review-eval-local/runs/batch-gate-2026-08-31
```

`--max-tokens` bounds each response (default 512); truncation is unavailable evidence and fails
harvest rather than becoming a verdict. `--approved-spend-usd` requires a finite, positive amount
and records the operator's approval; it is not an enforceable provider-side spend cap. OpenRouter
usage/cost fields are optional remote metadata and may be absent. The manifest records
dataset/model/provider, request IDs, request SHA-256, remote batch IDs, statuses, optional usage,
and approval metadata; it does not record raw event streams. `status` is a single poll and `poll`
repeats it. Polling exits unsuccessfully for failed, expired, cancelled, or unknown terminal states.
Remote status responses must contain one of OpenRouter's documented lifecycle statuses
(`validating`, `in_progress`, `finalizing`, `cancelling`, `completed`, `failed`, `expired`, or
`cancelled`); missing or unknown statuses fail closed. If submission already reports `completed`
without an inline `results` list, the manifest leaves the result artifact unset so the next `status`
poll fetches and persists the completed result before harvest. Polling also fails immediately when
any chunk is still `pending` without a provider batch ID; run `submit` (or retry a definitive
rejected submission) before polling. Each polled response must also identify the exact provider
batch recorded for that chunk; an absent or different batch ID fails closed before status, usage, or
result artifacts are persisted.

Harvest refuses incomplete batches, item errors, missing IDs, duplicate IDs, extra IDs, malformed
structured output, verdicts outside a case's allowed space, or a changed request artifact. A
definitive client-side HTTP rejection (a 4xx response other than 408 or 409, including 429) proves
that no batch was accepted: the chunk stays `pending`, records `submission_rejected`, and can be
retried after the cause is corrected. HTTP 408/409 and all 5xx responses are ambiguous and recorded
as `submission_unknown`; they are never automatically resubmitted because server-side acceptance
could otherwise create a duplicate billable batch. The final report remains compatible with the
normal evaluator; batch trials mark latency as unavailable rather than using polling time as a
per-request measurement.

If an ambiguous submission response included a batch ID, `status` can still poll that ID and
reconcile the remote batch; an ambiguous response without an ID remains unpollable and must not be
retried automatically.
