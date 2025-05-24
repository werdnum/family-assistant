#!/bin/sh

${VIRTUAL_ENV:-.venv}/bin/ruff check --fix --preview --ignore=E501 ${@:-src tests} && \
${VIRTUAL_ENV:-.venv}/bin/ruff format --preview ${@:-src tests} && \
${VIRTUAL_ENV:-.venv}/bin/basedpyright ${@:-src tests} && \
${VIRTUAL_ENV:-.venv}/bin/pylint -j0 --errors-only ${@:-src tests}
