#!/bin/sh

${VIRTUAL_ENV:-.venv}/bin/ruff check --fix --ignore=E501 ${@:-src tests} && \
${VIRTUAL_ENV:-.venv}/bin/black --preview ${@:-src tests} && \
${VIRTUAL_ENV:-.venv}/bin/pylint -j0 --errors-only ${@:-src tests}
