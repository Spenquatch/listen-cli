#!/usr/bin/env python3
"""
ASR daemon for listen-cli: runs once per tmux session in a hidden window.

- Listens on a Unix domain socket for:  TOGGLE <pane_id>
- On first toggle: starts realtime ASR; shows HUD (🎙 REC) and partials in @asr_preview
- On second toggle: stops ASR; pastes transcript into <pane_id> via tmux paste-buffer -p

Env:
  ASSEMBLYAI_API_KEY     (required)
  LISTEN_SESSION         (tmux session name, required)
  LISTEN_SOCKET          (path to UDS socket; default /tmp/listen-<session>.sock)
"""

from __future__ import annotations
import asyncio
import os
import shlex
import signal
import subprocess
import tempfile
import threading
from typing import Optional

# --- AssemblyAI ---------------------------------------------------------------
try:
    import assemblyai as aai
except Exception as e:  # pragma: no cover
    raise SystemExit(
        "assemblyai package is required. Install with:\n  poetry add assemblyai -E extras"
    ) from e


def tmux(*args: str) -> None:
    """Run a tmux command; raise on failure."""
    subprocess.run(["tmux", *args], check=False)


def tmux_set_var(name: str, value: str) -> None:
    tmux("set", "-gq", name, value)
    tmux("refresh-client", "-S")


def tmux_status_on(listening: bool) -> None:
    tmux_set_var("@asr_on", "1" if listening else "0")
    if not listening:
        tmux_set_var("@asr_preview", "")


def tmux_preview(text: str) -> None:
    text = " ".join(text.splitlines())
    if len(text) > 60:
        text = text[:60] + "…"
    tmux_set_var("@asr_preview", text)


def paste_into_pane(pane_id: str, text: str) -> None:
    # Use a temp file to avoid shell quoting issues, then bracket-paste (-p)
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        tmux("load-buffer", "-b", "listen_asr", path)
        tmux("paste-buffer", "-p", "-b", "listen_asr", "-t", pane_id)
        tmux("delete-buffer", "-b", "listen_asr")
        tmux("display-message", f"✅ Pasted ASR into {pane_id}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class SilentVoiceController:
    """Extracted & adapted from your original code, now without PTY bits."""

    def __init__(self):
        api_key = os.getenv("ASSEMBLYAI_API_KEY")
        if not api_key:
            raise RuntimeError("ASSEMBLYAI_API_KEY env var is required")
        aai.settings.api_key = api_key

        self._transcriber: Optional[aai.RealtimeTranscriber] = None
        self._thread: Optional[threading.Thread] = None
        self._listening = False
        self._buffer = []
        self._last_partial = ""
        self._lock = threading.Lock()

    # --- AAI callbacks --------------------------------------------------------
    def _on_open(self, _evt: aai.RealtimeSessionOpened):  # noqa: ANN001
        pass

    def _on_error(self, error: aai.RealtimeError):
        tmux("display-message", f"❌ ASR error: {error}")

    def _on_close(self):
        pass

    def _on_data(self, t: aai.RealtimeTranscript):
        if isinstance(t, aai.RealtimePartialTranscript):
            if t.text:
                self._last_partial = t.text
                tmux_preview(t.text)
        elif isinstance(t, aai.RealtimeFinalTranscript):
            if t.text:
                with self._lock:
                    self._buffer.append(t.text)
                    self._last_partial = ""
                tmux_preview(t.text)

    # --- Control --------------------------------------------------------------
    def is_listening(self) -> bool:
        return self._listening

    def start(self) -> None:
        if self._listening:
            return
        self._buffer.clear()
        self._listening = True
        tmux_status_on(True)

        self._transcriber = aai.RealtimeTranscriber(
            sample_rate=16000,
            on_data=self._on_data,
            on_error=self._on_error,
            on_open=self._on_open,
            on_close=self._on_close,
            disable_partial_transcripts=False,
        )
        self._transcriber.connect()

        def _stream():
            try:
                self._transcriber.stream(aai.extras.MicrophoneStream(sample_rate=16000))
            except Exception as e:  # pragma: no cover
                tmux("display-message", f"❌ Streaming error: {e}")
                self._listening = False

        self._thread = threading.Thread(target=_stream, daemon=True)
        self._thread.start()

    def _assemble_text(self) -> str:
        parts: list[str] = []
        with self._lock:
            if self._buffer:
                parts.append(" ".join(self._buffer))
            if self._last_partial:
                parts.append(self._last_partial)
        return " ".join(p for p in parts if p).strip()

    def stop_quick(self) -> str:
        """Stop listening immediately and return best-effort text.

        Does not block on websocket close; use shutdown() to close resources.
        """
        if not self._listening:
            return ""
        self._listening = False
        tmux_status_on(False)
        text = self._assemble_text()
        # No popup messages; HUD will reflect state and paste will provide feedback
        return text

    def shutdown(self) -> None:
        """Close the realtime transcriber and stop streaming (may block)."""
        try:
            if self._transcriber:
                self._transcriber.close()
        except Exception as e:  # pragma: no cover
            tmux("display-message", f"❌ Shutdown error: {e}")
        finally:
            self._transcriber = None


class ASRDaemon:
    """Unix socket command server for tmux keybinds."""

    def __init__(self, session: str, socket_path: Optional[str] = None):
        self.session = session
        self.socket_path = socket_path or f"/tmp/listen-{session}.sock"
        self.voice = SilentVoiceController()
        self._stopping = False

    async def _stop_and_maybe_paste(self, pane_id: str) -> None:
        try:
            # Get text immediately without waiting for websocket close
            text = self.voice.stop_quick()
            if text and pane_id:
                tmux_set_var("@asr_preview", "Pasting…")
                paste_into_pane(pane_id, text)
                tmux_set_var("@asr_preview", "")
            # Close the transcriber in background to release resources
            await asyncio.to_thread(self.voice.shutdown)
        finally:
            self._stopping = False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = (await reader.read(256)).decode().strip()
        parts = data.split()
        cmd = parts[0].upper() if parts else ""
        pane_id = parts[1] if len(parts) > 1 else ""

        if cmd == "TOGGLE":
            if not self.voice.is_listening() and not self._stopping:
                self.voice.start()
            else:
                # Flip HUD off immediately and stop in background so the keypress feels snappy
                if not self._stopping:
                    self._stopping = True
                    tmux_set_var("@asr_on", "0")
                    tmux_set_var("@asr_preview", "Pasting…")
                    asyncio.create_task(self._stop_and_maybe_paste(pane_id))
            writer.write(b"OK\n")
        elif cmd == "PING":
            writer.write(b"PONG\n")
        else:
            writer.write(b"ERR\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run(self):
        # Ensure no stale socket
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

        server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        os.chmod(self.socket_path, 0o600)

        # Stop gracefully on SIGTERM
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _sigterm(*_):
            stop_event.set()

        loop.add_signal_handler(signal.SIGTERM, _sigterm)

        # Serve forever until SIGTERM
        async with server:
            await stop_event.wait()
            server.close()
            await server.wait_closed()
            # If recording, stop now
            if self.voice.is_listening():
                self.voice.stop()


def main():
    session = os.getenv("LISTEN_SESSION")
    if not session:
        raise SystemExit("LISTEN_SESSION env var is required")
    socket_path = os.getenv("LISTEN_SOCKET") or f"/tmp/listen-{session}.sock"
    daemon = ASRDaemon(session=session, socket_path=socket_path)
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
