#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SESSION="${1:-loopgraph}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed" >&2
  exit 1
fi
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "Export DEEPSEEK_API_KEY before running this script." >&2
  exit 1
fi

# A long-lived tmux server does not automatically inherit newly exported
# variables. Explicitly update the server environment before creating panes.
tmux set-environment -g DEEPSEEK_API_KEY "$DEEPSEEK_API_KEY"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux new-session -d -s "$SESSION" -n A -c "$ROOT" "$ROOT/scripts/start-a.sh"
fi

window_exists() {
  tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -Fxq "$1"
}

if ! window_exists A; then
  tmux new-window -t "$SESSION" -n A -c "$ROOT" "$ROOT/scripts/start-a.sh"
fi
if ! window_exists B; then
  tmux new-window -t "$SESSION" -n B -c "$ROOT" "$ROOT/scripts/start-b.sh"
fi
if ! window_exists logs; then
  tmux new-window -t "$SESSION" -n logs -c "$ROOT" "bash -lc 'echo A: http://127.0.0.1:${A_PORT:-8080}; echo B: http://127.0.0.1:${B_PORT:-3081}; echo; echo Useful commands:; echo \"  loopgraph list\"; echo \"  dsh plugin --profile web list\"; echo \"  ls -lt ~/.dsh/loopgraph\"; exec bash'"
fi

# Keep an exited service pane visible so startup errors are inspectable.
tmux set-option -t "$SESSION:A" remain-on-exit on
tmux set-option -t "$SESSION:B" remain-on-exit on
tmux select-window -t "$SESSION:A"
exec tmux attach -t "$SESSION"
