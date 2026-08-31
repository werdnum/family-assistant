# Native Gemini batch review eval

The native Gemini runner is separate from the OpenRouter runner because its uploaded JSONL format,
job names, lifecycle states, and result envelopes are a different provider contract. Preparation is
local and deterministic:

```bash
python scripts/tool_call_review_gemini_batch.py prepare \
  --dataset .review-eval-local/public/deepset-browser-ablation-gate \
  --dataset .review-eval-local/public/injecagent-browser-ablation-both-gate \
  --tool-registry .review-eval-local/registry/deployment.json \
  --model gemini-3.7-flash --seeds 1 --batch-size 500 --max-tokens 512 \
  --run-dir .review-eval-local/runs/gemini-batch-gate
```

Use `prepare --dry-run` to run the same input validation without writing an artifact. `submit`,
`status`/`poll`, and `harvest` contact Google; `submit` requires `GEMINI_API_KEY`, a finite positive
`--approved-spend-usd`, and `--approve-spend`:

```bash
python scripts/tool_call_review_gemini_batch.py submit \
  --run-dir .review-eval-local/runs/gemini-batch-gate \
  --approved-spend-usd 10 --approve-spend
python scripts/tool_call_review_gemini_batch.py poll \
  --run-dir .review-eval-local/runs/gemini-batch-gate
python scripts/tool_call_review_gemini_batch.py harvest \
  --run-dir .review-eval-local/runs/gemini-batch-gate
```

The approved amount is an operator approval recorded in the private manifest, not a
provider-enforced spend cap. Request `max_tokens` is recorded and bounds each output; truncation, an
error arm, an unknown state, or malformed output is unavailable evidence and is never converted into
a verdict. Ambiguous upload or batch-creation outcomes are marked `submission_unknown` and are never
automatically retried. A successful job is harvested only after every output key exactly matches the
prepared keys, has one candidate, a `STOP` finish reason, nonempty text, and valid
`ToolCallReviewResponse` JSON. Usage metadata, including thinking tokens where supplied, is retained
as optional private metadata; no aggregate cost is claimed when the provider does not report it.

Input and result files, structured drafts, reasons, and manifests remain under `.review-eval-local`.
The report is a normal `EvalReport`, but asynchronous batch trials correctly record latency as
unavailable. A harvested report is a review draft for maintainer inspection and promotion through
the ordinary review-eval corpus workflow; it is not itself a shipped gate verdict.
