#!/bin/sh

${VIRTUAL_ENV:.venv}/bin/ruff check --ignore=E501 ${*:src tests}
${VIRTUAL_ENV:.venv}/bin/black --preview ${*:src tests}
