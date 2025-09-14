# Listen-CLI Project Handoff

## Project Overview
Voice-enabled wrapper for Claude Code that allows users to speak their prompts instead of typing. Uses AssemblyAI for real-time speech-to-text transcription.

**Goal**: `listen claude [args]` command that launches Claude normally but adds voice input via Ctrl+G hotkey.

---

## Current Status: ✅ WORKING with Real-time Typing

### Working Files (Priority Order)
1. **`/Users/spensermcconnell/__Active_Code/listen-cli/listen-cli-typing.py`** - CURRENT BEST VERSION
   - Real-time typing simulation into Claude's input box
   - Words appear character-by-character as transcribed
   - Uses AssemblyAI v3 RealtimeTranscriber

2. **`/Users/spensermcconnell/__Active_Code/listen-cli/voice_final.py`** - Working voice transcription (standalone)
   - Proven AssemblyAI v3 integration
   - Basic toggle functionality
   - Good reference for voice logic

3. **`/Users/spensermcconnell/__Active_Code/listen-cli/README.md`** - Updated for Poetry

### Legacy/Debug Files (Can Ignore)
- `voice_wrap.py` - Original broken v2 API version
- `voice_wrap_v3.py`, `voice_wrap_debug.py`, `voice_wrap_simple.py` - Debug attempts
- `listen-cli.py`, `listen-cli-simple.py`, `listen-cli-invisible.py` - UI iteration attempts
- All `test_*.py` and `debug_*.py` files - Debugging scripts

---

## Technical Solutions Implemented

### 1. AssemblyAI Integration ✅
- **API**: AssemblyAI v3 streaming API (not v2!)
- **SDK**: `assemblyai[extras]` package via Poetry
- **Key Fix**: Uses `RealtimeTranscriber` with `additional_headers` (not `extra_headers`)
- **Auth**: API key `f5115c8df6de446999a096a3edee97cb`

### 2. Dependencies ✅
```bash
poetry add "assemblyai[extras]"  # Includes PyAudio for microphone
```

### 3. System Requirements ✅
- macOS with PortAudio: `brew install portaudio`
- Microphone permissions granted to Terminal
- Python 3.10+ with Poetry

---

## Current Implementation: Real-time Typing

### How It Works
1. **Launch**: `poetry run python listen-cli-typing.py claude code --stdin`
2. **Start Voice**: Press Ctrl+G → begins transcription
3. **Real-time Display**: Words appear in Claude's input box as spoken
4. **Stop Voice**: Press Ctrl+G → stops transcription, text stays in input
5. **Send**: Press Enter → sends to Claude

### Technical Approach
- **PTY Management**: Spawns Claude as child process
- **Input Routing**: Normal keys → Claude, Ctrl+G → voice toggle
- **Typing Simulation**:
  - Tracks `current_typed_text` in Claude's input
  - On partial transcripts: backspace old text, type new text
  - Character-by-character injection with 50ms delay
  - Diff-based updates to handle transcript changes

---

## Issues Identified & Solutions

### ✅ SOLVED
1. **AssemblyAI v2 → v3 Migration**: Fixed endpoint and API usage
2. **WebSocket Headers**: Fixed `extra_headers` → `additional_headers` for websockets 15.x
3. **Audio Chunk Size**: Fixed 20ms → 100ms chunks for v3 API requirements
4. **Transcript Deduplication**: Implemented partial vs final handling
5. **Poetry Setup**: Updated README.md with proper Poetry instructions

### ⚠️ CURRENT ISSUE: Real-time Typing Logic
**Problem**: The typing simulation logic needs evaluation and refinement.

**Symptoms**: Words are appearing in Claude's input but the logic for handling:
- Partial transcript updates
- Backspace/correction timing
- Character injection speed
- Final vs partial differentiation

**Current Logic Location**: `listen-cli-typing.py:104-130` in `on_data()` method

---

## Next Steps for New Session

### 1. Evaluate Real-time Typing Logic
**Focus Areas**:
- **Backspace Logic**: `clear_input()` method - ensure correct number of backspaces
- **Typing Speed**: Currently 50ms delay per character - test if too fast/slow
- **Partial vs Final Handling**: Review when to update vs when to commit
- **Error Handling**: What happens if transcripts come in faster than typing?

### 2. Test Edge Cases
- Rapid speech changes
- Long phrases
- Transcription corrections
- Network latency effects

### 3. Optimization Opportunities
- Debounce rapid partial updates
- Smarter diff algorithms
- Better error recovery
- Performance tuning

---

## Environment Setup Commands

```bash
# Navigate to project
cd /Users/spensermcconnell/__Active_Code/listen-cli

# Install dependencies (Poetry required)
poetry install

# Test basic voice functionality
poetry run python voice_final.py /bin/cat

# Test current real-time typing version
poetry run python listen-cli-typing.py claude code --stdin
```

---

## Key Technical Details

### AssemblyAI Configuration
```python
api_key = "f5115c8df6de446999a096a3edee97cb"  # Working key
transcriber = aai.RealtimeTranscriber(
    sample_rate=16000,
    on_data=self.on_data,
    disable_partial_transcripts=False  # Need partials for real-time effect
)
```

### PTY Child Management
- Spawns Claude as child process with proper TTY setup
- Handles stdin/stdout routing
- Window resize support
- Clean shutdown on Ctrl+C

### Voice Control Flow
1. `start_listening()` → Creates RealtimeTranscriber, starts background thread
2. `on_data()` → Handles partial/final transcripts, injects characters
3. `stop_listening()` → Closes transcriber, text remains in Claude's input

---

## Critical Files to Examine

### Primary Working File
- **`listen-cli-typing.py`** - Current implementation with real-time typing

### Key Methods to Review
- `TypingVoiceController.on_data()` - Core typing logic
- `PTYChild.clear_input()` - Backspace implementation
- `PTYChild.type_text()` - Character injection

### Configuration
- **Environment**: `ASSEMBLYAI_API_KEY`, `VOICE_HOTKEY` (default ^G)
- **Dependencies**: Poetry with `assemblyai[extras]`
- **System**: macOS with PortAudio, mic permissions

---

## Success Criteria
- [x] Voice transcription working (AssemblyAI v3)
- [x] Real-time words appearing in Claude's input
- [ ] Smooth, natural typing simulation
- [ ] Proper handling of transcript corrections
- [ ] No UI conflicts with Claude
- [ ] Ready for production use

---

## Current State
**STATUS**: Real-time typing is functional but needs logic refinement. The foundation is solid - AssemblyAI integration works, PTY management works, character injection works. The focus should be on optimizing the typing simulation logic for a natural user experience.

**NEXT PRIORITY**: Evaluate and improve the `on_data()` method logic in `listen-cli-typing.py` for smoother real-time typing behavior.