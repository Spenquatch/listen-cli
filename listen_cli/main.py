"""
CLI entry for listen / listen-cli

Usage:
  listen nano
  listen vim .
  listen __toggle__ <session> <pane_id>   # internal: called by tmux keybind
"""

from __future__ import annotations
import os
import shlex
import socket
import subprocess
import sys
from datetime import datetime
from typing import NoReturn

from . import orchestration


def _tmux_cmd(*args: str, check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        check=check,
        text=True,
        capture_output=capture_output,
    )


def _tmux_get(option: str) -> str:
    proc = _tmux_cmd("show-option", "-gqv", option, check=False, capture_output=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _toggle(session: str, pane_id: str) -> int:
    """Send TOGGLE to the session's ASR daemon via Unix socket."""
    sock_path = os.getenv("LISTEN_SOCKET") or f"/tmp/listen-{session}.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(sock_path)
            s.sendall(f"TOGGLE {pane_id}\n".encode())
            _ = s.recv(64)
        return 0
    except Exception as e:
        # Helpful hint if daemon hasn't spawned yet
        print(f"[listen-cli] toggle failed: {e}\n"
              f"  session={session}\n  socket={sock_path}\n"
              "Is the ASR daemon running? (it starts in a hidden .asr window when you launch a session)",
              file=sys.stderr)
        return 1


def _log_hotkey(session: str, pane_id: str) -> int:
    """Debug helper: append hotkey activations to a log file."""
    log_path = os.getenv("LISTEN_HOTKEY_LOG") or "/tmp/listen-hotkeys.log"
    timestamp = datetime.now().isoformat(timespec="seconds")
    entry = f"{timestamp} session={session} pane={pane_id}\n"
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(entry)
    except OSError as exc:
        print(f"[listen-cli] failed to write hotkey log: {exc}", file=sys.stderr)
        return 1

    try:
        current = _tmux_get("@asr_on") or "0"
        new_state = "0" if current == "1" else "1"
        preview = "Debug listening…" if new_state == "1" else ""
        message = "Debug hotkey active" if new_state == "1" else ""
        _tmux_cmd("set", "-gq", "@asr_on", new_state)
        _tmux_cmd("set", "-gq", "@asr_preview", preview)
        _tmux_cmd("set", "-gq", "@asr_message", message)
        _tmux_cmd("refresh-client", "-S")
    except subprocess.CalledProcessError as exc:
        print(f"[listen-cli] tmux update failed: {exc}", file=sys.stderr)
        return 1

    # Do not print to stdout to avoid tmux showing a message buffer in the pane.
    return 0


def main(argv: list[str] | None = None) -> NoReturn:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Internal subcommand fired by tmux key binding
    if argv[:1] == ["__toggle__"]:
        if len(argv) != 3:
            print("usage: listen __toggle__ <session> <pane_id>", file=sys.stderr)
            sys.exit(2)
        sys.exit(_toggle(argv[1], argv[2]))
    if argv[:1] == ["__log__"]:
        if len(argv) != 3:
            print("usage: listen __log__ <session> <pane_id>", file=sys.stderr)
            sys.exit(2)
        sys.exit(_log_hotkey(argv[1], argv[2]))

    if not argv:
        print("usage: listen <app> [args...]\nexample: listen nano", file=sys.stderr)
        sys.exit(2)

    app = argv[0]
    app_args = argv[1:]

    # Launch orchestration (tmux wrapper)
    try:
        orchestration.launch(app, app_args)
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
