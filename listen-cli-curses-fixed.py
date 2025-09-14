#!/usr/bin/env python3
"""
Curses-based terminal UI with LOCAL INPUT BUFFER for proper Enter key handling.
Fixed version that allows message submission to Claude.
"""

import os
import sys
import pty
import select
import signal
import struct
import fcntl
import termios
import curses
import queue
import threading
import re
from typing import Optional, Tuple

import pyte
import assemblyai as aai


class PTYChild:
    """Manages PTY child process."""

    def __init__(self, argv):
        self.argv = argv
        self.master_fd = None
        self.child_pid = None

    def spawn(self):
        """Fork and exec child process in PTY."""
        self.master_fd, slave_fd = pty.openpty()

        # Get current terminal size
        rows, cols = os.get_terminal_size()

        # Set PTY size before fork
        size = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, size)

        pid = os.fork()

        if pid == 0:  # Child
            try:
                os.setsid()
                os.close(self.master_fd)

                # Make slave the controlling terminal
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                os.close(slave_fd)

                # Set controlling terminal
                if hasattr(termios, 'TIOCSCTTY'):
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)

                # Execute the command
                os.execvp(self.argv[0], self.argv)
            except Exception as e:
                print(f"Child error: {e}", file=sys.stderr)
                os._exit(127)
        else:  # Parent
            self.child_pid = pid
            os.close(slave_fd)

            # Make master non-blocking
            flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def send(self, data: bytes):
        """Send data to child process."""
        if self.master_fd and data:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def resize(self, rows: int, cols: int):
        """Notify child of terminal resize."""
        if self.master_fd:
            size = struct.pack('HHHH', rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, size)

    def kill(self):
        """Terminate child process."""
        if self.child_pid:
            try:
                os.killpg(self.child_pid, signal.SIGTERM)
            except:
                try:
                    os.kill(self.child_pid, signal.SIGTERM)
                except:
                    pass


class ThreadSafeVoiceController:
    """Voice controller that uses queue for thread-safe UI updates."""

    def __init__(self, ui_queue: queue.Queue, pipe_w: int):
        self.ui_queue = ui_queue
        self.pipe_w = pipe_w
        self.transcriber = None
        self.is_listening = False
        self.transcript_buffer = []
        self.stream_thread = None

        # Configure AssemblyAI
        api_key = os.getenv("ASSEMBLYAI_API_KEY", "f5115c8df6de446999a096a3edee97cb")
        aai.settings.api_key = api_key

    def on_open(self, session_opened: aai.RealtimeSessionOpened):
        """Session opened callback."""
        self.ui_queue.put(('status', '🎤 Voice: Listening...'))
        os.write(self.pipe_w, b'!')

    def on_data(self, transcript: aai.RealtimeTranscript):
        """Transcript data callback."""
        if isinstance(transcript, aai.RealtimeFinalTranscript):
            text = transcript.text.strip()
            if text:
                self.transcript_buffer.append(text + " ")
                # Update status with partial transcript
                preview = "".join(self.transcript_buffer)[-30:]
                self.ui_queue.put(('status', f'🎤 Voice: {preview}...'))
                os.write(self.pipe_w, b'!')

    def on_error(self, error: aai.RealtimeError):
        """Error callback."""
        self.ui_queue.put(('status', f'❌ Voice error: {error}'))
        os.write(self.pipe_w, b'!')

    def on_close(self):
        """Session closed callback."""
        pass

    def start_listening(self):
        """Start voice transcription."""
        if self.is_listening:
            return

        # Clear buffer for new session
        self.transcript_buffer = []
        self.is_listening = True

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

            # Start streaming in separate thread
            def stream_audio():
                try:
                    self.transcriber.stream(aai.extras.MicrophoneStream(sample_rate=16000))
                except Exception as e:
                    self.ui_queue.put(('status', f'❌ Streaming error: {e}'))
                    os.write(self.pipe_w, b'!')
                    self.is_listening = False

            self.stream_thread = threading.Thread(target=stream_audio, daemon=True)
            self.stream_thread.start()

        except Exception as e:
            self.ui_queue.put(('status', f'❌ Error starting voice: {e}'))
            os.write(self.pipe_w, b'!')
            self.is_listening = False

    def stop_listening(self):
        """Stop voice transcription and queue the transcript."""
        if not self.is_listening:
            return

        self.is_listening = False

        # Close transcriber
        if self.transcriber:
            try:
                self.transcriber.close()
            except Exception:
                pass
            self.transcriber = None

        # Queue the accumulated text for input buffer
        if self.transcript_buffer:
            full_text = "".join(self.transcript_buffer).strip()

            if full_text:
                # Clean up text
                full_text = full_text.replace('\n', ' ').replace('\r', ' ')
                full_text = re.sub(r'\s+', ' ', full_text).strip()

                # Queue for input buffer (not direct paste)
                self.ui_queue.put(('voice_text', full_text))
                self.ui_queue.put(('status', '✓ Voice: Added to input'))
                os.write(self.pipe_w, b'!')
            else:
                self.ui_queue.put(('status', 'Voice: No text'))
                os.write(self.pipe_w, b'!')
        else:
            self.ui_queue.put(('status', 'Voice: No text'))
            os.write(self.pipe_w, b'!')

        # Clear buffer
        self.transcript_buffer = []

    def toggle(self):
        """Toggle voice on/off."""
        if self.is_listening:
            self.stop_listening()
        else:
            self.start_listening()


class CursesUI:
    """Main curses UI handler with LOCAL INPUT BUFFER."""

    def __init__(self, child: PTYChild):
        self.child = child
        self.stdscr = None
        self.pad = None
        self.input_win = None  # New: window for input display
        self.screen = None  # pyte screen
        self.stream = None  # pyte stream
        self.scroll_pos = 0
        self.running = True
        self.voice_queue = queue.Queue()
        self.voice_pipe_r, self.voice_pipe_w = os.pipe()
        self.voice_controller = None

        # LOCAL INPUT BUFFER - Key to fixing Enter key issue!
        self.input_buffer = ""
        self.cursor_pos = 0

        # Make voice pipe non-blocking
        flags = fcntl.fcntl(self.voice_pipe_r, fcntl.F_GETFL)
        fcntl.fcntl(self.voice_pipe_r, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # Terminal dimensions
        self.rows = 24
        self.cols = 80

        # Color pairs (initialized later)
        self.color_pairs = {}

    def init_colors(self):
        """Initialize basic 16 ANSI colors."""
        if not curses.has_colors():
            return

        curses.start_color()
        curses.use_default_colors()

        # Map basic 16 ANSI colors
        for i in range(8):
            curses.init_pair(i + 1, i, -1)  # Normal colors
            if curses.COLORS >= 16:
                curses.init_pair(i + 9, i + 8, -1)  # Bright colors

        # Create color name mapping
        self.color_pairs = {
            'black': 1, 'red': 2, 'green': 3, 'yellow': 4,
            'blue': 5, 'magenta': 6, 'cyan': 7, 'white': 8,
            'bright_black': 9, 'bright_red': 10, 'bright_green': 11,
            'bright_yellow': 12, 'bright_blue': 13, 'bright_magenta': 14,
            'bright_cyan': 15, 'bright_white': 16
        }

    def get_curses_attr(self, char) -> int:
        """Convert pyte character attributes to curses attributes."""
        attr = curses.A_NORMAL

        # Handle colors
        if hasattr(char, 'fg') and char.fg in self.color_pairs:
            attr |= curses.color_pair(self.color_pairs[char.fg])

        # Handle text attributes
        if hasattr(char, 'bold') and char.bold:
            attr |= curses.A_BOLD
        if hasattr(char, 'italics') and char.italics:
            attr |= curses.A_ITALIC if hasattr(curses, 'A_ITALIC') else 0
        if hasattr(char, 'underscore') and char.underscore:
            attr |= curses.A_UNDERLINE
        if hasattr(char, 'reverse') and char.reverse:
            attr |= curses.A_REVERSE

        return attr

    def draw_input_area(self):
        """Draw the input buffer area (NEW!)."""
        if not self.input_win:
            return

        max_y, max_x = self.input_win.getmaxyx()

        # Clear the input window
        self.input_win.erase()

        # Draw a separator line
        try:
            self.input_win.addstr(0, 0, "─" * (max_x - 1), curses.A_DIM)
        except:
            pass

        # Draw the input prompt and buffer
        prompt = "► "
        try:
            self.input_win.addstr(1, 0, prompt, curses.A_BOLD)

            # Draw the input buffer
            if self.input_buffer:
                # Handle long input that needs to scroll
                display_start = max(0, len(self.input_buffer) - (max_x - len(prompt) - 2))
                display_text = self.input_buffer[display_start:]
                self.input_win.addstr(1, len(prompt), display_text)

            # Position cursor
            cursor_x = len(prompt) + min(self.cursor_pos, max_x - len(prompt) - 2)
            self.input_win.move(1, cursor_x)

        except curses.error:
            pass

        self.input_win.refresh()

    def draw_footer(self, status: str = ""):
        """Draw the footer with status information."""
        if not self.stdscr:
            return

        max_y, max_x = self.stdscr.getmaxyx()
        footer_y = max_y - 1

        # Build footer text
        if not status:
            status = "Ready"
        footer = f" ^G: Voice | q: Quit | ↑↓: Scroll | Enter: Send | {status} "
        footer = footer[:max_x].ljust(max_x)

        try:
            self.stdscr.addstr(footer_y, 0, footer, curses.A_REVERSE)
            self.stdscr.refresh()
        except curses.error:
            pass

    def render_screen_to_pad(self):
        """Render pyte screen buffer to curses pad (optimized with dirty tracking)."""
        if not self.pad or not self.screen:
            return

        # Only render dirty lines
        dirty_lines = self.screen.dirty.copy()
        self.screen.dirty.clear()

        for y in dirty_lines:
            if y >= len(self.screen.display):
                continue

            line = self.screen.display[y]
            for x, char in enumerate(line):
                if x >= self.cols:
                    break

                try:
                    # Get character and attributes
                    ch = char.data if hasattr(char, 'data') else char
                    if not ch or ch == '\x00':
                        ch = ' '

                    attr = self.get_curses_attr(char) if hasattr(char, 'fg') else curses.A_NORMAL

                    # Write to pad
                    self.pad.addstr(y, x, ch, attr)
                except (curses.error, UnicodeEncodeError):
                    pass

    def refresh_display(self):
        """Refresh the visible portion of the pad."""
        if not self.pad or not self.stdscr:
            return

        max_y, max_x = self.stdscr.getmaxyx()
        # Reserve space: 1 for footer, 3 for input area
        visible_rows = max_y - 4

        # Calculate visible area
        pad_height, pad_width = self.pad.getmaxyx()

        # Auto-scroll to follow cursor if at bottom
        if self.screen and hasattr(self.screen, 'cursor'):
            cursor_y = self.screen.cursor.y
            if cursor_y >= self.scroll_pos + visible_rows:
                self.scroll_pos = max(0, cursor_y - visible_rows + 1)

        # Ensure scroll position is valid
        self.scroll_pos = max(0, min(self.scroll_pos, pad_height - visible_rows))

        try:
            # Refresh the visible portion of the pad
            self.pad.refresh(
                self.scroll_pos, 0,  # pad position
                0, 0,                # screen position
                visible_rows - 1, min(pad_width - 1, max_x - 1)
            )
        except curses.error:
            pass

    def handle_resize(self):
        """Handle terminal resize event."""
        # Get new dimensions
        self.rows, self.cols = os.get_terminal_size()

        # Resize curses
        curses.resizeterm(self.rows, self.cols)

        # Notify child process
        self.child.resize(self.rows, self.cols)

        # Resize pyte screen (account for input area and footer)
        if self.screen:
            self.screen.resize(self.rows - 4, self.cols)

        # Recreate pad with new dimensions
        if self.pad:
            self.pad = curses.newpad(10000, self.cols)
            # Mark all lines as dirty to force full redraw
            if self.screen:
                self.screen.dirty.update(range(len(self.screen.display)))

        # Recreate input window
        if self.input_win:
            max_y, max_x = self.stdscr.getmaxyx()
            self.input_win = curses.newwin(3, max_x, max_y - 4, 0)

        # Clear and redraw
        if self.stdscr:
            self.stdscr.clear()
            self.stdscr.refresh()

        self.render_screen_to_pad()
        self.refresh_display()
        self.draw_input_area()
        self.draw_footer("Resized")

    def submit_input(self):
        """Submit the input buffer to the child process."""
        if self.input_buffer:
            # Send the buffered text with a newline for submission
            text_to_send = self.input_buffer + '\n'
            self.child.send(text_to_send.encode('utf-8'))

            # Clear the input buffer
            self.input_buffer = ""
            self.cursor_pos = 0

            # Update display
            self.draw_input_area()
            self.draw_footer("Sent")

    def run(self, stdscr):
        """Main UI loop."""
        self.stdscr = stdscr

        # Configure curses
        curses.curs_set(1)  # Show cursor for input
        stdscr.nodelay(True)  # Non-blocking input
        stdscr.timeout(0)

        # Initialize colors
        self.init_colors()

        # Get initial dimensions
        self.rows, self.cols = stdscr.getmaxyx()

        # Create pyte screen and stream (accounting for input area)
        self.screen = pyte.HistoryScreen(self.cols, self.rows - 4, history=1000)
        self.stream = pyte.ByteStream(self.screen)

        # Create pad for scrolling
        self.pad = curses.newpad(10000, self.cols)

        # Create input window (3 lines: separator, input, blank)
        self.input_win = curses.newwin(3, self.cols, self.rows - 4, 0)
        self.input_win.keypad(True)

        # Initialize voice controller
        self.voice_controller = ThreadSafeVoiceController(
            self.voice_queue, self.voice_pipe_w
        )

        # Setup SIGWINCH handler
        def sigwinch_handler(signum, frame):
            self.handle_resize()
        signal.signal(signal.SIGWINCH, sigwinch_handler)

        # Initial draw
        self.draw_input_area()
        self.draw_footer()

        # Main event loop
        while self.running:
            # Build select list
            read_fds = [sys.stdin.fileno(), self.child.master_fd, self.voice_pipe_r]

            try:
                readable, _, _ = select.select(read_fds, [], [], 0.1)
            except select.error:
                continue

            for fd in readable:
                if fd == self.child.master_fd:
                    # Read from PTY
                    try:
                        data = os.read(self.child.master_fd, 4096)
                        if data:
                            self.stream.feed(data)
                            self.render_screen_to_pad()
                            self.refresh_display()
                        else:
                            self.running = False
                    except OSError:
                        self.running = False

                elif fd == sys.stdin.fileno():
                    # Handle keyboard input
                    try:
                        key = stdscr.getch()
                        if key != -1:
                            self.handle_input(key)
                    except:
                        pass

                elif fd == self.voice_pipe_r:
                    # Voice event ready
                    try:
                        os.read(self.voice_pipe_r, 1024)  # Clear pipe
                        while not self.voice_queue.empty():
                            event_type, data = self.voice_queue.get()
                            if event_type == 'voice_text':
                                # Add voice text to input buffer
                                self.input_buffer += data
                                self.cursor_pos = len(self.input_buffer)
                                self.draw_input_area()
                            elif event_type == 'status':
                                # Update footer
                                self.draw_footer(data)
                    except:
                        pass

    def handle_input(self, key: int):
        """Handle keyboard input with LOCAL INPUT BUFFER."""

        # Special quit command (Ctrl+Q to avoid conflicts)
        if key == 17:  # Ctrl+Q
            self.running = False
            return

        # Scroll controls (work even when typing)
        elif key == curses.KEY_UP:
            self.scroll_pos = max(0, self.scroll_pos - 1)
            self.refresh_display()
            self.draw_footer(f"Line {self.scroll_pos}")

        elif key == curses.KEY_DOWN:
            self.scroll_pos += 1
            self.refresh_display()
            self.draw_footer(f"Line {self.scroll_pos}")

        elif key == curses.KEY_PPAGE:  # Page Up
            max_y, _ = self.stdscr.getmaxyx()
            self.scroll_pos = max(0, self.scroll_pos - (max_y - 4))
            self.refresh_display()
            self.draw_footer(f"Line {self.scroll_pos}")

        elif key == curses.KEY_NPAGE:  # Page Down
            max_y, _ = self.stdscr.getmaxyx()
            self.scroll_pos += max_y - 4
            self.refresh_display()
            self.draw_footer(f"Line {self.scroll_pos}")

        # Voice toggle
        elif key == 7:  # Ctrl+G
            if self.voice_controller:
                self.voice_controller.toggle()

        # ENTER KEY - Submit the input buffer!
        elif key in (10, 13, curses.KEY_ENTER):
            self.submit_input()

        # Backspace - Delete from input buffer
        elif key in (127, curses.KEY_BACKSPACE):
            if self.cursor_pos > 0:
                self.input_buffer = (
                    self.input_buffer[:self.cursor_pos-1] +
                    self.input_buffer[self.cursor_pos:]
                )
                self.cursor_pos -= 1
                self.draw_input_area()

        # Delete key
        elif key == curses.KEY_DC:
            if self.cursor_pos < len(self.input_buffer):
                self.input_buffer = (
                    self.input_buffer[:self.cursor_pos] +
                    self.input_buffer[self.cursor_pos+1:]
                )
                self.draw_input_area()

        # Left arrow - Move cursor
        elif key == curses.KEY_LEFT:
            if self.cursor_pos > 0:
                self.cursor_pos -= 1
                self.draw_input_area()

        # Right arrow - Move cursor
        elif key == curses.KEY_RIGHT:
            if self.cursor_pos < len(self.input_buffer):
                self.cursor_pos += 1
                self.draw_input_area()

        # Home - Move to start
        elif key == curses.KEY_HOME:
            self.cursor_pos = 0
            self.draw_input_area()

        # End - Move to end
        elif key == curses.KEY_END:
            self.cursor_pos = len(self.input_buffer)
            self.draw_input_area()

        # Ctrl+U - Clear input buffer
        elif key == 21:
            self.input_buffer = ""
            self.cursor_pos = 0
            self.draw_input_area()

        # Ctrl+C - Send interrupt to child
        elif key == 3:
            self.child.send(b'\x03')

        # Ctrl+D - Send EOF to child
        elif key == 4:
            self.child.send(b'\x04')

        # Regular printable characters - Add to input buffer
        elif 32 <= key <= 126:
            self.input_buffer = (
                self.input_buffer[:self.cursor_pos] +
                chr(key) +
                self.input_buffer[self.cursor_pos:]
            )
            self.cursor_pos += 1
            self.draw_input_area()

        # Tab key - Add tab to input
        elif key == 9:
            self.input_buffer = (
                self.input_buffer[:self.cursor_pos] +
                '\t' +
                self.input_buffer[self.cursor_pos:]
            )
            self.cursor_pos += 1
            self.draw_input_area()


def main():
    """Main entry point."""
    # Get command to run
    if len(sys.argv) > 1:
        argv = sys.argv[1:]
    else:
        argv = ['claude']  # Default to claude

    print("Starting curses UI with local input buffer...")
    print("Controls:")
    print("  Enter: Send message")
    print("  Ctrl+G: Toggle voice input")
    print("  Ctrl+Q: Quit")
    print("  Arrows/PgUp/PgDn: Scroll")
    print("  Ctrl+U: Clear input")
    print("")
    print("Starting in 2 seconds...")
    import time
    time.sleep(2)

    # Create PTY child
    child = PTYChild(argv)
    child.spawn()

    # Create and run UI
    ui = CursesUI(child)

    try:
        curses.wrapper(ui.run)
    finally:
        # Cleanup
        child.kill()
        os.close(ui.voice_pipe_r)
        os.close(ui.voice_pipe_w)
        print("\nCurses UI terminated.")


if __name__ == '__main__':
    main()