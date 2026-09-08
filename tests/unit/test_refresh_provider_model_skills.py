"""Tests for the provider model documentation mirrors.

The mirror copies each provider's page verbatim, so there is no parsing to test.
What is worth pinning is the surrounding behaviour: an unchanged page must not
produce a dated no-op commit, the provider's bytes must survive intact under our
header, and one provider's failure must not cost the others their refresh --
that last one is the defect that kept every snapshot stale for months.
"""

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "refresh-provider-model-skills.py"
)

PAGE = "# Models\n\nSome provider prose, including a `model-id` in a table.\n"


@pytest.fixture
def script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """The refresh script, writing into a temporary tree instead of the repo."""
    spec = importlib.util.spec_from_file_location(
        "refresh_provider_model_skills", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    yield module
    del sys.modules[spec.name]


def _mirror(script: ModuleType, skill_dir: str) -> Path:
    return script.ROOT / skill_dir / "references/current-models.md"


def test_the_provider_page_is_mirrored_verbatim_below_the_header(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "_fetch", lambda _url: PAGE)

    script._refresh("skills/gemini", "https://example.test/models.md", check=False)

    written = _mirror(script, "skills/gemini").read_text()
    assert written.startswith("<!-- Mirrored from https://example.test/models.md on ")
    assert script._body(written) == PAGE


def test_an_unchanged_page_is_not_rewritten(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise every weekly run would commit a new date and nothing else."""
    monkeypatch.setattr(script, "_fetch", lambda _url: PAGE)
    script._refresh("skills/gemini", "https://example.test/models.md", check=False)
    first = _mirror(script, "skills/gemini").read_text()

    changed = script._refresh(
        "skills/gemini", "https://example.test/models.md", check=False
    )

    assert changed is False
    assert _mirror(script, "skills/gemini").read_text() == first


def test_a_changed_page_is_rewritten(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "_fetch", lambda _url: PAGE)
    script._refresh("skills/gemini", "https://example.test/models.md", check=False)
    monkeypatch.setattr(script, "_fetch", lambda _url: PAGE + "\nA new model.\n")

    changed = script._refresh(
        "skills/gemini", "https://example.test/models.md", check=False
    )

    assert changed is True
    assert script._body(_mirror(script, "skills/gemini").read_text()).endswith(
        "A new model.\n"
    )


def test_check_reports_drift_without_writing(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script, "_fetch", lambda _url: PAGE)

    changed = script._refresh(
        "skills/gemini", "https://example.test/models.md", check=True
    )

    assert changed is True
    assert not _mirror(script, "skills/gemini").exists()


def test_one_provider_failing_leaves_the_others_refreshed(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch(url: str) -> str:
        if "openai" in url:
            raise OSError("404 Not Found")
        return PAGE

    monkeypatch.setattr(script, "_fetch", fetch)
    monkeypatch.setattr(
        script,
        "SOURCES",
        {
            "skills/gemini": "https://example.test/gemini.md",
            "skills/openai": "https://example.test/openai.md",
            "skills/anthropic": "https://example.test/anthropic.md",
        },
    )
    monkeypatch.setattr(sys, "argv", ["refresh-provider-model-skills.py"])

    exit_code = script.main()

    assert exit_code == 1
    assert script._body(_mirror(script, "skills/gemini").read_text()) == PAGE
    assert script._body(_mirror(script, "skills/anthropic").read_text()) == PAGE
    assert not _mirror(script, "skills/openai").exists()
