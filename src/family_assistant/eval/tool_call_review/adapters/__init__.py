"""Public-corpus adapters for the tool-call review evaluation harness.

Adapters map established prompt-injection corpora into this system's reviewer
positions: their injected payloads land in the email-body, tool-result,
browser-environment, and note-content slots of our case shapes. Adaptation is
more than relocation — the judge rules on a *tool call*, so every adapter pairs
the injected upstream text with the gated call the injection argues for (an
exfiltrating send, an unrequested egress), wrapped in the taint provenance our
contract expects. See ``docs/design/tool-call-review-eval.md`` (Public corpora).

The adapters — never the corpora themselves — are the committed artifacts. Each
adapter ships only a tiny synthetic sample in its corpus's upstream format under
``samples/<corpus>/``; the real corpus is fetched locally and mapped by
:mod:`scripts.build_public_corpus_cases`, which records the upstream revision
and license alongside the cases it writes.

This subpackage imports only from the harness's ``schema`` module and the
runtime taint/service types it re-exports; it never imports the package
``__init__`` (which pulls in the runner and its network dependencies), so it can
be exercised without a configured judge.
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
