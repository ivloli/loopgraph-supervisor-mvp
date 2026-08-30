#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "DEEPSEEK_API_KEY is not set. Export it before starting A." >&2
  exit 1
fi

source .venv/bin/activate
unset DSH_MODE
export PORT="${A_PORT:-8080}"
export SUPERVISOR_DB="${SUPERVISOR_DB:-$ROOT/supervisor.db}"
export DSH_SESSION_ROOT="${A_DSH_SESSION_ROOT:-$ROOT/.dsh-sessions-a}"
exec python -m loopgraph_supervisor.main
