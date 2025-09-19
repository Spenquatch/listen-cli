from __future__ import annotations
import sounddevice as sd
import numpy as np


class MicrophoneSource:
    def __init__(self, sample_rate: int, chunk_ms: int = 100):
        self.sample_rate = int(sample_rate)
        self.chunk_ms = int(chunk_ms)
        self.chunk_samples = max(1, int(self.sample_rate * self.chunk_ms / 1000))
        self._stream = None

    def open(self):
        self._stream = sd.InputStream(channels=1, dtype="float32", samplerate=self.sample_rate)
        self._stream.start()
        return self

    def close(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def read(self) -> np.ndarray:
        assert self._stream is not None
        frames, _overflowed = self._stream.read(self.chunk_samples)
        return frames.reshape(-1)
