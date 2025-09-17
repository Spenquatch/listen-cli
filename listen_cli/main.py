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
import sys
from typing import NoReturn

from . import orchestration


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


def main(argv: list[str] | None = None) -> NoReturn:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Internal subcommand fired by tmux key binding
    if argv[:1] == ["__toggle__"]:
        if len(argv) != 3:
            print("usage: listen __toggle__ <session> <pane_id>", file=sys.stderr)
            sys.exit(2)
        sys.exit(_toggle(argv[1], argv[2]))

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
