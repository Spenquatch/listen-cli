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
import sys
from typing import Optional

import libtmux  # pip install libtmux
from libtmux.exc import LibTmuxException


def _tmux_cmd(*args: str) -> None:
    subprocess.run(["tmux", *args], check=False)


def _status_hud(session) -> None:
    session.set_option("status", True)
    session.set_option("status-position", "bottom")
    session.set_option("status-interval", 1)
    # Status bar chrome
    session.set_option("status-style", "bg=colour236,fg=colour250")
    session.set_option("status-left-length", 200)
    session.set_option("message-style", "bg=colour235,fg=colour222")
    # Make the hidden window list blend into the bar (no lighter block)
    session.set_option("window-status-style", "bg=colour236,fg=colour236")
    session.set_option("window-status-current-style", "bg=colour236,fg=colour236")
    session.set_option(
        "status-left",
        '#{?@asr_on,#[fg=colour196] REC 🎙 ,#[fg=white] idle 🎙 } '
        '#[fg=colour240]| #[fg=colour252]#{@asr_preview} #[default]'
    )
    # Hide window list text; keep a single-space placeholder to avoid edge-cases
    session.cmd("set", "-gq", "window-status-format", " ")
    session.cmd("set", "-gq", "window-status-current-format", " ")

    # Initialize HUD variables
    session.cmd("set", "-gq", "@asr_on", "0")
    session.cmd("set", "-gq", "@asr_preview", "")
    session.cmd("set", "-gq", "@asr_message", "")
    session.set_option(
        "status-right",
        '#{?@asr_message,#[fg=colour240]#{@asr_message} #[fg=colour240]| ,}'
        ' #[fg=white]%I:%M %p #[default]'
    )
    # Do not override status-format at this time; keep defaults to avoid side effects.


def _kiosk_mode(session, server) -> None:
    # Hide tmux-ness & unbind defaults so users only see the TUI
    session.set_option("status", True)  # HUD needs status on
    session.set_option("mouse", False)
    session.set_option("xterm-keys", True)
    session.set_option("escape-time", 0)
    session.set_option("remain-on-exit", False)
    # Unbind default keys globally (tmux bindings are server-scoped). Use server.cmd so no -t is added.
    for tbl in ["root", "prefix", "copy-mode", "copy-mode-vi", "copy-mode-emacs"]:
        server.cmd("unbind-key", "-T", tbl, "-a")


def _start_asr_window(session, session_name: str, env_socket: str, socket_name: str) -> None:
    """Run the ASR daemon in a hidden window tied to this session."""
    python = shlex.quote(sys.executable)
    cmd = f"{python} -m listen_cli.asr"
    session.new_window(
        attach=False,
        window_name=".asr",
        window_shell=cmd,
        environment={
            "LISTEN_SESSION": session_name,
            "LISTEN_SOCKET": env_socket,
            "TMUX_SOCKET": socket_name,  # Pass the socket name for tmux commands
        },
    )


def launch(app: str, app_args: list[str], hotkey: Optional[str] = None) -> None:
    """Create session, bind hotkey, run app, and attach."""
    hotkey = hotkey or os.getenv("MYAPP_HOTKEY", "M-t")
    disable_asr = os.getenv("LISTEN_DISABLE_ASR")
    # Isolate everything in a dedicated tmux server (socket) so we don't
    # touch the user's default tmux server or bindings.
    session_name = f"listen_{os.getpid()}"
    socket_name = os.getenv("LISTEN_TMUX_SOCKET") or session_name
    server = libtmux.Server(socket_name=socket_name)

    # Wrap the main app command to kill the session when it exits
    app_cmd = " ".join([shlex.quote(app), *map(shlex.quote, app_args)])
    # Use sh -c to run the app and then kill the session when it exits
    wrapped_cmd = f"sh -c '{app_cmd}; tmux -L {shlex.quote(socket_name)} kill-session -t {shlex.quote(session_name)}'"

    session = server.new_session(session_name=session_name, attach=False, window_command=wrapped_cmd)
    window = session.attached_window
    pane = window.attached_pane

    _kiosk_mode(session, server)
    _status_hud(session)

    # Start ASR daemon in hidden window with known socket path
    socket_path = f"/tmp/listen-{session_name}.sock"
    if not disable_asr:
        _start_asr_window(session, session_name, socket_path, socket_name)
    else:
        session.cmd("display-message", "LISTEN: ASR disabled (LISTEN_DISABLE_ASR set)")
    window.select_window()

    # Bind global hotkey (no prefix): calls our CLI with __toggle__
    # #{session_name} and #{pane_id} expand inside tmux
    python = shlex.quote(sys.executable)
    # Ensure stale bindings are removed before rebinding
    server.cmd("unbind-key", "-n", hotkey)
    server.cmd("unbind-key", "-n", "M-q")
    if disable_asr:
        log_cmd = f"{python} -m listen_cli __log__ '#{{session_name}}' '#{{pane_id}}'"
        server.cmd("bind-key", "-n", hotkey, "run-shell", "-b", log_cmd)
    else:
        toggle_cmd = f"{python} -m listen_cli __toggle__ '#{{session_name}}' '#{{pane_id}}'"
        server.cmd("bind-key", "-n", hotkey, "run-shell", "-b", toggle_cmd)

    # Optional escape hatch: Alt-q kills session in this server only
    server.cmd("bind-key", "-n", "M-q", "run-shell", "-b", f"tmux detach-client \\; kill-session -t {shlex.quote(session_name)}")

    # Attach - this will block until the session ends or we detach
    try:
        server.attach_session(target_session=session_name)
    finally:
        # Kill the tmux session if it still exists
        try:
            server.cmd("kill-session", "-t", session_name)
        except LibTmuxException:
            pass
        # Kill the entire custom tmux server to ensure cleanup
        try:
            subprocess.run(["tmux", "-L", socket_name, "kill-server"],
                         check=False, capture_output=True)
        except Exception:
            pass
