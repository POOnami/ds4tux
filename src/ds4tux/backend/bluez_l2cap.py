from __future__ import annotations
import os
import time
import struct
import socket
import select
import logging
import threading
from typing import Optional

from ds4tux.device import (
    DS4Report, parse_input_report,
    build_bt_output_report_raw,
    build_bt_output_report_genuine,
    REPORT_ID_USB, REPORT_ID_BT,
    BT_OUTPUT_REPORT_SIZE,
)
logger = logging.getLogger("ds4tux.l2cap")

# ── Reconnection strategy ───────────────────────────────────────────────
#
# The DS4 reconnects via outgoing L2CAP to PSM 0x11/0x13 when the user
# presses PS.  Our approach is simple: keep trying l2cap_connect() in a
# loop.  The connect call blocks until either the DS4 responds to our
# page scan or the timeout expires.
#
# No BlueZ block/unblock cycle needed — outgoing l2cap_connect() to the
# DS4's PSM 0x11 creates an independent L2CAP channel from BlueZ's
# incoming listener on PSM 0x11.  Both coexist without interference.
#
# For first-time pairing, BlueZ's pair_ds4() is used to establish a
# Bluetooth link key.  After pairing, the controller is disconnected
# from BlueZ and our reconnect loop picks up PS button presses.
#
# Timing:
#   connect_loop(): try once with 3.0 s timeout (catches first PS press),
#                   fall back to background reconnector.
#   _start_reconnect(): try with 2.0 s connect timeout every 3.0 s.
#                       2.0 s covers the DS4's page-scan interval (1.28 s).
# ─────────────────────────────────────────────────────────────────────────

HID_PSM_INTR = 0x13
HID_PSM_CTRL = 0x11

HIDP_HEADER_DATA_INPUT = 0xa0
HIDP_HEADER_SET_REPORT = bytes([0x52, 0x11])

KEEPALIVE_INTERVAL = 3.0


class L2capBtDevice:
    def __init__(self, address: str, intr_sock: socket.socket, ctrl_sock: socket.socket,
                 hex_dump: bool = False):
        self.address = address
        self._intr = intr_sock
        self._ctrl = ctrl_sock
        self._ctrl.setblocking(False)
        self._intr.setblocking(False)
        self._dead = False
        self._report = DS4Report()
        self._on_report: Optional[callable] = None
        self._last_write = 0.0
        self._last_r = 0
        self._last_g = 0
        self._last_b = 0
        self._hex_dump = hex_dump
        self.device_mode = "auto"
        self.detected_copycat = None

    @property
    def report(self):
        return self._report

    @report.setter
    def report(self, value):
        self._report = value

    @property
    def name(self) -> str:
        return self.address

    @property
    def copycat(self) -> bool:
        if self.device_mode == "clone":
            return True
        return False  # auto, genuine → treat as non-copycat

    @property
    def is_bluetooth(self) -> bool:
        return True

    def set_report_callback(self, cb: callable):
        self._on_report = cb

    def fileno(self) -> Optional[int]:
        if self._dead or not self._intr:
            return None
        return self._intr.fileno()

    def read_report(self) -> Optional[DS4Report]:
        if self._dead or not self._intr:
            return None

        if self._ctrl:
            try:
                while True:
                    ctrl_data = self._ctrl.recv(512)
                    if not ctrl_data:
                        self._dead = True
                        return None
            except (BlockingIOError, socket.timeout):
                pass
            except (OSError, IOError) as e:
                logger.debug("L2CAP ctrl drain error: %s", e)
                self._dead = True
                return None

        try:
            data = self._intr.recv(512)
        except (socket.timeout, BlockingIOError):
            return None
        except (OSError, IOError) as e:
            logger.debug("L2CAP intr read error: %s", e)
            self._dead = True
            return None

        if not data:
            self._dead = True
            return None

        if self._hex_dump:
            hex_str = " ".join(f"{b:02x}" for b in data[:48])
            logger.info("HEX: %s", hex_str)

        if data[0] == 0xa0:
            # Raw L2CAP HIDP: [0xa0, report_id, hidp_status, seq, left_x, ...]
            # BlueZ profile:  [report_id, hidp_status, seq, left_x, ...]
            # Both have the HIDP status byte already — just strip the HIDP header.
            data = data[1:]
        elif data[0] == 0xa1:
            # HIDP DATA header: [0xa1, optional_param, report_id, ...]
            data = data[1:] if len(data) > 1 else b''
            # Skip optional HIDP parameter byte (e.g. 0xff flush timeout)
            # between the header and the report ID
            while data and data[0] not in (REPORT_ID_BT, REPORT_ID_USB):
                data = data[1:]

        if not data:
            return None

        is_bt = True
        report = parse_input_report(data, is_bluetooth=is_bt, copycat=self.copycat)
        if report:
            self._report = report
            if self._on_report:
                self._on_report(report)
        return report

    def _send_ctrl(self, pkt: bytes) -> bool:
        try:
            self._ctrl.setblocking(True)
            self._ctrl.settimeout(1.0)
            self._ctrl.sendall(pkt)
            return True
        except (OSError, IOError) as e:
            logger.debug("ctrl send failed: %s", e)
            self._dead = True
            return False
        finally:
            self._ctrl.setblocking(False)

    def _output_body(self, r: int, g: int, b: int,
                     blink_on: int = 0, blink_off: int = 0,
                     motor_left: int = 0, motor_right: int = 0) -> bytes:
        if self.device_mode == "genuine":
            return build_bt_output_report_genuine(r, g, b, blink_on, blink_off, motor_left, motor_right)
        return build_bt_output_report_raw(r, g, b, blink_on, blink_off, motor_left, motor_right)

    def write_output(self, r: int = 0, g: int = 0, b: int = 128,
                     blink_on: int = 0, blink_off: int = 0,
                     motor_left: int = 0, motor_right: int = 0) -> bool:
        if self._dead or not self._ctrl:
            return False
        body = self._output_body(r, g, b, blink_on, blink_off, motor_left, motor_right)
        if self._send_ctrl(HIDP_HEADER_SET_REPORT + body):
            self._last_write = time.time()
            self._last_r = r
            self._last_g = g
            self._last_b = b
            return True
        return False

    def keepalive(self, r: int = None, g: int = None, b: int = None):
        if self._dead or not self._ctrl:
            return
        now = time.time()
        if now - self._last_write >= KEEPALIVE_INTERVAL:
            r = self._last_r if r is None else r
            g = self._last_g if g is None else g
            b = self._last_b if b is None else b
            if self._send_ctrl(HIDP_HEADER_SET_REPORT + self._output_body(r, g, b)):
                self._last_write = now

    def update_battery(self):
        pass

    def close(self):
        self._dead = True
        for sock in (self._intr, self._ctrl):
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
        self._intr = None
        self._ctrl = None


def _drain_sock(sock: socket.socket):
    sock.setblocking(False)
    try:
        while True:
            d = sock.recv(512)
            if not d:
                break
    except (BlockingIOError, socket.timeout):
        pass
    except Exception:
        pass


class L2capBtBackend:
    def __init__(self, address: str = "0C:30:66:42:86:18", hex_dump: bool = False):
        self._address = address
        self._device: Optional[L2capBtDevice] = None
        self._on_connect: Optional[callable] = None
        self._on_disconnect: Optional[callable] = None
        self._reconnect = True
        self._reconnect_thread: Optional[threading.Thread] = None
        self._running = True
        self._status: str = "disconnected"
        self._on_status: Optional[callable] = None
        self._hex_dump = hex_dump

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str):
        self._status = value
        if self._on_status:
            try:
                self._on_status(value)
            except Exception:
                pass

    @property
    def name(self) -> str:
        return "l2cap"

    def set_callbacks(self, on_connect=None, on_disconnect=None):
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

    def setup(self):
        logger.info("L2CAP BT backend initialized (outgoing connect)")

    def _init_led(self, dev):
        body = build_bt_output_report_raw(255, 80, 0)
        pkt = HIDP_HEADER_SET_REPORT + body
        if dev._send_ctrl(pkt):
            time.sleep(0.05)
            dev._send_ctrl(pkt)
            logger.debug("init LED: orange sent")

    def _init_dev(self, dev: L2capBtDevice) -> L2capBtDevice:
        self._init_led(dev)
        self._device = dev
        if self._on_connect:
            self._on_connect(dev)
        return dev

    def _connect_l2cap(self, psm: int, timeout: float = 5.0) -> Optional[socket.socket]:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        sock.settimeout(timeout)
        try:
            sock.connect((self._address, psm))
            logger.debug("L2CAP PSM 0x%02x connected to %s", psm, self._address)
            return sock
        except (OSError, IOError) as e:
            logger.debug("L2CAP PSM 0x%02x connect failed: %s", psm, e)
            sock.close()
            return None

    def _grab_l2cap(self, timeout: float = 3.0) -> Optional[L2capBtDevice]:
        ctrl = self._connect_l2cap(HID_PSM_CTRL, timeout=timeout)
        if not ctrl:
            return None
        intr = self._connect_l2cap(HID_PSM_INTR, timeout=timeout)
        if not intr:
            ctrl.close()
            return None
        _drain_sock(ctrl)
        dev = L2capBtDevice(self._address, intr, ctrl, hex_dump=self._hex_dump)
        self._init_led(dev)
        logger.info("DS4 connected")
        return dev

    def connect_loop(self) -> bool:
        # Phase 1: try L2CAP directly — works if a BT link key exists
        # at the kernel level, regardless of BlueZ DB state.
        dev = self._grab_l2cap(timeout=2.0)
        if dev:
            self._device = dev
            self.status = "connected"
            if self._on_connect:
                self._on_connect(dev)
            return True

        # Phase 2: BlueZ-mediated pairing if device isn't in BlueZ DB.
        dev_path = None
        was_connected = False
        try:
            from ds4tux.backend.bluez_client import BluezClient
            client = BluezClient()
            for path, props in client.get_devices():
                if props.get("Address", "").upper() == self._address.upper():
                    dev_path = path
                    was_connected = props.get("Connected", False)
                    break

            if not dev_path:
                self.status = "pairing"
                logger.info("DS4 not in BlueZ — scanning for pairing mode")
                paired_path = client.pair_ds4(timeout=30.0)
                if paired_path:
                    dev_path = paired_path
                    was_connected = client.get_device_props(dev_path).get("Connected", False)
                    logger.info("DS4 paired via BlueZ")
            elif not client.get_device_props(dev_path).get("Paired", False):
                self.status = "pairing"
                logger.info("DS4 in BlueZ but unpaired — re-pairing")
                client.remove_device(dev_path)
                dev_path = client.pair_ds4(timeout=30.0)
                if dev_path:
                    was_connected = client.get_device_props(dev_path).get("Connected", False)

            if dev_path and was_connected:
                self.status = "connecting"
                client.disconnect(dev_path)
                time.sleep(0.5)
        except Exception as e:
            logger.debug("BlueZ error: %s", e)

        # Skip the 3s L2CAP grab if the device wasn't connected via BlueZ
        # (no point blocking for a timeout — go straight to reconnect loop)
        if was_connected or dev_path is None:
            dev = self._grab_l2cap(timeout=3.0)
            if dev:
                self._device = dev
                self.status = "connected"
                if self._on_connect:
                    self._on_connect(dev)
                return True
        elif dev_path:
            logger.info("DS4 not connected via BlueZ — starting background reconnect")

        if self._reconnect and self._running:
            self.status = "reconnecting"
            self._start_reconnect()
        return False

    def check_disconnections(self):
        if self._device and self._device._dead:
            dev = self._device
            self._device = None
            self.status = "disconnected"
            logger.info("DS4 disconnected: %s", dev.address)
            if self._on_disconnect:
                self._on_disconnect(dev)
            dev.close()
            if self._reconnect and self._running:
                self._start_reconnect()

    def poll_device_events(self, timeout: float = 1.0) -> bool:
        return True

    def keepalive(self, r: int = 0, g: int = 0, b: int = 0):
        if self._device and not self._device._dead:
            self._device.keepalive(r, g, b)

    def _start_reconnect(self):
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self.status = "reconnecting"

        def _loop():
            while self._running and self._reconnect and not self._device:
                ctrl = self._connect_l2cap(HID_PSM_CTRL, timeout=2.0)
                if not ctrl:
                    time.sleep(1.0)
                    continue

                ctrl.setblocking(False)

                # Hold CTRL open and poll INTR — don't close CTRL on failure
                # so the DS4's internal INTR readiness timer keeps running.
                while self._running and self._reconnect and not self._device:
                    try:
                        ctrl.recv(1, socket.MSG_PEEK)
                    except BlockingIOError:
                        pass
                    except OSError:
                        break

                    intr = self._connect_l2cap(HID_PSM_INTR, timeout=0.5)
                    if intr:
                        _drain_sock(ctrl)
                        dev = L2capBtDevice(self._address, intr, ctrl, hex_dump=self._hex_dump)
                        self._device = dev
                        self.status = "connected"
                        self._init_dev(dev)
                        return

                ctrl.close()
                time.sleep(1.0)

        self._reconnect_thread = threading.Thread(target=_loop, daemon=True)
        self._reconnect_thread.start()

    def set_enabled(self, enabled: bool):
        if enabled:
            self._reconnect = True
            self._running = True
            self._start_reconnect()
        else:
            self._reconnect = False
            if self._device:
                if self._on_disconnect:
                    self._on_disconnect(self._device)
                self._device.close()
                self._device = None

    def stop(self):
        self._running = False
        self._reconnect = False
        if self._device:
            if self._on_disconnect:
                self._on_disconnect(self._device)
            self._device.close()
            self._device = None
