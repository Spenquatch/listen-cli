#!/usr/bin/env python3
"""
Curses-based terminal UI with footer and infinite scroll.
Phase 1: Core PTY + pyte + curses rendering with SIGWINCH handling.
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
from typing import Optional, Tuple

import pyte

# Tool use counter for checking with thinkdeeper
TOOL_USE_COUNT = 0

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


class CursesUI:
    """Main curses UI handler."""

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
        # Pairs 1-8: normal colors on default background
        # Pairs 9-16: bright colors on default background
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

    def draw_footer(self, status: str = ""):
        """Draw the footer with status information."""
        if not self.stdscr:
            return

        max_y, max_x = self.stdscr.getmaxyx()
        footer_y = max_y - 1

        # Build footer text
        footer = f" ^G: Voice | q: Quit | ↑↓: Scroll | {status} "
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
                    # Skip characters that can't be rendered
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

        # Resize pyte screen
        if self.screen:
            self.screen.resize(self.rows - 1, self.cols)  # -1 for footer

        # Recreate pad with new dimensions
        if self.pad:
            # Create new pad (can't resize existing pad to be smaller)
            old_pad = self.pad
            self.pad = curses.newpad(10000, self.cols)

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
        curses.curs_set(0)  # Hide cursor
        stdscr.nodelay(True)  # Non-blocking input
        stdscr.timeout(0)

        # Initialize colors
        self.init_colors()

        # Get initial dimensions
        self.rows, self.cols = stdscr.getmaxyx()

        # Create pyte screen and stream
        # Use HistoryScreen for better scrollback
        self.screen = pyte.HistoryScreen(self.cols, self.rows - 1, history=1000)
        self.stream = pyte.ByteStream(self.screen)

        # Create pad for scrolling
        self.pad = curses.newpad(10000, self.cols)

        # Setup SIGWINCH handler
        def sigwinch_handler(signum, frame):
            self.handle_resize()
        signal.signal(signal.SIGWINCH, sigwinch_handler)

        # Initial draw
        self.draw_footer("Ready")

        # Main event loop
        while self.running:
            # Build select list
            read_fds = [sys.stdin.fileno(), self.child.master_fd, self.voice_pipe_r]

            try:
                readable, _, _ = select.select(read_fds, [], [], 0.1)
            except select.error:
                # Likely interrupted by SIGWINCH
                continue

            for fd in readable:
                if fd == self.child.master_fd:
                    # Read from PTY
                    try:
                        data = os.read(self.child.master_fd, 4096)
                        if data:
                            # Feed to pyte
                            self.stream.feed(data)
                            # Render updates
                            self.render_screen_to_pad()
                            self.refresh_display()
                        else:
                            # EOF - child exited
                            self.running = False
                    except OSError:
                        # Child process ended
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
                            if event_type == 'paste':
                                # Send transcript to child
                                self.child.send(data.encode('utf-8'))
                            elif event_type == 'status':
                                # Update footer
                                self.draw_footer(data)
                    except:
                        pass

    def handle_input(self, key: int):
        """Handle keyboard input."""
        global TOOL_USE_COUNT
        TOOL_USE_COUNT += 1

        if key == ord('q') or key == ord('Q'):
            # Quit
            self.running = False

        elif key == curses.KEY_UP:
            # Scroll up
            self.scroll_pos = max(0, self.scroll_pos - 1)
            self.refresh_display()
            self.draw_footer(f"Line {self.scroll_pos}")

        elif key == curses.KEY_DOWN:
            # Scroll down
            self.scroll_pos += 1
            self.refresh_display()
            self.draw_footer(f"Line {self.scroll_pos}")

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

        elif key == 7:  # Ctrl+G
            # Toggle voice (placeholder for now)
            self.draw_footer("Voice: Not implemented yet")

        elif key in (10, 13, curses.KEY_ENTER):  # Enter key
            # Send CR (ASCII 13) for message submission in Claude
            self.child.send(b'\r')

        elif key < 256:
            # Regular ASCII - pass through to child
            self.child.send(bytes([key]))

        else:
            # Special keys - convert to escape sequences
            key_map = {
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