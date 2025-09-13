#!/usr/bin/env python3
"""
listen-cli: Voice-enabled wrapper for Claude Code and other CLI tools.

Usage: python listen-cli.py claude [args...]
       python listen-cli.py codex [args...]
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

import assemblyai as aai
from assemblyai.streaming.v3 import (
    BeginEvent,
    StreamingClient,
    StreamingClientOptions,
    StreamingError,
    StreamingEvents,
    StreamingParameters,
    TerminationEvent,
    TurnEvent,
)
from typing import Type

def parse_args():
    """Parse command line arguments."""
    if len(sys.argv) < 2:
        print("Usage: python listen-cli.py <command> [args...]")
        print("Examples:")
        print("  python listen-cli.py claude code --stdin")
        print("  python listen-cli.py codex --stdin")
        sys.exit(1)

    return sys.argv[1:]  # Everything after the script name

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
        print(f"Launching: {' '.join(self.argv)}")
        self.master_fd, slave_fd = pty.openpty()
        pid = os.fork()

        if pid == 0:
            # Child process
            try:
                os.setsid()
                os.close(self.master_fd)
                os.dup2(slave_fd, 0)  # stdin
                os.dup2(slave_fd, 1)  # stdout
                os.dup2(slave_fd, 2)  # stderr
                os.close(slave_fd)

                if hasattr(termios, 'TIOCSCTTY'):
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)

                os.execvp(self.argv[0], self.argv)
            except Exception as e:
                print(f"Child error: {e}", file=sys.stderr)
                os._exit(127)
        else:
            # Parent process
            self.child_pid = pid
            os.close(slave_fd)

            # Make master fd non-blocking
            flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def send(self, data: bytes):
        """Send data to child process."""
        if self.master_fd and data:
            try:
                os.write(self.master_fd, data)
            except Exception:
                pass

    def kill(self):
        """Kill child process."""
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
    def __init__(self):
        self.transcript = ""
        self.listening = False
        self.status_lines = 3  # Reserve bottom 3 lines for our UI

    def get_terminal_size(self):
        """Get current terminal dimensions."""
        try:
            s = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00'*8)
            rows, cols, _, _ = struct.unpack('HHHH', s)
            return rows, cols
        except:
            return 24, 80  # fallback

    def setup_split_screen(self):
        """Set up terminal for split screen display."""
        rows, cols = self.get_terminal_size()

        # Clear screen and set scroll region (leave bottom lines for our UI)
        main_area_rows = rows - self.status_lines
        print(f"\033[2J", end="")  # Clear screen
        print(f"\033[1;{main_area_rows}r", end="")  # Set scroll region
        print(f"\033[{main_area_rows + 1};1H", end="")  # Move to status area

        # Draw separator line
        print("─" * cols)
        self.show_status()

        # Move cursor back to main area
        print(f"\033[1;1H", end="")
        sys.stdout.flush()

    def show_status(self):
        """Update the status area at bottom of terminal."""
        rows, cols = self.get_terminal_size()
        status_row = rows - self.status_lines + 2

        # Save cursor position
        print("\033[s", end="")

        # Move to status area and clear lines
        print(f"\033[{status_row};1H", end="")
        print("\033[K", end="")  # Clear line

        # Show status
        status = "🎤 LISTENING" if self.listening else "⏸️  idle"
        print(f"Voice: {status} (Ctrl+G to toggle)")

        # Show transcript
        print(f"\033[{status_row + 1};1H", end="")
        print("\033[K", end="")  # Clear line
        if self.transcript:
            # Truncate if too long
            display_text = self.transcript
            if len(display_text) > cols - 12:
                display_text = display_text[-(cols-15):] + "..."
            print(f"Transcript: {display_text}")
        else:
            print("Transcript: (none)")

        # Restore cursor position
        print("\033[u", end="")
        sys.stdout.flush()

    def update_transcript(self, text: str):
        """Update transcript display."""
        self.transcript = text
        self.show_status()

    def set_listening(self, listening: bool):
        """Update listening status."""
        self.listening = listening
        self.show_status()

    def clear_transcript(self):
        """Clear transcript."""
        self.transcript = ""
        self.show_status()

    def cleanup(self):
        """Reset terminal to normal mode."""
        print("\033[r", end="")  # Reset scroll region
        print("\033[2J", end="")  # Clear screen
        print("\033[1;1H", end="")  # Move to top
        sys.stdout.flush()

# ===============================
# Voice Controller
# ===============================

class VoiceController:
    def __init__(self, ui: TerminalUI):
        self.ui = ui
        self.client = None
        self.is_listening = False
        self.transcript_buffer = ""

        # Configure AssemblyAI
        api_key = os.getenv("ASSEMBLYAI_API_KEY", "f5115c8df6de446999a096a3edee97cb")
        aai.settings.api_key = api_key

    def on_begin(self, client: Type[StreamingClient], event: BeginEvent):
        print(f"\nVoice session started: {event.id}")

    def on_turn(self, client: Type[StreamingClient], event: TurnEvent):
        if event.transcript:
            self.transcript_buffer += event.transcript + " "
            self.ui.update_transcript(self.transcript_buffer.strip())

    def on_terminated(self, client: Type[StreamingClient], event: TerminationEvent):
        print(f"\nVoice session ended: {event.audio_duration_seconds}s processed")

    def on_error(self, client: Type[StreamingClient], error: StreamingError):
        print(f"\nVoice error: {error}")

    def start_listening(self):
        """Start voice transcription."""
        if self.is_listening:
            return

        self.transcript_buffer = ""
        self.ui.clear_transcript()
        self.ui.set_listening(True)

        try:
            self.client = StreamingClient(
                StreamingClientOptions(
                    api_key=aai.settings.api_key,
                    api_host="streaming.assemblyai.com",
                )
            )

            self.client.on(StreamingEvents.Begin, self.on_begin)
            self.client.on(StreamingEvents.Turn, self.on_turn)
            self.client.on(StreamingEvents.Termination, self.on_terminated)
            self.client.on(StreamingEvents.Error, self.on_error)

            self.client.connect(
                StreamingParameters(
                    sample_rate=16000,
                    format_turns=True
                )
            )

            self.is_listening = True

            # Start streaming in background thread
            def stream_audio():
                try:
                    self.client.stream(aai.extras.MicrophoneStream(sample_rate=16000))
                except Exception as e:
                    print(f"\nStreaming error: {e}")
                    self.is_listening = False

            self.stream_thread = threading.Thread(target=stream_audio, daemon=True)
            self.stream_thread.start()

        except Exception as e:
            print(f"\nError starting voice: {e}")
            self.ui.set_listening(False)

    def stop_listening(self):
        """Stop voice transcription and return transcript."""
        if not self.is_listening:
            return ""

        self.ui.set_listening(False)
        self.is_listening = False

        if self.client:
            try:
                self.client.disconnect(terminate=True)
            except Exception:
                pass
            self.client = None

        return self.transcript_buffer.strip()

# ===============================
# Main Application
# ===============================

def sync_winsize(pty_master_fd: int):
    """Sync terminal window size with PTY."""
    try:
        s = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00'*8)
        rows, cols, _, _ = struct.unpack('HHHH', s)
        if rows > 0 and cols > 0:
            fcntl.ioctl(pty_master_fd, termios.TIOCSWINSZ,
                       struct.pack('HHHH', rows, cols, 0, 0))
    except:
        pass

async def main_loop(child: PTYChild, ui: TerminalUI, voice: VoiceController):
    """Main application loop."""
    stdin_fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        print("Must run in a TTY")
        return

    # Set up terminal
    old_attrs = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)
    ui.setup_split_screen()

    try:
        while True:
            # Wait for input from stdin or child
            r, _, _ = select.select([stdin_fd, child.master_fd], [], [], 0.1)

            # Child output -> main screen area
            if child.master_fd in r:
                try:
                    data = os.read(child.master_fd, 4096)
                    if not data:
                        break  # Child process ended

                    # Write to main area (scroll region handles this)
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
                        # Stop listening and inject transcript
                        transcript = voice.stop_listening()

                        if transcript:
                            # Inject transcript into child's input
                            child.send(transcript.encode())
                            print(f"\n📝 Injected: {transcript}")
                        else:
                            print("\n📭 No transcript to inject")
                    else:
                        # Start listening
                        voice.start_listening()

                # Send remaining keystrokes to child
                if data:
                    child.send(data)

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        if voice.is_listening:
            voice.stop_listening()
        ui.cleanup()
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)

async def main():
    """Main entry point."""
    # Parse arguments
    child_args = parse_args()

    # Create components
    ui = TerminalUI()
    voice = VoiceController(ui)
    child = PTYChild(child_args)

    # Start child process
    child.spawn()

    # Set up window resize handling
    sync_winsize(child.master_fd)
    def handle_winch(signum, frame):
        sync_winsize(child.master_fd)
        ui.setup_split_screen()  # Redraw UI on resize
    signal.signal(signal.SIGWINCH, handle_winch)

    try:
        await main_loop(child, ui, voice)
    finally:
        child.kill()

if __name__ == "__main__":
    asyncio.run(main())