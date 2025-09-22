#!/usr/bin/env python3
"""
ASR daemon for listen-cli: runs once per tmux session in a hidden window.

- Listens on a Unix domain socket for:  TOGGLE <pane_id>
- On first toggle: starts realtime ASR; shows HUD (🎙 REC) and partials in @asr_preview
- On second toggle: stops ASR; pastes transcript into <pane_id> via tmux paste-buffer -p

Env:
  LISTEN_SESSION          (tmux session name, required)
  LISTEN_SOCKET           (path to UDS socket; default /tmp/listen-<session>.sock)
  LISTEN_ASR_PROVIDER     (assemblyai|sherpa_onnx; optional)
  LISTEN_PREWARM          (auto|always|never; optional)
  BACKGROUND_ALWAYS_LISTEN (on|off; optional override for local hot mic)
  BACKGROUND_PREBUFFER_SECONDS (float seconds of pre-roll for local hot mic)
  LISTEN_PUNCT_MODEL_DIR  (directory containing model.onnx and bpe.vocab; optional)
  LISTEN_PUNCT_PROVIDER   (cpu|cuda|coreml; optional)
  LISTEN_PUNCT_THREADS    (int threads for punctuation model; optional)
  LISTEN_DISABLE_PUNCT    (disable punctuation/casing when set)
  LISTEN_HUD_THROTTLE_MS  (throttle for HUD updates; optional)
  LISTEN_SHERPA_*         (model paths for sherpa-onnx provider)
  ASSEMBLYAI_API_KEY      (required for AssemblyAI provider)
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from .engines.base import BaseEngine

HUD_THROTTLE_DEFAULT = int(os.getenv("LISTEN_HUD_THROTTLE_MS", "75"))
LOCAL_PROVIDERS = {"sherpa_onnx"}
DEBUG_MODE = os.getenv("LISTEN_DEBUG")
DEBUG_PATH = os.getenv("LISTEN_DEBUG_LOG") or "/tmp/listen-daemon.log"


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------

def tmux(*args: str) -> None:
    """Run a tmux command; ignore exit code (daemon should not crash)."""
    socket = os.getenv("TMUX_SOCKET")
    if socket:
        subprocess.run(["tmux", "-L", socket, *args], check=False)
    else:
        subprocess.run(["tmux", *args], check=False)


def tmux_set_var(name: str, value: str) -> None:
    tmux("set", "-gq", name, value)
    tmux("refresh-client", "-S")


def tmux_status_on(listening: bool) -> None:
    tmux_set_var("@asr_on", "1" if listening else "0")


def tmux_preview(text: str) -> None:
    flat = " ".join(text.splitlines())
    if len(flat) > 60:
        flat = flat[:60] + "…"
    tmux_set_var("@asr_preview", flat)


def paste_into_pane(pane_id: str, text: str) -> None:
    # Use a temp file to avoid shell quoting issues, then bracket-paste (-p)
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        tmux("load-buffer", "-b", "listen_asr", path)
        tmux("paste-buffer", "-p", "-b", "listen_asr", "-t", pane_id)
        tmux("delete-buffer", "-b", "listen_asr")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def debug_log(message: str) -> None:
    if not DEBUG_MODE:
        return
    try:
        with open(DEBUG_PATH, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Provider selection & factory
# ---------------------------------------------------------------------------

def _default_sherpa_model_dir() -> Optional[Path]:
    root = Path(__file__).resolve().parent.parent
    candidate = root / "models" / "zipformer-en20m"
    return candidate if candidate.is_dir() else None


def _ensure_sherpa_env() -> bool:
    required = {
        "LISTEN_SHERPA_TOKENS": os.getenv("LISTEN_SHERPA_TOKENS"),
        "LISTEN_SHERPA_ENCODER": os.getenv("LISTEN_SHERPA_ENCODER"),
        "LISTEN_SHERPA_DECODER": os.getenv("LISTEN_SHERPA_DECODER"),
        "LISTEN_SHERPA_JOINER": os.getenv("LISTEN_SHERPA_JOINER"),
    }
    if all(required.values()):
        return True

    candidates: list[Path] = []
    env_dir = os.getenv("LISTEN_SHERPA_MODEL_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    default_dir = _default_sherpa_model_dir()
    if default_dir:
        candidates.append(default_dir)

    for directory in candidates:
        tokens = directory / "tokens.txt"
        encoder = directory / "encoder-epoch-99-avg-1.onnx"
        decoder = directory / "decoder-epoch-99-avg-1.onnx"
        joiner = directory / "joiner-epoch-99-avg-1.onnx"
        if all(p.is_file() for p in (tokens, encoder, decoder, joiner)):
            os.environ.setdefault("LISTEN_SHERPA_TOKENS", str(tokens))
            os.environ.setdefault("LISTEN_SHERPA_ENCODER", str(encoder))
            os.environ.setdefault("LISTEN_SHERPA_DECODER", str(decoder))
            os.environ.setdefault("LISTEN_SHERPA_JOINER", str(joiner))
            break

    required = {
        "LISTEN_SHERPA_TOKENS": os.getenv("LISTEN_SHERPA_TOKENS"),
        "LISTEN_SHERPA_ENCODER": os.getenv("LISTEN_SHERPA_ENCODER"),
        "LISTEN_SHERPA_DECODER": os.getenv("LISTEN_SHERPA_DECODER"),
        "LISTEN_SHERPA_JOINER": os.getenv("LISTEN_SHERPA_JOINER"),
    }
    return all(required.values())


def _use_hot_mic(provider: str) -> bool:
    override = (os.getenv("BACKGROUND_ALWAYS_LISTEN") or "").strip().lower()
    if override:
        if override in {"always", "on", "true", "1", "yes"}:
            return True
        if override in {"never", "off", "false", "0", "no"}:
            return False
    return provider in LOCAL_PROVIDERS


def make_engine(
    on_partial: Callable[[str], None],
    on_final: Callable[[str], None],
    on_error: Callable[[str], None],
    hud_throttle_ms: int,
) -> Tuple[BaseEngine, str]:
    from importlib import import_module

    provider_env = os.getenv("LISTEN_ASR_PROVIDER")
    provider = provider_env.lower() if provider_env else None

    def _build(provider_name: str) -> Tuple[BaseEngine, str]:
        if provider_name == "sherpa_onnx":
            if not _ensure_sherpa_env():
                raise RuntimeError("Missing LISTEN_SHERPA_* paths for sherpa-onnx provider")
            module = import_module("listen_cli.engines.sherpa_onnx")
            engine_cls = getattr(module, "SherpaOnnxEngine")
            kwargs = {
                "on_partial": on_partial,
                "on_final": on_final,
                "on_error": on_error,
                "hud_throttle_ms": hud_throttle_ms,
                "hot_mic": _use_hot_mic(provider_name),
            }
            engine = engine_cls(**kwargs)
            return engine, provider_name
        elif provider_name == "assemblyai":
            module = import_module("listen_cli.engines.assemblyai")
            engine_cls = getattr(module, "AssemblyAIEngine")
        else:
            raise RuntimeError(f"Unknown LISTEN_ASR_PROVIDER={provider_name}")
        engine = engine_cls(
            on_partial=on_partial,
            on_final=on_final,
            on_error=on_error,
            hud_throttle_ms=hud_throttle_ms,
        )
        return engine, provider_name

    if provider:
        engine, name = _build(provider)
        debug_log(
            f"engine-selection override provider={name} hot_mic={getattr(engine, 'hot_mic', False)}"
        )
        return engine, name

    if _ensure_sherpa_env():
        engine, name = _build("sherpa_onnx")
        debug_log(
            f"engine-selection auto provider=sherpa_onnx hot_mic={getattr(engine, 'hot_mic', False)}"
        )
        return engine, name

    if os.getenv("ASSEMBLYAI_API_KEY"):
        engine, name = _build("assemblyai")
        debug_log("engine-selection auto provider=assemblyai hot_mic=False")
        return engine, name

    raise RuntimeError(
        "No ASR provider configured. Set LISTEN_SHERPA_* env vars or ASSEMBLYAI_API_KEY."
    )


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class ASRDaemon:
    """Unix socket command server for tmux keybinds."""

    def __init__(self, session: str, socket_path: Optional[str] = None):
        self.session = session
        self.socket_path = socket_path or f"/tmp/listen-{session}.sock"
        self._stopping = False
        self._hud_throttle = HUD_THROTTLE_DEFAULT
        try:
            self.engine, self.provider = make_engine(
                on_partial=self._on_partial,
                on_final=self._on_final,
                on_error=self._on_error,
                hud_throttle_ms=self._hud_throttle,
            )
        except Exception as exc:
            debug_log(f"engine init failed: {exc}")
            raise
        debug_log(f"daemon init session={session} provider={self.provider}")
        tmux_set_var("@asr_preview", "")
        tmux_status_on(False)
        self._ready_watch_started = False
        self._maybe_watch_ready()
        if self._should_prewarm():
            prewarm = getattr(self.engine, "prewarm", None)
            if callable(prewarm):
                try:
                    debug_log("daemon prewarm start")
                    prewarm()
                    debug_log("daemon prewarm done")
                except Exception as exc:  # pragma: no cover
                    self._on_error(str(exc))

    # Callbacks from engines -------------------------------------------------
    def _on_partial(self, text: str) -> None:
        tmux_preview(text)

    def _on_final(self, _text: str) -> None:
        pass

    def _on_error(self, message: str) -> None:
        tmux_preview(f"Error: {message}")
        debug_log(f"engine error: {message}")

    # Internal helpers -------------------------------------------------------
    def _maybe_watch_ready(self) -> None:
        event = getattr(self.engine, "ready_event", None)
        if isinstance(event, threading.Event) and not event.is_set():
            tmux_set_var("@asr_message", "Loading…")

            def _wait():
                event.wait()
                tmux_preview("")
                tmux_set_var("@asr_message", "")

            if not self._ready_watch_started:
                watcher = threading.Thread(target=_wait, daemon=True)
                watcher.start()
                self._ready_watch_started = True
        else:
            tmux_set_var("@asr_message", "")

    def _should_prewarm(self) -> bool:
        mode = os.getenv("LISTEN_PREWARM", "auto").lower()
        if mode == "always":
            return True
        if mode == "never":
            return False
        return self.provider == "sherpa_onnx"

    def _start(self) -> None:
        debug_log("toggle start")
        tmux_status_on(True)
        tmux_preview("")
        try:
            self.engine.start()
            debug_log("engine start dispatched")
        except Exception as exc:  # pragma: no cover
            tmux_status_on(False)
            self._on_error(str(exc))
            debug_log(f"engine start error: {exc}")

    async def _stop_and_maybe_paste(self, pane_id: str) -> None:
        try:
            debug_log("toggle stop_quick begin")
            text = self.engine.stop_quick()
            debug_log(f"toggle stop_quick text_len={len(text)}")
            if text.strip() and pane_id:
                paste_into_pane(pane_id, text)
            tmux_preview("")
        finally:
            self._stopping = False
            debug_log("toggle stop complete")

    def toggle(self, pane_id: str) -> None:
        if not self.engine.is_listening() and not self._stopping:
            if not self.engine.is_ready():
                tmux_preview("Loading…")
                self._maybe_watch_ready()
                return
            self._start()
            return
        if self._stopping:
            debug_log("toggle ignored (stopping)")
            return
        self._stopping = True
        tmux_status_on(False)
        tmux_preview("Pasting…")
        debug_log("toggle stop scheduled")
        asyncio.create_task(self._stop_and_maybe_paste(pane_id))

    # Socket plumbing --------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = (await reader.read(256)).decode().strip()
        parts = data.split()
        cmd = parts[0].upper() if parts else ""
        pane_id = parts[1] if len(parts) > 1 else ""

        if cmd == "TOGGLE":
            debug_log(f"socket toggle pane={pane_id}")
            self.toggle(pane_id)
            writer.write(b"OK\n")
        elif cmd == "PING":
            writer.write(b"PONG\n")
        else:
            writer.write(b"ERR\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run(self):
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

        server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        os.chmod(self.socket_path, 0o600)

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _sigterm(*_args):
            stop_event.set()

        loop.add_signal_handler(signal.SIGTERM, _sigterm)

        async with server:
            await stop_event.wait()
            server.close()
            await server.wait_closed()
            if self.engine.is_listening():
                debug_log("shutdown while listening")
                self.engine.stop_quick()
            debug_log("daemon shutdown begin")
            await asyncio.to_thread(self.engine.shutdown)
            debug_log("daemon shutdown done")


def main():
    session = os.getenv("LISTEN_SESSION")
    if not session:
        raise SystemExit("LISTEN_SESSION env var is required")
    socket_path = os.getenv("LISTEN_SOCKET") or f"/tmp/listen-{session}.sock"
    daemon = ASRDaemon(session=session, socket_path=socket_path)
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
