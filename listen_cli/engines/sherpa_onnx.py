from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path
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
        deferred: bool = False,
    ):
        super().__init__(
            on_partial=on_partial,
            on_final=on_final,
            on_error=on_error,
            hud_throttle_ms=hud_throttle_ms,
        )
        # Store configuration for deferred loading
        self._encoder = os.getenv("LISTEN_SHERPA_ENCODER")
        self._decoder = os.getenv("LISTEN_SHERPA_DECODER")
        self._joiner = os.getenv("LISTEN_SHERPA_JOINER")
        self._tokens = os.getenv("LISTEN_SHERPA_TOKENS")
        if not all([self._encoder, self._decoder, self._joiner, self._tokens]):
            raise RuntimeError("Missing LISTEN_SHERPA_* model paths for sherpa-onnx provider")

        self._provider = os.getenv("LISTEN_SHERPA_PROVIDER", "cpu")
        self._threads = int(os.getenv("LISTEN_SHERPA_THREADS", "1"))
        self._decoding = os.getenv("LISTEN_SHERPA_DECODING", "greedy_search")
        self._rule1 = float(os.getenv("LISTEN_SHERPA_RULE1", "2.4"))
        self._rule2 = float(os.getenv("LISTEN_SHERPA_RULE2", "1.2"))
        self._rule3 = float(os.getenv("LISTEN_SHERPA_RULE3", "300"))

        # Initialize as None for deferred loading
        self.recognizer = None
        self.stream = None
        self._punctuator = None
        self._deferred = deferred
        self._initialized = False

        self.hot_mic = bool(hot_mic)
        self.mic_rate = int(os.getenv("LISTEN_SAMPLE_RATE", "48000"))
        self.chunk_ms = int(os.getenv("LISTEN_CHUNK_MS", "100"))
        self._initial_padding_frames = int(0.12 * self.mic_rate)
        self._prebuffer_seconds = float(os.getenv("BACKGROUND_PREBUFFER_SECONDS", "0.4"))
        self._prebuffer_max_frames = max(0, int(self.mic_rate * self._prebuffer_seconds))
        self._prebuffer = deque()
        self._prebuffer_frames = 0

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
        self._prebuffer_needs_flush = False
        self._raw_text = ""
        self._prebuffer_ready = False  # Track if prebuffer has filled once
        self._first_start_after_init = False  # Track first toggle after initialization

        if self.hot_mic:
            # Hot mic mode - will be ready after prewarming
            self.set_ready(False)
            # Kick off the background loop immediately so the mic stays warm.
            self._request_reset = True
            self._thread = threading.Thread(target=self._continuous_loop, daemon=True)
            self._thread.start()
        else:
            if not self._deferred:
                # Load immediately for non-hot-mic, non-deferred mode
                self._load_models()
            # Normal mode - ready immediately
            self.set_ready(True)

    # ------------------------------------------------------------------
    def _load_models(self) -> None:
        """Load recognizer and punctuator models."""
        if self._initialized:
            return

        # Load recognizer
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=self._tokens,
            encoder=self._encoder,
            decoder=self._decoder,
            joiner=self._joiner,
            provider=self._provider,
            num_threads=self._threads,
            sample_rate=16000,
            feature_dim=80,
            enable_endpoint_detection=False,
            rule1_min_trailing_silence=self._rule1,
            rule2_min_trailing_silence=self._rule2,
            rule3_min_utterance_length=self._rule3,
            decoding_method=self._decoding,
        )
        self.stream = self.recognizer.create_stream()

        # Load punctuator
        self._punctuator = self._load_punctuator()
        self._initialized = True

    def _update_status(self, message: str) -> None:
        """Thread-safe status update."""
        try:
            import subprocess
            cmd = ["tmux", "set-option", "-g", "@asr_message", message]
            subprocess.run(cmd, check=False, capture_output=True)
        except Exception:
            pass  # Ignore tmux errors

    # ------------------------------------------------------------------
    def _process_samples(self, samples: np.ndarray) -> None:
        with self._recognizer_lock:
            self.stream.accept_waveform(self.mic_rate, samples)
            while self.recognizer.is_ready(self.stream):
                self.recognizer.decode_stream(self.stream)
            raw = self.recognizer.get_result(self.stream)

        formatted = self._format_text(raw, final=False)

        with self._state_lock:
            self._raw_text = raw
            self._latest_result = formatted
            listening = self._listening

        if formatted and listening:
            self._emit_partial(formatted)

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
            self._raw_text = ""

        self._reset_event.set()
        return True

    def _prime_stream_with_silence(self) -> None:
        frames = max(0, self._initial_padding_frames)
        if frames == 0:
            return
        silence = np.zeros(frames, dtype="float32")
        with self._recognizer_lock:
            self.stream.accept_waveform(self.mic_rate, silence)
            # Process the padding immediately so internal states are ready.
            while self.recognizer.is_ready(self.stream):
                self.recognizer.decode_stream(self.stream)

    def _load_punctuator(self) -> Optional[sherpa_onnx.OnlinePunctuation]:
        if os.getenv("LISTEN_DISABLE_PUNCT"):
            return None

        model_dir_env = os.getenv("LISTEN_PUNCT_MODEL_DIR")
        candidates: list[Path] = []
        if model_dir_env:
            candidates.append(Path(model_dir_env))

        sherpa_model_dir = os.getenv("LISTEN_SHERPA_MODEL_DIR")
        if sherpa_model_dir:
            sherpa_path = Path(sherpa_model_dir)
            candidates.extend([sherpa_path, sherpa_path / "punctuation"])

        default_root = Path(__file__).resolve().parent.parent / "models"
        candidates.extend(
            [
                default_root / "punctuation",
                default_root / "zipformer-en20m",
                default_root / "zipformer-en20m" / "punctuation",
            ]
        )

        model_path: Optional[Path] = None
        vocab_path: Optional[Path] = None
        for directory in candidates:
            model_file = directory / "model.onnx"
            vocab_file = directory / "bpe.vocab"
            if model_file.is_file() and vocab_file.is_file():
                model_path = model_file
                vocab_path = vocab_file
                break

        if not model_path or not vocab_path:
            return None

        try:
            provider = os.getenv("LISTEN_PUNCT_PROVIDER", "cpu")
            threads = int(os.getenv("LISTEN_PUNCT_THREADS", "1"))
            debug_env = os.getenv("LISTEN_PUNCT_DEBUG")
            debug = False
            if debug_env is not None:
                debug = debug_env.strip().lower() in {"1", "true", "on", "yes"}
            config = sherpa_onnx.OnlinePunctuationConfig(
                model=sherpa_onnx.OnlinePunctuationModelConfig(
                    cnn_bilstm=str(model_path),
                    bpe_vocab=str(vocab_path),
                    num_threads=threads,
                    provider=provider,
                    debug=debug,
                )
            )
            return sherpa_onnx.OnlinePunctuation(config)
        except Exception as exc:  # pragma: no cover
            self.on_error(f"punctuator load failed: {exc}")
            return None

    def _append_prebuffer(self, samples: np.ndarray) -> None:
        if self._prebuffer_max_frames == 0:
            return
        # Copy to avoid referencing reused buffers from sounddevice
        chunk = np.array(samples, dtype="float32", copy=True)
        frames = len(chunk)
        if frames == 0:
            return
        with self._state_lock:
            self._prebuffer.append(chunk)
            self._prebuffer_frames += frames
            while self._prebuffer_frames > self._prebuffer_max_frames and self._prebuffer:
                oldest = self._prebuffer.popleft()
                self._prebuffer_frames -= len(oldest)

    def _drain_prebuffer(self) -> None:
        if self._prebuffer_max_frames == 0:
            return
        pending = []
        with self._state_lock:
            if not self._prebuffer:
                self._prebuffer_needs_flush = False
                return
            pending = list(self._prebuffer)
            self._prebuffer.clear()
            self._prebuffer_frames = 0
            self._prebuffer_needs_flush = False
        for chunk in pending:
            self._process_samples(chunk)

    def _reset_prebuffer(self) -> None:
        with self._state_lock:
            self._prebuffer.clear()
            self._prebuffer_frames = 0
            self._prebuffer_needs_flush = False
            self._raw_text = ""

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

    def _format_text(self, text: str, *, final: bool) -> str:
        cleaned = text.strip()
        if not cleaned:
            return ""
        lower = cleaned.lower()
        if final and self._punctuator:
            try:
                return self._punctuator.AddPunctuationWithCase(lower)
            except Exception as exc:  # pragma: no cover
                self.on_error(f"punctuation error: {exc}")
        return lower.capitalize()

    def _continuous_loop(self) -> None:
        try:
            # Phase 1: Load models
            self._update_status("Loading... 🎙")
            self._load_models()

            # Phase 2: Initialize audio
            self._update_status("Loading... 🎙")
            with MicrophoneSource(self.mic_rate, self.chunk_ms) as mic:
                self._thread_ready.set()
                self._prime_stream_with_silence()

                # Phase 3: Fill prebuffer with real-time audio
                self._update_status("Loading... 🎙")

                # Drain any buffered audio first
                for _ in range(3):
                    try:
                        mic.read()  # Discard buffered samples
                    except:
                        pass

                # Fill prebuffer with real-time audio over actual time period
                start_time = time.time()
                while time.time() - start_time < self._prebuffer_seconds:
                    samples = mic.read()
                    self._append_prebuffer(samples)
                    if self._shutdown_event.is_set():
                        break

                # Phase 4: Ready
                self._update_status("")  # Clear status
                self.set_ready(True)
                self._prebuffer_ready = True
                self._first_start_after_init = True  # Mark that we're ready for first use

                # Normal loop continues
                while not self._shutdown_event.is_set():
                    if self._handle_pending_reset():
                        continue
                    if not self.is_listening():
                        if self._shutdown_event.is_set():
                            break
                        samples = mic.read()
                        self._append_prebuffer(samples)
                        continue

                    if self._prebuffer_needs_flush:
                        self._drain_prebuffer()
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
        # Ensure models are loaded for non-hot-mic mode
        if not self._initialized:
            self._load_models()

        if self.hot_mic:
            self._thread_ready.wait(timeout=2.0)
            with self._state_lock:
                if self._listening:
                    return
                self._latest_result = ""
                self._raw_text = ""
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
                if self._prebuffer_max_frames:
                    # On first start after init, reset prebuffer instead of draining
                    if self._first_start_after_init:
                        self._prebuffer.clear()
                        self._prebuffer_frames = 0
                        self._prebuffer_needs_flush = False
                        self._first_start_after_init = False
                        # First start after init - reset prebuffer to avoid cutting off speech
                    else:
                        self._prebuffer_needs_flush = True
                    self._latest_result = ""
                    self._raw_text = ""
            return

        with self._state_lock:
            if self._listening:
                return
            self._listening = True
            self._latest_result = ""
            self._raw_text = ""
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
                formatted = self._format_text(final_text, final=True)
            else:
                with self._state_lock:
                    raw = self._raw_text
                formatted = self._format_text(raw, final=True)

            with self._state_lock:
                self._latest_result = formatted
                self._raw_text = ""

            return formatted.strip()

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

        if final_text:
            formatted = self._format_text(final_text, final=True)
        else:
            with self._state_lock:
                raw = self._raw_text
            formatted = self._format_text(raw, final=True)

        with self._state_lock:
            self._latest_result = formatted
            self._raw_text = ""

        self._reset_prebuffer()

        return formatted.strip()

    def shutdown(self) -> None:
        if self.hot_mic:
            self._shutdown_event.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=1.5)
            self._reset_prebuffer()
            return

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def prewarm(self) -> None:
        # Recognizer construction loads models eagerly; hot-mic threads are
        # already spun up in __init__ when enabled.
        pass
