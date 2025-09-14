#!/usr/bin/env python3
"""
Invisible listen-cli: Silent voice capture with minimal UI interference.
Shows only brief start/stop messages, no real-time transcript display.
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
# Silent Voice Controller
# ===============================

class SilentVoiceController:
    def __init__(self):
        self.transcriber = None
        self.is_listening = False
        self.transcript_buffer = ""

        # Configure AssemblyAI
        api_key = os.getenv("ASSEMBLYAI_API_KEY", "f5115c8df6de446999a096a3edee97cb")
        aai.settings.api_key = api_key

    def on_open(self, session_opened: aai.RealtimeSessionOpened):
        # Silent - no output during transcription
        pass

    def on_data(self, transcript: aai.RealtimeTranscript):
        if isinstance(transcript, aai.RealtimeFinalTranscript):
            # Only collect final transcripts - no display
            if transcript.text:
                self.transcript_buffer += transcript.text + " "

    def on_error(self, error: aai.RealtimeError):
        # Only show errors
        print(f"\n❌ Voice error: {error}")

    def on_close(self):
        # Silent close
        pass

    def start_listening(self):
        if self.is_listening:
            return

        self.transcript_buffer = ""
        self.is_listening = True

        # Brief start message
        print("\n🎤 Listening... (Ctrl+G again to send)")

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

            # Start streaming in background - completely silent
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
            return ""

        self.is_listening = False

        if self.transcriber:
            try:
                self.transcriber.close()
            except Exception:
                pass
            self.transcriber = None

        final_text = self.transcript_buffer.strip()

        # Show what we captured and injected
        if final_text:
            print(f"\n📝 Captured: {final_text}")
        else:
            print(f"\n📭 No speech detected")

        return final_text

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

async def main_loop(child: PTYChild, voice: SilentVoiceController):
    stdin_fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        print("Must run in a TTY")
        return

    old_attrs = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)

    # Show initial help message
    print("🎙️  Listen-CLI: Ctrl+G to toggle voice input, Ctrl+C to exit\n")

    try:
        while True:
            r, _, _ = select.select([stdin_fd, child.master_fd], [], [], 0.1)

            # Child output -> stdout (Claude has full control)
            if child.master_fd in r:
                try:
                    data = os.read(child.master_fd, 4096)
                    if not data:
                        break

                    # Pass through all Claude output unchanged
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
                        # Stop and inject transcript
                        transcript = voice.stop_listening()
                        if transcript:
                            # Send transcript to Claude's input
                            child.send(transcript.encode())
                    else:
                        # Start listening
                        voice.start_listening()

                # Send all other input to child
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
        print("Usage: python listen-cli-invisible.py <command> [args...]")
        print("Examples:")
        print("  python listen-cli-invisible.py claude code --stdin")
        print("  python listen-cli-invisible.py codex --stdin")
        sys.exit(1)

    child_args = sys.argv[1:]
    voice = SilentVoiceController()
    child = PTYChild(child_args)

    child.spawn()

    # Handle window resize
    sync_winsize(child.master_fd)
    signal.signal(signal.SIGWINCH, lambda s, f: sync_winsize(child.master_fd))

    try:
        await main_loop(child, voice)
    finally:
        child.kill()

if __name__ == "__main__":
    asyncio.run(main())