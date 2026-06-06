"""
ds4tux main entry point.
"""

from __future__ import annotations
import os
import sys
import time
import errno
import signal
import select
import logging
import threading
import argparse
import subprocess
from typing import Optional

from ds4tux import __version__
from ds4tux.config import load_config, DEFAULT_CONFIG, write_config, find_config, save_device_info, _default_write_path
from ds4tux.device import DS4Report, build_output_report, detect_copycat
from ds4tux.actions.led import LEDController
from ds4tux.actions.battery import BatteryMonitor
from ds4tux.actions.gyro import GyroProcessor
from ds4tux.actions.latency import LatencyMonitor
from ds4tux.uinput import UinputManager
from ds4tux.settings.client import SOCKET_PATH, SettingsClient

logger = logging.getLogger("ds4tux")


class DS4Controller:
    def __init__(self, device_id: str, config: dict, uinput_mgr: UinputManager):
        self.device_id = device_id
        self.config = config
        self.uinput_mgr = uinput_mgr
        self.device = None
        self._running = False

        led_cfg = config.get("led", {})
        self.led = LEDController(
            r=led_cfg.get("r", 0),
            g=led_cfg.get("g", 0),
            b=led_cfg.get("b", 128),
            brightness=config.get("led_brightness", 60),
        )
        self.battery = BatteryMonitor()
        self.gyro = GyroProcessor()
        self.latency = LatencyMonitor()
        self.uinput_dev = None
        self._last_keepalive = 0.0
        self._keepalive_interval = config.get("keepalive_interval", 5.0)
        self._mapping = config.get("mapping", "xpad")
        self._gyro = config.get("gyro", False)
        self._on_report_callbacks: list = []
        self._callbacks_lock = threading.Lock()
        self._settings_server = None
        self._enabled = True
        self._status = "disconnected"
        self._backend = None

    def set_settings_server(self, server):
        self._settings_server = server

    def set_device(self, dev):
        self.device = dev

        self.uinput_dev = self.uinput_mgr.get_or_create(
            self.device_id, mapping=self._mapping, gyro=self._gyro
        )

        self.led.set_write_fn(lambda r, g, b, bo, bf: self._write_led(r, g, b, bo, bf))
        self.apply_config()

        if self.uinput_dev and self.uinput_dev._uinput:
            try:
                _ = self.uinput_dev._uinput.device
            except Exception:
                pass

    def _write_led(self, r: int, g: int, b: int, blink_on: int = 0, blink_off: int = 0) -> bool:
        if not self.device:
            return False
        return self.device.write_output(r=r, g=g, b=b, blink_on=blink_on, blink_off=blink_off)

    def process_report(self, report: DS4Report):
        if not report:
            return

        self.latency.update(report)

        fx, fy, fz = self.gyro.process(report.gyro_raw_x, report.gyro_raw_y, report.gyro_raw_z)
        report.gyro_filtered_x = fx
        report.gyro_filtered_y = fy
        report.gyro_filtered_z = fz

        just_low = self.battery.update(report)

        if just_low:
            self.led.set_blink(200, 400, duration=2.0)

        self.led.update()

        if self._enabled and self.uinput_mgr._active and self.uinput_dev:
            self.uinput_dev.emit(report)
        elif not self._enabled:
            pass
        else:
            logger.debug("uinput not ready: active=%s dev=%s",
                         self.uinput_mgr._active, self.uinput_dev is not None)

        now = time.time()
        if now - self._last_keepalive >= self._keepalive_interval:
            self.led.update()
            self._last_keepalive = now

        if self._on_report_callbacks:
            with self._callbacks_lock:
                cbs = list(self._on_report_callbacks)
            for cb in cbs:
                try:
                    cb(self.device_id, report)
                except Exception as e:
                    logger.warning("Report callback failed: %s", e)

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        if self._backend and hasattr(self._backend, 'set_enabled'):
            self._backend.set_enabled(enabled)
        if not enabled and self.uinput_dev:
            self.uinput_dev.emit_reset()

    def apply_config(self):
        cfg = self.config
        led_cfg = cfg.get("led", {})
        self.led.set_color(
            led_cfg.get("r", 0),
            led_cfg.get("g", 0),
            led_cfg.get("b", 128),
        )
        self.led.set_brightness(cfg.get("led_brightness", 60))
        self.led.update()
        if self.device:
            mode = cfg.get("device_mode", "auto")
            self.device.device_mode = mode
            dev_copycat = getattr(self.device, "copycat", False)
            if mode == "clone":
                is_copycat = True
            elif mode == "genuine":
                is_copycat = False
            else:  # auto
                mac = getattr(self.device, "address", None)
                saved = cfg.get("device", {}).get(mac, {}) if mac else {}
                saved_copycat = saved.get("detected_copycat")
                if saved_copycat is not None:
                    is_copycat = saved_copycat
                    if hasattr(self.device, 'detected_copycat'):
                        self.device.detected_copycat = saved_copycat
                else:
                    is_copycat = dev_copycat
            if is_copycat:
                self.led.set_color(255, 80, 0)
                self.led.update()
        old_mapping = self._mapping
        self._mapping = cfg.get("mapping", "xpad")
        self._gyro = cfg.get("gyro", False)
        if self._mapping != old_mapping:
            self.uinput_mgr.remove(self.device_id)
            self.uinput_dev = self.uinput_mgr.get_or_create(
                self.device_id, mapping=self._mapping, gyro=self._gyro,
            )
        if self._settings_server:
            self._settings_server._broadcast_state()

    def cleanup(self):
        if self.uinput_dev:
            self.uinput_dev.emit_reset()
        if self.led:
            try:
                self.led.set_write_fn(lambda r, g, b, bo, bf: self._write_led(0, 0, 0, 0, 0))
                self.led.update()
            except Exception:
                pass
        if self.device:
            try:
                self.device.write_output(r=0, g=0, b=0)
            except Exception:
                pass

    def add_report_callback(self, cb):
        with self._callbacks_lock:
            self._on_report_callbacks.append(cb)

    def remove_report_callback(self, cb):
        with self._callbacks_lock:
            try:
                self._on_report_callbacks.remove(cb)
            except ValueError:
                pass


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args(argv: list[str] = sys.argv[1:]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="ds4tux",
        description="DualShock 4 userspace driver for Linux",
    )
    ap.add_argument("--version", action="version", version=f"ds4tux {__version__}")
    ap.add_argument("--config", "-c", help="Path to config file")
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    ap.add_argument("--backend", choices=["bluez", "hidraw", "l2cap"],
                    help="Connection backend (default: auto-detect)")
    ap.add_argument("--usb", action="store_true", help="Use USB/hidraw mode")
    ap.add_argument("--led", help="LED color as hex RRGGBB")
    ap.add_argument("--mapping", choices=["xpad", "xboxdrv", "ds4"],
                    help="uinput mapping")
    ap.add_argument("--copycat", choices=["auto", "clone", "genuine"],
                    help="Force device mode (auto/clone/genuine)")
    ap.add_argument("--hex", "-x", action="store_true",
                    help="Hex dump raw input reports")
    ap.add_argument("--bt-addr", default=None,
                    help="Bluetooth MAC address of DS4")
    ap.add_argument("--block-kmod", action="store_true",
                    help="Blacklist hid_playstation kernel module")
    ap.add_argument("--settings", "-s", action="store_true",
                    help="Open TUI settings app")
    ap.add_argument("--service", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("action", nargs="?",
                    choices=["block-kmod", "unblock-kmod"],
                    help="Run a one-shot action and exit")
    return ap.parse_args(argv)


def _find_ds4_address() -> Optional[str]:
    """Look up a paired/connected DS4 address from BlueZ, or None."""
    try:
        from ds4tux.backend.bluez_client import BluezClient
        client = BluezClient()
        for path, props in client.get_devices():
            modalias = props.get("Modalias", "")
            name = props.get("Name", "")
            if not ("054C" in modalias.upper()
                    or "PLAYSTATION" in name.upper()
                    or "WIRELESS CONTROLLER" in name.upper()):
                continue
            addr = props.get("Address", "")
            if addr and (props.get("Paired", False) or props.get("Connected", False)):
                return addr
    except Exception as e:
        logger.debug("BlueZ device lookup failed: %s", e)
    return None


def _disconnect_bluez(device_path: str):
    """Disconnect a device from BlueZ so L2CAP PSMs are freed."""
    try:
        from ds4tux.backend.bluez_client import BluezClient
        client = BluezClient()
        client.disconnect(device_path)
        logger.debug("Disconnected %s from BlueZ", device_path)
        time.sleep(0.5)
    except Exception:
        pass


def _detect_l2cap_backend(args):
    from ds4tux.backend.bluez_l2cap import L2capBtBackend
    addr = args.bt_addr or _find_ds4_address()
    if not addr and args.backend == "l2cap":
        logger.warning("No --bt-addr given and no paired DS4 found in BlueZ")
        logger.warning("Use --bt-addr XX:XX:XX:XX:XX:XX or run without --backend for auto mode")
    if addr:
        logger.info("Using L2CAP backend for %s", addr)
        return L2capBtBackend(address=addr, hex_dump=args.hex)
    logger.info("Using L2CAP backend (press PS to connect)")
    return L2capBtBackend(hex_dump=args.hex)


def _detect_usb_backend():
    """Scan pyudev for a USB DS4 hidraw device. Returns HidrawBackend or None."""
    try:
        import pyudev
        ctx = pyudev.Context()
        known_pids = {"05c4", "09cc", "0ce6"}
        for dev in ctx.list_devices(subsystem="hidraw"):
            ancestors = list(dev.ancestors)
            if any(a.subsystem == "bluetooth" for a in ancestors) and \
               not any(a.subsystem == "usb" for a in ancestors):
                continue
            hidraw_node = dev.device_node
            if not hidraw_node:
                continue
            vid = pid = None
            hid_name = ""
            for a in ancestors:
                if a.subsystem == "usb":
                    try:
                        v = a.attributes.get("idVendor").decode().strip().lower()
                        if v and not v.startswith("0000"):
                            vid = v
                            pid = a.attributes.get("idProduct").decode().strip().lower()
                            break
                    except Exception:
                        pass
                if a.subsystem == "hid":
                    hid_name = a.get("HID_NAME", "")
            if vid == "054c" and pid in known_pids:
                logger.info("DS4 detected via USB/hidraw at %s", hidraw_node)
                from ds4tux.backend.hidraw import HidrawBackend
                return HidrawBackend()
            if hid_name and any(kw in hid_name.lower()
                               for kw in ("wireless controller", "dualshock")):
                logger.info("DS4 detected via USB/hidraw (name match) at %s",
                            hidraw_node)
                from ds4tux.backend.hidraw import HidrawBackend
                return HidrawBackend()
    except Exception as e:
        logger.debug("USB detection failed: %s", e)
    return None


def _scan_usb_ds4_devices(seen: set[str]):
    """Scan for USB DS4 hidraw devices not yet seen, probe and save copycat."""
    try:
        import pyudev
        ctx = pyudev.Context()
    except ImportError:
        return
    known_pids = {"05c4", "09cc", "0ce6"}
    for dev in ctx.list_devices(subsystem="hidraw"):
        syspath = dev.sys_path
        if syspath in seen:
            continue
        ancestors = list(dev.ancestors)
        if any(a.subsystem == "bluetooth" for a in ancestors) and \
           not any(a.subsystem == "usb" for a in ancestors):
            continue
        hidraw_node = dev.device_node
        if not hidraw_node:
            continue
        is_ds4 = False
        hid_name = ""
        hid_syspath = None
        for a in ancestors:
            if a.subsystem == "usb":
                try:
                    v = a.attributes.get("idVendor").decode().strip().lower()
                    p = a.attributes.get("idProduct").decode().strip().lower()
                    if v == "054c" and p in known_pids:
                        is_ds4 = True
                except Exception:
                    pass
            if a.subsystem == "hid":
                hid_name = a.get("HID_NAME", "")
                hid_syspath = a.sys_path
        if not is_ds4 and hid_name and \
           any(kw in hid_name.lower() for kw in ("wireless controller", "dualshock")):
            is_ds4 = True
        if not is_ds4:
            continue
        mac = None
        if hid_syspath:
            try:
                with open(os.path.join(hid_syspath, "HID_UNIQ")) as f:
                    uniq = f.read().strip()
                if ":" in uniq:
                    mac_b = bytes(int(x, 16) for x in uniq.split(":"))
                    mac = ":".join(f"{b:02x}" for b in mac_b)
            except (OSError, IOError, ValueError):
                pass
        logger.info("USB DS4 found at %s (mac=%s)", hidraw_node, mac or "N/A")
        seen.add(syspath)
        if not mac:
            continue
        try:
            fd = os.open(hidraw_node, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            copycat, _ = detect_copycat(fd)
        except Exception:
            continue
        finally:
            os.close(fd)
        save_device_info(mac, {"detected_copycat": copycat})
        logger.info("USB DS4 %s: copycat=%s — saved to config", mac, copycat)


def _detect_bluez_backend(args):
    """Try to find or pair a DS4 via BlueZ. Returns L2CAP backend or None."""
    try:
        from ds4tux.backend.bluez_client import BluezClient
        client = BluezClient()

        found_addr = None
        for path, props in client.get_devices():
            modalias = props.get("Modalias", "")
            name = props.get("Name", "")
            if not ("054C" in modalias.upper()
                    or "PLAYSTATION" in name.upper()
                    or "WIRELESS CONTROLLER" in name.upper()):
                continue
            addr = props.get("Address", "")
            if not addr:
                continue
            if props.get("Paired", False) or props.get("Connected", False):
                found_addr = addr
                break

        if not found_addr:
            logger.info("No paired DS4 — scanning for one in pair mode (PS+Share)...")
            device_path = client.pair_ds4(timeout=30.0)
            if device_path:
                props = client.get_device_props(device_path)
                found_addr = props.get("Address", "")
                if found_addr and props.get("Connected", False):
                    logger.info("DS4 paired at %s — disconnecting for L2CAP", found_addr)
                    _disconnect_bluez(device_path)

        if found_addr:
            from ds4tux.backend.bluez_l2cap import L2capBtBackend
            logger.info("Using L2CAP backend for %s", found_addr)
            return L2capBtBackend(address=found_addr, hex_dump=args.hex)
    except Exception as e:
        logger.debug("BluezClient unavailable (%s), falling through", e)
    return None


def init_backend(args, config):
    if args.backend == "l2cap":
        return _detect_l2cap_backend(args)
    if args.backend == "bluez":
        return _detect_l2cap_backend(args)
    if args.usb or args.backend == "hidraw":
        from ds4tux.backend.hidraw import HidrawBackend
        return HidrawBackend()

    # Auto-detect: try USB, then BlueZ, then fall back to hidraw
    backend = _detect_usb_backend()
    if backend:
        return backend
    backend = _detect_bluez_backend(args)
    if backend:
        return backend

    logger.warning("No DS4 found — use --bt-addr or connect controller")
    from ds4tux.backend.hidraw import HidrawBackend
    return HidrawBackend()


def handle_kmod_block(block: bool):
    if not block:
        return
    if os.geteuid() != 0:
        logger.warning("--block-kmod requires root. Use sudo or disable in config.")
        return

    conf_path = "/etc/modprobe.d/ds4tux.conf"
    try:
        with open(conf_path, "w") as f:
            f.write("# ds4tux: block kernel HID drivers for DS4\n")
            f.write("# hid_playstation is NOT blacklisted — it creates hidraw and coexists with userspace\n")
            f.write("blacklist hid_sony\n")
            f.write("blacklist hidp\n")
        logger.info("Written %s", conf_path)
    except IOError as e:
        logger.error("Cannot write %s: %s", conf_path, e)
        return

    # Unload hid_sony and hidp, keep hid_playstation loaded
    for mod in ("hid_sony", "hidp"):
        ret = os.system(f"modprobe -r {mod} 2>/dev/null")
        if ret == 0:
            logger.info("Unloaded %s module", mod)
    # Ensure hid_playstation is loaded (needed for USB hidraw creation)
    ret = os.system("modprobe hid_playstation 2>/dev/null")
    if ret == 0:
        logger.debug("Loaded hid_playstation")


def unblock_kmod():
    conf_path = "/etc/modprobe.d/ds4tux.conf"
    if os.path.exists(conf_path):
        try:
            os.remove(conf_path)
            logger.info("Removed %s", conf_path)
        except IOError as e:
            logger.error("Cannot remove %s: %s", conf_path, e)
    ret = os.system("modprobe hid_playstation 2>/dev/null")
    if ret == 0:
        logger.info("Loaded hid_playstation module (required for USB)")
    else:
        logger.debug("hid_playstation not available")


def _check_usb_ds4_present() -> bool:
    """Return True if a USB DS4 hidraw device is currently present."""
    try:
        import pyudev
        ctx = pyudev.Context()
        known_pids = {"05c4", "09cc", "0ce6"}
        for dev in ctx.list_devices(subsystem="hidraw"):
            ancestors = list(dev.ancestors)
            if any(a.subsystem == "bluetooth" for a in ancestors) and \
               not any(a.subsystem == "usb" for a in ancestors):
                continue
            for a in ancestors:
                if a.subsystem == "usb":
                    try:
                        v = a.attributes.get("idVendor").decode().strip().lower()
                        if v == "054c":
                            pid = a.attributes.get("idProduct").decode().strip().lower()
                            if pid in known_pids:
                                return True
                    except Exception:
                        pass
        return False
    except Exception as e:
        logger.debug("USB presence check failed: %s", e)
        return False


def _is_service_running() -> bool:
    pid_path = "/run/ds4tux.pid"
    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())
        if os.path.isdir(f"/proc/{pid}"):
            return True
    except (OSError, ValueError, IOError):
        pass
    if os.path.exists(SOCKET_PATH):
        import socket
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(SOCKET_PATH)
            sock.close()
            return True
        except (OSError, IOError, ConnectionRefusedError):
            pass
    return False


def _show_service_status():
    print("ds4tux: service is running", flush=True)
    pid_path = "/run/ds4tux.pid"
    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())
        if os.path.isdir(f"/proc/{pid}"):
            print(f"  PID: {pid}  ({pid_path})", flush=True)
    except (OSError, ValueError, IOError):
        pass
    cfg = find_config()
    if cfg:
        print(f"  Config: {cfg}", flush=True)
    log_root = "/root/.cache/ds4tux/daemon.log"
    try:
        r = subprocess.run(["sudo", "test", "-f", log_root],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            print(f"  Log: {log_root}  (sudo tail -f {log_root})", flush=True)
    except Exception:
        pass
    user_log = os.path.expanduser("~/.cache/ds4tux/daemon.log")
    if os.path.exists(user_log):
        print(f"  Log: {user_log}", flush=True)
    print(f"  TUI:  sudo -u $USER ds4tux --settings", flush=True)
    print(f"  Stop: sudo rc-service ds4tux stop", flush=True)


def _reload_config_from(config_path: str, controllers: dict, settings_server=None):
    try:
        new_config = load_config(config_path)
        for ctrl in controllers.values():
            ctrl.config = new_config
            ctrl.apply_config()
        if settings_server:
            settings_server._broadcast_state()
        logger.info("Config reloaded from %s", config_path)
    except Exception as e:
        logger.warning("Config reload failed: %s", e)


def run_daemon(args, config, controllers: dict[str, DS4Controller],
               uinput_mgr: UinputManager,
               stop_event: threading.Event):
    from ds4tux.backend.hidraw import HidrawBackend
    from ds4tux.backend.bluez_l2cap import L2capBtBackend

    # Direct ds4tux logging to a file so curses TUI stays clean.
    # Add the file handler to the ds4tux logger and stop propagation
    # to root (which may have a stderr handler from setup_logging()).
    log_dir = os.path.expanduser("~/.cache/ds4tux")
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(os.path.join(log_dir, "daemon.log"))
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    ds4tux_logger = logging.getLogger("ds4tux")
    ds4tux_logger.addHandler(fh)
    ds4tux_logger.propagate = False
    ds4tux_logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    # Start settings server BEFORE init_backend so the TUI gets
    # pairing/reconnection status broadcasts from the start.
    settings_server = _start_settings_server(None, None)

    backend = init_backend(args, config)

    _auto_mode = not args.backend and not args.usb
    ctrl_ref = []

    if settings_server:
        settings_server.set_backend(backend)

    def status_print_connect(dev_id, ctrl):
        print(f"Connected: {dev_id}", flush=True)
        def print_battery(did, report):
            chg = " (charging)" if report.is_charging else ""
            print(f"Battery: {report.battery_percent}%{chg}", flush=True)
            ctrl.remove_report_callback(print_battery)
        ctrl.add_report_callback(print_battery)

    def on_connect(dev):
        dev_id = getattr(dev, 'address', str(dev.fileno()))
        logger.info("Controller connected: %s", dev_id)
        ctrl = DS4Controller(dev_id, load_config(_config_path), uinput_mgr)
        # USB with hid_playstation causes double input if mapping != "ds4"
        # (Steam reads hidraw directly while we also emit via uinput)
        if not getattr(dev, "is_bluetooth", True):
            logger.info("USB connection — forcing 'ds4' mapping to minimize double-input risk")
            ctrl._mapping = "ds4"
        ctrl.set_device(dev)
        ctrl._status = "connected"
        controllers[dev_id] = ctrl
        ctrl_ref.append(ctrl)

        ctrl._backend = backend
        if settings_server:
            settings_server.set_controller(ctrl)
            settings_server.set_controllers_dict(controllers)
        if not args.settings:
            status_print_connect(dev_id, ctrl)

    def on_disconnect(dev):
        dev_id = getattr(dev, 'address', str(dev.fileno()))
        logger.info("Controller disconnected: %s", dev_id)
        ctrl = controllers.pop(dev_id, None)
        if ctrl:
            ctrl._status = "disconnected"
            ctrl.cleanup()
        if not args.settings:
            print("Disconnected", flush=True)

    def _switch_backend(new_backend):
        nonlocal backend, last_poll, _usb_check_cooldown, _scanned_usb
        for dev_id in list(controllers):
            ctrl = controllers.pop(dev_id)
            ctrl._status = "disconnected"
            ctrl.cleanup()
        backend.stop()
        backend = new_backend
        backend.set_callbacks(on_connect=on_connect, on_disconnect=on_disconnect)
        backend.setup()
        if settings_server:
            settings_server.set_backend(backend)
        if isinstance(backend, L2capBtBackend):
            if not backend.connect_loop():
                logger.info("Background BT reconnect active")
        elif hasattr(backend, 'enumerate_existing'):
            backend.enumerate_existing()
            logger.info("Watching for hidraw devices...")
        last_poll = time.time()
        _usb_check_cooldown = time.time() + 2.0
        _scanned_usb.clear()

    backend.set_callbacks(on_connect=on_connect, on_disconnect=on_disconnect)
    backend.setup()

    if isinstance(backend, L2capBtBackend):
        logger.info("L2CAP BT backend: connecting to %s", backend._address)
        if not backend.connect_loop():
            logger.info("Background reconnect active")
    elif hasattr(backend, 'name') and backend.name == "bluez":
        logger.info("Waiting for DS4 connection via BlueZ...")
        logger.info("Press PS button on your controller to connect")
    elif hasattr(backend, 'enumerate_existing'):
        backend.enumerate_existing()
        logger.info("Watching for hidraw devices...")

    last_poll = time.time()
    _usb_check_cooldown = time.time() + 2.0
    _config_last_mtime = 0.0
    _config_path = find_config(args.config) or _default_write_path()
    _scanned_usb: set[str] = set()

    try:
        while not stop_event.is_set():
            now = time.time()

            # Config file watcher — poll mtime every 2s
            if _config_path:
                try:
                    mtime = os.path.getmtime(_config_path)
                    if mtime > _config_last_mtime + 0.1:
                        _config_last_mtime = mtime
                        _reload_config_from(_config_path, controllers, settings_server)
                except OSError:
                    pass

            if isinstance(backend, HidrawBackend):
                if now - last_poll >= 0.5:
                    backend.poll_device_events(timeout=0.1)
                    backend.check_disconnections()
                    last_poll = now
                if _auto_mode and not controllers:
                    logger.info("No USB controllers — switching to Bluetooth")
                    _switch_backend(L2capBtBackend(
                        address=_find_ds4_address(),
                        hex_dump=args.hex,
                    ))
                    continue
            elif isinstance(backend, L2capBtBackend):
                if now - last_poll >= 0.5:
                    backend.check_disconnections()
                    last_poll = now
                for dev_id, ctrl_inst in list(controllers.items()):
                    dev = ctrl_inst.device
                    if dev and not dev._dead:
                        dev.keepalive()
                if now >= _usb_check_cooldown:
                    _usb_check_cooldown = now + 5.0
                    _scan_usb_ds4_devices(_scanned_usb)

            have_data = False
            for dev_id, ctrl in list(controllers.items()):
                dev = ctrl.device
                if dev and dev.fileno() is not None:
                    try:
                        r, _, _ = select.select([dev], [], [], 0.001)
                        if r:
                            have_data = True
                            while not stop_event.is_set():
                                report = dev.read_report()
                                if report:
                                    dev.update_battery()
                                    ctrl.process_report(report)
                                else:
                                    break
                    except (OSError, IOError) as e:
                        if e.errno not in (errno.EAGAIN, errno.EINTR):
                            pass

            if not controllers or not have_data:
                time.sleep(0.01)
    finally:
        backend.stop()
        if settings_server:
            settings_server.stop()


def _start_settings_server(ctrl: Optional[DS4Controller], backend=None):
    try:
        from ds4tux.settings.server import SettingsServer
        server = SettingsServer()
        if backend:
            server.set_backend(backend)
        if ctrl:
            server.set_controller(ctrl)
            ctrl.set_settings_server(server)
        server.start()
        return server
    except Exception as e:
        logger.warning("Settings server not available: %s", e)
        return None


def main():
    args = parse_args()
    config = load_config(args.config)

    # One-shot actions
    if args.action == "block-kmod":
        handle_kmod_block(True)
        return
    if args.action == "unblock-kmod":
        unblock_kmod()
        return

    # CLI config flags: write config and exit (unless --settings)
    config_changed = False
    if args.led:
        try:
            rgb = args.led.lstrip("#")
            config["led"] = {"r": int(rgb[0:2], 16), "g": int(rgb[2:4], 16), "b": int(rgb[4:6], 16)}
        except (ValueError, IndexError):
            logger.error("Invalid LED color: %s", args.led)
        config_changed = True
    if args.mapping:
        config["mapping"] = args.mapping
        config_changed = True
    if args.copycat:
        config["device_mode"] = args.copycat
        config_changed = True

    if config_changed and not args.settings:
        path = write_config(config)
        print(f"Config written: {path}", flush=True)
        if _is_service_running():
            try:
                client = SettingsClient()
                if client.connect():
                    client.send_command({"type": "reload_config", "config": config})
                    client.close()
            except Exception as e:
                logger.debug("Failed to notify service of config change: %s", e)
        return

    # Bare ds4tux (no action, no --settings, no --service): check service
    if not args.settings and not args.service:
        if _is_service_running():
            _show_service_status()
            log_path = "/root/.cache/ds4tux/daemon.log"
            try:
                r = subprocess.run(["sudo", "test", "-f", log_path],
                                   capture_output=True, timeout=5)
                if r.returncode == 0:
                    print(f"\nTailing daemon log (Ctrl+C to stop):", flush=True)
                    try:
                        subprocess.run(["sudo", "tail", "-f", log_path], timeout=None)
                    except KeyboardInterrupt:
                        print("\n", flush=True)
                    return
            except Exception:
                pass
            user_log = os.path.expanduser("~/.cache/ds4tux/daemon.log")
            if os.path.exists(user_log):
                print(f"\nTailing daemon log (Ctrl+C to stop):", flush=True)
                try:
                    subprocess.run(["tail", "-f", user_log], timeout=None)
                except KeyboardInterrupt:
                    print("\n", flush=True)
            return
        print("ds4tux: service not running", flush=True)
        print("Use:     sudo ds4tux --service    (run foreground)", flush=True)
        print("Or:      sudo rc-service ds4tux start   (start service)", flush=True)
        print("Config:  ds4tux --led RRGGBB      (write config and exit)", flush=True)
        print("TUI:     ds4tux --settings        (open settings)", flush=True)
        return

    setup_logging(args.verbose)

    controllers: dict[str, DS4Controller] = {}
    uinput_mgr = UinputManager()
    stop_event = threading.Event()

    if args.settings:
        from ds4tux.settings.tui import DS4TuxTUI
        if _is_service_running():
            logger.info("Service is running — connecting to existing daemon")
            DS4TuxTUI.run(config=config)
            return
        if os.path.exists(SOCKET_PATH):
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass
    else:
        def handle_sig(sig, frame):
            logger.info("Shutting down...")
            stop_event.set()
        signal.signal(signal.SIGINT, handle_sig)
        signal.signal(signal.SIGTERM, handle_sig)

    block_kmod = config.get("block_kmod", False) or args.block_kmod
    if block_kmod:
        handle_kmod_block(True)

    daemon_thread = threading.Thread(
        target=run_daemon,
        args=(args, config, controllers, uinput_mgr, stop_event),
        daemon=True,
    )
    daemon_thread.start()

    if args.settings:
        for _ in range(200):
            if os.path.exists(SOCKET_PATH) or not daemon_thread.is_alive():
                break
            time.sleep(0.1)
        if not daemon_thread.is_alive() and not os.path.exists(SOCKET_PATH):
            logger.warning("Daemon failed to start")
        DS4TuxTUI.run(config=config)
    else:
        print("Listening...", flush=True)
        try:
            stop_event.wait()
        except KeyboardInterrupt:
            stop_event.set()
        print("Shutting down...", flush=True)

    stop_event.set()
    daemon_thread.join(timeout=3.0)
    for ctrl in list(controllers.values()):
        ctrl.cleanup()
    uinput_mgr.close_all()
    if block_kmod:
        unblock_kmod()


if __name__ == "__main__":
    main()
