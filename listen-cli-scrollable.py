#!/usr/bin/env python3
"""
Listen-CLI with scrollable PTY buffer - maintains all output in memory.
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
import pyte

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
# Virtual Terminal Screen
# ===============================

class VirtualScreen:
    """Virtual terminal that captures PTY output and allows scrolling."""

    def __init__(self, height, width):
        self.height = height
        self.width = width
        # Use pyte to create a virtual terminal that interprets ANSI codes
        self.screen = pyte.Screen(width, height)
        self.stream = pyte.ByteStream(self.screen)
        self.history = deque(maxlen=1000)  # Store scrollback
        self.scroll_offset = 0

    def feed(self, data: bytes):
        """Feed PTY output to virtual terminal."""
        self.stream.feed(data)

        # Save lines that scroll off the top
        if self.screen.cursor.y >= self.height - 1:
            # A line is about to scroll off
            top_line = self.screen.display[0]
            if top_line.strip():  # Only save non-empty lines
                self.history.append(top_line)

    def get_display_lines(self, with_scroll=False):
        """Get lines to display, accounting for scroll offset."""
        if with_scroll and self.scroll_offset > 0:
            # Show history when scrolled up
            history_lines = list(self.history)
            all_lines = history_lines + self.screen.display

            start = max(0, len(history_lines) - self.scroll_offset)
            end = start + self.height

            return all_lines[start:end]
        else:
            # Normal view - just show current screen
            return self.screen.display

    def scroll_up(self, lines=1):
        """Scroll view up (into history)."""
        max_scroll = len(self.history)
        self.scroll_offset = min(self.scroll_offset + lines, max_scroll)

    def scroll_down(self, lines=1):
        """Scroll view down (toward current output)."""
        self.scroll_offset = max(0, self.scroll_offset - lines)

    def reset_scroll(self):
        """Reset to bottom (current output)."""
        self.scroll_offset = 0

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
# Terminal UI Manager
# ===============================

class TerminalUI:
    def __init__(self, term):
        self.term = term
        self.header_height = 2
        self.footer_height = 2
        self.content_height = term.height - self.header_height - self.footer_height
        self.status = "Ready"
        self.recording = False
        self.pasting = False

    def draw_header(self):
        """Draw header at top."""
        with self.term.location(0, 0):
            print(self.term.black_on_white + self.term.center(" 🎙️ Listen-CLI - Voice Input for Claude "))
        with self.term.location(0, 1):
            print(self.term.dim + "─" * self.term.width + self.term.normal)

    def draw_footer(self):
        """Draw footer at bottom."""
        with self.term.location(0, self.term.height - 2):
            print(self.term.dim + "─" * self.term.width + self.term.normal)

        status_left = " [Ctrl+G: Toggle] [↑↓: Scroll]"
        if self.recording:
            status_right = f"[🔴 Recording] "
        else:
            status_right = f"[{self.status}] "

        padding = self.term.width - len(status_left) - len(status_right)

        with self.term.location(0, self.term.height - 1):
            print(self.term.black_on_white + status_left + " " * max(0, padding) + status_right + self.term.normal)

    def draw_content(self, lines):
        """Draw content area with provided lines."""
        # Clear content area first
        for y in range(self.header_height, self.term.height - self.footer_height):
            with self.term.location(0, y):
                print(self.term.clear_eol, end='')

        # Draw visible lines
        for i, line in enumerate(lines[:self.content_height]):
            with self.term.location(0, self.header_height + i):
                # Truncate line to terminal width
                print(line[:self.term.width], end='')

    def update_status(self, status, recording=None):
        """Update status in footer."""
        if self.pasting:
            return

        self.status = status
        if recording is not None:
            self.recording = recording
        self.draw_footer()

    def full_redraw(self, screen_lines):
        """Redraw entire UI."""
        if not self.pasting:
            self.draw_header()
            self.draw_content(screen_lines)
            self.draw_footer()

# ===============================
# Buffer Voice Controller
# ===============================

class BufferVoiceController:
    def __init__(self, child: PTYChild, ui: TerminalUI):
        self.child = child
        self.ui = ui
        self.transcriber = None
        self.is_listening = False
        self.transcript_buffer = []
        self.use_bracketed_paste = True

        # Configure AssemblyAI
        api_key = os.getenv("ASSEMBLYAI_API_KEY", "f5115c8df6de446999a096a3edee97cb")
        aai.settings.api_key = api_key

    def on_open(self, session_opened: aai.RealtimeSessionOpened):
        self.ui.update_status("Recording...", recording=True)

    def on_data(self, transcript: aai.RealtimeTranscript):
        if isinstance(transcript, aai.RealtimeFinalTranscript):
            text = transcript.text.strip()
            if text:
                self.transcript_buffer.append(text + " ")
                word_count = sum(len(t.split()) for t in self.transcript_buffer)
                self.ui.update_status(f"Recording... ({word_count} words)", recording=True)

    def on_error(self, error: aai.RealtimeError):
        self.ui.update_status(f"Error: {error}", recording=False)

    def on_close(self):
        pass

    def start_listening(self):
        if self.is_listening:
            return

        self.transcript_buffer = []
        self.is_listening = True
        self.ui.update_status("Starting...", recording=True)

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
                    self.ui.update_status("Stream error", recording=False)
                    self.is_listening = False

            self.stream_thread = threading.Thread(target=stream_audio, daemon=True)
            self.stream_thread.start()

        except Exception:
            self.ui.update_status("Failed to start", recording=False)
            self.is_listening = False

    def stop_listening(self):
        if not self.is_listening:
            return

        self.is_listening = False
        self.ui.update_status("Processing...", recording=False)

        if self.transcriber:
            try:
                self.transcriber.close()
            except Exception:
                pass
            self.transcriber = None

        self._paste_buffer()
        self.ui.update_status("Ready", recording=False)

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

        # Set pasting flag
        self.ui.pasting = True

        try:
            time.sleep(0.1)

            if self.use_bracketed_paste:
                try:
                    self.child.send(b'\x1b[200~')
                    self.child.send(full_text.encode('utf-8'))
                    self.child.send(b'\x1b[201~')
                except Exception:
                    self.child.send(full_text.encode('utf-8'))
            else:
                self.child.send(full_text.encode('utf-8'))

            time.sleep(0.1)

        finally:
            self.ui.pasting = False

        self.transcript_buffer = []

# ===============================
# Main Application
# ===============================

async def main():
    if len(sys.argv) < 2:
        print("Usage: python listen-cli-scrollable.py <command> [args...]")
        print("Example: python listen-cli-scrollable.py claude")
        sys.exit(1)

    term = Terminal()

    # Create UI
    ui = TerminalUI(term)

    # Create virtual screen for PTY output
    vscreen = VirtualScreen(ui.content_height, term.width)

    # Create PTY child with FULL terminal dimensions
    # The child doesn't know about our header/footer, give it full terminal size
    child_args = sys.argv[1:]
    child = PTYChild(child_args, term.height, term.width)
    child.spawn()

    # Create voice controller
    voice = BufferVoiceController(child, ui)

    # Save terminal state
    stdin_fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(stdin_fd)

    try:
        # Enter fullscreen and setup
        print(term.enter_fullscreen)
        print(term.hide_cursor)

        # Initial draw
        ui.full_redraw(vscreen.get_display_lines())

        # Enter raw mode
        tty.setraw(stdin_fd)

        # Main event loop
        last_redraw = time.time()
        while True:
            r, _, _ = select.select([stdin_fd, child.master_fd], [], [], 0.1)

            # Handle PTY output
            if child.master_fd in r:
                data = child.read()
                if not data:
                    break

                # Feed to virtual screen
                vscreen.feed(data)

                # Redraw content area (throttled)
                if time.time() - last_redraw > 0.05:  # Max 20 FPS
                    ui.draw_content(vscreen.get_display_lines(with_scroll=True))
                    last_redraw = time.time()

            # Handle user input
            if stdin_fd in r:
                data = os.read(stdin_fd, 1024)
                if not data:
                    break

                # Check for special keys
                if b'\x1b[A' in data:  # Up arrow
                    vscreen.scroll_up()
                    ui.draw_content(vscreen.get_display_lines(with_scroll=True))
                    continue
                elif b'\x1b[B' in data:  # Down arrow
                    vscreen.scroll_down()
                    ui.draw_content(vscreen.get_display_lines(with_scroll=True))
                    continue

                # Check for voice hotkey
                if CTRL_HOTKEY in data:
                    data = data.replace(CTRL_HOTKEY, b"")

                    if voice.is_listening:
                        voice.stop_listening()
                    else:
                        voice.start_listening()

                    # Reset scroll when interacting
                    vscreen.reset_scroll()

                # Send other keystrokes to child
                if data:
                    child.send(data)
                    # Reset scroll on input
                    vscreen.reset_scroll()

    except KeyboardInterrupt:
        pass
    finally:
        # Cleanup
        if voice.is_listening:
            voice.stop_listening()

        child.kill()

        # Restore terminal
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        print(term.exit_fullscreen)
        print(term.show_cursor)
        print(term.clear)

if __name__ == "__main__":
    asyncio.run(main())