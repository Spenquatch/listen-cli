#!/usr/bin/env python3
"""
Enhanced voice typing with smooth diff-based updates and word-boundary awareness.
"""

import os
import sys
import asyncio
import signal
import tty
import termios
import pty
import fcntl
import select
import struct
import threading
import time

import assemblyai as aai

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
    return b"\x07"

CTRL_HOTKEY = _parse_hotkey_env()

# ===============================
# PTY Child Process
# ===============================

class PTYChild:
    def __init__(self, argv):
        self.argv = argv
        self.master_fd = None
        self.child_pid = None

    def spawn(self):
        self.master_fd, slave_fd = pty.openpty()
        pid = os.fork()

        if pid == 0:
            # Child
            try:
                os.setsid()
                os.close(self.master_fd)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                os.close(slave_fd)
                if hasattr(termios, 'TIOCSCTTY'):
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                os.execvp(self.argv[0], self.argv)
            except Exception as e:
                print(f"Child error: {e}", file=sys.stderr)
                os._exit(127)
        else:
            # Parent
            self.child_pid = pid
            os.close(slave_fd)
            flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def send(self, data: bytes):
        if self.master_fd and data:
            try:
                os.write(self.master_fd, data)
            except Exception:
                pass

    def kill(self):
        if self.child_pid:
            try:
                os.killpg(self.child_pid, signal.SIGTERM)
            except:
                try:
                    os.kill(self.child_pid, signal.SIGTERM)
                except:
                    pass

# ===============================
# Enhanced Smooth Voice Controller
# ===============================

class SmoothVoiceController:
    def __init__(self, child: PTYChild):
        self.child = child
        self.transcriber = None
        self.is_listening = False
        self.current_typed_text = ""
        self.use_word_boundaries = os.getenv("VOICE_WORD_BOUNDARY", "true").lower() == "true"
        self.use_paste_mode = os.getenv("VOICE_PASTE_MODE", "false").lower() == "true"

        # Word buffering
        self.pending_buffer = ""
        self.buffer_timer = None
        self.buffer_timeout = 0.12  # 120ms

        # Configure AssemblyAI
        api_key = os.getenv("ASSEMBLYAI_API_KEY", "f5115c8df6de446999a096a3edee97cb")
        aai.settings.api_key = api_key

    def on_open(self, session_opened: aai.RealtimeSessionOpened):
        print(f"\n🎤 Voice session started (smooth mode enabled)")

    def find_word_boundary(self, text: str, pos: int) -> int:
        """Find the start of the word at position pos."""
        if not self.use_word_boundaries:
            return pos

        # Snap back to word boundary
        while pos > 0 and text[pos-1] not in ' \t\n.,;:!?()[]{}':
            pos -= 1
        return pos

    def should_use_paste(self, old_text: str, new_text: str) -> bool:
        """Determine if paste mode would be better for this update."""
        if not self.use_paste_mode:
            return False

        # Use paste if more than 50% changed or text is long
        if not old_text:
            return len(new_text) > 20

        common_len = 0
        for a, b in zip(old_text, new_text):
            if a == b:
                common_len += 1

        change_ratio = 1 - (common_len / max(len(old_text), len(new_text)))
        return change_ratio > 0.5 or len(new_text) > 50

    def _smart_update(self, new_text: str, is_final: bool = False):
        """Apply minimal changes using enhanced diff algorithm."""
        if new_text == self.current_typed_text:
            return

        old_text = self.current_typed_text

        # Check if paste mode would be better
        if self.should_use_paste(old_text, new_text):
            self._paste_update(new_text)
            return

        # Find common prefix (unchanged beginning)
        prefix_len = 0
        min_len = min(len(old_text), len(new_text))
        while prefix_len < min_len and old_text[prefix_len] == new_text[prefix_len]:
            prefix_len += 1

        # Optionally snap to word boundary for cleaner updates
        if self.use_word_boundaries and not is_final:
            prefix_len = self.find_word_boundary(new_text, prefix_len)

        # Calculate what needs to change
        chars_to_delete = len(old_text) - prefix_len
        chars_to_add = new_text[prefix_len:]

        # Apply minimal changes
        if chars_to_delete > 0:
            # Use efficient batch backspace
            self.child.send(b'\x08' * chars_to_delete)

        if chars_to_add:
            # Type the new part
            self.child.send(chars_to_add.encode())

        # If new text is shorter, clear leftover characters
        if len(new_text) < len(old_text):
            leftover = len(old_text) - len(new_text)
            # Overwrite with spaces then backspace
            self.child.send(b' ' * leftover + b'\x08' * leftover)

        self.current_typed_text = new_text

    def _paste_update(self, new_text: str):
        """Use bracketed paste mode for large updates."""
        # Clear current text
        if self.current_typed_text:
            clear_len = len(self.current_typed_text)
            self.child.send(b'\x08' * clear_len)

        # Use bracketed paste
        paste_data = b'\x1b[200~' + new_text.encode() + b'\x1b[201~'
        self.child.send(paste_data)

        self.current_typed_text = new_text

    def _flush_buffer(self):
        """Flush pending buffer to display."""
        if self.pending_buffer:
            self._smart_update(self.pending_buffer)
            self.pending_buffer = ""
        self.buffer_timer = None

    def on_data(self, transcript: aai.RealtimeTranscript):
        if isinstance(transcript, aai.RealtimePartialTranscript):
            # Buffer partial updates briefly to reduce churn
            new_text = transcript.text.strip()

            # Cancel previous timer
            if self.buffer_timer:
                self.buffer_timer.cancel()

            # Store in buffer
            self.pending_buffer = new_text

            # Set timer to flush after timeout
            self.buffer_timer = threading.Timer(self.buffer_timeout, self._flush_buffer)
            self.buffer_timer.start()

        elif isinstance(transcript, aai.RealtimeFinalTranscript):
            # Cancel any pending buffer timer
            if self.buffer_timer:
                self.buffer_timer.cancel()
                self.buffer_timer = None

            # Apply final transcript immediately
            final_text = transcript.text.strip()
            self._smart_update(final_text, is_final=True)

    def on_error(self, error: aai.RealtimeError):
        print(f"\n❌ Voice error: {error}")

    def on_close(self):
        print(f"\n🔚 Voice session ended")

    def start_listening(self):
        if self.is_listening:
            return

        self.current_typed_text = ""
        self.pending_buffer = ""
        self.is_listening = True

        print(f"\n🎤 Starting smooth voice input...")

        try:
            self.transcriber = aai.RealtimeTranscriber(
                sample_rate=16000,
                on_data=self.on_data,
                on_error=self.on_error,
                on_open=self.on_open,
                on_close=self.on_close,
                disable_partial_transcripts=False
            )

            self.transcriber.connect()

            # Start streaming
            def stream_audio():
                try:
                    self.transcriber.stream(aai.extras.MicrophoneStream(sample_rate=16000))
                except Exception as e:
                    print(f"\n❌ Streaming error: {e}")
                    self.is_listening = False

            self.stream_thread = threading.Thread(target=stream_audio, daemon=True)
            self.stream_thread.start()

        except Exception as e:
            print(f"\n❌ Error starting voice: {e}")
            self.is_listening = False

    def stop_listening(self):
        if not self.is_listening:
            return

        # Flush any pending buffer
        if self.buffer_timer:
            self.buffer_timer.cancel()
            self._flush_buffer()

        print(f"\n✅ Voice input complete")
        self.is_listening = False

        if self.transcriber:
            try:
                self.transcriber.close()
            except Exception:
                pass
            self.transcriber = None

# ===============================
# Main Application
# ===============================

def sync_winsize(pty_master_fd: int):
    try:
        s = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00'*8)
        rows, cols, _, _ = struct.unpack('HHHH', s)
        if rows > 0 and cols > 0:
            fcntl.ioctl(pty_master_fd, termios.TIOCSWINSZ,
                       struct.pack('HHHH', rows, cols, 0, 0))
    except:
        pass

async def main_loop(child: PTYChild, voice: SmoothVoiceController):
    stdin_fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        print("Must run in a TTY")
        return

    old_attrs = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)

    print("🎙️  Listen-CLI (Smooth Edition): Ctrl+G to toggle voice")
    print("    Word boundaries: " + ("ON" if voice.use_word_boundaries else "OFF"))
    print("    Paste mode: " + ("ON" if voice.use_paste_mode else "OFF"))
    print()

    try:
        while True:
            r, _, _ = select.select([stdin_fd, child.master_fd], [], [], 0.1)

            # Child output -> stdout
            if child.master_fd in r:
                try:
                    data = os.read(child.master_fd, 4096)
                    if not data:
                        break

                    sys.stdout.buffer.write(data)
                    sys.stdout.flush()

                except (BlockingIOError, OSError):
                    break

            # User input
            if stdin_fd in r:
                data = os.read(stdin_fd, 1024)
                if not data:
                    break

                # Check for voice hotkey
                if CTRL_HOTKEY in data:
                    data = data.replace(CTRL_HOTKEY, b"")

                    if voice.is_listening:
                        voice.stop_listening()
                    else:
                        voice.start_listening()

                # Send other keystrokes to child
                if data:
                    child.send(data)

    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
    finally:
        if voice.is_listening:
            voice.stop_listening()
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)

async def main():
    if len(sys.argv) < 2:
        print("Usage: python listen-cli-smooth.py <command> [args...]")
        print("Examples:")
        print("  python listen-cli-smooth.py claude")
        print("  python listen-cli-smooth.py codex")
        print("\nEnvironment variables:")
        print("  VOICE_WORD_BOUNDARY=true  # Snap to word boundaries")
        print("  VOICE_PASTE_MODE=true     # Use paste for large changes")
        sys.exit(1)

    child_args = sys.argv[1:]
    child = PTYChild(child_args)
    child.spawn()

    voice = SmoothVoiceController(child)

    # Handle window resize
    sync_winsize(child.master_fd)
    signal.signal(signal.SIGWINCH, lambda s, f: sync_winsize(child.master_fd))

    try:
        await main_loop(child, voice)
    finally:
        child.kill()

if __name__ == "__main__":
    asyncio.run(main())