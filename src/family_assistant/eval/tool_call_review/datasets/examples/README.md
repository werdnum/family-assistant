# Example eval cases

Two example cases that exist only to exercise the runner end to end. They are not the curated manual
seed set (that lives elsewhere and is reasoned about per case).

- `delegation_blind_deny.yaml` — the delegated-run blind-deny scenario as a `conversation` case: a
  delegated sub-conversation whose rows are provenance-stubbed, so the reviewer has no trusted
  objective to align the proposed message against. Labeled `benign` (the message is legitimate); the
  point is that the reviewer is blind, which is what shadow data surfaced.
- `benign_twin.yaml` — the obvious benign twin: the same tool and argument shape, but the trusted
  user's request is present as trusted-tier content, so the reviewer can align against clear intent.

Run the harness against this directory to load and (with credentials) score them:

```bash
python scripts/tool_call_review_eval.py \
  --dataset src/family_assistant/eval/tool_call_review/datasets/examples \
  --dry-run
```
