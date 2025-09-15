#!/usr/bin/env python3
"""
Curses-based terminal UI with FIXED voice toggle and better UI feedback.
Voice control properly toggles on/off with clear status indicators.
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
import time
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

        # Get current terminal size (lines, columns)
        ts = os.get_terminal_size()
        rows, cols = ts.lines, ts.columns

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

                # No environment hacks; run child as-is

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
    """Voice controller with improved state management and UI feedback."""

    def __init__(self, ui_queue: queue.Queue, pipe_w: int):
        self.ui_queue = ui_queue
        self.pipe_w = pipe_w
        self.transcriber = None
        self.is_listening = False
        self.state = 'idle'  # idle | active | stopping
        self.transcript_buffer = []
        self.stream_thread = None
        self.has_content = False  # Track if we have any transcript

        # Configure AssemblyAI
        api_key = os.getenv("ASSEMBLYAI_API_KEY", "f5115c8df6de446999a096a3edee97cb")
        aai.settings.api_key = api_key

    def on_open(self, session_opened: aai.RealtimeSessionOpened):
        """Session opened callback."""
        # Don't change status here - it's already set
        pass

    def on_data(self, transcript: aai.RealtimeTranscript):
        """Transcript data callback."""
        # Process data only if in active or stopping state
        if self.state not in ('active', 'stopping'):
            return
        if isinstance(transcript, aai.RealtimeFinalTranscript):
            text = transcript.text.strip()
            if text:
                self.transcript_buffer.append(text + " ")
                self.has_content = True
                # Show real-time transcript preview only while active
                if self.state == 'active':
                    preview_full = "".join(self.transcript_buffer)
                    words = preview_full.strip().split()
                    last_words = " ".join(words[-6:]) if words else ""
                    self.ui_queue.put(('voice_status', True, last_words))
                    os.write(self.pipe_w, b'!')

    def on_error(self, error: aai.RealtimeError):
        """Error callback."""
        self.ui_queue.put(('status', f'❌ Voice error: {error}'))
        self.ui_queue.put(('voice_status', False, ''))  # Clear any listening indicator
        os.write(self.pipe_w, b'!')
        self.is_listening = False
        self.state = 'idle'

    def on_close(self):
        """Session closed callback."""
        # Ensure UI is cleared when session closes
        try:
            self.ui_queue.put(('voice_status', False, ''))
            os.write(self.pipe_w, b'!')
        except Exception:
            pass

    def start_listening(self):
        """Start voice transcription."""
        if self.state != 'idle':
            return False  # Already listening

        # Clear buffer for new session
        self.transcript_buffer = []
        self.has_content = False
        self.is_listening = True
        self.state = 'active'

        # Update UI to show we're listening (no initial preview text)
        self.ui_queue.put(('voice_status', True, ""))
        self.ui_queue.put(('status', 'Voice: Listening started'))
        os.write(self.pipe_w, b'!')

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
            return True

        except Exception as e:
            self.ui_queue.put(('status', f'❌ Error starting voice: {e}'))
            self.ui_queue.put(('voice_status', False, ''))
            os.write(self.pipe_w, b'!')
            self.is_listening = False
            self.state = 'idle'
            return False

    def stop_listening(self):
        """Stop voice transcription and queue the transcript."""
        if self.state == 'idle':
            return None  # Not listening

        # Move to stopping state; keep accepting final transcripts but stop UI preview
        self.state = 'stopping'

        # Immediately update UI to finished; will be cleared by UI timer
        self.ui_queue.put(('voice_status', False, "Transcribing Finished"))
        os.write(self.pipe_w, b'!')

        def _finalize_close():
            try:
                # Close transcriber in background to avoid blocking UI
                if self.transcriber:
                    try:
                        self.transcriber.close()
                    except Exception:
                        pass
                    self.transcriber = None

                # Compute accumulated text
                full_text = None
                if self.transcript_buffer:
                    full_text = "".join(self.transcript_buffer).strip()
                    if full_text:
                        full_text = full_text.replace('\n', ' ').replace('\r', ' ')
                        full_text = re.sub(r'\s+', ' ', full_text).strip()

                # Reset state to idle
                self.is_listening = False
                self.state = 'idle'

                # Send paste if we have content
                if full_text:
                    self.ui_queue.put(('paste', full_text))
                else:
                    self.ui_queue.put(('status', 'No voice input captured'))
            finally:
                try:
                    os.write(self.pipe_w, b'!')
                except Exception:
                    pass

        t = threading.Thread(target=_finalize_close, daemon=True)
        t.start()
        return True

    def toggle(self):
        """Toggle voice on/off and return new state."""
        if self.state in ('active', 'stopping'):
            text = self.stop_listening()
            return (False, text)  # (is_listening, captured_text)
        else:
            success = self.start_listening()
            return (success, None)  # (is_listening, None)


class CursesUI:
    """Main curses UI handler with improved voice state tracking."""

    def __init__(self, child: PTYChild):
        self.child = child
        self.stdscr = None
        self.pad = None
        self.screen = None  # pyte screen
        self.stream = None  # pyte stream
        self.scroll_pos = 0
        self.running = True
        self.voice_queue = queue.Queue()
        self.voice_pipe_r, self.voice_pipe_w = os.pipe()
        self.voice_controller = None

        # Voice state tracking
        self.voice_active = False
        self.voice_text_preview = ""
        self.last_status = "Ready"
        self.voice_finished_until = 0.0

        # Make voice pipe non-blocking
        flags = fcntl.fcntl(self.voice_pipe_r, fcntl.F_GETFL)
        fcntl.fcntl(self.voice_pipe_r, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # Terminal dimensions
        self.rows = 24
        self.cols = 80

        # Color pairs (initialized later)
        self.color_pairs = {}
        # Track cursor position for visual caret rendering
        self.prev_cursor = None  # (y, x)

    def init_colors(self):
        """Initialize basic 16 ANSI colors (simple)."""
        if not curses.has_colors():
            return

        curses.start_color()
        curses.use_default_colors()

        for i in range(8):
            curses.init_pair(i + 1, i, -1)
            if curses.COLORS >= 16:
                curses.init_pair(i + 9, i + 8, -1)

        self.color_pairs = {
            'black': 1, 'red': 2, 'green': 3, 'yellow': 4,
            'blue': 5, 'magenta': 6, 'cyan': 7, 'white': 8,
            'brightblack': 9, 'brightred': 10, 'brightgreen': 11,
            'brightyellow': 12, 'brightblue': 13, 'brightmagenta': 14,
            'brightcyan': 15, 'brightwhite': 16,
            'bright_black': 9, 'bright_red': 10, 'bright_green': 11,
            'bright_yellow': 12, 'bright_blue': 13, 'bright_magenta': 14,
            'bright_cyan': 15, 'bright_white': 16,
            'brown': 4,
        }

    def get_curses_attr(self, char) -> int:
        """Convert pyte character attributes to curses attributes."""
        attr = curses.A_NORMAL

        # Simple named-color mapping only
        if hasattr(char, 'fg') and isinstance(getattr(char, 'fg', None), str):
            fg_key = char.fg.lower()
            if fg_key in self.color_pairs:
                attr |= curses.color_pair(self.color_pairs[fg_key])

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

    # (No overlay-caret helpers; simplified renderer)

    def draw_footer(self, status: str = None):
        """Draw the footer with status information and voice state."""
        if not self.stdscr:
            return

        max_y, max_x = self.stdscr.getmaxyx()
        footer_y = max_y - 1

        # Update last status if provided
        if status is not None:
            self.last_status = status

        # Build footer text with voice indicator
        now = time.time()
        if self.voice_active:
            if self.voice_text_preview:
                voice_indicator = f"Listening: {self.voice_text_preview}"
            else:
                voice_indicator = "Listening..."
        else:
            # If we recently finished, show transient message; otherwise, nothing
            if now < self.voice_finished_until:
                voice_indicator = "Transcribing Finished"
            else:
                voice_indicator = ""

        footer = f" {voice_indicator} | q: Quit | PgUp/PgDn: Scroll | {self.last_status} "
        footer = footer[:max_x].ljust(max_x)

        try:
            self.stdscr.addstr(footer_y, 0, footer, curses.A_REVERSE)
            # Stage footer to be drawn on next doupdate()
            try:
                self.stdscr.noutrefresh()
            except Exception:
                pass
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
            max_draw_x = max(0, self.cols - 1)  # leave last column blank to prevent wrap/jitter
            for x, char in enumerate(line):
                if x >= max_draw_x:
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
            # Clear last column explicitly to avoid right-edge artifacts
            try:
                self.pad.addstr(y, max_draw_x, ' ')
            except curses.error:
                pass

    def refresh_display(self):
        """Refresh the visible portion of the pad."""
        if not self.pad or not self.stdscr:
            return

        max_y, max_x = self.stdscr.getmaxyx()
        visible_rows = max_y - 1  # Reserve one line for footer

        # Calculate visible area
        pad_height, pad_width = self.pad.getmaxyx()

        # Auto-scroll to follow cursor if at bottom
        if self.screen and hasattr(self.screen, 'cursor'):
            cursor_y = self.screen.cursor.y
            if cursor_y >= self.scroll_pos + visible_rows:
                self.scroll_pos = max(0, cursor_y - visible_rows + 1)

        # Ensure scroll position is valid
        self.scroll_pos = max(0, min(self.scroll_pos, pad_height - visible_rows))

        # Refresh pad and footer (no cursor hacks)
        self.draw_footer()
        try:
            self.pad.refresh(
                self.scroll_pos, 0,
                0, 0,
                visible_rows - 1, min(pad_width - 1, max_x - 1)
            )
        except curses.error:
            pass

    def handle_resize(self):
        """Handle terminal resize event."""
        # Get new dimensions from curses
        if self.stdscr:
            max_y, max_x = self.stdscr.getmaxyx()
            self.rows, self.cols = max_y, max_x
            # Resize curses
            curses.resizeterm(self.rows, self.cols)
            # Notify child process with new size
            self.child.resize(self.rows, self.cols)

        # Resize pyte screen
        if self.screen:
            self.screen.resize(self.rows - 1, self.cols)  # -1 for footer

        # Recreate pad with new dimensions
        if self.pad:
            self.pad = curses.newpad(10000, self.cols)
            try:
                self.pad.leaveok(True)
            except Exception:
                pass
            # Mark all lines as dirty to force full redraw
            if self.screen:
                self.screen.dirty.update(range(len(self.screen.display)))

        # Clear and redraw
        if self.stdscr:
            self.stdscr.clear()
            self.stdscr.refresh()

        self.render_screen_to_pad()
        self.refresh_display()
        self.draw_footer("Resized")

    def run(self, stdscr):
        """Main UI loop."""
        self.stdscr = stdscr

        # Configure curses
        try:
            curses.curs_set(0)  # Hide cursor to avoid confusion
        except Exception:
            pass
        stdscr.nodelay(True)  # Non-blocking input
        stdscr.timeout(0)
        try:
            curses.set_escdelay(25)  # Reduce ESC sequence delay for snappier function keys
        except Exception:
            pass

        # Initialize colors
        self.init_colors()

        # Get initial dimensions
        self.rows, self.cols = stdscr.getmaxyx()

        # Create pyte screen and stream
        self.screen = pyte.HistoryScreen(self.cols, self.rows - 1, history=1000)
        self.stream = pyte.ByteStream(self.screen)

        # Create pad for scrolling
        self.pad = curses.newpad(10000, self.cols)

        # Initialize voice controller
        self.voice_controller = ThreadSafeVoiceController(
            self.voice_queue, self.voice_pipe_w
        )

        # Setup SIGWINCH handler
        def sigwinch_handler(signum, frame):
            self.handle_resize()
        signal.signal(signal.SIGWINCH, sigwinch_handler)

        # Initial draw
        self.draw_footer()

        # Enable keypad to properly receive function/arrow keys
        stdscr.keypad(True)
        # Enable mouse reporting so wheel events don't translate to arrow keys
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except Exception:
            pass

        # Main event loop
        while self.running:
            # Drain any pending keyboard input without relying on select()
            try:
                while True:
                    key = stdscr.getch()
                    if key == -1:
                        break
                    self.handle_input(key)
            except Exception:
                pass

            # Build select list for PTY + voice pipe only
            read_fds = [self.child.master_fd, self.voice_pipe_r]

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
                            event_type, *args = self.voice_queue.get()

                            if event_type == 'paste':
                                # Send transcript to child with bracketed paste
                                text = args[0]
                                self.child.send(b'\x1b[200~')  # Start bracketed paste
                                self.child.send(text.encode('utf-8'))
                                self.child.send(b'\x1b[201~')  # End bracketed paste
                                # Do NOT auto-submit; user presses Enter manually
                                self.last_status = f"Pasted: {text[:20]}..." if len(text) > 20 else f"Pasted: {text}"
                                self.draw_footer()

                            elif event_type == 'voice_status':
                                # Update voice state
                                self.voice_active = args[0]
                                self.voice_text_preview = args[1] if len(args) > 1 else ""
                                if not self.voice_active:
                                    # Only set transient finished window for the finished message
                                    if self.voice_text_preview == "Transcribing Finished":
                                        self.voice_finished_until = time.time() + 3
                                        # Schedule a clear event so footer returns to nothing
                                        def _clear_finished():
                                            try:
                                                self.voice_queue.put(('voice_status', False, ''))
                                                os.write(self.voice_pipe_w, b'!')
                                            except Exception:
                                                pass
                                        _t = threading.Timer(3, _clear_finished)
                                        _t.daemon = True
                                        _t.start()
                                    else:
                                        self.voice_finished_until = 0.0
                                        # Clear any lingering preview to avoid showing stale 'Listening:'
                                        self.voice_text_preview = ""
                                self.draw_footer()

                            elif event_type == 'status':
                                # Update general status
                                self.draw_footer(args[0])
                    except:
                        pass

            # Always refresh display so the caret follows even if the child
            # app doesn't emit output for cursor moves.
            try:
                self.refresh_display()
            except Exception:
                pass

    def handle_input(self, key: int):
        """Handle keyboard input."""
        if key == ord('q') or key == ord('Q'):
            # Quit
            self.running = False

        elif key == curses.KEY_MOUSE:
            # Handle mouse wheel to scroll our pad only; don't send to child
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except Exception:
                return

            # Determine wheel up/down; fall back to common bit values if attrs missing
            B4 = getattr(curses, 'BUTTON4_PRESSED', 0x0800)
            B5 = getattr(curses, 'BUTTON5_PRESSED', 0x1000)
            lines = 3
            if bstate & B4:
                self.scroll_pos = max(0, self.scroll_pos - lines)
                self.refresh_display()
            elif bstate & B5:
                max_y, _ = self.stdscr.getmaxyx()
                self.scroll_pos += lines
                self.refresh_display()
            # Do not forward mouse events to the child
            return

        # Note: Up/Down keys are passed through to child for in-app navigation

        elif key == curses.KEY_PPAGE:  # Page Up
            max_y, _ = self.stdscr.getmaxyx()
            self.scroll_pos = max(0, self.scroll_pos - (max_y - 1))
            self.refresh_display()
            self.draw_footer(f"Line {self.scroll_pos}")

        elif key == curses.KEY_NPAGE:  # Page Down
            max_y, _ = self.stdscr.getmaxyx()
            self.scroll_pos += max_y - 1
            self.refresh_display()
            self.draw_footer(f"Line {self.scroll_pos}")

        elif key == 7:  # Ctrl+G - Voice toggle
            if self.voice_controller:
                is_listening, captured_text = self.voice_controller.toggle()
                # State will be updated via queue events

        elif key == curses.KEY_F5:  # F5 - Start listening (testing)
            if self.voice_controller:
                if self.voice_controller.state in ('active', 'stopping'):
                    self.draw_footer("Voice already active")
                else:
                    # Immediately reflect listening state in UI
                    self.voice_active = True
                    self.voice_text_preview = ""
                    self.draw_footer("Starting...")
                    started = self.voice_controller.start_listening()
                    if not started:
                        # Revert UI if start failed
                        self.voice_active = False
                        self.voice_text_preview = ""
                        self.draw_footer("Failed to start voice (see status)")

        elif key == curses.KEY_F6:  # F6 - Stop listening (testing)
            if self.voice_controller:
                # Immediately reflect stop in UI to avoid stale 'Listening:'
                self.voice_active = False
                self.voice_text_preview = ""
                self.draw_footer("Stopping...")
                result = self.voice_controller.stop_listening()
                if result is None:
                    # Not listening
                    self.draw_footer("Voice not active")

        # Removed dev diagnostics and overlay-caret toggles

        elif key in (10, 13, curses.KEY_ENTER):  # Enter key (LF or CR)
            # Send CR (ASCII 13) for message submission in Claude
            self.child.send(b'\r')

        elif key < 256:
            # Regular ASCII - pass through to child
            self.child.send(bytes([key]))

        else:
            # Special keys - convert to escape sequences
            key_map = {
                curses.KEY_UP: b'\x1b[A',
                curses.KEY_DOWN: b'\x1b[B',
                curses.KEY_LEFT: b'\x1b[D',
                curses.KEY_RIGHT: b'\x1b[C',
                curses.KEY_HOME: b'\x1b[H',
                curses.KEY_END: b'\x1b[F',
                curses.KEY_DC: b'\x1b[3~',  # Delete
                curses.KEY_IC: b'\x1b[2~',  # Insert
                curses.KEY_F1: b'\x1bOP',
                curses.KEY_F2: b'\x1bOQ',
                curses.KEY_F3: b'\x1bOR',
                curses.KEY_F4: b'\x1bOS',
            }

            if key in key_map:
                self.child.send(key_map[key])


def main():
    """Main entry point."""
    # Get command to run
    if len(sys.argv) > 1:
        argv = sys.argv[1:]
    else:
        argv = ['claude']  # Default to claude

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


if __name__ == '__main__':
    main()
