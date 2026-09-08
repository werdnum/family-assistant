#!/usr/bin/env python3
"""Mirror each provider's published model documentation into its skill's references.

The providers publish these pages in an LLM-addressable form so that consumers do
not have to parse them, so this mirrors them verbatim rather than extracting
records into a schema of our own. There is nothing here for a provider's next
reformat to break: the file an agent reads is the file the provider wrote.
"""

import argparse
import datetime
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "family-assistant-provider-model-refresh/2.0"

SOURCES = {
    ".agents/skills/gemini-api-dev": "https://ai.google.dev/gemini-api/docs/models.md.txt",
    ".agents/skills/openai-api-dev": "https://developers.openai.com/api/docs/models.md",
    ".agents/skills/anthropic-api-dev": "https://platform.claude.com/docs/en/models/overview.md",
}

HEADER_PREFIX = "<!-- Mirrored from "


def _fetch(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30.0) as response:
        return response.read().decode("utf-8")


def _body(mirrored: str) -> str:
    """Return a mirrored file's provider content, without our provenance header."""
    if not mirrored.startswith(HEADER_PREFIX):
        return mirrored
    _, _, remainder = mirrored.partition("\n\n")
    return remainder


def _render(url: str, body: str, retrieved: str) -> str:
    return (
        f"{HEADER_PREFIX}{url} on {retrieved}.\n"
        "     Fetch that URL directly if this looks out of date. -->\n\n"
        f"{body}"
    )


def _refresh(skill_dir: str, url: str, *, check: bool) -> bool:
    """Mirror one provider's page. Returns whether the mirrored content changed."""
    path = ROOT / skill_dir / "references/current-models.md"
    body = _fetch(url)
    existing = path.read_text() if path.exists() else None
    if existing is not None and _body(existing) == body:
        return False

    if not check:
        retrieved = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render(url, body, retrieved))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if a mirror is out of date without writing it.",
    )
    args = parser.parse_args()

    stale: list[str] = []
    failed: list[str] = []
    # Each provider is mirrored independently: one provider's outage or moved page
    # leaves the others refreshed, rather than taking the whole run down with it.
    for skill_dir, url in SOURCES.items():
        try:
            changed = _refresh(skill_dir, url, check=args.check)
        except (OSError, UnicodeDecodeError) as error:
            print(f"Failed: {skill_dir} <- {url} ({error})", file=sys.stderr)
            failed.append(skill_dir)
            continue
        if changed:
            stale.append(skill_dir)
            print(f"{'Stale' if args.check else 'Updated'}: {skill_dir}")

    if not stale and not failed:
        print("Provider model documentation mirrors are current.")
    return 1 if failed or (args.check and stale) else 0


if __name__ == "__main__":
    sys.exit(main())
