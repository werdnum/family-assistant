# Consumed gate generations

Marker files recording that a gate generation of the tool-call review eval has been consumed (see
"Held-out discipline" in
[docs/design/tool-call-review-eval.md](../../../../../../docs/design/tool-call-review-eval.md)). A
gate generation is single-use: the first gate run over it writes a `<generation_hash>.json` marker
here, and any later gate run over the same generation is refused a shippable stamp.

**Markers must be committed alongside any shippable stamp.** The ledger is repository-tracked
precisely so that consumption survives fresh clones and second worktrees — a gitignored ledger would
let another checkout re-stamp an already-consumed generation. Committing markers is safe: each one
contains only the generation's content hash, the gate status and reason, the ceiling, and a
timestamp — no case content and no private data.

Deleting a marker un-consumes a generation and is therefore an auditable act: do it only in a
reviewed commit that explains why re-gating that generation is legitimate.
