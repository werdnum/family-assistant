"""Standalone jq runner, executed in a child process by ``jq_query``.

The jq binding holds the GIL for the whole of a program's execution, so running
it in a thread of the server process still freezes the event loop. Running it
here instead means a slow program can be killed and never stalls the server.

Run as ``python -I jq_worker.py <jq_program> <json_path>``. On success the
query result is written to stdout as indented JSON: the single value when the
program yields exactly one, otherwise a list of all values. Failures exit with
one of the codes below and a message on stderr.

This file must import nothing from ``family_assistant``: it runs isolated (``-I``)
so that its directory, which holds modules such as ``types.py``, does not shadow
the standard library.
"""

import json
import sys
from pathlib import Path

import jq

EXIT_JQ_ERROR = 2
EXIT_INVALID_JSON = 3
EXIT_INVALID_UTF8 = 4


def main(jq_program: str, json_path: str) -> int:
    try:
        json_data = json.loads(Path(json_path).read_bytes().decode("utf-8"))
    except UnicodeDecodeError:
        return EXIT_INVALID_UTF8
    except json.JSONDecodeError as e:
        sys.stderr.write(str(e))
        return EXIT_INVALID_JSON

    try:
        results = jq.compile(jq_program).input_value(json_data).all()
    except ValueError as e:
        sys.stderr.write(str(e))
        return EXIT_JQ_ERROR

    value = results[0] if len(results) == 1 else results
    sys.stdout.write(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
