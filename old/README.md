# Voice PTT Wrapper for Claude Code / Codex (macOS & Linux POC)

A single‑file Python terminal wrapper that:
- **Passes through** to a real CLI (Claude Code, Codex, or any command).
- Adds **push‑to‑talk** voice capture with **streaming transcription**.
- Shows **live captions** in‑terminal.
- Supports **trigger words**: say “send it” to inject your text into the CLI; say “cancel” to clear and send SIGINT to the child.
- Uses an **STT adapter** so you can start fast with an API and swap providers or go local later.

> This POC targets **macOS/Linux**. Windows would need ConPTY + different keyboard handling and is **not** included.

---

## 1) Files to create

**⚠️ IMPORTANT: This project uses Poetry for dependency management. DO NOT use pip or manual venv! ⚠️**

Create **two** files in an empty folder:

### `voice_wrap.py`
```python
#!/usr/bin/env python3
"""
Voice PTT wrapper for any CLI (Claude Code / Codex examples shown).

Features
- PTY passthrough with in-terminal live captions (Rich).
- Push-to-talk hotkey (default Ctrl-G), streaming STT (AssemblyAI or Fake).
- "send it" to submit buffered text, "cancel" to clear and SIGINT the child.

Tested targets: macOS 13+/14+, Ubuntu 22.04/24.04.
Python: 3.10+.

Env vars (optional):
  STT_PROVIDER=assemblyai|fake
  ASSEMBLYAI_API_KEY=sk_...
  ASSEMBLYAI_WS_URL=wss://api.assemblyai.com/v2/realtime/ws?sample_rate=16000
  VOICE_SEND_WORDS="send it,send,ship it,go"
  VOICE_CANCEL_WORDS="cancel,abort,stop,nevermind"
  VOICE_FUZZ_THRESH=88           # 0..100 (rapidfuzz partial_ratio)
  VOICE_HOTKEY="^G"              # e.g. "^G", "ctrl-g", "0x07"
  VOICE_INPUT_DEVICE=2           # sounddevice device index or name
  VOICE_SAMPLE_RATE=16000
  VOICE_BLOCK_MS=20
"""

import os, sys, asyncio, signal, tty, termios, pty, fcntl, select, json, base64, struct
from typing import AsyncIterator, Optional, Dict, Any, Callable
from dataclasses import dataclass
from rapidfuzz import fuzz
import numpy as np
import sounddevice as sd
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

# =========================
# Config / Keywords / Utils
# =========================

console = Console()

def _parse_hotkey_env(default="^G") -> bytes:
    s = os.getenv("VOICE_HOTKEY", default).strip()
    try:
        if s.lower().startswith("0x"):
            return bytes([int(s, 16) & 0xFF])
        if s.startswith("^") and len(s) == 2:
            return bytes([ord(s[1].upper()) & 0x1F])
        if s.lower().startswith("ctrl-") and len(s) == 6:
            return bytes([ord(s[-1].upper()) & 0x1F])
    except Exception:
        pass
    # fallback: BEL (^G)
    return b"\x07"

CTRL_HOTKEY = _parse_hotkey_env()

SEND_WORDS = [s.strip().lower() for s in os.getenv("VOICE_SEND_WORDS", "send it,send,ship it,go").split(",") if s.strip()]
CANCEL_WORDS = [s.strip().lower() for s in os.getenv("VOICE_CANCEL_WORDS", "cancel,abort,stop,nevermind").split(",") if s.strip()]
FUZZ_THRESH = int(os.getenv("VOICE_FUZZ_THRESH", "88"))

def fuzzy_match_any(text: str, words, thresh=FUZZ_THRESH) -> bool:
    t = text.lower().strip()
    return any(fuzz.partial_ratio(t, w) >= thresh for w in words)

# ===================
# STT Adapter (Base)
# ===================

@dataclass
class STTEvent:
    text: str
    is_final: bool
    latency_ms: Optional[int] = None

class STTAdapter:
    async def stream(self, pcm_chunks: AsyncIterator[bytes], *, metadata: Dict[str, Any]) -> AsyncIterator[STTEvent]:
        raise NotImplementedError

# ==========================================
# AssemblyAI Streaming WebSocket (Simple POC)
# ==========================================

class AssemblyAIStreaming(STTAdapter):
    def __init__(self, api_key: str, url: Optional[str] = None):
        import websockets  # lazy import
        self._websockets = websockets
        self.api_key = api_key
        self.url = url or "wss://api.assemblyai.com/v2/realtime/ws?sample_rate=16000"

    async def stream(self, pcm_chunks: AsyncIterator[bytes], *, metadata: Dict[str, Any]) -> AsyncIterator[STTEvent]:
        ws = await self._websockets.connect(self.url, extra_headers={"Authorization": self.api_key})
        try:
            async def _keepalive():
                try:
                    while True:
                        await ws.ping()
                        await asyncio.sleep(10)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

            async def send_loop():
                try:
                    async for chunk in pcm_chunks:
                        b64 = base64.b64encode(chunk).decode()
                        await ws.send(json.dumps({"audio_data": b64}))
                    try:
                        await ws.send(json.dumps({"terminate_session": True}))
                    except Exception:
                        pass
                except asyncio.CancelledError:
                    # we were asked to stop; that's fine
                    pass
                except Exception:
                    # swallow send errors; recv loop will exit too
                    pass

            ping_task = asyncio.create_task(_keepalive())
            send_task = asyncio.create_task(send_loop())

            try:
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    text = data.get("text", "") or ""
                    mtype = data.get("message_type")
                    is_final = (mtype == "FinalTranscript")
                    yield STTEvent(text=text, is_final=is_final)
            except self._websockets.exceptions.ConnectionClosed:
                pass
            except asyncio.CancelledError:
                pass
            finally:
                ping_task.cancel()
                send_task.cancel()
                await asyncio.gather(ping_task, send_task, return_exceptions=True)
                try:
                    await ws.close()
                except Exception:
                    pass
        finally:
            # if ws.close() above raised, ensure close anyway
            try:
                await ws.close()
            except Exception:
                pass

# =========================
# Fake STT (for dry tests)
# =========================

class FakeStreaming(STTAdapter):
    """
    Use STT_PROVIDER=fake and optionally provide FAKE_STT_SCRIPT with lines like:
      partial: hello
      final: hello world
      sleep: 300
      final: send it
    """
    def __init__(self):
        script = os.getenv("FAKE_STT_SCRIPT", "final: hello world\nfinal: send it")
        self.events = []
        for line in script.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("partial:"):
                self.events.append(("partial", line.split("partial:",1)[1].strip()))
            elif line.startswith("final:"):
                self.events.append(("final", line.split("final:",1)[1].strip()))
            elif line.startswith("sleep:"):
                self.events.append(("sleep", int(line.split("sleep:",1)[1].strip())))
            else:
                self.events.append(("final", line))

    async def stream(self, pcm_chunks: AsyncIterator[bytes], *, metadata: Dict[str, Any]) -> AsyncIterator[STTEvent]:
        # Consume pcm_chunks so the producer isn't blocked (discarded)
        async def _drain():
            try:
                async for _ in pcm_chunks:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
        drain_task = asyncio.create_task(_drain())

        try:
            for kind, val in self.events:
                if kind == "sleep":
                    await asyncio.sleep(val/1000.0)
                elif kind == "partial":
                    yield STTEvent(text=val, is_final=False)
                elif kind == "final":
                    yield STTEvent(text=val, is_final=True)
                await asyncio.sleep(0.05)
        finally:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)

def build_stt() -> STTAdapter:
    provider = os.getenv("STT_PROVIDER", "assemblyai").lower()
    if provider == "assemblyai":
        key = os.getenv("ASSEMBLYAI_API_KEY")
        if not key:
            console.print("[red]ASSEMBLYAI_API_KEY not set[/red]")
            sys.exit(1)
        url = os.getenv("ASSEMBLYAI_WS_URL")
        return AssemblyAIStreaming(api_key=key, url=url)
    if provider == "fake":
        return FakeStreaming()
    console.print(f"[red]Unsupported STT_PROVIDER: {provider}[/red]")
    sys.exit(1)

# ===========================
# Microphone → PCM16 chunks
# ===========================

class MicStream:
    """Capture mono PCM16 frames and yield small chunks suitable for streaming."""
    def __init__(self, samplerate: int, block_ms: int, device: Optional[object] = None):
        self.sr = int(samplerate)
        self.block_samples = max(1, int(self.sr * (block_ms/1000.0)))
        self.device = device
        self.q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self.stream = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def _callback(self, indata, frames, time, status):
        # PortAudio thread → hand off to asyncio loop thread-safely
        if indata is None:
            return
        if indata.ndim == 2 and indata.shape[1] > 1:
            mono = np.mean(indata, axis=1)
        else:
            mono = indata.reshape(-1)
        pcm16 = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        if self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(self.q.put_nowait, pcm16)
            except Exception:
                pass

    async def __aenter__(self):
        self.loop = asyncio.get_running_loop()
        self.stream = sd.InputStream(
            samplerate=self.sr,
            channels=1,
            dtype='float32',
            blocksize=self.block_samples,
            callback=self._callback,
            device=self.device
        )
        self.stream.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.stream:
            self.stream.stop()
            self.stream.close()
        # drain queue to unblock any awaiters
        try:
            while not self.q.empty():
                self.q.get_nowait()
        except Exception:
            pass

    async def chunks(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.q.get()
            yield chunk

# ===============================
# PTY child (Claude/Codex/etc.)
# ===============================

class PTYChild:
    def __init__(self, argv):
        self.argv = argv
        self.master_fd = None
        self.slave_fd = None
        self.child_pid = None

    def spawn(self):
        self.master_fd, self.slave_fd = pty.openpty()
        pid = os.fork()
        if pid == 0:
            # --- Child ---
            try:
                os.setsid()
                # Child must NOT keep master open
                try:
                    os.close(self.master_fd)
                except Exception:
                    pass
                # Attach stdio to slave
                os.dup2(self.slave_fd, 0)
                os.dup2(self.slave_fd, 1)
                os.dup2(self.slave_fd, 2)
                try:
                    os.close(self.slave_fd)
                except Exception:
                    pass
                # Acquire controlling terminal (line discipline, signals)
                if hasattr(termios, 'TIOCSCTTY'):
                    try:
                        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                    except Exception:
                        pass
                os.execvp(self.argv[0], self.argv)
            except FileNotFoundError:
                print(f"{self.argv[0]}: command not found", file=sys.stderr)
            except Exception as e:
                print(f"child exec failed: {e}", file=sys.stderr)
            os._exit(127)
        else:
            # --- Parent ---
            self.child_pid = pid
            try:
                os.close(self.slave_fd)
            except Exception:
                pass
            flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def send(self, data: bytes):
        if self.master_fd is not None and data:
            try:
                os.write(self.master_fd, data)
            except Exception:
                pass

    def interrupt(self):
        if self.child_pid:
            try:
                os.killpg(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            except PermissionError:
                try:
                    os.kill(self.child_pid, signal.SIGINT)
                except Exception:
                    pass

    def kill(self):
        if self.child_pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.child_pid, sig)
                    break
                except Exception:
                    try:
                        os.kill(self.child_pid, sig)
                        break
                    except Exception:
                        pass

    def wait(self):
        if self.child_pid:
            try:
                os.waitpid(self.child_pid, 0)
            except ChildProcessError:
                pass

# ==============
# PTY utilities
# ==============

def sync_winsize(pty_master_fd: int):
    try:
        s = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00'*8)
        rows, cols, _, _ = struct.unpack('HHHH', s)
        if rows == 0 or cols == 0:
            return
        fcntl.ioctl(pty_master_fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    except Exception:
        pass

# ==============================
# Live UI status (bottom panel)
# ==============================

class StatusUI:
    def __init__(self):
        self.listening = False
        self.partial = ""
        self.buffer = ""
        self.note = ""
        self.live: Optional[Live] = None

    def render(self):
        txt = Text()
        txt.append(f"[{'LISTENING' if self.listening else 'idle'}] ", style="bold green" if self.listening else "bold grey37")
        if self.partial:
            txt.append(self.partial, style="yellow")
        elif self.buffer:
            txt.append(self.buffer, style="cyan")
        if self.note:
            txt.append(f"\n{self.note}", style="magenta")
        return Panel(txt, title="Voice Control", border_style="blue")

    def set_partial(self, t: str):
        self.partial = t

    def push_final(self, t: str):
        t = t.strip()
        if not t:
            return
        if self.buffer:
            self.buffer += " " + t
        else:
            self.buffer = t
        self.partial = ""

    def clear(self):
        self.partial = ""
        self.buffer = ""

# ======================
# Orchestrator / Main
# ======================

async def stt_loop(ui: StatusUI, stt: STTAdapter, submit_cb: Callable[[str], None], interrupt_cb: Callable[[], None],
                   sr: int, block_ms: int, device: Optional[object]):
    ui.note = "Listening… say 'send it' to submit, 'cancel' to abort."
    if ui.live:
        ui.live.update(ui.render())

    try:
        async with MicStream(samplerate=sr, block_ms=block_ms, device=device) as mic:
            async for ev in stt.stream(mic.chunks(), metadata={"sr": sr}):
                seg = (ev.text or "").strip()
                # Update caption line
                ui.set_partial(seg)
                # Only act on short final segments to avoid accidental sends
                if ev.is_final and seg:
                    sgl = seg.lower()
                    # Check triggers BEFORE buffering, so we don't include them in the submission
                    if len(sgl.split()) <= 3 and fuzzy_match_any(sgl, SEND_WORDS):
                        to_send = ui.buffer.strip()
                        ui.note = "Submitting…"
                        if ui.live: ui.live.update(ui.render())
                        if to_send:
                            submit_cb(to_send + "\n")
                        ui.clear()
                        ui.note = "Submitted."
                    elif len(sgl.split()) <= 3 and fuzzy_match_any(sgl, CANCEL_WORDS):
                        ui.note = "Canceled. (SIGINT sent to child)"
                        if ui.live: ui.live.update(ui.render())
                        interrupt_cb()
                        ui.clear()
                    else:
                        ui.push_final(seg)
                if ui.live:
                    ui.live.update(ui.render())
    except asyncio.CancelledError:
        # graceful stop
        pass
    finally:
        ui.note = "Stopped listening."
        if ui.live:
            ui.live.update(ui.render())

async def pass_through_loop(child: PTYChild, ui: StatusUI):
    stdin_fd = sys.stdin.fileno()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        console.print("[red]This wrapper must be run in a TTY/terminal.[/red]")
        return

    old_attrs = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)

    listening_task: Optional[asyncio.Task] = None
    stt = build_stt()

    # Audio config
    sr = int(os.getenv("VOICE_SAMPLE_RATE", "16000"))
    block_ms = int(os.getenv("VOICE_BLOCK_MS", "20"))
    dev_env = os.getenv("VOICE_INPUT_DEVICE", "").strip()
    device: Optional[object] = None
    if dev_env:
        try:
            device = int(dev_env)
        except ValueError:
            device = dev_env  # name

    def submit_cb(text: str):
        try:
            child.send(text.encode())
        except Exception:
            pass

    def interrupt_cb():
        child.interrupt()

    try:
        with Live(ui.render(), console=console, refresh_per_second=12, screen=False) as live:
            ui.live = live
            while True:
                r, _, _ = select.select([stdin_fd, child.master_fd], [], [], 0.03)

                # child -> stdout
                if child.master_fd in r:
                    try:
                        data = os.read(child.master_fd, 4096)
                        if not data:
                            break
                        sys.stdout.buffer.write(data)
                        sys.stdout.flush()
                        live.refresh()
                    except BlockingIOError:
                        pass
                    except OSError:
                        break

                # stdin -> child (unless hotkey)
                if stdin_fd in r:
                    data = os.read(stdin_fd, 1024)
                    if not data:
                        break
                    if CTRL_HOTKEY in data:
                        data = data.replace(CTRL_HOTKEY, b"")
                        if listening_task and not listening_task.done():
                            listening_task.cancel()
                            try:
                                await listening_task
                            except Exception:
                                pass
                            listening_task = None
                            ui.listening = False
                            ui.note = "Stopped listening."
                            live.update(ui.render())
                        else:
                            ui.listening = True
                            ui.note = "Starting mic…"
                            live.update(ui.render())
                            listening_task = asyncio.create_task(
                                stt_loop(ui, stt, submit_cb, interrupt_cb, sr=sr, block_ms=block_ms, device=device)
                            )
                    if data:
                        child.send(data)

                if ui.live:
                    ui.live.update(ui.render())

    finally:
        if listening_task and not listening_task.done():
            listening_task.cancel()
            try:
                await listening_task
            except Exception:
                pass
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)

async def main():
    if os.name != "posix":
        print("This POC currently targets POSIX (Linux/macOS).", file=sys.stderr)
        sys.exit(1)

    if "--" not in sys.argv:
        console.print("[bold]Usage:[/bold] python voice_wrap.py -- <command> [args...]")
        console.print("Example: python voice_wrap.py -- claude code --stdin")
        sys.exit(1)

    dashdash = sys.argv.index("--")
    argv = sys.argv[dashdash+1:]
    if not argv:
        console.print("[red]No command given after --[/red]")
        sys.exit(1)

    ui = StatusUI()
    child = PTYChild(argv)
    child.spawn()

    # sync initial PTY size and keep it synced on terminal resize
    sync_winsize(child.master_fd)
    def _on_winch(signum, frame):
        try:
            sync_winsize(child.master_fd)
        except Exception:
            pass
    signal.signal(signal.SIGWINCH, _on_winch)

    try:
        await pass_through_loop(child, ui)
    except KeyboardInterrupt:
        pass
    finally:
        child.kill()
        child.wait()
        console.print("\n[grey62]voice_wrap: exit[/grey62]")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError:
        # For environments where there's an existing loop policy
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
```

### `pyproject.toml`
```toml
[tool.poetry]
name = "voice-ptt-wrapper"
version = "0.1.0"
description = "Voice PTT wrapper for Claude Code / Codex"
authors = ["Your Name <you@example.com>"]

[tool.poetry.dependencies]
python = "^3.10"
websockets = ">=12.0"
sounddevice = ">=0.4.6"
numpy = ">=1.26.0"
rapidfuzz = ">=3.6.1"
rich = ">=13.7.0"

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"
```

---

## 2) System prerequisites

* **Python** 3.10 or newer
* **Poetry** for dependency management (install from https://python-poetry.org/docs/#installation)
* **PortAudio** for microphone access:

  * macOS: `brew install portaudio`
  * Ubuntu/Debian: `sudo apt update && sudo apt install -y libportaudio2 portaudio19-dev`
  * Arch/Manjaro: `sudo pacman -S --noconfirm portaudio`

Grant mic permission on macOS if prompted (System Settings → Privacy & Security → Microphone).

**⚠️ DO NOT use pip install or python -m venv - Poetry manages the virtual environment automatically! ⚠️**

---

## 3) Install

**⚠️ DO NOT USE pip OR manual venv - Use Poetry only! ⚠️**

```bash
poetry install
```

To run commands in the Poetry environment:
```bash
poetry shell  # Activate the virtual environment
# OR
poetry run python voice_wrap.py -- <command>  # Run directly with Poetry
```

---

## 4) Configure STT (fastest path: AssemblyAI)

```bash
export STT_PROVIDER=assemblyai
export ASSEMBLYAI_API_KEY=sk_...           # your key
# optional:
# export ASSEMBLYAI_WS_URL="wss://api.assemblyai.com/v2/realtime/ws?sample_rate=16000"
```

> Tune triggers & behavior (optional):

```bash
export VOICE_SEND_WORDS="send it,send,ship it,go"
export VOICE_CANCEL_WORDS="cancel,abort,stop,nevermind"
export VOICE_FUZZ_THRESH=88
export VOICE_HOTKEY="^G"        # e.g. "^G", "ctrl-g", or "0x07"
export VOICE_INPUT_DEVICE=2     # device index or name; run a quick Python snippet to list (see Troubleshooting)
export VOICE_SAMPLE_RATE=16000
export VOICE_BLOCK_MS=20
```

---

## 5) Run (wrapping your real CLI)

Claude Code example:

```bash
poetry run python voice_wrap.py -- claude code --stdin
```

Codex example:

```bash
poetry run python voice_wrap.py -- codex --stdin
```

Any other CLI works too. The wrapper behaves like a normal terminal for that command and adds voice control.

**Controls**

* Press **Ctrl‑G** (or your `VOICE_HOTKEY`) to start/stop listening.
* Say **“send it”** to submit the current transcript to the child CLI.
* Say **“cancel”** to clear the buffer and send **SIGINT** to the child.
* Everything you type still passes through to the child unchanged.

---

## 6) Dry tests (no API, no mic)

Validate behavior with the **fake** STT:

```bash
export STT_PROVIDER=fake
export FAKE_STT_SCRIPT=$'final: hello world\nsleep: 300\nfinal: send it'
poetry run python voice_wrap.py -- /bin/cat
```

What you should see:

* The status panel shows the “hello world” caption appear.
* After ~300 ms, the fake STT emits “send it”.
* The wrapper injects “hello world\n” into `/bin/cat`.
* `/bin/cat` echoes `hello world` to the screen.

**Cancel test**

```bash
export STT_PROVIDER=fake
export FAKE_STT_SCRIPT=$'final: cancel'
poetry run python voice_wrap.py -- yes
```

* The `yes` command spams output.
* The fake STT emits “cancel”.
* The wrapper sends SIGINT; `yes` stops.

---

## 7) Live mic tests (real STT)

1. Start your real CLI:

```bash
export STT_PROVIDER=assemblyai
export ASSEMBLYAI_API_KEY=sk_...
poetry run python voice_wrap.py -- claude code --stdin
```

2. Press **Ctrl‑G** to start listening.  
3. Speak a sentence, then say **“send it”** as its own short phrase.  
4. The sentence should be injected into the child (observe its prompt/response).  
5. To test interruption, speak **“cancel”** while a long‑running child action is happening.

**Notes**

* Keep your **trigger words** short and isolated. The wrapper only fires on final segments up to **3 words** to reduce accidental sends.
* If your terminal UI flickers, that’s the small status panel refreshing. You can disable it by replacing the `with Live(...)` block in `pass_through_loop` with a no‑op and updating the 2–3 `.update()` calls accordingly.

---

## 8) Troubleshooting

* **Mic device not found**  
  Install PortAudio, then try again. On Linux, ensure your user has access to audio devices. To list devices:

  ```python
  import sounddevice as sd; print(sd.query_devices())
  ```
  Then set a device explicitly: `export VOICE_INPUT_DEVICE=2` (or a device **name**).

* **No captions**  
  Check your API key and network. For AssemblyAI, confirm `ASSEMBLYAI_API_KEY` is exported in the same shell.

* **Child ignores SIGINT**  
  The wrapper escalates to `SIGTERM`/`SIGKILL` on exit. During a session, `cancel` sends SIGINT to the child’s **process group** (TTY‑style). Some tools ignore SIGINT; try a different command or handle termination in the child.

* **Weird child layout**  
  The wrapper syncs terminal size on start and on window resize (SIGWINCH). If your terminal doesn’t propagate size, try a different emulator.

* **Hotkey doesn’t toggle**  
  Some terminals intercept certain control keys. Set a different hotkey via `VOICE_HOTKEY` (e.g., `^T` or `0x14`).

---

## 9) Design notes

* **Adapter boundary**: `build_stt()` returns an `STTAdapter`. Add more providers by implementing `.stream(...)` and switching via `STT_PROVIDER`.
* **Latency**: mic frames are ~20 ms at 16 kHz mono PCM16. Most streaming STT vendors handle this well.
* **Safety**: triggers act only on short **final** segments and are **not** added to the buffer. Tune with `VOICE_FUZZ_THRESH` and word lists.
* **PTY**: The child is a session leader with the PTY slave as its controlling TTY. This yields correct behavior for Ctrl‑C, job control, etc.

---

## 10) What’s in scope for this POC

* macOS/Linux only.
* PTY pass‑through, live captions, send/cancel triggers.
* AssemblyAI adapter + Fake adapter for tests.

Future ideas:
* Deepgram/OpenAI/Azure streaming adapters.
* Separate wake word vs. triggers.
* Windows support via ConPTY (`pywinpty`) and `msvcrt` key handling.

---

## 11) Quick A→Z script (Copy‑paste friendly)

**⚠️ DO NOT USE pip OR venv - Poetry only! ⚠️**

```bash
# Create folder
mkdir -p voice-ptt && cd voice-ptt

# Create files
cat > voice_wrap.py <<'PY'
# (paste the entire voice_wrap.py content from this README section)
PY

cat > pyproject.toml <<'TOML'
[tool.poetry]
name = "voice-ptt-wrapper"
version = "0.1.0"
description = "Voice PTT wrapper for Claude Code / Codex"
authors = ["Your Name <you@example.com>"]

[tool.poetry.dependencies]
python = "^3.10"
websockets = ">=12.0"
sounddevice = ">=0.4.6"
numpy = ">=1.26.0"
rapidfuzz = ">=3.6.1"
rich = ">=13.7.0"

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"
TOML

# System deps
# macOS: brew install portaudio
# Debian/Ubuntu: sudo apt update && sudo apt install -y libportaudio2 portaudio19-dev
# Arch/Manjaro: sudo pacman -S --noconfirm portaudio

# Install Poetry if not already installed:
# curl -sSL https://install.python-poetry.org | python3 -

# Install dependencies with Poetry (NO pip, NO venv!)
poetry install

# Dry test (no mic, no API)
export STT_PROVIDER=fake
export FAKE_STT_SCRIPT=$'final: hello world\nsleep: 300\nfinal: send it'
poetry run python voice_wrap.py -- /bin/cat

# Live test (AssemblyAI)
export STT_PROVIDER=assemblyai
export ASSEMBLYAI_API_KEY=sk_...   # set your key
poetry run python voice_wrap.py -- claude code --stdin
# Press Ctrl-G (or your VOICE_HOTKEY), speak, then say "send it" to submit to the child.
```

