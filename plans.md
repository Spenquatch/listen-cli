# listen-cli — Comprehensive A→Z Implementation Plan (Third Pass)

This document is a self-contained, end-to-end execution plan. It assumes zero prior knowledge. By following this linearly, a single agent can implement a pluggable ASR layer, add a local `sherpa-onnx` (EN‑20M streaming Zipformer) engine, preserve all current functionality, and validate with smoke tests.

It includes: background context, explicit invariants, file-by-file edits, code scaffolds, commands to run, sanity checks, and troubleshooting. Do not skip steps.

-------------------------------------------------------------------------------

## 1) What You’re Building

- Wrap any CLI/TUI in a tmux session with a global hotkey to capture speech and paste the transcript safely (bracketed paste), with a HUD in the tmux status bar.
- Providers (ASR backends):
  - AssemblyAI realtime (existing; refactor behind interface).
  - sherpa‑onnx EN‑20M streaming Zipformer (local, CPU by default; optional CUDA/CoreML).

Key behaviors
- Hotkey toggles: REC on first press; on second press paste final text and return to idle.
- HUD shows REC/idle left; transcript preview immediately to the right of the divider on the left; white 12‑hour clock on the right.
- No popups; no pane focus changes; fast paste.

Hot model, cold mic (no first‑word cutoffs)
- Engines must be prewarmed so there is zero model warm-up delay on the first toggle.
- Keep models/recognizers loaded ("hot") for the life of the tmux session. Local engines (sherpa-onnx) keep the microphone loop warm continuously; the toggle only gates HUD updates and pasting.
- Remote providers (AssemblyAI, etc.) continue to start/stop mic capture on each toggle.
- `BACKGROUND_ALWAYS_LISTEN=off` forces local engines back to push-to-talk; `on` forces continuous loops even for future local models.

Invariants you MUST keep
- Paste uses tmux buffer + `paste-buffer -p` (bracketed paste). Never write to the app pty directly.
- Hotkey keybinding is global (no prefix), runs `run-shell -b` in tmux, and never steals pane focus.
- All tmux interaction (HUD, paste, keybinds) happens in the isolated tmux server created for each run.
- ASR daemon lives in a hidden `.asr` window and dies with the session.
- Toggle-off must paste immediately and then shut down ASR in the background.

-------------------------------------------------------------------------------

## 2) Repo Snapshot (paths you will touch)

- `listen_cli/main.py` — CLI (parses `__toggle__`, launches orchestration).
- `listen_cli/orchestration.py` — tmux session creation, isolated server, binds hotkeys, HUD.
- `listen_cli/asr.py` — daemon (UDS server, toggles ASR, HUD updates, paste).
- You will add:
  - `listen_cli/engines/base.py` (provider interface)
  - `listen_cli/engines/assemblyai.py` (refactor of current AAI path)
  - `listen_cli/engines/sherpa_onnx.py` (new local provider)
  - `listen_cli/audio.py` (microphone abstraction)
- Docs:
  - `plans.md` (this file)
  - `AGENTS.md` (agent quick‑guide)

High-level architecture
```
terminal → listen <app>
  └─ libtmux: create dedicated tmux server + session
       ├─ window 0: app pane (target of paste)
       ├─ window .asr: python -m listen_cli.asr (daemon)
       └─ status bar HUD (REC/idle + preview + clock)

hotkey (Alt‑t) → tmux run-shell -b "python -m listen_cli __toggle__ <session> <pane>"
  └─ main.py _toggle() → connect UDS /tmp/listen-<session>.sock → send TOGGLE
      └─ asr.py ASRDaemon handles:
           - start: engine.start(); HUD on
           - stop: text = engine.stop_quick(); paste; engine.shutdown() in background; HUD idle
```

-------------------------------------------------------------------------------

## 3) Prerequisites

- Python 3.10+
- Poetry
- tmux ≥ 3.2
- OS mic permissions:
  - macOS: System Settings → Privacy & Security → Microphone → enable your terminal.
  - Ensure Option/Alt sends Meta/ESC+ for your terminal if using `M‑t`.

Verify current baseline
- `poetry install`
- `LISTEN_DISABLE_ASR=1 poetry run listen nano`
- Press hotkey (default `M‑t`) — HUD flips REC/idle; no pane switch; no popups.

Verify current ASR (AssemblyAI)
- Ensure `ASSEMBLYAI_API_KEY` is set.
- `poetry run listen nano`
- Press hotkey → speak 2–3 seconds → press again → paste should be fast.
- If mic permissions are missing on macOS, grant them and retry.

-------------------------------------------------------------------------------

## 4) Dependencies

Edit `pyproject.toml` to ensure these deps:
- `libtmux >= 0.46`
- `sounddevice >= 0.4.6`
- `websockets >= 12.0`
- `assemblyai[extras] >= 0.43.1`
- `sherpa-onnx` (add)

Install
- `poetry add sherpa-onnx`

Sanity check
- `poetry run python -c "import sherpa_onnx, sounddevice, libtmux; print('ok')"`
- If this fails, resolve before continuing.

-------------------------------------------------------------------------------

## 5) Provider Abstraction

Create `listen_cli/engines/base.py`:
- Class `BaseEngine`:
  - `__init__(self, *, on_partial, on_final, on_error)` — callbacks receive strings.
  - `start(self) -> None` — non‑blocking; begin mic/decoder loop in background.
  - `stop_quick(self) -> str` — return best‑effort final text immediately (include last partial); do not block shutdown.
  - `shutdown(self) -> None` — close resources; may block; safe to call in background.
  - `is_listening(self) -> bool`.
Notes
- Engines must not touch tmux. They only invoke callbacks.
- Engines should throttle partial callbacks (e.g., ≥ 75 ms between updates) and truncate to ~60 chars.
- Engines should prewarm (load models/resources) at construction time or via an explicit `prewarm()` so the first toggle has no delay.

Reference scaffold (paste this into `listen_cli/engines/base.py` as a starting point)
```
from __future__ import annotations
from typing import Callable, Optional
import time


class BaseEngine:
    def __init__(self,
                 *,
                 on_partial: Callable[[str], None],
                 on_final: Callable[[str], None],
                 on_error: Callable[[str], None],
                 hud_throttle_ms: int = 75):
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_error = on_error
        self._last_hud_ts = 0.0
        self._hud_throttle = max(0, hud_throttle_ms) / 1000.0

    def start(self) -> None:  # non-blocking
        raise NotImplementedError

    def stop_quick(self) -> str:  # immediate best-effort text
        raise NotImplementedError

    def shutdown(self) -> None:  # may block
        raise NotImplementedError

    def is_listening(self) -> bool:
        raise NotImplementedError

    # helper for throttling HUD updates
    def _emit_partial(self, text: str) -> None:
        now = time.time()
        if now - self._last_hud_ts >= self._hud_throttle:
            self._last_hud_ts = now
            if len(text) > 60:
                text = text[:60] + '…'
            self.on_partial(text)
```

-------------------------------------------------------------------------------

## 6) Microphone Abstraction

Create `listen_cli/audio.py`:
- `class MicrophoneSource:`
  - `def __init__(self, sample_rate: int, chunk_ms: int = 100)`
  - Context manager `.open()` opens `sounddevice.InputStream(channels=1, dtype='float32', samplerate=sample_rate)`
  - `.read()` returns a 1‑D float32 numpy array of length `chunk_samples`.
Notes
- Engines pass the actual mic rate to their SDKs. sherpa‑onnx will resample internally to 16 kHz.

Reference scaffold (paste this into `listen_cli/audio.py`)
```
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
        self._stream = sd.InputStream(channels=1, dtype='float32', samplerate=self.sample_rate)
        self._stream.start()
        return self

    def close(self):
        if self._stream is not None:
            self._stream.stop(); self._stream.close(); self._stream = None

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def read(self) -> np.ndarray:
        assert self._stream is not None
        frames, _ = self._stream.read(self.chunk_samples)  # blocking read
        return frames.reshape(-1)
```

-------------------------------------------------------------------------------

## 7) Refactor AssemblyAI to Engine

File: `listen_cli/engines/assemblyai.py`
- Move logic from `SilentVoiceController` into `class AssemblyAIEngine(BaseEngine)`.
- Map AAI callbacks:
  - partial → `on_partial(text)`
  - final → internal buffer; also `on_partial(text)` for HUD continuity (optional)
- `stop_quick()` — merge buffered finals + last partial and return.
- `shutdown()` — close AAI transcriber.

Reference mapping (AssemblyAI → Engine methods)
- Current `SilentVoiceController.start()` → `AssemblyAIEngine.start()`
- Current AAI callbacks → inside engine:
  - on partial: `self._emit_partial(text)`
  - on final: buffer; optionally also call `_emit_partial(text)` to keep HUD fresh
- `stop_quick()` → merge `self._buffer + last_partial`
- `shutdown()` → `self._transcriber.close()`

Edge cases to preserve
- If no speech, return empty string from `stop_quick()` so daemon shows “no speech” behavior (current code clears preview and does not paste).

-------------------------------------------------------------------------------

## 8) Implement sherpa‑onnx Engine

File: `listen_cli/engines/sherpa_onnx.py`

Inputs (env vars)
- `LISTEN_SHERPA_TOKENS`, `LISTEN_SHERPA_ENCODER`, `LISTEN_SHERPA_DECODER`, `LISTEN_SHERPA_JOINER`
- Optional: `LISTEN_SHERPA_PROVIDER=cpu|cuda|coreml` (default cpu), `LISTEN_SHERPA_THREADS=1`, `LISTEN_SHERPA_DECODING=greedy_search`
- Endpoint rules: `LISTEN_SHERPA_RULE1=2.4`, `LISTEN_SHERPA_RULE2=1.2`, `LISTEN_SHERPA_RULE3=300`
- Mic: `LISTEN_SAMPLE_RATE` (default 48000), `LISTEN_CHUNK_MS=100`

Create recognizer (confirmed API)
- `import sherpa_onnx`
- `recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(tokens=…, encoder=…, decoder=…, joiner=…, num_threads=threads, sample_rate=16000, feature_dim=80, enable_endpoint_detection=True, rule1_min_trailing_silence=r1, rule2_min_trailing_silence=r2, rule3_min_utterance_length=r3, decoding_method=decoding, provider=provider)`

Start()
- `stream = recognizer.create_stream()`
- Spawn a background thread:
  - `with MicrophoneSource(mic_rate, chunk_ms).open() as mic:` loop:
    - `samples = mic.read()`
    - `stream.accept_waveform(mic_rate, samples)`
    - `while recognizer.is_ready(stream): recognizer.decode_stream(stream)`
    - `text = recognizer.get_result(stream)` → `on_partial(text)` (throttled)
    - `if recognizer.is_endpoint(stream):` then `final = recognizer.get_result_all(stream).text`; buffer and `on_final(final)`; `recognizer.reset(stream)`

stop_quick()
- Stop mic loop immediately; return buffered finals + last partial.

shutdown()
- Ensure the loop ends; dispose recognizer/stream (call `stream.input_finished()` only if needed during teardown).

Reference scaffold (paste this into `listen_cli/engines/sherpa_onnx.py`)
```
from __future__ import annotations
import os
import threading
import time
import numpy as np
from typing import Optional

import sherpa_onnx
from .base import BaseEngine
from ..audio import MicrophoneSource


class SherpaOnnxEngine(BaseEngine):
    def __init__(self, *, on_partial, on_final, on_error):
        super().__init__(on_partial=on_partial, on_final=on_final, on_error=on_error)
        # resolve env
        enc = os.getenv('LISTEN_SHERPA_ENCODER')
        dec = os.getenv('LISTEN_SHERPA_DECODER')
        joi = os.getenv('LISTEN_SHERPA_JOINER')
        tok = os.getenv('LISTEN_SHERPA_TOKENS')
        if not all([enc, dec, joi, tok]):
            raise RuntimeError('Missing sherpa-onnx model env vars')
        provider = os.getenv('LISTEN_SHERPA_PROVIDER', 'cpu')
        threads = int(os.getenv('LISTEN_SHERPA_THREADS', '1'))
        decoding = os.getenv('LISTEN_SHERPA_DECODING', 'greedy_search')
        r1 = float(os.getenv('LISTEN_SHERPA_RULE1', '2.4'))
        r2 = float(os.getenv('LISTEN_SHERPA_RULE2', '1.2'))
        r3 = float(os.getenv('LISTEN_SHERPA_RULE3', '300'))

        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=tok,
            encoder=enc,
            decoder=dec,
            joiner=joi,
            num_threads=threads,
            sample_rate=16000,
            feature_dim=80,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=r1,
            rule2_min_trailing_silence=r2,
            rule3_min_utterance_length=r3,
            decoding_method=decoding,
            provider=provider,
        )
        self.stream = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._buffer = []
        self._last_partial = ''
        self.mic_rate = int(os.getenv('LISTEN_SAMPLE_RATE', '48000'))
        self.chunk_ms = int(os.getenv('LISTEN_CHUNK_MS', '100'))

    def is_listening(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self.stream = self.recognizer.create_stream()
        self._buffer.clear(); self._last_partial = ''
        self._running = True

        def _loop():
            try:
                with MicrophoneSource(self.mic_rate, self.chunk_ms) as mic:
                    while self._running:
                        samples = mic.read()
                        self.stream.accept_waveform(self.mic_rate, samples)
                        while self.recognizer.is_ready(self.stream):
                            self.recognizer.decode_stream(self.stream)
                        # partial
                        txt = self.recognizer.get_result(self.stream)
                        if txt:
                            self._last_partial = txt
                            self._emit_partial(txt)
                        # endpoint
                        if self.recognizer.is_endpoint(self.stream):
                            full = self.recognizer.get_result_all(self.stream).text
                            if full:
                                self._buffer.append(full)
                                self._emit_partial(full)
                            self.recognizer.reset(self.stream)
            except Exception as e:
                self.on_error(str(e))
            finally:
                pass

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def _assemble_text(self) -> str:
        parts = []
        if self._buffer:
            parts.append(' '.join(self._buffer))
        if self._last_partial:
            parts.append(self._last_partial)
        return ' '.join(p for p in parts if p).strip()

    def stop_quick(self) -> str:
        if not self._running:
            return ''
        self._running = False
        return self._assemble_text()

    def shutdown(self) -> None:
        self._running = False
        # allow thread to exit and release mic/recognizer resources
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
```
-------------------------------------------------------------------------------

## 9) Engine Factory in Daemon

Modify `listen_cli/asr.py`:
- Add a provider factory:
  - If `LISTEN_ASR_PROVIDER` is set, choose explicitly.
  - Else, auto‑detect sherpa availability (all four model files exist) → choose `sherpa_onnx`, else `assemblyai`.
- Wire callbacks:
  - `on_partial`: set `@asr_preview` (truncate) and `refresh-client -S` (or rely on engine throttling to limit updates).
  - `on_final`: append to daemon’s buffer (used only if you want segment‑by‑segment accumulation during long captures); optional.
  - `on_error`: briefly surface text in `@asr_preview` (do not show popups); clear after a second.
 - Toggle behavior (already implemented):
  - On: `engine.start()`; `@asr_on=1`. Engine must already be prewarmed; starting should not incur model load.
  - Off: `@asr_on=0`, set `@asr_preview="Pasting…"`, get `text = engine.stop_quick()`, paste, then clear preview. Do NOT call `engine.shutdown()` on toggle‑off; keep the engine hot for subsequent toggles. Only call `shutdown()` when the session/daemon exits.

Prewarm policy (single knob)
- Default behavior: prewarm local engines (e.g., sherpa‑onnx) at daemon start; do not prewarm remote engines (AssemblyAI).
- Env control: `LISTEN_PREWARM=auto|always|never` (default `auto`).
  - `auto`: prewarm only local engines; remote engines connect on toggle.
  - `always`: prewarm all providers (including remote); audio capture still depends on whether the provider runs continuously (local) or push-to-talk (remote).
  - `never`: prewarm none (construct/connect on first toggle only).

Hot-mic override
- `BACKGROUND_ALWAYS_LISTEN=on|off` (unset uses provider defaults). Use `off` to keep local engines push-to-talk; `on` to force continuous loops in any background-capable engine.

Default provider selection
- If the four sherpa model files are available (`LISTEN_SHERPA_TOKENS/ENCODER/DECODER/JOINER` or a shipped model directory), default to `sherpa_onnx`.
- Otherwise, default to `assemblyai` if `ASSEMBLYAI_API_KEY` is present.
- Users can override with `LISTEN_ASR_PROVIDER`.

Control layer & toggle abstraction
- Keep toggling logic encapsulated so it can be invoked from multiple sources, not only tmux hotkeys.
- In the daemon, implement a `toggle(pane_id)` function that contains the exact start/stop sequence. The UDS handler for `TOGGLE <pane_id>` should delegate to this function.
- Future triggers (e.g., keyword detection, external clients) can call the same function or send `TOGGLE` over the socket without touching tmux.
- Do not implement keyword detection now; just ensure the toggle path is a pure callable that doesn’t depend on tmux.

Factory scaffold (edits in `listen_cli/asr.py`)
```
# near top of file
import importlib


def _detect_sherpa_paths() -> bool:
    import os
    req = [
        os.getenv('LISTEN_SHERPA_TOKENS'),
        os.getenv('LISTEN_SHERPA_ENCODER'),
        os.getenv('LISTEN_SHERPA_DECODER'),
        os.getenv('LISTEN_SHERPA_JOINER'),
    ]
    return all(req)


def make_engine(on_partial, on_final, on_error):
    provider = os.getenv('LISTEN_ASR_PROVIDER')
    if provider is None:
        provider = 'sherpa_onnx' if _detect_sherpa_paths() else 'assemblyai'
    if provider == 'sherpa_onnx':
        from .engines.sherpa_onnx import SherpaOnnxEngine
        return SherpaOnnxEngine(on_partial=on_partial, on_final=on_final, on_error=on_error)
    elif provider == 'assemblyai':
        from .engines.assemblyai import AssemblyAIEngine
        return AssemblyAIEngine(on_partial=on_partial, on_final=on_final, on_error=on_error)
    else:
        raise RuntimeError(f'Unknown LISTEN_ASR_PROVIDER={provider}')
```

Daemon callback wiring (conceptual)
```
def on_partial(text: str):
    tmux_set_var('@asr_preview', text)

def on_final(text: str):
    # Optional: can accumulate if desired; paste path uses stop_quick()
    pass

def on_error(msg: str):
    tmux_set_var('@asr_preview', f'Error: {msg}')
```

-------------------------------------------------------------------------------

## 10) HUD & Orchestration (verify)

File: `listen_cli/orchestration.py`
- Isolated tmux server per run: `Server(socket_name=session_name or LISTEN_TMUX_SOCKET)`
- Bind/unbind via `server.cmd()`; use `run-shell -b`; unbind stale keys before rebinding.
- HUD left: `REC/idle` + divider + `@asr_preview` (set `status-left-length` large enough).
- HUD right: optional message + white `%I:%M %p`.
- Hide window list by setting `window-status-format` and `window-status-current-format` to a single space and matching their styles to the bar background.

Explicit tmux options to keep (already present in `orchestration.py`, verify):
- `status on`, `status-position bottom`, `status-interval 1`
- `status-style bg=colour236,fg=colour250`
- `message-style bg=colour235,fg=colour222`
- `window-status-style bg=colour236,fg=colour236`
- `window-status-current-style bg=colour236,fg=colour236`
- `status-left-length 200`
- `window-status-format " "`, `window-status-current-format " "`
- `status-left '#{?@asr_on,#[fg=colour196] REC 🎙 ,#[fg=white] idle 🎙 } #[fg=colour240]| #[fg=colour252]#{@asr_preview} #[default]'`
- `status-right '#{?@asr_message,#[fg=colour240]#{@asr_message} #[fg=colour240]| ,} #[fg=white]%I:%M %p #[default]'`

-------------------------------------------------------------------------------

## 11) Configuration Matrix

Global
- `LISTEN_ASR_PROVIDER=assemblyai|sherpa_onnx`
- `LISTEN_SAMPLE_RATE` (mic) default 48000 (sounddevice default)
- `LISTEN_CHUNK_MS=100`
- `LISTEN_HUD_THROTTLE_MS=75`

AssemblyAI
- `ASSEMBLYAI_API_KEY`

sherpa‑onnx
- `LISTEN_SHERPA_TOKENS`
- `LISTEN_SHERPA_ENCODER`
- `LISTEN_SHERPA_DECODER`
- `LISTEN_SHERPA_JOINER`
- Optional: `LISTEN_SHERPA_PROVIDER`, `LISTEN_SHERPA_THREADS`, `LISTEN_SHERPA_DECODING`, endpoint rules

Tmux
- `LISTEN_TMUX_SOCKET` to name the dedicated server (helps debugging).

Defaults recommended for end users
- If you have sherpa models locally, export the four `LISTEN_SHERPA_*` paths to prefer local offline ASR.
- Otherwise set `ASSEMBLYAI_API_KEY` and use the AssemblyAI provider.

-------------------------------------------------------------------------------

## 12) Validation (Manual Smoke Tests)

Baseline HUD (no ASR)
- `LISTEN_DISABLE_ASR=1 LISTEN_TMUX_SOCKET=listen-test poetry run listen nano`
- Press `M‑t`; HUD flips REC/idle; no pane switch; check `/tmp/listen-hotkeys.log` if configured.

AssemblyAI
- `ASSEMBLYAI_API_KEY=… LISTEN_ASR_PROVIDER=assemblyai LISTEN_TMUX_SOCKET=listen-aai poetry run listen nano`
- Speak; see partials; toggle off → paste is fast; `.asr` exists.

sherpa‑onnx
- Set envs to point to model files; then:
- `LISTEN_ASR_PROVIDER=sherpa_onnx LISTEN_TMUX_SOCKET=listen-sherpa poetry run listen nano`
- Speak; partials appear; pause speaking triggers endpoint → finalizes silently; toggle off → paste is fast.

First‑word capture (no warm‑up cutoffs)
- Immediately after launching a new session (before any toggle), wait ~2–3 seconds and press the hotkey. The first partial should appear within ~100–200 ms, not seconds.
- If the first tokens are missing, verify prewarm is working: sherpa-onnx builds the recognizer and keeps the mic warm from daemon start; remote providers may still need `LISTEN_PREWARM=always` plus a toggle before audio flows.

Edge cases
- Toggle on/off quickly without speaking: HUD flips correctly, preview clears, and no paste occurs.
- Long utterance: partials scroll; endpoint breaks into segments; final paste on toggle-off includes last partial.
- Mic at 44.1k or 48k: sherpa handles resampling; no error.

Multiple sessions
- Run two shells each with a different `LISTEN_TMUX_SOCKET`; ensure independent operation.

-------------------------------------------------------------------------------

## 13) Troubleshooting

No hotkey effect
- Ensure terminal sends Meta (Option=Esc+ on macOS).
- `tmux -L <socket> list-keys -T root | rg run-shell` shows a single binding.
- If bindings linger, `tmux -L <socket> kill-server` and relaunch.

HUD static but paste works
- Reduce `LISTEN_HUD_THROTTLE_MS`.

sherpa model load errors
- Verify paths to tokens/encoder/decoder/joiner exist; try `provider=cpu`, `threads=1`.

Paste slow
- Ensure `stop_quick()` returns immediately and `shutdown()` happens in background.

ASR engine thread doesn’t stop
- Ensure the loop honors the stop signals: `_stop_event` for push-to-talk mode and `_shutdown_event`/`_listening` gates for hot-mic mode. `stop_quick()` should flip those flags and `shutdown()` must join the background thread.

HUD preview missing on left
- Confirm `status-left-length` is large; ensure `@asr_preview` is updated and throttling isn’t set too high.

First tokens missing after first toggle
- Confirm prewarm policy: local engines prewarm at daemon start and keep the mic loop hot automatically (unless `BACKGROUND_ALWAYS_LISTEN=off`); remote engines prewarm only if `LISTEN_PREWARM=always`.
- For sherpa‑onnx, ensure `OnlineRecognizer.from_transducer()` is called during engine construction, not at first toggle.

-------------------------------------------------------------------------------

## 14) Rollback & Guards

- `LISTEN_DISABLE_ASR=1` for HUD‑only debug.
- Force remote provider: `LISTEN_ASR_PROVIDER=assemblyai`.
- Reset server state: `tmux -L <socket> kill-server`.

-------------------------------------------------------------------------------

## 15) Exact Work Checklist (tick top‑down)

1. `poetry add sherpa-onnx`
2. Add files:
   - `listen_cli/engines/base.py` (interface)
   - `listen_cli/audio.py` (mic)
3. Refactor AAI into `listen_cli/engines/assemblyai.py` using the interface.
4. Implement `listen_cli/engines/sherpa_onnx.py` per §8 APIs.
5. Modify `listen_cli/asr.py` to select engine and wire callbacks; keep fast toggle‑off path.
6. Verify HUD styling in `listen_cli/orchestration.py` (left REC/idle + preview, right clock).
7. Manual smoke tests (§12) for HUD‑only, AAI, sherpa‑onnx.
8. Document envs in README; add a short “Getting Models” note for sherpa‑onnx.

Optional dev aids
- Add a `LISTEN_DEBUG=1` env that causes the daemon to log non-intrusively to a file (e.g., `/tmp/listen-daemon.log`). Do not print to stdout; avoid tmux popups.

-------------------------------------------------------------------------------

## 16) Notes for Future Agents

- Don’t add tmux popups; rely on HUD.
- Provider code must not touch tmux; only daemon sets user options.
- Keep bind/unbind logic at the server scope; always pass `-b` to `run-shell`.
- Keep `status-left-length` sufficient for preview; truncate preview to ~60 chars before setting.

Security & privacy
- Socket perms: keep 0600.
- No transcript persistence on disk beyond temp tmux buffer; avoid logging transcript text in debug logs unless explicitly enabled.

Performance tips
- For sherpa-onnx on CPU, prefer the int8 models if latency is high.
- Keep `num_threads=1` initially; tune up only if CPU allows.

End of plan.

End of plan.
