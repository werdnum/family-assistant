"""The two pylint entry points must stay in agreement.

`scripts/format-and-lint.sh` is what `poe lint` and the CI lint job run;
`scripts/run-tests.sh` is what `poe test` runs. When one of them filters pylint
messages and the other does not, CI can be green while `poe test` is red (or the
reverse), and the difference is invisible until someone runs both. Both must
therefore hand pylint the whole `.pylintrc` ruleset.
"""

from __future__ import annotations

import re

import pytest

from family_assistant.paths import PROJECT_ROOT

# Matches a pylint invocation and captures everything up to the end of the
# command, e.g. `"${VIRTUAL_ENV:-.venv}"/bin/pylint -j0 src tests &`.
_PYLINT_INVOCATION = re.compile(r"/bin/pylint\s+(?P<args>[^\n&|]*)")

# Options that narrow what pylint reports, and so would make one entry point
# accept code the other rejects. Every spelling pylint accepts has to match:
# the long forms take a value either as `--disable all` or attached as
# `--disable=all`, and the short forms either as `-d all` or attached as
# `-dall`.
_MESSAGE_FILTERING_OPTION = re.compile(
    r"""^(?:
        --errors-only
      | --(?:disable|enable)(?:=.*)?
      | -[Ede]
    )""",
    re.VERBOSE,
)

_LINT_SCRIPTS = ("format-and-lint.sh", "run-tests.sh")


def _pylint_invocations(script_name: str) -> list[str]:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text()
    return [
        match.group("args").strip() for match in _PYLINT_INVOCATION.finditer(script)
    ]


@pytest.mark.parametrize("script_name", _LINT_SCRIPTS)
def test_lint_entry_point_invokes_pylint_exactly_once(script_name: str) -> None:
    assert len(_pylint_invocations(script_name)) == 1


@pytest.mark.parametrize("script_name", _LINT_SCRIPTS)
def test_lint_entry_point_does_not_filter_pylint_messages(script_name: str) -> None:
    for args in _pylint_invocations(script_name):
        offenders = [
            token for token in args.split() if _MESSAGE_FILTERING_OPTION.match(token)
        ]
        assert not offenders, (
            f"{script_name} narrows pylint with {offenders}; that would let it "
            "disagree with the other lint entry point. Change .pylintrc instead."
        )


@pytest.mark.parametrize(
    "token",
    [
        "--errors-only",
        "-E",
        "--disable",
        "--disable=all",
        "-d",
        "-dall",
        "--enable",
        "--enable=similarities",
        "-e",
        "-esimilarities",
    ],
)
def test_message_filtering_options_are_recognized(token: str) -> None:
    assert _MESSAGE_FILTERING_OPTION.match(token)


@pytest.mark.parametrize("token", ["-j0", "--jobs=0", "src", "tests", "--rcfile=x"])
def test_harmless_arguments_are_not_flagged(token: str) -> None:
    assert not _MESSAGE_FILTERING_OPTION.match(token)
