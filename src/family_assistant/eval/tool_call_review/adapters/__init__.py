"""Public-corpus adapters for the tool-call review evaluation harness.

Adapters map established prompt-injection corpora into controlled browser
visibility-ablation groups. Every attack row emits attack and benign-twin
controls at hidden and full visibility; Deepset also emits unpaired full-only
natural-benign controls. The browser payload has no live tool-registry
dependency. See ``docs/design/tool-call-review-eval.md`` (Public corpora).

The adapters — never the corpora themselves — are the committed artifacts. Each
adapter ships only a tiny synthetic sample in its corpus's upstream format under
``samples/<corpus>/``; the real corpus is fetched locally and mapped by
:mod:`scripts.build_public_corpus_cases`, which records the upstream revision
and license alongside the cases it writes.

This subpackage never imports the package ``__init__`` (which pulls in the
runner and its network dependencies), so it can be exercised without a
configured judge.
"""

from __future__ import annotations

from family_assistant.eval.tool_call_review.adapters.base import (
    AdaptedCase,
    AdaptedLineage,
    Adapter,
)
from family_assistant.eval.tool_call_review.adapters.deepset_prompt_injections import (
    DeepsetPromptInjectionsAdapter,
)
from family_assistant.eval.tool_call_review.adapters.injecagent import (
    InjecAgentAdapter,
)

ADAPTERS: dict[str, type[Adapter]] = {
    DeepsetPromptInjectionsAdapter.corpus_id: DeepsetPromptInjectionsAdapter,
    InjecAgentAdapter.corpus_id: InjecAgentAdapter,
}

__all__ = [
    "ADAPTERS",
    "AdaptedCase",
    "AdaptedLineage",
    "Adapter",
    "DeepsetPromptInjectionsAdapter",
    "InjecAgentAdapter",
]
