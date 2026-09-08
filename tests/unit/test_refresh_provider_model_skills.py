"""Tests for the provider model snapshot extractors.

The extractors read three documentation pages nobody here controls, and each
page has already been restructured under them at least once — a renamed
section heading or a dropped emphasis marker turned the whole refresh into an
`IndexError`. These cases pin the shape each extractor is written against and,
more importantly, pin that a page it cannot parse raises rather than yielding a
plausible-looking empty or partial snapshot.

Upstream drift itself is caught by the weekly refresh workflow failing, not
here: a fixture cannot know the real page moved.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "refresh-provider-model-skills.py"
)

OPENAI_PAGE = """# Models

## Featured models

- [GPT-6 Astra](/api/docs/models/gpt-6-astra.md): Our most capable model
- [GPT-5.6 Terra](/api/docs/models/gpt-5.6-terra.md): Balances intelligence and cost

## Browse our full catalog of models

- [GPT-4](/api/docs/models/gpt-4.md): An older high-intelligence GPT model
"""

ANTHROPIC_PAGE = """# Models overview

## Compare models

| Feature | Claude Fable 5.1 | Claude Opus 5 |
| :------ | :--------------- | :------------ |
| Description | For long-horizon agentic work | For complex agentic coding |
| Claude API ID | `claude-fable-5-1` | `claude-opus-5` |
| Claude API alias | `claude-fable-5-1` | `claude-opus-5` |

* **Claude API ID:** Every Claude model ID is a pinned snapshot.

## Using the Models API
"""


def _script() -> ModuleType:
    """Import the refresh script as a module; its filename is not importable."""
    spec = importlib.util.spec_from_file_location(
        "refresh_provider_model_skills", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_openai_reads_only_the_featured_section() -> None:
    models = _script()._extract_openai_models(OPENAI_PAGE)

    assert [model.model_id for model in models] == ["gpt-6-astra", "gpt-5.6-terra"]
    assert models[0].name == "GPT-6 Astra"
    assert models[0].description == "Our most capable model"
    assert (
        models[0].source_url
        == "https://developers.openai.com/api/docs/models/gpt-6-astra.md"
    )


def test_anthropic_reads_ids_and_descriptions_from_the_comparison_table() -> None:
    models = _script()._extract_anthropic_models(ANTHROPIC_PAGE)

    assert [model.model_id for model in models] == ["claude-fable-5-1", "claude-opus-5"]
    assert [model.name for model in models] == ["Claude Fable 5.1", "Claude Opus 5"]
    assert models[0].description == "For long-horizon agentic work"


def test_anthropic_accepts_an_emphasised_row_label() -> None:
    """The page has spelled these labels both ways; neither should decide the id."""
    emphasised = ANTHROPIC_PAGE.replace("| Claude API ID |", "| **Claude API ID** |")

    models = _script()._extract_anthropic_models(emphasised)

    assert [model.model_id for model in models] == ["claude-fable-5-1", "claude-opus-5"]


@pytest.mark.parametrize(
    ("extractor", "page"),
    [
        (
            "_extract_openai_models",
            OPENAI_PAGE.replace("## Featured models", "## Picks"),
        ),
        (
            "_extract_anthropic_models",
            ANTHROPIC_PAGE.replace("## Compare models", "## Lineup"),
        ),
        (
            "_extract_anthropic_models",
            ANTHROPIC_PAGE.replace("| Claude API ID |", "| Model code |"),
        ),
    ],
)
def test_a_restructured_page_raises(extractor: str, page: str) -> None:
    with pytest.raises((IndexError, ValueError, StopIteration)):
        getattr(_script(), extractor)(page)
