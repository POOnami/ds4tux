"""
Unix socket server for TUI ↔ daemon communication.
"""

from __future__ import annotations
import os
import json
import time
import socket
import threading
import logging
from typing import Optional

logger = logging.getLogger("ds4tux.settings")

SOCKET_PATH = "/run/ds4tux/control.sock"


class SettingsServer:
    def __init__(self):
        self._controller = None
        self._backend = None
        self._clients: list[socket.socket] = []
        self._clients_lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_broadcast = 0.0
        self._controllers_dict: Optional[dict] = None

    def set_controllers_dict(self, controllers: dict):
        self._controllers_dict = controllers

    def set_backend(self, backend):
        self._backend = backend
        if hasattr(backend, '_on_status'):
            backend._on_status = lambda s: self._broadcast_state()
        self._broadcast_state()

    def set_controller(self, ctrl):
        self._controller = ctrl
        ctrl.add_report_callback(self._on_report)
        self._broadcast_state()

    def _on_report(self, device_id: str, report):
        self._broadcast_state()

    def _broadcast_state(self):
        # Throttle to ~30 Hz to prevent socket buffer buildup when the TUI
        # reader thread falls behind (GIL contention with _draw()).
        now = time.time()
        if now - self._last_broadcast < 0.033:
            return
        self._last_broadcast = now
        state = self._build_state()
        payload = json.dumps(state).encode() + b"\n"
        with self._clients_lock:
            if not self._clients:
                return
            dead = []
            for sock in self._clients:
                try:
                    sock.sendall(payload)
                except (OSError, IOError):
                    dead.append(sock)
            for sock in dead:
                self._clients.remove(sock)
        for sock in dead:
            try:
                sock.close()
            except Exception:
                pass

    def _build_state(self) -> dict:
        ctrl = self._controller
        if not ctrl or not ctrl.device:
            status = ctrl._status if ctrl else "disconnected"
            enabled = ctrl._enabled if ctrl else True
            if hasattr(self._backend, 'status'):
                status = self._backend.status
            usb_detected = getattr(self._backend, 'usb_detected_no_sudo', False)
            return {"type": "state", "connected": False, "status": status, "enabled": enabled,
                    "usb_detected_no_sudo": usb_detected}

        report = ctrl.device.report
        return {
            "type": "state",
            "connected": True,
            "battery": report.battery_percent,
            "charging": report.is_charging,
            "copycat": getattr(ctrl.device, "copycat", False),
            "detected_copycat": getattr(ctrl.device, "detected_copycat", getattr(ctrl.device, "copycat", False)),
            "device_mode": ctrl.config.get("device_mode", "auto"),
            "mac": getattr(ctrl.device, "address", ""),
            "led": {
                "r": ctrl.led._target_r,
                "g": ctrl.led._target_g,
                "b": ctrl.led._target_b,
                "brightness": ctrl.led._brightness,
            },
            "enabled": ctrl._enabled,
            "status": ctrl._status,
            "bluetooth": getattr(ctrl.device, "is_bluetooth", True),
            "mapping": ctrl._mapping,
            "buttons": {
                "cross": report.button_cross,
                "circle": report.button_circle,
                "square": report.button_square,
                "triangle": report.button_triangle,
                "l1": report.button_l1,
                "r1": report.button_r1,
                "l2": report.button_l2,
                "r2": report.button_r2,
                "l3": report.button_l3,
                "r3": report.button_r3,
                "share": report.button_share,
                "options": report.button_options,
                "ps": report.button_ps,
                "trackpad": report.button_trackpad,
            },
            "axes": {
                "lx": report.left_analog_x,
                "ly": report.left_analog_y,
                "rx": report.right_analog_x,
                "ry": report.right_analog_y,
                "l2": report.l2_analog,
                "r2": report.r2_analog,
                "dpad": report.dpad,
            },
            "gyro": {
                "raw": [report.gyro_raw_x, report.gyro_raw_y, report.gyro_raw_z],
                "filtered": [report.gyro_filtered_x, report.gyro_filtered_y, report.gyro_filtered_z],
            },
            "accel": [report.accel_x, report.accel_y, report.accel_z],
            "touchpad": [
                {"active": report.trackpad_touch1, "x": report.trackpad_x1, "y": report.trackpad_y1},
                {"active": report.trackpad_touch2, "x": report.trackpad_x2, "y": report.trackpad_y2},
            ],
            "latency": ctrl.latency.get_stats(),
        }

    def handle_command(self, cmd: dict):
        cmd_type = cmd.get("type")

        if cmd_type == "set_enabled":
            enabled = cmd.get("enabled", True)
            if self._controller:
                self._controller.set_enabled(enabled)
            elif self._backend and hasattr(self._backend, 'set_enabled'):
                self._backend.set_enabled(enabled)
            self._broadcast_state()
            return

        if not self._controller:
            return

        if cmd_type == "set_led":
            self._controller.led.set_color(
                cmd.get("r", 0),
                cmd.get("g", 0),
                cmd.get("b", 128),
            )
            if "brightness" in cmd:
                self._controller.led.set_brightness(cmd["brightness"])
            self._controller.led.update()

        elif cmd_type == "set_mapping":
            self._controller._mapping = cmd.get("mapping", "xpad")
            self._controller.uinput_mgr.remove(self._controller.device_id)
            self._controller.uinput_dev = self._controller.uinput_mgr.get_or_create(
                self._controller.device_id, mapping=self._controller._mapping,
                gyro=self._controller._gyro,
            )

        elif cmd_type == "blink":
            self._controller.led.set_blink(
                cmd.get("on_ms", 200),
                cmd.get("off_ms", 200),
                duration=cmd.get("duration", 2.0),
            )
            self._controller.led.update()

        elif cmd_type == "set_copycat":
            if self._controller:
                mode = cmd.get("mode", "auto")
                mode = {"yes": "clone", "no": "genuine"}.get(mode, mode)
                self._controller.config["device_mode"] = mode
                if hasattr(self._controller, 'apply_config'):
                    self._controller.apply_config()
                from ds4tux.config import write_config, save_device_info
                write_config(self._controller.config)
                dev = self._controller.device
                if dev:
                    mac = getattr(dev, "address", None)
                    if mac and mode in ("clone", "genuine"):
                        save_device_info(mac, {"detected_copycat": mode == "clone"})
                self._broadcast_state()

        elif cmd_type == "reload_config":
            if not self._controller:
                return
            from ds4tux.config import write_config
            new_cfg = cmd.get("config")
            if new_cfg:
                merged = dict(self._controller.config)
                merged.update(new_cfg)
                self._controller.config = merged
                if hasattr(self._controller, 'apply_config'):
                    self._controller.apply_config()
                write_config(merged)
            else:
                from ds4tux.config import load_config
                new_cfg = load_config()
                if self._controller:
                    self._controller.config = new_cfg
                    if hasattr(self._controller, 'apply_config'):
                        self._controller.apply_config()
            self._broadcast_state()

    def start(self):
        self._running = True
        os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._server.bind(SOCKET_PATH)
        except OSError:
            os.unlink(SOCKET_PATH)
            self._server.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o777)
        self._server.listen(5)
        self._server.settimeout(0.5)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        last_rebroadcast = time.time()
        while self._running:
            try:
                conn, _ = self._server.accept()
                conn.settimeout(0.5)
                with self._clients_lock:
                    self._clients.append(conn)
                t = threading.Thread(target=self._handle_client, args=(conn,))
                t.daemon = True
                t.start()
            except socket.timeout:
                pass
            except Exception as e:
                logger.debug("Socket accept error: %s", e)
            now = time.time()
            if now - last_rebroadcast >= 2.0:
                self._broadcast_state()
                last_rebroadcast = now

    def _handle_client(self, conn: socket.socket):
        self._broadcast_state()
        buf = b""
        while self._running:
            try:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        try:
                            cmd = json.loads(line)
                            self.handle_command(cmd)
                        except json.JSONDecodeError:
                            pass
            except socket.timeout:
                continue
            except (OSError, IOError):
                break

        try:
            conn.close()
        except Exception:
            pass

    def stop(self):
        self._running = False
        with self._clients_lock:
            for sock in self._clients:
                try:
                    sock.close()
                except Exception:
                    pass
            self._clients.clear()
        try:
            self._server.close()
        except Exception:
            pass
        try:
            os.unlink(SOCKET_PATH)
        except Exception:
            pass
