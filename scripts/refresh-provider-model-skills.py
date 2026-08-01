#!/usr/bin/env python3
"""Refresh provider skill model snapshots from official public documentation."""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "family-assistant-provider-model-refresh/1.0"

GEMINI_SOURCE = "https://ai.google.dev/gemini-api/docs/models.md.txt"
OPENAI_SOURCE = "https://developers.openai.com/api/docs/models.md"
ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/models/overview.md"


@dataclass(frozen=True)
class Model:
    """A provider model extracted from official documentation."""

    name: str
    model_id: str
    description: str
    source_url: str


def _fetch(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30.0) as response:
        return response.read().decode("utf-8")


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _extract_openai_models(markdown: str) -> list[Model]:
    section = markdown.split("## Recommended models", maxsplit=1)[1].split(
        "## Browse our full catalog", maxsplit=1
    )[0]
    matches = re.findall(
        r"^- \[([^]]+)]\((/api/docs/models/([a-z0-9.\-]+)\.md)\): (.+)$",
        section,
        flags=re.MULTILINE,
    )
    if not matches:
        raise ValueError("OpenAI recommended-model section could not be parsed")
    return [
        Model(
            name=name,
            model_id=model_id,
            description=description.rstrip("."),
            source_url=f"https://developers.openai.com{path}",
        )
        for name, path, model_id, description in matches
    ]


def _extract_anthropic_models(markdown: str) -> list[Model]:
    section = markdown.split("### Latest models comparison", maxsplit=1)[1].split(
        "<Info>", maxsplit=1
    )[0]
    lines = [line for line in section.splitlines() if line.startswith("|")]
    header = next(line for line in lines if "Claude Fable" in line)
    descriptions = next(line for line in lines if "**Description**" in line)
    api_ids = next(line for line in lines if "**Claude API ID**" in line)

    names = _table_cells(header)[1:]
    description_cells = _table_cells(descriptions)[1:]
    id_cells = _table_cells(api_ids)[1:]
    if not names or not (len(names) == len(description_cells) == len(id_cells)):
        raise ValueError("Anthropic latest-model table could not be parsed")

    return [
        Model(
            name=name,
            model_id=model_id,
            description=description,
            source_url=ANTHROPIC_SOURCE,
        )
        for name, model_id, description in zip(
            names, id_cells, description_cells, strict=True
        )
    ]


def _gemini_catalog_entries(markdown: str) -> list[tuple[str, str, str]]:
    current = markdown.split("## Previous models", maxsplit=1)[0]
    entries: list[tuple[str, str, str]] = []

    card_pattern = re.compile(
        r"\[### ([^\n]+)\n([^\n]+)\n(?:Stable|(?:New )?Preview)]"
        r"\((https://ai\.google\.dev/gemini-api/docs/models/[a-z0-9.\-]+)\)"
    )
    heading_pattern = re.compile(
        r"^### \[([^]]+)]"
        r"\((https://ai\.google\.dev/gemini-api/docs/models/[a-z0-9.\-]+)\)"
        r"\n\n([^\n]+)",
        flags=re.MULTILINE,
    )
    for name, description, url in card_pattern.findall(current):
        entries.append((name, description, url))
    for name, url, description in heading_pattern.findall(current):
        if "Deprecated" in name or "Shut down" in name:
            continue
        if "/lyria-" in url:
            continue
        entries.append((name, description, url))

    deduplicated: dict[str, tuple[str, str, str]] = {}
    for entry in entries:
        deduplicated.setdefault(entry[2], entry)
    if not deduplicated:
        raise ValueError("Gemini current-model catalog could not be parsed")
    return list(deduplicated.values())


def _extract_gemini_models(markdown: str) -> list[Model]:
    models: list[Model] = []
    seen_ids: set[str] = set()
    for name, description, page_url in _gemini_catalog_entries(markdown):
        detail = _fetch(f"{page_url}.md.txt")
        codes = re.findall(r"\| Model code \|[^\n]*?`([^`]+)`", detail)
        model_id = codes[0] if codes else page_url.rsplit("/", maxsplit=1)[-1]
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        models.append(
            Model(
                name=name,
                model_id=model_id,
                description=description.rstrip("."),
                source_url=page_url,
            )
        )
    return models


def _render(provider: str, source_url: str, models: list[Model]) -> str:
    snapshot = {
        "provider": provider,
        "source": source_url,
        "generated_by": "scripts/refresh-provider-model-skills.py",
        "models": [
            {
                "id": model.model_id,
                "name": model.name,
                "description": model.description,
                "source": model.source_url,
            }
            for model in models
        ],
    }
    return json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"


def _snapshots() -> dict[Path, str]:
    gemini_source = _fetch(GEMINI_SOURCE)
    openai_source = _fetch(OPENAI_SOURCE)
    anthropic_source = _fetch(ANTHROPIC_SOURCE)
    return {
        ROOT / ".agents/skills/gemini-api-dev/references/current-models.json": _render(
            "Gemini", GEMINI_SOURCE, _extract_gemini_models(gemini_source)
        ),
        ROOT / ".agents/skills/openai-api-dev/references/current-models.json": _render(
            "OpenAI", OPENAI_SOURCE, _extract_openai_models(openai_source)
        ),
        ROOT
        / ".agents/skills/anthropic-api-dev/references/current-models.json": _render(
            "Anthropic", ANTHROPIC_SOURCE, _extract_anthropic_models(anthropic_source)
        ),
    }


def _apply_snapshots(snapshots: dict[Path, str], *, check: bool) -> int:
    stale: list[Path] = []
    for path, content in snapshots.items():
        existing = path.read_text() if path.exists() else None
        if existing == content:
            continue
        stale.append(path)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    if not stale:
        print("Provider model skill snapshots are current.")
        return 0
    for path in stale:
        print(f"{'Stale' if check else 'Updated'}: {path.relative_to(ROOT)}")
    return 1 if check else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if generated snapshots differ without writing them.",
    )
    args = parser.parse_args()
    return _apply_snapshots(_snapshots(), check=args.check)


if __name__ == "__main__":
    sys.exit(main())
