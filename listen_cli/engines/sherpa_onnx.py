from __future__ import annotations
import os
import threading
from typing import Optional

import sherpa_onnx

from .base import BaseEngine
from ..audio import MicrophoneSource


class SherpaOnnxEngine(BaseEngine):
    """Local sherpa-onnx streaming engine."""

    def __init__(self, *, on_partial, on_final, on_error, hud_throttle_ms: int = 75):
        super().__init__(
            on_partial=on_partial,
            on_final=on_final,
            on_error=on_error,
            hud_throttle_ms=hud_throttle_ms,
        )
        encoder = os.getenv("LISTEN_SHERPA_ENCODER")
        decoder = os.getenv("LISTEN_SHERPA_DECODER")
        joiner = os.getenv("LISTEN_SHERPA_JOINER")
        tokens = os.getenv("LISTEN_SHERPA_TOKENS")
        if not all([encoder, decoder, joiner, tokens]):
            raise RuntimeError("Missing LISTEN_SHERPA_* model paths for sherpa-onnx provider")

        provider = os.getenv("LISTEN_SHERPA_PROVIDER", "cpu")
        threads = int(os.getenv("LISTEN_SHERPA_THREADS", "1"))
        decoding = os.getenv("LISTEN_SHERPA_DECODING", "greedy_search")
        rule1 = float(os.getenv("LISTEN_SHERPA_RULE1", "2.4"))
        rule2 = float(os.getenv("LISTEN_SHERPA_RULE2", "1.2"))
        rule3 = float(os.getenv("LISTEN_SHERPA_RULE3", "300"))

        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=tokens,
            encoder=encoder,
            decoder=decoder,
            joiner=joiner,
            provider=provider,
            num_threads=threads,
            sample_rate=16000,
            feature_dim=80,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=rule1,
            rule2_min_trailing_silence=rule2,
            rule3_min_utterance_length=rule3,
            decoding_method=decoding,
        )

        self.stream: Optional[sherpa_onnx.OnlineStream] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._buffer: list[str] = []
        self._last_partial = ""
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self.mic_rate = int(os.getenv("LISTEN_SAMPLE_RATE", "48000"))
        self.chunk_ms = int(os.getenv("LISTEN_CHUNK_MS", "100"))

    def _loop(self) -> None:
        try:
            assert self.stream is not None
            with MicrophoneSource(self.mic_rate, self.chunk_ms) as mic:
                while not self._stop_event.is_set():
                    samples = mic.read()
                    if self._stop_event.is_set():
                        break
                    self.stream.accept_waveform(self.mic_rate, samples)
                    while self.recognizer.is_ready(self.stream):
                        self.recognizer.decode_stream(self.stream)
                    partial = self.recognizer.get_result(self.stream)
                    if partial:
                        with self._lock:
                            self._last_partial = partial
                        self._emit_partial(partial)
                    if self.recognizer.is_endpoint(self.stream):
                        final_text = self.recognizer.get_result(self.stream)
                        if final_text:
                            with self._lock:
                                self._buffer.append(final_text)
                                self._last_partial = ""
                            self._emit_partial(final_text)
                            self.on_final(final_text)
                        self.recognizer.reset(self.stream)
        except Exception as exc:  # pragma: no cover
            self.on_error(str(exc))

    def is_listening(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self.stream = self.recognizer.create_stream()
        self._buffer.clear()
        self._last_partial = ""
        self._stop_event.clear()
        self._running = True

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _assemble_text(self) -> str:
        with self._lock:
            parts: list[str] = []
            if self._buffer:
                parts.append(" ".join(self._buffer))
            if self._last_partial:
                parts.append(self._last_partial)
        return " ".join(parts).strip()

    def stop_quick(self) -> str:
        if not self._running:
            return ""
        self._running = False
        self._stop_event.set()
        return self._assemble_text()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        self.stream = None

    def prewarm(self) -> None:
        # Recognizer construction loads models eagerly, so nothing else to do.
        pass
