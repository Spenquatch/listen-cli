# Curses Terminal UI Implementation

## Overview

Successfully implemented a curses-based terminal UI with footer and infinite scroll for listen-cli. The implementation follows all expert recommendations for thread safety, performance, and proper terminal emulation.

## Files Created

### `listen-cli-curses.py`
Basic curses UI without voice (Phase 1)
- PTY child process management
- pyte terminal emulation
- Curses pad with infinite scroll
- SIGWINCH handler for terminal resize
- Keyboard passthrough with escape sequences
- Fixed footer with status
- Dirty line optimization

### `listen-cli-curses-voice.py`
Complete implementation with voice integration
- All features from basic version
- Thread-safe voice integration using queue + pipe
- AssemblyAI real-time transcription
- Ctrl+G to toggle voice
- Bracketed paste for transcript insertion
- Status updates in footer

## Architecture

```
PTY Child Process
       ↓ (raw bytes)
pyte ByteStream
       ↓ (parses ANSI)
pyte Screen (terminal state)
       ↓ (render)
Curses Pad (scrollable)
       ↓
Terminal Display
```

Voice integration uses separate thread with queue:
```
Voice Thread → Queue → Main Loop → PTY
                ↓
            Pipe (wake signal)
```

## Key Design Decisions

1. **Thread Safety**: All curses operations in main thread only. Voice uses queue + pipe for communication.

2. **Terminal Emulation**: pyte handles ANSI escape codes, maintaining proper terminal state.

3. **Performance**: Only dirty lines are rendered (tracked by pyte.Screen.dirty).

4. **Colors**: Basic 16 ANSI colors supported (256-color deferred).

5. **Scrollback**: Using pyte.HistoryScreen(1000 lines) instead of giant pad.

6. **Resize Handling**: SIGWINCH handler properly resizes PTY, pyte screen, and curses pad.

## Usage

### Basic UI (no voice)
```bash
poetry run python listen-cli-curses.py [command]
# Examples:
poetry run python listen-cli-curses.py /bin/bash
poetry run python listen-cli-curses.py claude
```

### With Voice Input
```bash
poetry run python listen-cli-curses-voice.py [command]
# Default command is 'claude'
poetry run python listen-cli-curses-voice.py
```

## Controls

- **Ctrl+G**: Toggle voice recording
- **q**: Quit
- **↑/↓**: Scroll line by line
- **PageUp/PageDown**: Scroll by page
- All other keys pass through to child process

## Implementation Highlights

### Thread-Safe Voice Integration
```python
# Voice thread puts events in queue
self.ui_queue.put(('paste', transcript))
self.ui_queue.put(('status', 'Voice: Listening...'))

# Main loop processes queue
if voice_pipe_r in readable:
    while not self.voice_queue.empty():
        event_type, data = self.voice_queue.get()
        # Handle events...
```

### Dirty Line Optimization
```python
# Only render changed lines
dirty_lines = self.screen.dirty.copy()
self.screen.dirty.clear()
for y in dirty_lines:
    # Render line...
```

### SIGWINCH Handler
```python
def handle_resize(self):
    curses.resizeterm(rows, cols)
    self.child.resize(rows, cols)
    self.screen.resize(rows - 1, cols)
    # Recreate pad and redraw
```

## Testing Status

✅ PTY child process spawning
✅ Terminal emulation with pyte
✅ Curses rendering with colors
✅ Keyboard input passthrough
✅ Scrolling (arrows, PageUp/Down)
✅ Terminal resize handling
✅ Voice recording and transcription
✅ Thread-safe UI updates
✅ Bracketed paste mode

## Expert Recommendations Implemented

All critical issues identified by the expert analysis were addressed:

1. ✅ Thread safety (queue + pipe mechanism)
2. ✅ SIGWINCH handler (terminal resize)
3. ✅ Performance optimization (dirty line tracking)
4. ✅ Color mapping (16 basic colors)
5. ✅ Keyboard passthrough (with escape sequences)
6. ✅ Voice integration (thread-safe)

## Dependencies

```toml
[tool.poetry.dependencies]
python = "^3.10"
pyte = "^0.8.1"
assemblyai = {extras = ["extras"], version = "^0.43.1"}
```

## Next Steps

The implementation is complete and production-ready. Possible enhancements:

1. Add 256-color support (requires LRU cache for color pairs)
2. Implement search functionality
3. Add session recording/playback
4. Support for multiple tabs/windows