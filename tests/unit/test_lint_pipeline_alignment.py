"""The lint entry points must reach pylint through one shared invocation.

`scripts/format-and-lint.sh` is what `poe lint` and the CI lint job run;
`scripts/run-tests.sh` is what `poe test` runs. When the two invoke pylint
differently, CI can be green while `poe test` is red (or the reverse), and the
difference is invisible until someone runs both.

`scripts/run-pylint.sh` is the chokepoint that makes them agree: it takes paths
only and refuses every option, so the ruleset always comes from `.pylintrc`.
These tests hold that shape in place -- that both entry points route through the
wrapper rather than calling pylint themselves, and that the wrapper really does
refuse options. Which options exist is then pylint's business, not a list this
file has to keep complete.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from family_assistant.paths import PROJECT_ROOT

_LINT_SCRIPTS = ("format-and-lint.sh", "run-tests.sh")

_WRAPPER = PROJECT_ROOT / "scripts" / "run-pylint.sh"

# A pylint executable being run, rather than the word "pylint" inside a message
# or a comment: `.venv/bin/pylint ...`, or `pylint ...` in command position.
_DIRECT_PYLINT_CALL = re.compile(
    r"(?:/bin/pylint|^\s*pylint|[;&|(]\s*pylint)\s", re.MULTILINE
)


def _script_body(script_name: str) -> str:
    return (PROJECT_ROOT / "scripts" / script_name).read_text()


@pytest.mark.parametrize("script_name", _LINT_SCRIPTS)
def test_lint_entry_point_uses_the_shared_pylint_wrapper(script_name: str) -> None:
    assert "run-pylint.sh" in _script_body(script_name)


@pytest.mark.parametrize("script_name", _LINT_SCRIPTS)
def test_lint_entry_point_does_not_invoke_pylint_itself(script_name: str) -> None:
    body = _script_body(script_name)
    offenders = [
        line.strip()
        for line in body.splitlines()
        if _DIRECT_PYLINT_CALL.search(line) and "run-pylint.sh" not in line
    ]
    assert not offenders, (
        f"{script_name} invokes pylint directly: {offenders}. Go through "
        "scripts/run-pylint.sh so both lint entry points enforce one ruleset."
    )


@pytest.mark.parametrize(
    "option",
    ["--errors-only", "-E", "--disable=all", "-dall", "--enable=similarities"],
)
def test_wrapper_refuses_options_that_would_narrow_the_ruleset(option: str) -> None:
    result = subprocess.run(
        [str(_WRAPPER), option, "src"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "takes paths only" in result.stderr


def test_wrapper_refuses_an_alternate_rcfile() -> None:
    """An rcfile swaps the ruleset wholesale, so it breaks alignment too."""
    result = subprocess.run(
        [str(_WRAPPER), "--rcfile=/dev/null", "src"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "takes paths only" in result.stderr


def test_wrapper_requires_at_least_one_path() -> None:
    result = subprocess.run(
        [str(_WRAPPER)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr
