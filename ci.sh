#!/usr/bin/env bash
#
# The gate for this repo: the GitHub workflow runs this script and nothing
# else, so a local pass is a CI pass. Run it before committing.
#
# The seam test needs the gwbase dev broker (gw-dev-rabbit, started from
# gridworks-base with ./arm.sh or ./x86.sh) on localhost:5672 with its MQTT
# door on 1885; without one it self-skips.
#
# Usage: ./ci.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

step() { printf '\n=== %s ===\n' "$1"; shift; "$@"; }

step "uv sync (locked)" uv sync --all-groups --locked
step "ruff check" uv run ruff check --no-fix .
step "ruff format --check" uv run ruff format --check .
step "pyright" uv run pyright

if ! nc -z localhost 5672 2>/dev/null; then
  echo "NOTE: no broker on localhost:5672 — the seam test will self-skip."
fi
step "tests" uv run pytest -q

printf '\nAll checks passed.\n'
