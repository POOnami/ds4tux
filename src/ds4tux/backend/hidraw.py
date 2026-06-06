"""
hidraw backend for DualShock 4 (USB and fallback Bluetooth).
Uses evdev (grabbed) for input, hidraw for LED/output control.
"""

from __future__ import annotations
import os
import errno
import fcntl
import select
import logging
from typing import Optional

import pyudev
from evdev import InputDevice, ecodes

from ds4tux.device import (
    DS4Report, build_output_report, detect_copycat, parse_input_report,
    USB_OUTPUT_REPORT_SIZE, BT_OUTPUT_REPORT_SIZE, USB_REPORT_SIZE,
)
from ds4tux.config import save_device_info
from ds4tux.exceptions import BackendError, DeviceError

logger = logging.getLogger("ds4tux.hidraw")

EVIOCGRAB = 0x40044590

# Map evdev ABS codes to DS4Report field names
ABS_MAP = {
    ecodes.ABS_X: "left_analog_x",
    ecodes.ABS_Y: "left_analog_y",
    ecodes.ABS_RX: "right_analog_x",
    ecodes.ABS_RY: "right_analog_y",
    ecodes.ABS_Z: "l2_analog",
    ecodes.ABS_RZ: "r2_analog",
}

# Map evdev KEY codes to DS4Report field names
KEY_MAP = {
    ecodes.BTN_SOUTH: "button_cross",    # BTN_304
    ecodes.BTN_EAST: "button_circle",     # BTN_305
    ecodes.BTN_NORTH: "button_triangle",  # BTN_307
    ecodes.BTN_WEST: "button_square",     # BTN_308
    ecodes.BTN_TL: "button_l1",           # BTN_310
    ecodes.BTN_TR: "button_r1",           # BTN_311
    ecodes.BTN_SELECT: "button_share",    # BTN_312
    ecodes.BTN_START: "button_options",   # BTN_313
    ecodes.BTN_THUMBL: "button_l3",       # BTN_314
    ecodes.BTN_THUMBR: "button_r3",       # BTN_315
    ecodes.BTN_MODE: "button_ps",         # BTN_316
    ecodes.BTN_TOUCH: "button_trackpad",  # BTN_317
}

class HidrawDevice:
    def __init__(self, hidraw_path: str, evdev_path: Optional[str] = None,
                 is_bluetooth: bool = False, copycat: bool = False,
                 mac: Optional[bytes] = None,
                 kernel_evdev_nodes: Optional[list[str]] = None,
                 input_syspaths: Optional[list[str]] = None):
        self.hidraw_path = hidraw_path
        self.evdev_path = evdev_path
        self.is_bluetooth = is_bluetooth
        self.copycat = copycat
        self.detected_copycat = copycat
        self.device_mode = "auto"
        self.mac = mac
        self.battery_path: Optional[str] = None
        self._report = DS4Report()
        self._fd: Optional[int] = None       # hidraw fd (for output only)
        self._evdev: Optional[InputDevice] = None
        self._on_report: Optional[callable] = None
        self._dead = False
        self._touchpad_inhibited = False
        self._parent_syspath: Optional[str] = None
        self._kernel_evdev_nodes = kernel_evdev_nodes or []
        self._grabbed_fds: list[int] = []
        self._input_syspaths = input_syspaths or []

    @property
    def report(self):
        return self._report

    @report.setter
    def report(self, value):
        self._report = value

    @property
    def address(self) -> str:
        if self.mac:
            return ":".join(f"{b:02x}" for b in self.mac)
        return os.path.basename(self.evdev_path or self.hidraw_path)

    @property
    def name(self) -> str:
        return os.path.basename(self.evdev_path or self.hidraw_path)

    @property
    def _effective_copycat(self) -> bool:
        if self.device_mode == "clone":
            return True
        if self.device_mode == "genuine":
            return False
        return self.copycat  # auto → use detected value

    def set_report_callback(self, cb: callable):
        self._on_report = cb

    def open(self) -> bool:
        try:
            self._fd = os.open(self.hidraw_path, os.O_RDWR | os.O_NONBLOCK)
        except (OSError, IOError) as e:
            logger.error("Cannot open hidraw %s: %s", self.hidraw_path, e)
            return False

        if self.mac is None:
            try:
                copycat, mac = detect_copycat(self._fd)
                self.copycat = copycat
                self.detected_copycat = copycat
                self.mac = mac
                if mac:
                    mac_str = ":".join(f"{b:02x}" for b in mac)
                    save_device_info(mac_str, {"detected_copycat": copycat})
            except DeviceError:
                logger.debug("Feature report detection failed, defaulting")

        if self.evdev_path and os.path.exists(self.evdev_path):
            try:
                self._evdev = InputDevice(self.evdev_path)
                self._grab()
            except Exception as e:
                logger.warning("Could not grab evdev %s: %s", self.evdev_path, e)

        # Inhibit all hid_playstation input devices via sysfs to prevent
        # event delivery to existing readers (Steam, desktop, etc.)
        for inp_path in self._input_syspaths:
            inhib_path = os.path.join(inp_path, "inhibited")
            if os.path.exists(inhib_path):
                try:
                    with open(inhib_path, "w") as f:
                        f.write("1\n")
                    logger.debug("Inhibited input device %s", inp_path)
                except Exception as e:
                    logger.warning("Could not inhibit %s: %s", inp_path, e)

        # Grab kernel evdev nodes from hid_playstation to prevent double input
        for ev_node in self._kernel_evdev_nodes:
            try:
                fd = os.open(ev_node, os.O_RDONLY | os.O_NONBLOCK)
                fcntl.ioctl(fd, EVIOCGRAB, 1)
                self._grabbed_fds.append(fd)
                logger.debug("Grabbed kernel evdev %s", ev_node)
            except Exception as e:
                logger.warning("Could not grab kernel evdev %s: %s", ev_node, e)

        logger.info(
            "Opened %s (evdev=%s, BT=%s, copycat=%s)",
            self.hidraw_path, self.evdev_path,
            self.is_bluetooth, self.copycat,
        )
        return True

    def uninhibit_touchpad(self):
        if not self._touchpad_inhibited or not self._parent_syspath:
            return
        for child_name in os.listdir(self._parent_syspath):
            child_path = os.path.join(self._parent_syspath, child_name)
            if not child_name.startswith("input"):
                continue
            name_file = os.path.join(child_path, "name")
            if not os.path.exists(name_file):
                continue
            try:
                name = open(name_file).read().strip()
            except (OSError, IOError):
                continue
            if "Touchpad" not in name:
                continue
            inhib_path = os.path.join(child_path, "inhibited")
            if os.path.exists(inhib_path):
                try:
                    with open(inhib_path, "w") as f:
                        f.write("0\n")
                    logger.info("Touchpad uninhibited")
                except (OSError, IOError):
                    pass
        self._touchpad_inhibited = False

    def _grab(self):
        if not self._evdev:
            return
        try:
            fcntl.ioctl(self._evdev.fd, EVIOCGRAB, 1)
        except (IOError, OSError) as e:
            logger.warning("EVIOCGRAB failed: %s", e)

    def _ungrab(self):
        if not self._evdev:
            return
        try:
            fcntl.ioctl(self._evdev.fd, EVIOCGRAB, 0)
        except (IOError, OSError):
            pass

    def fileno(self) -> Optional[int]:
        if self._dead:
            return None
        # Copycats: evdev never produces events, use hidraw fd for select
        if self.copycat:
            return self._fd
        if self._evdev:
            return self._evdev.fileno()
        return self._fd

    def read_report(self) -> Optional[DS4Report]:
        # Copycats: kernel driver (playstation) creates evdev but doesn't forward events.
        # Always read raw from hidraw on copycats, fallback to evdev only for genuine DS4.
        if self.copycat or not self._evdev:
            return self._read_hidraw()
        return self._read_evdev()

    def _read_evdev(self) -> Optional[DS4Report]:
        if not self._evdev:
            return None
        try:
            event = self._evdev.read_one()
        except BlockingIOError:
            return None
        except Exception as e:
            logger.error("evdev read error: %s", e)
            return None

        if event is None:
            return None

        r = self._report

        if event.type == ecodes.EV_ABS:
            field = ABS_MAP.get(event.code)
            if field:
                setattr(r, field, event.value)
            elif event.code == ecodes.ABS_HAT0X:
                old_dpad = r.dpad
                x = event.value
                if x == -1:
                    r.dpad = 6  # left
                elif x == 1:
                    r.dpad = 2  # right
                elif old_dpad in (6, 2):
                    r.dpad = 8  # neutral
            elif event.code == ecodes.ABS_HAT0Y:
                old_dpad = r.dpad
                y = event.value
                if y == -1:
                    r.dpad = 0  # up
                elif y == 1:
                    r.dpad = 4  # down
                elif old_dpad in (0, 4):
                    r.dpad = 8  # neutral

        elif event.type == ecodes.EV_KEY:
            field = KEY_MAP.get(event.code)
            if field:
                setattr(r, field, bool(event.value))
            elif event.code == ecodes.BTN_TL2:
                r.button_l2 = bool(event.value)
            elif event.code == ecodes.BTN_TR2:
                r.button_r2 = bool(event.value)

        elif event.type == ecodes.EV_SYN:
            r.timestamp = event.sec + event.usec / 1e6
            self._report = r
            if self._on_report:
                self._on_report(r)
            return r

        return None

    def _read_hidraw(self) -> Optional[DS4Report]:
        if self._fd is None or self._dead:
            return None
        try:
            data = os.read(self._fd, USB_REPORT_SIZE)
        except BlockingIOError:
            return None
        except (OSError, IOError) as e:
            logger.debug("hidraw read error: %s", e)
            self._dead = True
            return None

        if not data:
            return None

        report = parse_input_report(data, is_bluetooth=self.is_bluetooth, copycat=self._effective_copycat)
        if report:
            self._report = report
            if self._on_report:
                self._on_report(report)
        return report

    def write_output(self, r: int = 0, g: int = 0, b: int = 128,
                     blink_on: int = 0, blink_off: int = 0,
                     motor_left: int = 0, motor_right: int = 0) -> bool:
        if self._fd is None or self._dead:
            return False
        try:
            data = build_output_report(
                copycat=self._effective_copycat, is_bluetooth=self.is_bluetooth,
                r=r, g=g, b=b, blink_on=blink_on, blink_off=blink_off,
                motor_left=motor_left, motor_right=motor_right,
            )
            os.write(self._fd, data)
            return True
        except (OSError, IOError) as e:
            logger.debug("Write output failed: %s", e)
            self._dead = True
            return False

    def update_battery(self):
        if self.battery_path and os.path.exists(self.battery_path):
            try:
                cap = int(open(f"{self.battery_path}/capacity", "r").read().strip())
                self._report.usb_plugged = True  # USB-connected; hid_playstation manages charging
                self._report.battery = cap // 10  # scale 0-100 to 0-10
            except (OSError, ValueError):
                pass

    def close(self):
        # Uninhibit all inhibited input devices
        for inp_path in self._input_syspaths:
            inhib_path = os.path.join(inp_path, "inhibited")
            if os.path.exists(inhib_path):
                try:
                    with open(inhib_path, "w") as f:
                        f.write("0\n")
                except Exception:
                    pass
        self.uninhibit_touchpad()
        self._ungrab()
        for fd in self._grabbed_fds:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)
                os.close(fd)
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
        self._grabbed_fds.clear()
        if self._fd is not None:
            try:
                os.close(self._fd)
            except (OSError, IOError):
                pass
            self._fd = None
        if self._evdev:
            try:
                self._evdev.close()
            except Exception:
                pass
            self._evdev = None


class HidrawBackend:
    def __init__(self):
        self._ctx: Optional[pyudev.Context] = None
        self._monitor: Optional[pyudev.Monitor] = None
        self._devices: dict[str, HidrawDevice] = {}
        self._connection_callback = None
        self._disconnection_callback = None
        self.usb_detected_no_sudo = False

    @property
    def name(self) -> str:
        return "hidraw"

    def set_callbacks(self, on_connect=None, on_disconnect=None):
        self._connection_callback = on_connect
        self._disconnection_callback = on_disconnect

    def setup(self):
        self._ctx = pyudev.Context()
        self._monitor = pyudev.Monitor.from_netlink(self._ctx)
        self._monitor.filter_by(subsystem="hidraw")

    def enumerate_existing(self):
        if not self._ctx:
            return
        self.usb_detected_no_sudo = False
        for dev in self._ctx.list_devices(subsystem="hidraw"):
            self._check_device(dev)

    def _check_device(self, udev_dev) -> bool:
        syspath = udev_dev.sys_path
        if syspath in self._devices:
            return False
        hidraw_node = udev_dev.device_node
        if not hidraw_node:
            return False
        ancestors = list(udev_dev.ancestors)
        # Skip pure BT hidraw (no USB ancestor)
        if any(a.subsystem == "bluetooth" for a in ancestors) and \
           not any(a.subsystem == "usb" for a in ancestors):
            return False
        # Check via USB VID/PID from sysfs — works with hid_playstation loaded
        vid = pid = hid_name = None
        hid_syspath = None
        for a in ancestors:
            if a.subsystem == "usb":
                try:
                    v = a.attributes.get("idVendor").decode().strip().lower()
                    if v and not v.startswith("0000"):
                        vid = v
                        p = a.attributes.get("idProduct").decode().strip().lower()
                        pid = p
                        break  # stop at first real USB device, skip hub
                except Exception:
                    pass
            if a.subsystem == "hid":
                hid_name = a.get("HID_NAME", "")
                hid_syspath = a.sys_path
        known_pids = {"05c4", "09cc", "0ce6"}
        if vid == "054c" and pid in known_pids:
            copycat = False
        elif any(kw in hid_name.lower() for kw in ("wireless controller", "dualshock")):
            copycat = vid != "054c" if vid else True
        else:
            return False
        # Get MAC from HID_UNIQ (set by hid_playstation)
        mac = None
        for a in ancestors:
            if a.subsystem == "hid":
                uniq = a.get("HID_UNIQ", "")
                if uniq and ":" in uniq:
                    try:
                        mac = bytes(int(x, 16) for x in uniq.split(":"))
                    except ValueError:
                        pass
                break
        # Find kernel evdev nodes from hid_playstation to grab them (prevent double input)
        kernel_evdev_nodes: list[str] = []
        input_syspaths: list[str] = []
        if hid_syspath:
            inp_dir = os.path.join(hid_syspath, "input")
            if os.path.isdir(inp_dir):
                for inp_name in os.listdir(inp_dir):
                    inp_path = os.path.join(inp_dir, inp_name)
                    if not os.path.isdir(inp_path) or not inp_name.startswith("input"):
                        continue
                    input_syspaths.append(inp_path)
                    for child_name in os.listdir(inp_path):
                        if child_name.startswith("event"):
                            kernel_evdev_nodes.append(f"/dev/input/{child_name}")
        # Find battery path from HID device's power_supply child
        # (hid_playstation zeroes the raw report's battery byte)
        battery_path = None
        if hid_syspath:
            ps_dir = os.path.join(hid_syspath, "power_supply")
            if os.path.isdir(ps_dir):
                for entry in os.listdir(ps_dir):
                    if entry.startswith("ps-controller-battery-"):
                        battery_path = f"/sys/class/power_supply/{entry}"
                        break

        logger.info("DS4 detected at %s (copycat=%s, mac=%s)",
                     hidraw_node, copycat,
                     ":".join(f"{b:02x}" for b in mac) if mac else "N/A")
        if os.geteuid() != 0:
            self.usb_detected_no_sudo = True
            logger.warning("USB DS4 found at %s — requires sudo for USB access", hidraw_node)
            return False
        dev = HidrawDevice(hidraw_node, evdev_path=None, is_bluetooth=False,
                           copycat=copycat, mac=mac,
                           kernel_evdev_nodes=kernel_evdev_nodes,
                           input_syspaths=input_syspaths)
        dev.battery_path = battery_path
        if not dev.open():
            return False
        self._devices[syspath] = dev
        if self._connection_callback:
            self._connection_callback(dev)
        return True

    def poll_device_events(self, timeout: float = 1.0) -> bool:
        if not self._monitor:
            return False
        try:
            dev = self._monitor.poll(timeout=timeout)
            if dev:
                self._check_device(dev)
            return True
        except Exception:
            return False

    def check_disconnections(self):
        disconnected = []
        for syspath, dev in self._devices.items():
            if dev._dead:
                disconnected.append(syspath)
                continue
            fno = dev.fileno()
            if fno is not None:
                try:
                    fcntl.fcntl(fno, fcntl.F_GETFD)
                except (IOError, OSError):
                    disconnected.append(syspath)
        for syspath in disconnected:
            dev = self._devices.pop(syspath, None)
            if dev:
                logger.info("Device disconnected: %s", dev.hidraw_path)
                if self._disconnection_callback:
                    self._disconnection_callback(dev)
                dev.close()

    def stop(self):
        for dev in list(self._devices.values()):
            if self._disconnection_callback:
                self._disconnection_callback(dev)
            dev.close()
        self._devices.clear()
