#!/usr/bin/env bash
# my-app — tmux kiosk wrapper with global hotkey paste + clean ASR teardown
# Usage:
#   ./my-app nano 'Your text to paste when you press Alt-t'
#
# Customize the hotkey (global, no prefix):
#   MYAPP_HOTKEY="C-g" ./my-app nano '...'
#
# Optionally launch your ASR daemon and have it auto-tear down on exit:
#   MYAPP_ASR_CMD="python3 asr_daemon.py --socket /tmp/asr.sock" ./my-app nano '...'

set -euo pipefail

# --- subcommands (none needed for this simple paste test) ---------------------

# --- args ---------------------------------------------------------------------
if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found on PATH" >&2; exit 1
fi

APP="${1:-nano}"
shift || true
TEXT="${*:-}"
SESSION="myapp_$$"
HOTKEY="${MYAPP_HOTKEY:-M-t}"   # e.g., M-t (Alt-t), C-g, M-\`, etc.

# --- optional ASR background process -----------------------------------------
ASR_PID=""
if [[ -n "${MYAPP_ASR_CMD:-}" ]]; then
  # Start your ASR daemon in the background and remember its PID
  bash -lc "$MYAPP_ASR_CMD" &
  ASR_PID=$!
fi

cleanup() {
  # Kill ASR daemon if we started one
  if [[ -n "$ASR_PID" ]]; then
    kill -TERM "$ASR_PID" 2>/dev/null || true
    wait "$ASR_PID" 2>/dev/null || true
  fi
  # Ensure the tmux session is gone
  tmux kill-session -t "$SESSION" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- session ------------------------------------------------------------------
tmux new-session -d -s "$SESSION" "$APP"

# kiosk: hide tmux-ness
tmux set -t "$SESSION" -g status off
tmux set -t "$SESSION" -g mouse off
tmux set -t "$SESSION" -g xterm-keys on
tmux set -t "$SESSION" -g escape-time 0
tmux set -t "$SESSION" -g remain-on-exit off

# nuke default keybindings so only *our* hotkeys exist
for tbl in root prefix copy-mode copy-mode-vi copy-mode-emacs; do
  tmux unbind-key -T "$tbl" -a 2>/dev/null || true
done

# load the provided TEXT into a named buffer (safe for quotes/newlines)
tmux set-buffer -b myapp_hotkey -- "${TEXT}"

# global hotkey (no prefix): paste the buffer into the current pane with bracketed paste
tmux bind-key -n "$HOTKEY" paste-buffer -p -b myapp_hotkey

# optional escape hatch to quit everything (Alt-q)
tmux bind-key -n M-q run-shell "tmux detach-client \; kill-session -t \"$SESSION\""

# --- go -----------------------------------------------------------------------
tmux attach -t "$SESSION"
