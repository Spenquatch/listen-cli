from __future__ import annotations
import os
import threading
from typing import Optional

import assemblyai as aai

from .base import BaseEngine


class AssemblyAIEngine(BaseEngine):
    """Realtime AssemblyAI provider behind the BaseEngine interface."""

    def __init__(self, *, on_partial, on_final, on_error, hud_throttle_ms: int = 75):
        super().__init__(
            on_partial=on_partial,
            on_final=on_final,
            on_error=on_error,
            hud_throttle_ms=hud_throttle_ms,
        )
        api_key = os.getenv("ASSEMBLYAI_API_KEY")
        if not api_key:
            raise RuntimeError("ASSEMBLYAI_API_KEY env var is required for AssemblyAI provider")
        aai.settings.api_key = api_key

        self._transcriber: Optional[aai.RealtimeTranscriber] = None
        self._thread: Optional[threading.Thread] = None
        self._mic_stream: Optional[aai.extras.MicrophoneStream] = None
        self._listening = False
        self._buffer: list[str] = []
        self._last_partial = ""
        self._lock = threading.Lock()
        self._connected = False

        # AssemblyAI is ready immediately (no prewarming needed)
        self.set_ready(True)

    # ------------------------------------------------------------------
    def _handle_error(self, error: Exception) -> None:
        self.on_error(str(error))

    def _on_data(self, transcript: aai.RealtimeTranscript) -> None:
        if isinstance(transcript, aai.RealtimePartialTranscript):
            if transcript.text:
                with self._lock:
                    self._last_partial = transcript.text
                self._emit_partial(transcript.text)
        elif isinstance(transcript, aai.RealtimeFinalTranscript):
            if transcript.text:
                with self._lock:
                    self._buffer.append(transcript.text)
                    self._last_partial = ""
                self._emit_partial(transcript.text)
                self.on_final(transcript.text)

    def _assemble_text(self) -> str:
        with self._lock:
            parts: list[str] = []
            if self._buffer:
                parts.append(" ".join(self._buffer))
            if self._last_partial:
                parts.append(self._last_partial)
        return " ".join(parts).strip()

    def _build_transcriber(self) -> aai.RealtimeTranscriber:
        return aai.RealtimeTranscriber(
            sample_rate=16000,
            on_data=self._on_data,
            on_error=self._handle_error,
            on_open=lambda _evt: None,
            on_close=lambda: None,
            disable_partial_transcripts=False,
        )

    def _ensure_transcriber(self) -> None:
        if self._transcriber is None:
            self._transcriber = self._build_transcriber()
        if not self._connected:
            self._transcriber.connect()
            self._connected = True

    # ------------------------------------------------------------------
    def is_listening(self) -> bool:
        return self._listening

    def start(self) -> None:
        if self._listening:
            return

        self._buffer.clear()
        self._last_partial = ""
        self._listening = True

        try:
            self._ensure_transcriber()
        except Exception as exc:  # pragma: no cover
            self._listening = False
            self._handle_error(exc)
            return

        self._mic_stream = aai.extras.MicrophoneStream(sample_rate=16000)

        def _stream() -> None:
            try:
                assert self._transcriber is not None
                assert self._mic_stream is not None
                self._transcriber.stream(self._mic_stream)
            except Exception as exc:  # pragma: no cover
                self._handle_error(exc)
            finally:
                if self._mic_stream is not None:
                    try:
                        self._mic_stream.close()
                    except Exception:  # pragma: no cover
                        pass
                    self._mic_stream = None

        self._thread = threading.Thread(target=_stream, daemon=True)
        self._thread.start()

    def stop_quick(self) -> str:
        if not self._listening:
            return ""
        self._listening = False
        if self._mic_stream is not None:
            try:
                self._mic_stream.close()
            except Exception:  # pragma: no cover
                pass
            finally:
                self._mic_stream = None
        if self._transcriber is not None:
            try:
                self._transcriber.force_end_utterance()
            except Exception:  # pragma: no cover
                pass
        return self._assemble_text()

    def shutdown(self) -> None:
        self._listening = False
        if self._mic_stream is not None:
            try:
                self._mic_stream.close()
            except Exception:  # pragma: no cover
                pass
            finally:
                self._mic_stream = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        if self._transcriber is not None:
            try:
                self._transcriber.close()
            except Exception as exc:  # pragma: no cover
                self._handle_error(exc)
            finally:
                self._transcriber = None
                self._connected = False

    def prewarm(self) -> None:
        try:
            self._ensure_transcriber()
        except Exception as exc:  # pragma: no cover
            self._handle_error(exc)
