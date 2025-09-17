"""
tmux orchestration for listen-cli.

- Creates a tmux session running the target TUI
- Kiosk mode (hide bindings), status-bar HUD, global hotkey (default Alt-t)
- Starts ASR daemon in a hidden '.asr' window (one per session)
- Hotkey triggers '__toggle__ <session> <pane_id>' on our CLI, which signals the daemon
"""

from __future__ import annotations
import os
import shlex
import subprocess
from typing import Optional

import libtmux  # pip install libtmux


def _tmux_cmd(*args: str) -> None:
    subprocess.run(["tmux", *args], check=False)


def _status_hud(session) -> None:
    session.set_option("status", True)
    session.set_option("status-position", "bottom")
    session.set_option("status-interval", 1)
    session.set_option("status-style", "bg=colour236,fg=colour250")
    session.set_option("message-style", "bg=colour235,fg=colour222")
    session.set_option("window-status-style", "bg=colour236,fg=colour244")
    session.set_option("window-status-current-style", "bg=colour238,fg=colour255,bold")

    # Initialize HUD variables
    session.cmd("set", "-gq", "@asr_on", "0")
    session.cmd("set", "-gq", "@asr_preview", "")
    session.set_option(
        "status-right",
        '#{?@asr_on,#[fg=colour196]🎙 REC,#[fg=colour244]🎙 idle} '
        '#[fg=colour240]| #[fg=colour252]#{@asr_preview} #[fg=colour240]| %H:%M #[default]'
    )


def _kiosk_mode(session) -> None:
    # Hide tmux-ness & unbind defaults so users only see the TUI
    session.set_option("status", True)  # HUD needs status on
    session.set_option("mouse", False)
    session.set_option("xterm-keys", True)
    session.set_option("escape-time", 0)
    session.set_option("remain-on-exit", False)
    for tbl in ["root", "prefix", "copy-mode", "copy-mode-vi", "copy-mode-emacs"]:
        session.cmd("unbind-key", "-T", tbl, "-a")


def _start_asr_window(session, session_name: str, env_socket: str) -> None:
    # Run the ASR daemon in a hidden window; it dies with the session
    env = f"LISTEN_SESSION={shlex.quote(session_name)} LISTEN_SOCKET={shlex.quote(env_socket)}"
    # Use python -m listen_cli.asr so it runs from the installed package
    cmd = f'{env} python -m listen_cli.asr'
    session.new_window(attach=False, window_name=".asr", window_shell=cmd)


def launch(app: str, app_args: list[str], hotkey: Optional[str] = None) -> None:
    """Create session, bind hotkey, run app, and attach."""
    hotkey = hotkey or os.getenv("MYAPP_HOTKEY", "M-t")
    server = libtmux.Server()
    session_name = f"listen_{os.getpid()}"
    session = server.new_session(session_name=session_name, attach=False, window_command=" ".join([shlex.quote(app), *map(shlex.quote, app_args)]))
    window = session.attached_window
    pane = window.attached_pane

    _kiosk_mode(session)
    _status_hud(session)

    # Start ASR daemon in hidden window with known socket path
    socket_path = f"/tmp/listen-{session_name}.sock"
    _start_asr_window(session, session_name, socket_path)

    # Bind global hotkey (no prefix): calls our CLI with __toggle__
    # #{session_name} and #{pane_id} expand inside tmux
    toggle_cmd = f"listen __toggle__ '#{{session_name}}' '#{{pane_id}}'"
    session.cmd("bind-key", "-n", hotkey, "run-shell", toggle_cmd)

    # Optional escape hatch: Alt-q kills session
    session.cmd("bind-key", "-n", "M-q", "run-shell", f"tmux detach-client \\; kill-session -t {shlex.quote(session_name)}")

    # Attach
    server.attach_session(target_session=session_name)