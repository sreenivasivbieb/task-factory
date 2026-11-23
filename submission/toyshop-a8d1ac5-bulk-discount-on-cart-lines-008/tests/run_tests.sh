#!/bin/sh
# Runs the hidden feature tests against a solver's working copy.
# Usage: REPO_DIR=/repo sh tests/run_tests.sh   (run from the package folder)
set -eu
PKG_DIR=$(cd "$(dirname "$0")/.." && pwd)
REPO_DIR=${REPO_DIR:-/repo}
cp -R "$PKG_DIR/tests/files/." "$REPO_DIR/"
cd "$REPO_DIR"
mkdir -p .factory
python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 --junitxml=.factory/report.xml tests/test_bulk_discount.py
