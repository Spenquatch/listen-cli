# listen-cli

listen-cli wraps any terminal program in an isolated tmux server and adds a global microphone toggle that pastes transcripts with bracketed paste safety. A hidden `.asr` window hosts the Python daemon and status HUD so the target TUI keeps focus.

## Quick Start

```bash
poetry install
poetry run listen nano  # wraps `nano` with the listen HUD and hotkey (default Alt-t)
```

Press the hotkey once to start recording (HUD flips to `REC 🎙`). Press again to paste the transcript into the active pane. All pastes flow through `tmux load-buffer` + `paste-buffer -p` to keep bracketed paste intact.

## Choosing an ASR provider

The daemon loads one provider at launch and keeps engines hot across toggles. Selection is automatic but can be overridden with `LISTEN_ASR_PROVIDER=assemblyai|sherpa_onnx`.

### Local sherpa-onnx (default when models are present)

Download the EN-20M streaming Zipformer bundle and point the environment variables at the four model files:

```bash
export LISTEN_SHERPA_TOKENS=/path/to/tokens.txt
export LISTEN_SHERPA_ENCODER=/path/to/encoder-epoch-99-avg-1.onnx
export LISTEN_SHERPA_DECODER=/path/to/decoder-epoch-99-avg-1.onnx
export LISTEN_SHERPA_JOINER=/path/to/joiner-epoch-99-avg-1.onnx
```

If you keep the bundle in the repo at `sherpa/models/zipformer-en20m/`, the daemon will discover it automatically—no env vars needed. Optional knobs:

- `LISTEN_SHERPA_PROVIDER=cpu|cuda|coreml` (default `cpu`)
- `LISTEN_SHERPA_THREADS` (defaults to `1`)
- `LISTEN_SHERPA_DECODING` (`greedy_search`, `modified_beam_search`, …)
- Endpoint tuning via `LISTEN_SHERPA_RULE1`, `RULE2`, `RULE3`

### AssemblyAI realtime

Set your API key and either let auto-detection fall back to it or force it explicitly:

```bash
export ASSEMBLYAI_API_KEY=sk-...
export LISTEN_ASR_PROVIDER=assemblyai  # optional; auto if sherpa models missing
```

AssemblyAI keeps a websocket warm between toggles so the second press pastes instantly.

### Prewarm policy

Control when engines prewarm with `LISTEN_PREWARM` (`auto` default):

- `auto`: prewarm only local engines (sherpa-onnx)
- `always`: prewarm all providers (including remote)
- `never`: lazy-load on the first toggle

## Smoke tests

Run the HUD without ASR:

```bash
LISTEN_DISABLE_ASR=1 LISTEN_TMUX_SOCKET=listen-test poetry run listen nano
```

Try each provider:

```bash
ASSEMBLYAI_API_KEY=... LISTEN_ASR_PROVIDER=assemblyai poetry run listen nano
LISTEN_ASR_PROVIDER=sherpa_onnx LISTEN_SHERPA_TOKENS=... poetry run listen nano
```

Use different `LISTEN_TMUX_SOCKET` values to confirm isolated servers when running multiple sessions.

## Architecture & Session Cleanup

**CRITICAL:** The tmux session cleanup mechanism is event-driven and relies on command wrapping. DO NOT change this without understanding the implications.

### How Session Cleanup Works

When you run `poetry run listen nano`, the orchestration system:

1. **Creates an isolated tmux server** using a custom socket (`-L socket_name`)
2. **Wraps the main app command** in a shell script that ensures cleanup:
   ```sh
   sh -c 'nano; tmux -L socket kill-session -t session'
   ```
3. **Starts the ASR daemon** in a hidden `.asr` window within the same session
4. **Attaches to the session** and blocks until the session ends

When nano (or any app) exits:
- The shell wrapper immediately runs `tmux kill-session`
- This kills the entire session including the ASR daemon window
- The `attach_session()` call returns and triggers final cleanup
- The custom tmux server is also terminated

### Why This Design

- **Event-driven:** No polling, immediate cleanup when main app exits
- **Reliable:** Uses tmux's built-in command chaining, not unreliable hooks
- **Isolated:** Custom tmux server prevents interference with user's tmux
- **Compatible:** ASR daemon runs in tmux context for status bar and paste operations

### Previous Failed Approaches

- ❌ `pane-died` hooks: Unreliable for natural process exits
- ❌ Subprocess ASR daemon: Breaks tmux integration (status bar, paste)
- ❌ Polling monitors: Resource waste and unnecessary complexity

**If cleanup stops working:** Check that the command wrapping in `orchestration.py` is intact. The main app must be wrapped with the kill-session command.
