from __future__ import annotations

import os
import threading
from typing import Optional

import numpy as np
import sherpa_onnx

from .base import BaseEngine
from ..audio import MicrophoneSource


class SherpaOnnxEngine(BaseEngine):
    """Local sherpa-onnx streaming engine."""

    def __init__(
        self,
        *,
        on_partial,
        on_final,
        on_error,
        hud_throttle_ms: int = 75,
        hot_mic: bool = False,
    ):
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
            enable_endpoint_detection=False,  # Manual toggle controls paste
            rule1_min_trailing_silence=rule1,
            rule2_min_trailing_silence=rule2,
            rule3_min_utterance_length=rule3,
            decoding_method=decoding,
        )

        self.hot_mic = bool(hot_mic)
        self.stream = self.recognizer.create_stream()
        self.mic_rate = int(os.getenv("LISTEN_SAMPLE_RATE", "48000"))
        self.chunk_ms = int(os.getenv("LISTEN_CHUNK_MS", "100"))
        self._initial_padding_frames = int(0.12 * self.mic_rate)

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._shutdown_event = threading.Event()
        self._thread_ready = threading.Event()
        self._reset_event = threading.Event()
        self._request_reset = False

        self._recognizer_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._latest_result = ""
        self._listening = False
        self._padding_frames = 0

        if self.hot_mic:
            # Kick off the background loop immediately so the mic stays warm.
            self._request_reset = True
            self._thread = threading.Thread(target=self._continuous_loop, daemon=True)
            self._thread.start()
            # Wait for the loop to start; avoid hanging if something goes wrong.
            self._thread_ready.wait(timeout=2.0)
            self._reset_event.wait(timeout=2.0)

    # ------------------------------------------------------------------
    def _process_samples(self, samples: np.ndarray) -> None:
        with self._recognizer_lock:
            self.stream.accept_waveform(self.mic_rate, samples)
            while self.recognizer.is_ready(self.stream):
                self.recognizer.decode_stream(self.stream)
            result = self.recognizer.get_result(self.stream)

        with self._state_lock:
            self._latest_result = result
            listening = self._listening

        if result and listening:
            self._emit_partial(result)

    def _handle_pending_reset(self) -> bool:
        with self._state_lock:
            if not self._request_reset or self._listening:
                return False
            self._request_reset = False

        with self._recognizer_lock:
            self.recognizer.reset(self.stream)

        with self._state_lock:
            self._latest_result = ""
            self._padding_frames = 0
            self._last_hud_ts = 0.0

        self._reset_event.set()
        return True

    def _inject_padding_if_needed(self) -> None:
        with self._state_lock:
            padding = self._padding_frames
            if padding:
                self._padding_frames = 0
        if not padding:
            return
        silence = np.zeros(padding, dtype="float32")
        with self._recognizer_lock:
            self.stream.accept_waveform(self.mic_rate, silence)

    def _continuous_loop(self) -> None:
        try:
            with MicrophoneSource(self.mic_rate, self.chunk_ms) as mic:
                self._thread_ready.set()
                while not self._shutdown_event.is_set():
                    if self._handle_pending_reset():
                        continue
                    if not self.is_listening():
                        if self._shutdown_event.is_set():
                            break
                        mic.read()  # discard chunk to keep stream alive
                        continue

                    self._inject_padding_if_needed()
                    samples = mic.read()
                    if self._shutdown_event.is_set():
                        break
                    self._process_samples(samples)
        except Exception as exc:  # pragma: no cover
            self.on_error(str(exc))
        finally:
            self._reset_event.set()
            self._thread_ready.set()

    # ------------------------------------------------------------------
    def is_listening(self) -> bool:
        with self._state_lock:
            return self._listening

    def start(self) -> None:
        if self.hot_mic:
            self._thread_ready.wait(timeout=2.0)
            with self._state_lock:
                if self._listening:
                    return
                self._latest_result = ""
                self._listening = False
                self._padding_frames = 0
                self._reset_event.clear()
                self._request_reset = True

            if not self._reset_event.wait(timeout=1.0):
                self.on_error("sherpa-onnx reset timed out; continuing")

            with self._state_lock:
                self._padding_frames = self._initial_padding_frames
                self._listening = True
                self._last_hud_ts = 0.0
            return

        with self._state_lock:
            if self._listening:
                return
            self._listening = True
            self._latest_result = ""
            self._padding_frames = self._initial_padding_frames
            self._last_hud_ts = 0.0

        with self._recognizer_lock:
            self.recognizer.reset(self.stream)
            if self._padding_frames:
                silence = np.zeros(self._padding_frames, dtype="float32")
                self.stream.accept_waveform(self.mic_rate, silence)
            self._padding_frames = 0

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._segment_loop, daemon=True)
        self._thread.start()

    def _segment_loop(self) -> None:
        try:
            with MicrophoneSource(self.mic_rate, self.chunk_ms) as mic:
                while not self._stop_event.is_set():
                    samples = mic.read()
                    if self._stop_event.is_set():
                        break
                    self._process_samples(samples)
        except Exception as exc:  # pragma: no cover
            self.on_error(str(exc))

    def stop_quick(self) -> str:
        if self.hot_mic:
            with self._state_lock:
                if not self._listening:
                    return ""
                self._listening = False
            with self._recognizer_lock:
                final_text = self.recognizer.get_result(self.stream)
            if final_text:
                with self._state_lock:
                    self._latest_result = final_text
            return (final_text or self._latest_result).strip()

        with self._state_lock:
            if not self._listening:
                return ""
            self._listening = False

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

        with self._recognizer_lock:
            final_text = self.recognizer.get_result(self.stream)
            self.recognizer.reset(self.stream)

        with self._state_lock:
            if final_text:
                self._latest_result = final_text

        return (final_text or self._latest_result).strip()

    def shutdown(self) -> None:
        if self.hot_mic:
            self._shutdown_event.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=1.5)
            return

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def prewarm(self) -> None:
        # Recognizer construction loads models eagerly; hot-mic threads are
        # already spun up in __init__ when enabled.
        pass
