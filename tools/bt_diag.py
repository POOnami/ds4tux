#!/usr/bin/env python3
"""
BT diagnostic: pair DS4, connect, and determine the data path.

Tests three scenarios:
1. Profile1 (userspace) — register BlueZ profile, wait for NewConnection
2. Kernel hidp — load hidp, wait for /dev/hidraw* to appear
3. Raw L2CAP — connect directly via AF_BLUETOOTH socket

Run: python3 -m tools.bt_diag [--pair]
"""

from __future__ import annotations
import os
import sys
import time
import errno
import select
import socket
import struct
import logging
import threading
from typing import Optional

# Add project to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi
gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import GLib, Gio

from ds4tux.backend.bluez_client import BluezClient, HID_UUID, HID_PSM_CTRL, HID_PSM_INTR
from ds4tux.device import parse_input_report, BT_REPORT_SIZE

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bt_diag")

PROFILE_PATH = "/ds4tux/diag_profile"

PROFILE_XML = """
<node>
  <interface name="org.bluez.Profile1">
    <method name="NewConnection">
      <arg type="o" name="device" direction="in"/>
      <arg type="h" name="fd" direction="in"/>
      <arg type="a{sv}" name="fd_properties" direction="in"/>
    </method>
    <method name="Release"/>
    <method name="RequestDisconnection">
      <arg type="o" name="device" direction="in"/>
    </method>
  </interface>
</node>
"""

HIDRAW_BASE = "/dev/hidraw"


class Diagnostic:
    def __init__(self):
        self.client = BluezClient()
        self.bus = None
        self.profile_reg_id = 0
        self.newconn_event = threading.Event()
        self.conn_fd: Optional[int] = None
        self.conn_device: Optional[str] = None

    def register_profile(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        node = Gio.DBusNodeInfo.new_for_xml(PROFILE_XML)
        self.profile_reg_id = self.bus.register_object(
            PROFILE_PATH, node.interfaces[0],
            self._on_method_call, None, None,
        )
        opts = {
            "Name": GLib.Variant("s", "DS4Tux-Diag"),
            "Role": GLib.Variant("s", "server"),
            "AutoConnect": GLib.Variant("b", True),
        }
        self.bus.call_sync(
            "org.bluez", "/org/bluez", "org.bluez.ProfileManager1",
            "RegisterProfile",
            GLib.Variant("(osa{sv})", (PROFILE_PATH, HID_UUID, opts)),
            None, Gio.DBusCallFlags.NONE, -1, None,
        )
        logger.info("Profile1 registered for HID UUID")

    def unregister_profile(self):
        try:
            self.bus.call_sync(
                "org.bluez", "/org/bluez", "org.bluez.ProfileManager1",
                "UnregisterProfile",
                GLib.Variant("(s)", (PROFILE_PATH,)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except Exception:
            pass
        if self.profile_reg_id:
            self.bus.unregister_object(self.profile_reg_id)

    def _on_method_call(self, connection, sender, obj_path,
                        iface, method, params, invocation):
        if method == "NewConnection":
            dev_path, fd, props = params.unpack()
            self.conn_device = str(dev_path)
            self.conn_fd = fd
            logger.info(">>> NewConnection: device=%s fd=%d", self.conn_device, fd)
            self.newconn_event.set()
            invocation.return_value(None)
        elif method == "Release":
            logger.info("Profile released")
            invocation.return_value(None)
        elif method == "RequestDisconnection":
            logger.info("Disconnection requested")
            invocation.return_value(None)

    def check_existing_hidraw(self) -> Optional[int]:
        """Find hidraw nodes for this DS4. Returns the hidraw path or None."""
        existing = set()
        for f in os.listdir("/dev"):
            if f.startswith("hidraw"):
                existing.add(f)

        # Wait a moment for new hidraw to appear
        time.sleep(2)

        for f in os.listdir("/dev"):
            if f.startswith("hidraw") and f not in existing:
                path = f"/dev/{f}"
                logger.info(">>> New hidraw device: %s", path)
                return path
        return None

    def read_hidraw_raw(self, hidraw_path: str, duration: float = 5.0):
        """Read raw HID reports from a hidraw node."""
        try:
            fd = os.open(hidraw_path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            logger.error("Cannot open %s: %s", hidraw_path, e)
            return

        logger.info("Reading from %s for %.1f seconds...", hidraw_path, duration)
        start = time.time()
        count = 0
        while time.time() - start < duration:
            try:
                data = os.read(fd, 78)
                if data:
                    count += 1
                    r = parse_input_report(data, is_bluetooth=(len(data) > 64))
                    if r:
                            btn_str = "".join([
                                "X" if r.button_cross else ".",
                                "C" if r.button_circle else ".",
                                "S" if r.button_square else ".",
                                "T" if r.button_triangle else ".",
                            ])
                            logger.info("Report #%d: left=(%d,%d) right=(%d,%d) dp=%d buttons=%s raw_len=%d",
                                         count,
                                         r.left_analog_x, r.left_analog_y,
                                         r.right_analog_x, r.right_analog_y,
                                         r.dpad, btn_str, len(data))
            except (OSError, IOError) as e:
                if e.errno != errno.EAGAIN:
                    logger.error("Read error: %s", e)
                    break
                time.sleep(0.01)
        os.close(fd)
        logger.info("Read %d reports from %s", count, hidraw_path)

    def test_l2cap_direct(self, address: str):
        """Try direct L2CAP connection to DS4 on PSM 0x13 (interrupt)."""
        logger.info("Attempting direct L2CAP connection to %s...", address)
        try:
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
            sock.settimeout(5.0)

            # Check and set L2CAP MTU for full reports
            SOL_L2CAP = 6
            L2CAP_OPTIONS = 1
            try:
                opts = sock.getsockopt(SOL_L2CAP, L2CAP_OPTIONS, 7)
                omtu, imtu, flush_to, mode = struct.unpack('<HHHB', opts)
                logger.info("L2CAP defaults: omtu=%d imtu=%d flush_to=%d mode=%d",
                            omtu, imtu, flush_to, mode)
            except Exception:
                pass

            # Try setting larger MTU before connect (might not work here)
            try:
                # struct l2cap_options: omtu, imtu, flush_to(16 each), mode(8)
                new_opts = struct.pack('<HHHB', 128, 672, 0, 0)
                sock.setsockopt(SOL_L2CAP, L2CAP_OPTIONS, new_opts)
            except Exception:
                pass

            sock.connect((address, HID_PSM_INTR))
            logger.info(">>> L2CAP connection established on PSM 0x%02x", HID_PSM_INTR)

            # Check MTU after connect
            try:
                opts = sock.getsockopt(SOL_L2CAP, L2CAP_OPTIONS, 7)
                omtu, imtu, flush_to, mode = struct.unpack('<HHHB', opts)
                logger.info("L2CAP after connect: omtu=%d imtu=%d", omtu, imtu)
            except Exception:
                pass

            sock.settimeout(1.0)
            start = time.time()
            count = 0
            max_len = 0
            while time.time() - start < 8.0:
                try:
                    data = sock.recv(512)  # larger buffer to handle full MTU
                    if data:
                        count += 1
                        max_len = max(max_len, len(data))
                        if len(data) > 30:
                            logger.info("L2CAP recv #%d: %d bytes, first bytes: %s",
                                         count, len(data), data[:16].hex())
                            # Try to parse as DS4 BT HID report
                            if len(data) > 2 and data[0] == 0xa1:
                                # Strip HIDP header
                                hid_report = data[1:]
                                if hid_report and hid_report[0] == 0x11:
                                    r = parse_input_report(hid_report, is_bluetooth=True)
                                    if r:
                                        logger.info("  Parsed: left=(%d,%d) dp=%d cross=%s",
                                                     r.left_analog_x, r.left_analog_y,
                                                     r.dpad, r.button_cross)
                                elif hid_report and hid_report[0] == 0x01:
                                    # USB format over BT (copycat quirk)
                                    # HIDP header stripped, but report ID 0x01 means USB format
                                    full_report = bytearray(64)
                                    full_report[0] = 0x01
                                    # Copy what we have so far
                                    copy_len = min(len(hid_report), 64)
                                    full_report[:copy_len] = hid_report[:copy_len]
                                    r = parse_input_report(bytes(full_report), is_bluetooth=False)
                                    if r:
                                        logger.info("  Parsed (USB-over-BT): left=(%d,%d) dp=%d",
                                                     r.left_analog_x, r.left_analog_y, r.dpad)
                        elif len(data) < 12:
                            # Short packet - log hex
                            logger.debug("L2CAP short recv #%d: %d bytes: %s",
                                          count, len(data), data.hex())
                        else:
                            logger.debug("L2CAP recv #%d: %d bytes: %s",
                                          count, len(data), data.hex())
                except socket.timeout:
                    continue
            logger.info("L2CAP done: %d packets, max packet size=%d", count, max_len)
            sock.close()
            return max_len > 30
        except Exception as e:
            logger.warning("L2CAP direct connection failed: %s", e)
            return False

    def run(self, do_pair: bool = False, address: Optional[str] = None):
        logger.info("=== BT Diagnostic ===")

        # Step 1: Register BlueZ Profile1
        logger.info("\n--- Step 1: Register Profile1 ---")
        self.register_profile()

        # Step 2: Pair / find DS4
        logger.info("\n--- Step 2: Pair / Find DS4 ---")
        if do_pair:
            logger.info("Put DS4 in pairing mode (PS+Share). Scanning...")
            dev_path = self.client.pair_ds4(timeout=60.0)
        else:
            dev_path = None
            if address:
                pair_dev = None
                for path, props in self.client.get_devices():
                    if props.get("Address", "").upper() == address.upper():
                        pair_dev = path
                        break
                if pair_dev:
                    props = dict(self.client.get_device_props(pair_dev))
                    if not props.get("Paired", False):
                        logger.info("Device found but not paired. Pairing...")
                        self.client.pair(pair_dev)
                        self.client.trust(pair_dev)
                    dev_path = pair_dev
                else:
                    found = self.client.find_ds4()
                    dev_path = found[0] if found else None

            if not dev_path:
                found = self.client.find_ds4()
                dev_path = found[0] if found else None

        if dev_path:
            props = self.client.get_device_props(dev_path)
            addr = props.get("Address", "?")
            name = props.get("Name", "?")
            logger.info("Found device: %s (%s) at %s", name, addr, dev_path)

            if not props.get("Paired", False):
                logger.info("Pairing...")
                self.client.pair(dev_path)
                self.client.trust(dev_path)
                time.sleep(1)

            if props.get("Connected", False):
                logger.info("DS4 already connected via BlueZ")
        else:
            logger.warning("No DS4 device found. Is it in pairing mode?")
            logger.warning("You can specify --address 18:86:42:66:30:00")
            self.unregister_profile()
            return

        # Step 3: Wait for DS4 to connect (PS button)
        logger.info("\n--- Step 3: Wait for DS4 ---")
        logger.info("Power on DS4 (press PS button). Waiting for BT connection...")
        for _ in range(30):
            props = self.client.get_device_props(dev_path)
            if props.get("Connected", False):
                logger.info("DS4 connected via BlueZ!")
                break
            time.sleep(1.0)
        else:
            logger.warning("DS4 not connected via BlueZ after 30s. Trying L2CAP anyway...")

        # Step 4: Try direct L2CAP (bypasses BlueZ ConnectProfile entirely)
        logger.info("\n--- Step 4: Direct L2CAP connection ---")
        l2cap_ok = self.test_l2cap_direct(addr)
        if l2cap_ok:
            logger.info("L2CAP test passed!")
        else:
            logger.warning("L2CAP test failed")

        # Step 5: Quick BlueZ ConnectProfile test (for completeness)
        logger.info("\n--- Step 5: BlueZ ConnectProfile test ---")
        self.client.connect_profile(dev_path)
        self.newconn_event.wait(timeout=5.0)
        if self.newconn_event.is_set():
            logger.info("Profile1 NewConnection fired! fd=%d", self.conn_fd)
            # Read from the profile fd
            try:
                os.set_blocking(self.conn_fd, False)
                for _ in range(20):
                    try:
                        data = os.read(self.conn_fd, 78)
                        if data:
                            logger.info("Profile1 data: %d bytes: %s", len(data), data[:16].hex())
                    except (OSError, IOError) as e:
                        if e.errno != errno.EAGAIN:
                            break
                    time.sleep(0.1)
            except Exception as e:
                logger.error("Profile1 read error: %s", e)
        else:
            logger.info("No Profile1 NewConnection (expected — Profile1 is not the BT data path)")

        # Cleanup
        logger.info("\n--- Cleanup ---")
        if self.conn_fd is not None:
            try:
                os.close(self.conn_fd)
            except Exception:
                pass
        self.unregister_profile()
        self.client.cleanup()
        logger.info("=== Diagnostic complete ===")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="BT Diagnostic for DS4")
    ap.add_argument("--pair", action="store_true", help="Initiate pairing")
    ap.add_argument("--address", help="DS4 Bluetooth MAC address")
    args = ap.parse_args()

    diag = Diagnostic()
    diag.run(do_pair=args.pair, address=args.address)


if __name__ == "__main__":
    main()
