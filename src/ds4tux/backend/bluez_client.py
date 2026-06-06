"""
BlueZ D-Bus client for DS4 pairing, connection, and monitoring.
Pure Gio D-Bus implementation — no bluetoothctl or bluez-utils needed.
"""

from __future__ import annotations
import os
import time
import struct
import socket
import select
import logging
import threading
from typing import Optional, Callable

import gi
gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import GLib, Gio

from ds4tux.device import (
    DS4Report, parse_input_report, build_output_report,
    BT_REPORT_SIZE, BT_OUTPUT_REPORT_SIZE,
)
from ds4tux.exceptions import BackendError, DeviceError

logger = logging.getLogger("ds4tux.bluez")

BLUEZ_SERVICE = "org.bluez"
BLUEZ_OBJECT = "/org/bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
PROFILE_MGR_IFACE = "org.bluez.ProfileManager1"
PROFILE_IFACE = "org.bluez.Profile1"
BATTERY_IFACE = "org.bluez.BatteryProviderManager1"

HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"

SONY_VID = 0x054c
DS4_COPYCAT_PID = 0x09cc

# HIDP protocol: L2CAP PSMs for HID
HID_PSM_CTRL = 0x11
HID_PSM_INTR = 0x13





def _unwrap(v):
    if isinstance(v, GLib.Variant):
        return v.unpack()
    return v


def _unwraps(d: dict) -> dict:
    return {k: _unwrap(v) for k, v in d.items()}


class BluezClient:
    def __init__(self):
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.GError as e:
            raise BackendError(f"Cannot connect to system D-Bus: {e}")
        self._adapter_path: Optional[str] = None
        self._glib_loop: Optional[GLib.MainLoop] = None
        self._glib_thread: Optional[threading.Thread] = None
        self._device_callback: Optional[Callable] = None
        self._signal_cancellable = None
        self._signal_matches: list[int] = []

    # --- Helpers ---

    def _call(self, bus_name, path, iface, method, params=None, reply_type=None):
        try:
            return self._bus.call_sync(
                bus_name, path, iface, method,
                params, reply_type,
                Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.GError as e:
            logger.debug("D-Bus call %s.%s failed: %s", iface, method, e)
            raise

    def _get_prop(self, path: str, iface: str, prop: str):
        result = self._call(
            BLUEZ_SERVICE, path,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (iface, prop)),
            GLib.VariantType("(v)"),
        )
        return _unwrap(result.unpack()[0])

    def _set_prop(self, path: str, iface: str, prop: str, value):
        self._call(
            BLUEZ_SERVICE, path,
            "org.freedesktop.DBus.Properties",
            "Set",
            GLib.Variant("(ssv)", (iface, prop, GLib.Variant(_variant_type(value), value))),
            None,
        )

    # --- Adapter ---

    def get_adapter(self) -> str:
        if self._adapter_path:
            return self._adapter_path
        objs = self._call(
            BLUEZ_SERVICE, "/", "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects", None, None,
        ).unpack()
        obj_dict = objs[0] if isinstance(objs, tuple) and len(objs) == 1 else objs
        for path, ifaces in obj_dict.items():
            if ADAPTER_IFACE in ifaces:
                self._adapter_path = str(path)
                return self._adapter_path
        raise BackendError("No Bluetooth adapter found")

    def adapter_powered(self) -> bool:
        try:
            return bool(self._get_prop(self.get_adapter(), ADAPTER_IFACE, "Powered"))
        except Exception:
            return False

    def set_adapter_powered(self, on: bool):
        self._set_prop(self.get_adapter(), ADAPTER_IFACE, "Powered", on)

    # --- Discovery ---

    def start_discovery(self):
        self._call(
            BLUEZ_SERVICE, self.get_adapter(), ADAPTER_IFACE,
            "StartDiscovery", None, None,
        )
        logger.info("Bluetooth discovery started")

    def stop_discovery(self):
        try:
            self._call(
                BLUEZ_SERVICE, self.get_adapter(), ADAPTER_IFACE,
                "StopDiscovery", None, None,
            )
            logger.info("Bluetooth discovery stopped")
        except GLib.GError:
            pass

    # --- Device enumeration ---

    def get_devices(self) -> list[tuple[str, dict]]:
        objs = self._call(
            BLUEZ_SERVICE, "/", "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects", None, None,
        ).unpack()
        # objs is a tuple of 1 dict: ({path: {iface: props}},)
        obj_dict = objs[0] if isinstance(objs, tuple) and len(objs) == 1 else objs
        devices = []
        for path, ifaces in obj_dict.items():
            if DEVICE_IFACE in ifaces:
                props = _unwraps(ifaces[DEVICE_IFACE])
                devices.append((str(path), props))
        return devices

    def find_ds4(self, address: Optional[str] = None) -> Optional[tuple[str, dict]]:
        candidates = []
        for path, props in self.get_devices():
            if address and props.get("Address", "").upper() == address.upper():
                return path, props
            modalias = props.get("Modalias", "")
            name = props.get("Name", "")
            # Match Sony VID (054c) and prefer our specific PID (09cc)
            if "054C" in modalias.upper():
                if "09CC" in modalias.upper() or "05C4" in modalias.upper():
                    candidates.append((path, props))
                elif "PLAYSTATION" in name.upper() or "WIRELESS CONTROLLER" in name.upper():
                    candidates.append((path, props))
            elif "PLAYSTATION" in name.upper() or "WIRELESS CONTROLLER" in name.upper():
                candidates.append((path, props))
        # Return best match: prefer connected, then paired, then first
        def sort_key(item):
            props = item[1]
            return (0 if props.get("Connected", False) else 1,
                    0 if props.get("Paired", False) else 1)
        candidates.sort(key=sort_key)
        return candidates[0] if candidates else None

    # --- Pairing ---

    def pair(self, device_path: str) -> bool:
        try:
            self._call(
                BLUEZ_SERVICE, device_path, DEVICE_IFACE,
                "Pair", None, None,
            )
            logger.info("Pairing initiated for %s", device_path)
            return True
        except GLib.GError as e:
            logger.warning("Pair failed: %s", e)
            return False

    def trust(self, device_path: str):
        try:
            self._set_prop(device_path, DEVICE_IFACE, "Trusted", True)
            logger.info("Device %s trusted", device_path)
        except GLib.GError as e:
            logger.warning("Trust failed: %s", e)

    def block_device(self, device_path: str):
        try:
            self._set_prop(device_path, DEVICE_IFACE, "Blocked", True)
            logger.info("Device %s blocked", device_path)
        except GLib.GError as e:
            logger.warning("Block failed: %s", e)

    def unblock_device(self, device_path: str):
        try:
            self._set_prop(device_path, DEVICE_IFACE, "Blocked", False)
            logger.info("Device %s unblocked", device_path)
        except GLib.GError as e:
            logger.warning("Unblock failed: %s", e)

    def connect_profile(self, device_path: str, uuid: str = HID_UUID):
        try:
            self._call(
                BLUEZ_SERVICE, device_path, DEVICE_IFACE,
                "ConnectProfile",
                GLib.Variant("(s)", (uuid,)),
                None,
            )
            logger.info("Connected profile %s on %s", uuid, device_path)
        except GLib.GError as e:
            logger.warning("ConnectProfile failed: %s", e)

    def disconnect(self, device_path: str):
        try:
            self._call(
                BLUEZ_SERVICE, device_path, DEVICE_IFACE,
                "Disconnect", None, None,
            )
        except GLib.GError:
            pass

    def remove_device(self, device_path: str):
        try:
            self._call(
                BLUEZ_SERVICE, self.get_adapter(), ADAPTER_IFACE,
                "RemoveDevice",
                GLib.Variant("(o)", (device_path,)),
                None,
            )
        except GLib.GError:
            pass

    # --- Signal monitoring ---

    def _on_properties_changed(self, connection, sender, path, iface, signal, params, user_data):
        if iface != DEVICE_IFACE:
            return
        try:
            changed, invalidated = params.unpack()
            props = _unwraps(changed)
        except Exception:
            return
        if self._device_callback:
            self._device_callback(path, props)

    def set_device_callback(self, cb: Callable[[str, dict], None]):
        self._device_callback = cb
        match_id = self._bus.signal_subscribe(
            BLUEZ_SERVICE, "org.freedesktop.DBus.Properties",
            "PropertiesChanged", None, DEVICE_IFACE,
            Gio.DBusSignalFlags.NONE,
            self._on_properties_changed, None,
        )
        self._signal_matches.append(match_id)

    def start_glib_loop(self):
        if self._glib_loop:
            return
        self._glib_loop = GLib.MainLoop()
        self._glib_thread = threading.Thread(target=self._glib_loop.run, daemon=True)
        self._glib_thread.start()

    def stop_glib_loop(self):
        if self._glib_loop:
            self._glib_loop.quit()
            self._glib_loop = None
        for mid in self._signal_matches:
            try:
                self._bus.signal_unsubscribe(mid)
            except Exception:
                pass
        self._signal_matches.clear()

    # --- Bluetooth agent ---

    AGENT_PATH = "/ds4tux/agent"
    AGENT_CAPABILITY = "NoInputNoOutput"

    def _register_agent(self) -> bool:
        """Register a NoInputNoOutput agent so BlueZ accepts DS4 pairing
        without interactive PIN entry.  DS4 controllers use just-works
        pairing so 'NoInputNoOutput' is sufficient."""
        agent_xml = """
        <node>
          <interface name="org.bluez.Agent1">
            <method name="Release"/>
            <method name="RequestPinCode">
              <arg type="o" name="device" direction="in"/>
              <arg type="s" name="pin" direction="out"/>
            </method>
            <method name="RequestPasskey">
              <arg type="o" name="device" direction="in"/>
              <arg type="u" name="passkey" direction="out"/>
            </method>
            <method name="DisplayPasskey">
              <arg type="o" name="device" direction="in"/>
              <arg type="u" name="passkey" direction="in"/>
              <arg type="q" name="entered" direction="in"/>
            </method>
            <method name="RequestConfirmation">
              <arg type="o" name="device" direction="in"/>
              <arg type="u" name="passkey" direction="in"/>
            </method>
            <method name="AuthorizeService">
              <arg type="o" name="device" direction="in"/>
              <arg type="s" name="uuid" direction="in"/>
            </method>
            <method name="Cancel"/>
          </interface>
        </node>
        """
        node = Gio.DBusNodeInfo.new_for_xml(agent_xml)
        iface = node.interfaces[0]

        def on_method_call(connection, sender, object_path,
                           interface_name, method_name, parameters, invocation):
            if method_name in ("Release", "Cancel"):
                logger.debug("Agent %s called", method_name)
                invocation.return_value(None)
            elif method_name == "RequestPinCode":
                invocation.return_value(GLib.Variant("(s)", ("0000",)))
            elif method_name in ("RequestPasskey", "DisplayPasskey",
                                 "RequestConfirmation"):
                logger.debug("Agent %s (auto-accept)", method_name)
                invocation.return_value(GLib.Variant("(u)", (0,)))
            elif method_name == "AuthorizeService":
                logger.debug("Agent AuthorizeService (auto-accept)")
                invocation.return_value(None)

        try:
            self._bus.register_object(
                self.AGENT_PATH, iface, on_method_call, None, None,
            )
            self._bus.call_sync(
                BLUEZ_SERVICE, BLUEZ_OBJECT, "org.bluez.AgentManager1",
                "RegisterAgent",
                GLib.Variant("(os)", (self.AGENT_PATH, self.AGENT_CAPABILITY)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            self._bus.call_sync(
                BLUEZ_SERVICE, BLUEZ_OBJECT, "org.bluez.AgentManager1",
                "RequestDefaultAgent",
                GLib.Variant("(o)", (self.AGENT_PATH,)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            logger.info("Bluetooth agent registered (NoInputNoOutput)")
            return True
        except GLib.GError as e:
            logger.debug("Agent registration failed: %s", e)
            return False

    def _unregister_agent(self):
        try:
            self._bus.call_sync(
                BLUEZ_SERVICE, BLUEZ_OBJECT, "org.bluez.AgentManager1",
                "UnregisterAgent",
                GLib.Variant("(o)", (self.AGENT_PATH,)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.GError:
            pass

# --- High-level pairing flow ---

    def pair_ds4(self, timeout: float = 60.0) -> Optional[str]:
        event = threading.Event()
        paired_path: list[Optional[str]] = [None]

        # Remove any stale unpaired device entries so BlueZ re-discovers
        already = self.find_ds4()
        if already:
            path, props = already
            if props.get("Paired", False):
                logger.info("DS4 already paired at %s", path)
                return path
            logger.info("Removing stale device entry (unpaired)")
            self.remove_device(path)

        def on_change(path: str, props: dict):
            if paired_path[0]:
                return
            if props.get("Paired", False):
                modalias = props.get("Modalias", "")
                name = props.get("Name", "")
                addr = props.get("Address", "")
                logger.info("Device paired: %s (%s) %s", name, addr, path)
                paired_path[0] = path
                event.set()

        self.set_device_callback(on_change)
        self.start_glib_loop()

        self._register_agent()

        logger.info("Put DS4 in pairing mode (PS + Share). Scanning...")
        self.start_discovery()

        tried: set[str] = set()

        end = time.time() + timeout
        while time.time() < end and not event.is_set():
            for dev_path, dev_props in self.get_devices():
                if paired_path[0]:
                    break
                addr = dev_props.get("Address", "")
                modalias = dev_props.get("Modalias", "")
                name = dev_props.get("Name", "")

                if not ("054C" in modalias.upper()
                        or "PLAYSTATION" in name.upper()
                        or "WIRELESS CONTROLLER" in name.upper()):
                    continue

                if dev_props.get("Paired", False):
                    logger.info("DS4 already paired: %s (%s)", name, addr)
                    paired_path[0] = dev_path
                    event.set()
                    break

                if dev_path not in tried:
                    tried.add(dev_path)
                    logger.info("Trying to pair with %s (%s)...", name, addr)
                    if self.pair(dev_path):
                        self.trust(dev_path)
                    else:
                        logger.info("Pair failed for %s — removing", addr)
                        self.remove_device(dev_path)

            event.wait(timeout=1.0)

        self.stop_discovery()
        self._device_callback = None
        self.stop_glib_loop()

        if paired_path[0]:
            logger.info("DS4 paired successfully: %s", paired_path[0])
        else:
            logger.warning("DS4 pairing timed out")
        return paired_path[0]

    def connect_ds4(self, device_path: str) -> bool:
        self.connect_profile(device_path, HID_UUID)
        return True

    def wait_for_connected(self, device_path: str, timeout: float = 30.0) -> bool:
        event = threading.Event()
        result: list[bool] = [False]

        def on_change(path: str, props: dict):
            if path != device_path:
                return
            if props.get("Connected", False):
                logger.info("DS4 connected: %s", path)
                result[0] = True
                event.set()

        self.set_device_callback(on_change)
        self.start_glib_loop()

        props = dict(self.get_device_props(device_path))
        if props.get("Connected", False):
            self._device_callback = None
            return True

        event.wait(timeout=timeout)
        self._device_callback = None
        return result[0]

    def get_device_props(self, device_path: str) -> dict:
        result = self._call(
            BLUEZ_SERVICE, device_path,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (DEVICE_IFACE,)),
            GLib.VariantType("(a{sv})"),
        )
        return _unwraps(result.unpack()[0])

    def cleanup(self):
        self._unregister_agent()
        self.stop_glib_loop()
        try:
            self.stop_discovery()
        except Exception:
            pass


def _variant_type(val):
    if isinstance(val, bool):
        return "b"
    if isinstance(val, int):
        return "i"
    if isinstance(val, str):
        return "s"
    if isinstance(val, float):
        return "d"
    return "s"
