#!/usr/bin/env python3
"""
Headless STT daemon for pasting transcripts into a tmux pane.

Usage:
  ASSEMBLYAI_API_KEY=... LISTEN_TMUX_TARGET=<pane|%id> \
  poetry run python listen-stt-daemon.py

Controls:
  - Send SIGUSR1 to toggle listening on/off.
    Example: kill -USR1 $(cat /tmp/listen_stt.pid)

Target pane selection order when pasting:
  1) LISTEN_TMUX_TARGET env var (if set)
  2) /tmp/listen_stt_target file (last pane from sttctl)
  3) Current active pane (tmux display -p "#{pane_id}")

Notes:
  - No auto-Enter on paste; user presses Enter manually.
  - Logs to /tmp/listen_stt.log
"""

import os
import re
import sys
import time
import signal
import threading
import subprocess

import assemblyai as aai

LOG = "/tmp/listen_stt.log"
PIDFILE = "/tmp/listen_stt.pid"
TARGETFILE = "/tmp/listen_stt_target"


def log(msg: str):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


class STTDaemon:
    def __init__(self):
        api_key = os.getenv("ASSEMBLYAI_API_KEY")
        if not api_key:
            print("ASSEMBLYAI_API_KEY not set", file=sys.stderr)
            sys.exit(2)
        aai.settings.api_key = api_key

        self.is_listening = False
        self.transcriber = None
        self.buf = []
        self.thread = None
        self.lock = threading.Lock()

        # optional fixed tmux target
        self.env_target = os.getenv("LISTEN_TMUX_TARGET")

    # -------- tmux helpers --------
    def _get_tmux_target(self) -> str:
        if self.env_target:
            return self.env_target
        try:
            if os.path.exists(TARGETFILE):
                with open(TARGETFILE) as f:
                    t = f.read().strip()
                    if t:
                        return t
        except Exception:
            pass
        # Fallback to active pane
        try:
            out = subprocess.check_output(["tmux", "display", "-p", "#{pane_id}"])  # e.g. %1
            return out.decode().strip()
        except Exception:
            return "%0"

    def _tmux_paste(self, text: str):
        target = self._get_tmux_target()
        try:
            subprocess.run(["tmux", "load-buffer", "-b", "listen_stt", "--", text], check=True)
            subprocess.run(["tmux", "paste-buffer", "-t", target, "-b", "listen_stt"], check=True)
            log(f"Pasted to {target}: {text[:40]!r}")
        except Exception as e:
            log(f"tmux paste failed: {e}")

    # -------- STT callbacks --------
    def _on_data(self, tr):
        from assemblyai import RealtimeFinalTranscript
        if isinstance(tr, RealtimeFinalTranscript):
            t = (tr.text or "").strip()
            if t:
                with self.lock:
                    self.buf.append(t + " ")

    def _on_error(self, err):
        log(f"STT error: {err}")

    def _stream(self):
        try:
            self.transcriber.stream(aai.extras.MicrophoneStream(sample_rate=16000))
        except Exception as e:
            log(f"stream error: {e}")
            with self.lock:
                self.is_listening = False

    # -------- control --------
    def start(self):
        if self.is_listening:
            return
        with self.lock:
            self.buf = []
            self.is_listening = True
        try:
            self.transcriber = aai.RealtimeTranscriber(
                sample_rate=16000,
                on_data=self._on_data,
                on_error=self._on_error,
                disable_partial_transcripts=False,
            )
            self.transcriber.connect()
            self.thread = threading.Thread(target=self._stream, daemon=True)
            self.thread.start()
            log("Listening started")
        except Exception as e:
            log(f"start error: {e}")
            with self.lock:
                self.is_listening = False

    def stop(self):
        if not self.is_listening:
            return
        with self.lock:
            self.is_listening = False
        try:
            if self.transcriber:
                try:
                    self.transcriber.close()
                except Exception:
                    pass
                self.transcriber = None
        finally:
            full = ""
            with self.lock:
                if self.buf:
                    full = "".join(self.buf)
                    full = full.replace("\n", " ").replace("\r", " ")
                    full = re.sub(r"\s+", " ", full).strip()
                self.buf = []
        if full:
            self._tmux_paste(full)
            log("Listening stopped; pasted transcript")
        else:
            log("Listening stopped; no text")

    def toggle(self):
        if self.is_listening:
            self.stop()
        else:
            self.start()


def main():
    d = STTDaemon()

    # PID file
    try:
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    def sigusr1(_signo, _frame):
        d.toggle()

    def sigterm(_s, _f):
        d.stop()
        sys.exit(0)

    signal.signal(signal.SIGUSR1, sigusr1)
    signal.signal(signal.SIGTERM, sigterm)
    signal.signal(signal.SIGINT, sigterm)

    log("Daemon ready. Send SIGUSR1 to toggle.")
    # Sleep forever
    try:
        while True:
            time.sleep(1)
    finally:
        d.stop()


if __name__ == "__main__":
    main()

