"""The two pylint entry points must stay in agreement.

`scripts/format-and-lint.sh` is what `poe lint` and the CI lint job run;
`scripts/run-tests.sh` is what `poe test` runs. When one of them filters pylint
messages and the other does not, CI can be green while `poe test` is red (or the
reverse), and the difference is invisible until someone runs both. Both must
therefore hand pylint the whole `.pylintrc` ruleset.
"""

from __future__ import annotations

import re

from family_assistant.paths import PROJECT_ROOT

# Matches a pylint invocation and captures everything up to the end of the
# command, e.g. `"${VIRTUAL_ENV:-.venv}"/bin/pylint -j0 src tests &`.
_PYLINT_INVOCATION = re.compile(r"/bin/pylint\s+(?P<args>[^\n&|]*)")

# Flags that would narrow what pylint reports, and so make one entry point
# accept code the other rejects.
_MESSAGE_FILTERING_FLAGS = ("--errors-only", "-E", "--disable", "-d", "--enable", "-e")


def _pylint_invocations(script_name: str) -> list[str]:
    script = (PROJECT_ROOT / "scripts" / script_name).read_text()
    return [
        match.group("args").strip() for match in _PYLINT_INVOCATION.finditer(script)
    ]


def test_both_lint_entry_points_invoke_pylint() -> None:
    assert len(_pylint_invocations("format-and-lint.sh")) == 1
    assert len(_pylint_invocations("run-tests.sh")) == 1


def test_neither_entry_point_filters_pylint_messages() -> None:
    for script_name in ("format-and-lint.sh", "run-tests.sh"):
        for args in _pylint_invocations(script_name):
            tokens = args.split()
            offenders = [token for token in tokens if token in _MESSAGE_FILTERING_FLAGS]
            assert not offenders, (
                f"{script_name} narrows pylint with {offenders}; that would let it "
                "disagree with the other lint entry point. Change .pylintrc instead."
            )
