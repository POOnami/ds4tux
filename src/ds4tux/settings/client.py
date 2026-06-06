"""
Client library for connecting to the ds4tux settings socket.
"""

from __future__ import annotations
import os
import json
import socket
import threading
from typing import Optional, Callable

SOCKET_PATH = "/run/ds4tux/control.sock"


class SettingsClient:
    def __init__(self):
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._on_state: Optional[Callable] = None

    def connect(self) -> bool:
        if not os.path.exists(SOCKET_PATH):
            return False
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(SOCKET_PATH)
            self._sock.settimeout(0.5)
            self._running = True
            self._thread = threading.Thread(target=self._reader, daemon=True)
            self._thread.start()
            return True
        except (OSError, IOError, ConnectionRefusedError):
            self._sock = None
            return False

    def _reader(self):
        buf = b""
        while self._running and self._sock:
            try:
                data = self._sock.recv(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        try:
                            msg = json.loads(line)
                            if msg.get("type") == "state" and self._on_state:
                                self._on_state(msg)
                        except json.JSONDecodeError:
                            pass
            except socket.timeout:
                continue
            except (OSError, IOError):
                break

    def send_command(self, cmd: dict):
        if not self._sock:
            return
        try:
            data = json.dumps(cmd).encode() + b"\n"
            self._sock.sendall(data)
        except (OSError, IOError):
            pass

    def close(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
