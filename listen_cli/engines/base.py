from __future__ import annotations
from typing import Callable
import threading
import time


class BaseEngine:
    def __init__(
        self,
        *,
        on_partial: Callable[[str], None],
        on_final: Callable[[str], None],
        on_error: Callable[[str], None],
        hud_throttle_ms: int = 75,
    ):
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_error = on_error
        self._last_hud_ts = 0.0
        self._hud_throttle = max(0, hud_throttle_ms) / 1000.0
        self._ready_event = threading.Event()
        # Start as not ready - subclasses will set when appropriate

    def start(self) -> None:
        raise NotImplementedError

    def stop_quick(self) -> str:
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError

    def is_listening(self) -> bool:
        raise NotImplementedError

    def _emit_partial(self, text: str) -> None:
        now = time.time()
        if now - self._last_hud_ts < self._hud_throttle:
            return
        self._last_hud_ts = now
        text = " ".join(text.split())
        if len(text) > 60:
            text = text[:60] + "…"
        self.on_partial(text)

    def set_ready(self, ready: bool) -> None:
        if ready:
            self._ready_event.set()
        else:
            self._ready_event.clear()

    def is_ready(self) -> bool:
        return self._ready_event.is_set()

    @property
    def ready_event(self) -> threading.Event:
        return self._ready_event
