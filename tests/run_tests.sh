#!/usr/bin/env bash
# Run the Label on a Cable test suite with coverage.
#
#   tests/run_tests.sh                 # whole suite
#   tests/run_tests.sh -k push         # extra args go to pytest
#
# Needs a Python with QGIS bindings + pytest, pytest-qt, pytest-cov.
# By default uses the micromamba env "qgis-test"; set LOC_TEST_PYTHON to
# point at another interpreter (e.g. /usr/bin/python3 with python3-qgis).
set -eo pipefail
cd "$(dirname "$0")/.."

if [[ -z "${LOC_TEST_PYTHON:-}" ]]; then
    export MAMBA_ROOT_PREFIX="${MAMBA_ROOT_PREFIX:-$HOME/micromamba}"
    eval "$("$HOME/.local/bin/micromamba" shell hook -s bash)"
    # conda activate.d scripts reference unset variables — no `set -u` here
    micromamba activate qgis-test
    LOC_TEST_PYTHON=python
fi
set -u

export QT_QPA_PLATFORM=offscreen
mkdir -p tests/reports

"$LOC_TEST_PYTHON" -m pytest -c tests/pytest.ini --rootdir . \
    --cov --cov-config=tests/.coveragerc \
    --cov-report=term-missing --cov-report=html --cov-report=xml \
    --junitxml=tests/reports/junit.xml \
    "$@"
