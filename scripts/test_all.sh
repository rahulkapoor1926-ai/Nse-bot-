#!/bin/sh
# Runs the NSE suite and the untouched upstream suite. Standard library only.
set -e
cd "$(dirname "$0")/.."
echo "== NSE options bot tests"
python3 -m unittest discover -s tests
echo "== upstream day-trader-skill tests (unchanged)"
(cd upstream && python3 -m unittest discover -s tests)
find . -name __pycache__ -prune -exec rm -rf {} +
