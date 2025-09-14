#!/usr/bin/env python3
"""
Listen-CLI with terminal regions - header/footer with Claude in the middle.
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
import shutil

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
# Terminal UI Manager
# ===============================

class TerminalUI:
    def __init__(self):
        self.width, self.height = shutil.get_terminal_size()
        self.status = "Ready"
        self.recording = False

    def setup_regions(self):
        """Set up terminal with header, content area, and footer."""
        # Clear screen and home cursor
        sys.stdout.write('\033[2J\033[H')

        # Draw header (line 1-2)
        self._draw_header()

        # Draw footer (last 2 lines)
        self._draw_footer()

        # Set scrolling region (lines 3 to height-2)
        # This constrains Claude to the middle area
        sys.stdout.write(f'\033[3;{self.height-2}r')

        # Position cursor in content area
        sys.stdout.write('\033[3;1H')
        sys.stdout.flush()

    def _draw_header(self):
        """Draw the header at top of screen."""
        sys.stdout.write('\033[1;1H')  # Go to line 1
        sys.stdout.write('\033[7m')  # Reverse video
        header = " 🎙️ Listen-CLI - Voice Input for Claude ".center(self.width)
        sys.stdout.write(header[:self.width])
        sys.stdout.write('\033[0m')  # Reset
        sys.stdout.write('\033[2;1H')  # Line 2
        sys.stdout.write('─' * self.width)

    def _draw_footer(self):
        """Draw the footer at bottom of screen."""
        # Save cursor position
        sys.stdout.write('\033[s')

        # Draw separator line
        sys.stdout.write(f'\033[{self.height-1};1H')
        sys.stdout.write('─' * self.width)

        # Draw status line
        sys.stdout.write(f'\033[{self.height};1H')
        sys.stdout.write('\033[K')  # Clear line

        status_left = f" [Ctrl+G: Toggle Recording]"
        status_right = f"[Status: {self.status}] "
        padding = self.width - len(status_left) - len(status_right)

        sys.stdout.write('\033[7m')  # Reverse video
        sys.stdout.write(status_left)
        sys.stdout.write(' ' * max(0, padding))
        sys.stdout.write(status_right)
        sys.stdout.write('\033[0m')  # Reset

        # Restore cursor position
        sys.stdout.write('\033[u')
        sys.stdout.flush()

    def update_status(self, status: str, recording: bool = None):
        """Update the status line without affecting content area."""
        self.status = status
        if recording is not None:
            self.recording = recording

        # Save cursor, update footer, restore cursor
        sys.stdout.write('\033[s')
        self._draw_footer()
        sys.stdout.write('\033[u')
        sys.stdout.flush()

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

                # Set terminal size for child
                os.environ['LINES'] = str(shutil.get_terminal_size().lines - 4)
                os.environ['COLUMNS'] = str(shutil.get_terminal_size().columns)

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
        # Only accumulate final transcripts to avoid duplicates
        if isinstance(transcript, aai.RealtimeFinalTranscript):
            text = transcript.text.strip()
            if text:
                self.transcript_buffer.append(text + " ")
                # Update status with word count
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
                except Exception as e:
                    self.ui.update_status(f"Stream error", recording=False)
                    self.is_listening = False

            self.stream_thread = threading.Thread(target=stream_audio, daemon=True)
            self.stream_thread.start()

        except Exception as e:
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

        # Replace newlines with spaces to prevent premature submission
        full_text = full_text.replace('\n', ' ').replace('\r', ' ')
        full_text = re.sub(r'\s+', ' ', full_text).strip()

        # Try bracketed paste first
        if self.use_bracketed_paste:
            try:
                self.child.send(b'\x1b[200~')
                self.child.send(full_text.encode('utf-8'))
                self.child.send(b'\x1b[201~')
            except Exception:
                self.child.send(full_text.encode('utf-8'))
        else:
            self.child.send(full_text.encode('utf-8'))

        self.transcript_buffer = []

# ===============================
# Main Application
# ===============================

def sync_winsize(pty_master_fd: int, ui_height_offset: int = 4):
    try:
        s = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00'*8)
        rows, cols, _, _ = struct.unpack('HHHH', s)
        # Reduce rows by UI offset (header + footer)
        adjusted_rows = max(1, rows - ui_height_offset)
        if adjusted_rows > 0 and cols > 0:
            fcntl.ioctl(pty_master_fd, termios.TIOCSWINSZ,
                       struct.pack('HHHH', adjusted_rows, cols, 0, 0))
    except:
        pass

async def main_loop(child: PTYChild, voice: BufferVoiceController, ui: TerminalUI):
    stdin_fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        print("Must run in a TTY")
        return

    old_attrs = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)

    # Set up the UI regions
    ui.setup_regions()

    try:
        while True:
            r, _, _ = select.select([stdin_fd, child.master_fd], [], [], 0.1)

            # Child output -> stdout (will be constrained to scrolling region)
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
        # Clean exit
        pass
    finally:
        if voice.is_listening:
            voice.stop_listening()

        # Reset terminal
        sys.stdout.write('\033[?25h')  # Show cursor
        sys.stdout.write(f'\033[1;{ui.height}r')  # Reset scrolling region
        sys.stdout.write('\033[2J\033[H')  # Clear screen
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)

async def main():
    if len(sys.argv) < 2:
        print("Usage: python listen-cli-regions.py <command> [args...]")
        print("Example: python listen-cli-regions.py claude")
        sys.exit(1)

    # Initialize UI
    ui = TerminalUI()

    child_args = sys.argv[1:]
    child = PTYChild(child_args)
    child.spawn()

    voice = BufferVoiceController(child, ui)

    # Handle window resize
    sync_winsize(child.master_fd, 4)  # 4 lines for header/footer
    signal.signal(signal.SIGWINCH, lambda s, f: sync_winsize(child.master_fd, 4))

    try:
        await main_loop(child, voice, ui)
    finally:
        child.kill()

if __name__ == "__main__":
    asyncio.run(main())