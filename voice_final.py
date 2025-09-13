#!/usr/bin/env python3
"""
Voice wrapper using AssemblyAI v3 streaming API.
Based on official AssemblyAI example.
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
from assemblyai.streaming.v3 import (
    BeginEvent,
    StreamingClient,
    StreamingClientOptions,
    StreamingError,
    StreamingEvents,
    StreamingParameters,
    StreamingSessionParameters,
    TerminationEvent,
    TurnEvent,
)
import logging
from typing import Type

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
# PTY child
# ===============================

class PTYChild:
    def __init__(self, argv):
        self.argv = argv
        self.master_fd = None
        self.child_pid = None

    def spawn(self):
        print(f"Spawning: {' '.join(self.argv)}")
        self.master_fd, slave_fd = pty.openpty()
        pid = os.fork()
        if pid == 0:
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
                print(f"Child error: {e}")
                os._exit(127)
        else:
            self.child_pid = pid
            os.close(slave_fd)
            flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def send(self, data: bytes):
        if self.master_fd and data:
            try:
                os.write(self.master_fd, data)
                print(f"📤 Sent to child: {repr(data.decode('utf-8', errors='replace'))}")
            except Exception as e:
                print(f"Error sending to child: {e}")

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
# Voice Control
# ===============================

class VoiceController:
    def __init__(self):
        self.client = None
        self.is_listening = False
        self.transcript_buffer = ""

        # Set up API key
        api_key = "f5115c8df6de446999a096a3edee97cb"
        aai.settings.api_key = api_key
        print(f"AssemblyAI configured with key: {api_key[:10]}...")

    def on_begin(self, client: Type[StreamingClient], event: BeginEvent):
        print(f"🚀 Session started: {event.id}")

    def on_turn(self, client: Type[StreamingClient], event: TurnEvent):
        print(f"💬 '{event.transcript}' (end_of_turn={event.end_of_turn})")

        # Add to buffer
        if event.transcript:
            self.transcript_buffer += event.transcript + " "

        # Enable formatting if not already formatted
        if event.end_of_turn and not event.turn_is_formatted:
            params = StreamingSessionParameters(format_turns=True)
            client.set_params(params)

    def on_terminated(self, client: Type[StreamingClient], event: TerminationEvent):
        print(f"🔚 Session terminated: {event.audio_duration_seconds}s processed")

    def on_error(self, client: Type[StreamingClient], error: StreamingError):
        print(f"❌ Error: {error}")

    def start_listening(self):
        if self.is_listening:
            return

        print("🎤 Starting listening...")
        self.transcript_buffer = ""

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
            print("[LISTENING]")

            # Start streaming in background thread
            def stream_audio():
                try:
                    self.client.stream(aai.extras.MicrophoneStream(sample_rate=16000))
                except Exception as e:
                    print(f"Streaming error: {e}")
                    self.is_listening = False

            self.stream_thread = threading.Thread(target=stream_audio, daemon=True)
            self.stream_thread.start()

        except Exception as e:
            print(f"Error starting transcription: {e}")
            self.is_listening = False

    def stop_listening(self):
        if not self.is_listening:
            return

        print("🛑 Stopping listening...")
        self.is_listening = False

        if self.client:
            try:
                self.client.disconnect(terminate=True)
            except Exception as e:
                print(f"Error disconnecting: {e}")
            self.client = None

        return self.transcript_buffer.strip()

# ===============================
# Main
# ===============================

def sync_winsize(pty_master_fd: int):
    try:
        s = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00'*8)
        rows, cols, _, _ = struct.unpack('HHHH', s)
        if rows > 0 and cols > 0:
            fcntl.ioctl(pty_master_fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    except:
        pass

async def main_loop(child: PTYChild):
    stdin_fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        print("Must run in a TTY")
        return

    old_attrs = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)

    voice = VoiceController()

    print("=== Voice Wrapper (AssemblyAI v3) ===")
    print("Ctrl+G: Toggle listening (start/stop & send)")
    print("Ctrl+C: Exit")
    print()
    print("[idle]")

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

                if CTRL_HOTKEY in data:
                    data = data.replace(CTRL_HOTKEY, b"")

                    if voice.is_listening:
                        # Stop and send
                        transcript = voice.stop_listening()

                        if transcript:
                            text_to_send = transcript + "\n"
                            child.send(text_to_send.encode())
                        else:
                            print("📭 No text captured")

                        print("[idle]")
                    else:
                        # Start listening
                        voice.start_listening()

                if data:
                    child.send(data)

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        if voice.is_listening:
            voice.stop_listening()
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)

async def main():
    if len(sys.argv) < 2:
        print("Usage: python voice_final.py <command> [args...]")
        print("Example: python voice_final.py /bin/cat")
        sys.exit(1)

    argv = sys.argv[1:]
    child = PTYChild(argv)
    child.spawn()

    sync_winsize(child.master_fd)
    signal.signal(signal.SIGWINCH, lambda s, f: sync_winsize(child.master_fd))

    try:
        await main_loop(child)
    finally:
        child.kill()

if __name__ == "__main__":
    asyncio.run(main())