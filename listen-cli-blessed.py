#!/usr/bin/env python3
"""
Listen-CLI with blessed - Proper terminal windowing with isolated PTY.
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
import re
import time
from collections import deque

import assemblyai as aai
from blessed import Terminal

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
    def __init__(self, argv, height, width):
        self.argv = argv
        self.master_fd = None
        self.child_pid = None
        self.height = height
        self.width = width

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

                # Set terminal size for child
                os.environ['LINES'] = str(self.height)
                os.environ['COLUMNS'] = str(self.width)

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

            # Set window size
            self.set_winsize(self.height, self.width)

    def set_winsize(self, rows, cols):
        if self.master_fd:
            try:
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ,
                           struct.pack('HHHH', rows, cols, 0, 0))
            except:
                pass

    def send(self, data: bytes):
        if self.master_fd and data:
            try:
                os.write(self.master_fd, data)
            except Exception:
                pass

    def read(self):
        if self.master_fd:
            try:
                return os.read(self.master_fd, 4096)
            except (BlockingIOError, OSError):
                return None
        return None

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
# Blessed Terminal Window Manager
# ===============================

class TerminalWindow:
    def __init__(self, term):
        self.term = term
        self.content_buffer = deque(maxlen=1000)  # Store lines of content
        self.header_height = 2
        self.footer_height = 2
        self.content_start = self.header_height
        self.content_height = self.term.height - self.header_height - self.footer_height
        self.scroll_offset = 0
        self.status = "Ready"
        self.recording = False
        self.pasting = False  # Flag to prevent updates during paste

    def draw_header(self):
        """Draw header at top of terminal."""
        if self.pasting:
            return  # Skip during paste

        with self.term.location(0, 0):
            print(self.term.black_on_white + self.term.center(" 🎙️ Listen-CLI - Voice Input for Claude "))
        with self.term.location(0, 1):
            print(self.term.dim + "─" * self.term.width + self.term.normal)

    def draw_footer(self):
        """Draw footer at bottom of terminal."""
        if self.pasting:
            return  # Skip during paste

        with self.term.location(0, self.term.height - 2):
            print(self.term.dim + "─" * self.term.width + self.term.normal)

        status_left = " [Ctrl+G: Toggle Recording]"
        if self.recording:
            status_right = f"[🔴 Recording] "
        else:
            status_right = f"[{self.status}] "

        padding = self.term.width - len(status_left) - len(status_right)

        with self.term.location(0, self.term.height - 1):
            print(self.term.black_on_white + status_left + " " * max(0, padding) + status_right + self.term.normal)

    def update_status(self, status, recording=None):
        """Update status without interfering with content."""
        if self.pasting:
            return  # Skip during paste

        self.status = status
        if recording is not None:
            self.recording = recording
        self.draw_footer()

    def add_content(self, data: bytes):
        """Add PTY output to content buffer."""
        if self.pasting:
            # During paste, write directly without buffering
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
            return

        # Parse and buffer the content
        text = data.decode('utf-8', errors='replace')
        lines = text.split('\n')

        for line in lines:
            if line:
                self.content_buffer.append(line)

        self.render_content()

    def render_content(self):
        """Render content window from buffer."""
        if self.pasting:
            return  # Skip during paste

        # Clear content area
        for y in range(self.content_start, self.content_start + self.content_height):
            with self.term.location(0, y):
                print(self.term.clear_eol, end='')

        # Render visible lines
        visible_lines = list(self.content_buffer)[-self.content_height:]
        for i, line in enumerate(visible_lines):
            with self.term.location(0, self.content_start + i):
                print(line[:self.term.width], end='')

    def clear_display(self):
        """Clear the entire display."""
        print(self.term.clear)
        self.draw_header()
        self.draw_footer()

# ===============================
# Buffer Voice Controller
# ===============================

class BufferVoiceController:
    def __init__(self, child: PTYChild, window: TerminalWindow):
        self.child = child
        self.window = window
        self.transcriber = None
        self.is_listening = False
        self.transcript_buffer = []
        self.use_bracketed_paste = True

        # Configure AssemblyAI
        api_key = os.getenv("ASSEMBLYAI_API_KEY", "f5115c8df6de446999a096a3edee97cb")
        aai.settings.api_key = api_key

    def on_open(self, session_opened: aai.RealtimeSessionOpened):
        self.window.update_status("Recording...", recording=True)

    def on_data(self, transcript: aai.RealtimeTranscript):
        if isinstance(transcript, aai.RealtimeFinalTranscript):
            text = transcript.text.strip()
            if text:
                self.transcript_buffer.append(text + " ")
                word_count = sum(len(t.split()) for t in self.transcript_buffer)
                self.window.update_status(f"Recording... ({word_count} words)", recording=True)

    def on_error(self, error: aai.RealtimeError):
        self.window.update_status(f"Error: {error}", recording=False)

    def on_close(self):
        pass

    def start_listening(self):
        if self.is_listening:
            return

        self.transcript_buffer = []
        self.is_listening = True
        self.window.update_status("Starting...", recording=True)

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

            def stream_audio():
                try:
                    self.transcriber.stream(aai.extras.MicrophoneStream(sample_rate=16000))
                except Exception:
                    self.window.update_status("Stream error", recording=False)
                    self.is_listening = False

            self.stream_thread = threading.Thread(target=stream_audio, daemon=True)
            self.stream_thread.start()

        except Exception:
            self.window.update_status("Failed to start", recording=False)
            self.is_listening = False

    def stop_listening(self):
        if not self.is_listening:
            return

        self.is_listening = False
        self.window.update_status("Processing...", recording=False)

        if self.transcriber:
            try:
                self.transcriber.close()
            except Exception:
                pass
            self.transcriber = None

        self._paste_buffer()
        self.window.update_status("Ready", recording=False)

    def _paste_buffer(self):
        """Paste the accumulated transcript buffer."""
        if not self.transcript_buffer:
            return

        full_text = "".join(self.transcript_buffer).strip()
        if not full_text:
            return

        # Replace newlines with spaces
        full_text = full_text.replace('\n', ' ').replace('\r', ' ')
        full_text = re.sub(r'\s+', ' ', full_text).strip()

        # Set pasting flag to prevent UI updates
        self.window.pasting = True

        try:
            # Small delay to ensure terminal is ready
            time.sleep(0.1)

            # Try bracketed paste
            if self.use_bracketed_paste:
                try:
                    self.child.send(b'\x1b[200~')
                    self.child.send(full_text.encode('utf-8'))
                    self.child.send(b'\x1b[201~')
                except Exception:
                    self.child.send(full_text.encode('utf-8'))
            else:
                self.child.send(full_text.encode('utf-8'))

            # Allow paste to complete
            time.sleep(0.1)

        finally:
            # Clear pasting flag
            self.window.pasting = False

        self.transcript_buffer = []

# ===============================
# Main Application
# ===============================

async def main():
    if len(sys.argv) < 2:
        print("Usage: python listen-cli-blessed.py <command> [args...]")
        print("Example: python listen-cli-blessed.py claude")
        sys.exit(1)

    term = Terminal()

    # Calculate content area dimensions
    content_height = term.height - 4  # Header (2) + Footer (2)
    content_width = term.width

    # Create PTY child
    child_args = sys.argv[1:]
    child = PTYChild(child_args, content_height, content_width)
    child.spawn()

    # Create window manager
    window = TerminalWindow(term)

    # Create voice controller
    voice = BufferVoiceController(child, window)

    # Save terminal state
    stdin_fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(stdin_fd)

    try:
        # Enter fullscreen mode
        print(term.enter_fullscreen)
        print(term.hide_cursor)

        # Set up display
        window.clear_display()

        # Set scrolling region for content area only
        # This constrains Claude's output to the middle section
        print(f'\033[{window.content_start + 1};{term.height - window.footer_height}r')
        print(f'\033[{window.content_start + 1};1H')  # Position cursor in content area
        sys.stdout.flush()

        # Enter raw mode
        tty.setraw(stdin_fd)

        # Main event loop
        while True:
            # Check for input
            r, _, _ = select.select([stdin_fd, child.master_fd], [], [], 0.1)

            # Handle PTY output
            if child.master_fd in r:
                data = child.read()
                if not data:
                    break

                # Write PTY output (constrained by scrolling region)
                sys.stdout.buffer.write(data)
                sys.stdout.flush()

            # Handle user input
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
        pass
    finally:
        # Cleanup
        if voice.is_listening:
            voice.stop_listening()

        child.kill()

        # Restore terminal
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        print('\033[r')  # Reset scrolling region
        print(term.exit_fullscreen)
        print(term.show_cursor)
        print(term.clear)

if __name__ == "__main__":
    asyncio.run(main())