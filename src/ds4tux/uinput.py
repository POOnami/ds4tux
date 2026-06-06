"""
uinput device creation for virtual gamepad emulation.
Creates evdev virtual devices that games and Steam can consume.
"""

from __future__ import annotations
import os
import struct
import time
import logging
from typing import Optional
from evdev import UInput, ecodes as ec, AbsInfo

from ds4tux.device import DS4Report

logger = logging.getLogger("ds4tux.uinput")

AXIS_MIN = 0
AXIS_MAX = 255
AXIS_CENTER = 127
AXIS_FUZZ = 0
AXIS_FLAT = 0

TRIGGER_MIN = 0
TRIGGER_MAX = 255


MAPPINGS: dict[str, dict] = {}


def _name(name: str, bus: int = 0x03, vendor: int = 0x054C,
          product: int = 0x09CC, version: int = 0x8111) -> dict:
    return {
        "name": name,
        "bustype": bus,
        "vendor": vendor,
        "product": product,
        "version": version,
    }


MAPPINGS["xpad"] = {
    **_name("Xbox 360 Controller", vendor=0x045E, product=0x028E, version=0x0110),
    "axes": [
        (ec.ABS_X, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_Y, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_RX, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_RY, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_Z, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)),
        (ec.ABS_RZ, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)),
        (ec.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
        (ec.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
    ],
    "buttons": [
        ec.BTN_A, ec.BTN_B, ec.BTN_X, ec.BTN_Y,
        ec.BTN_TL, ec.BTN_TR, ec.BTN_TL2, ec.BTN_TR2,
        ec.BTN_SELECT, ec.BTN_START, ec.BTN_MODE,
        ec.BTN_THUMBL, ec.BTN_THUMBR,
    ],
}

MAPPINGS["xboxdrv"] = {
    **_name("Xbox Gamepad (userspace)", vendor=0x045E, product=0x028E, version=0x0110),
    "axes": [
        (ec.ABS_X, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_Y, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_RX, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_RY, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_Z, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)),
        (ec.ABS_RZ, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)),
        (ec.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
        (ec.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
    ],
    "buttons": [
        ec.BTN_A, ec.BTN_B, ec.BTN_X, ec.BTN_Y,
        ec.BTN_TL, ec.BTN_TR, ec.BTN_TL2, ec.BTN_TR2,
        ec.BTN_SELECT, ec.BTN_START, ec.BTN_MODE,
        ec.BTN_THUMBL, ec.BTN_THUMBR,
    ],
}

MAPPINGS["ds4"] = {
    **_name("Sony DualShock 4"),
    "axes": [
        (ec.ABS_X, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_Y, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_RX, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_RY, AbsInfo(0, AXIS_MIN, AXIS_MAX, AXIS_FUZZ, AXIS_FLAT, AXIS_CENTER)),
        (ec.ABS_Z, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)),
        (ec.ABS_RZ, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)),
        (ec.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
        (ec.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
    ],
    "buttons": [
        ec.BTN_A, ec.BTN_B, ec.BTN_X, ec.BTN_Y,
        ec.BTN_TL, ec.BTN_TR, ec.BTN_TL2, ec.BTN_TR2,
        ec.BTN_SELECT, ec.BTN_START, ec.BTN_MODE,
        ec.BTN_THUMBL, ec.BTN_THUMBR,
    ],
}

GYRO_AXES = [
    (ec.ABS_RX, AbsInfo(0, -32768, 32767, 0, 0, 0)),
    (ec.ABS_RY, AbsInfo(0, -32768, 32767, 0, 0, 0)),
    (ec.ABS_RZ, AbsInfo(0, -32768, 32767, 0, 0, 0)),
]

DPAD_MAP = {
    0: (0, -1), 1: (1, -1), 2: (1, 0), 3: (1, 1),
    4: (0, 1), 5: (-1, 1), 6: (-1, 0), 7: (-1, -1),
    8: (0, 0),
}


class UinputDevice:
    def __init__(self, mapping_name: str = "xpad", gyro: bool = False):
        self.mapping_name = mapping_name
        self.gyro_enabled = gyro
        self._uinput: Optional[UInput] = None
        self._gyro_uinput: Optional[UInput] = None
        self._last_report: Optional[DS4Report] = None
        self._active = True

        cfg = MAPPINGS.get(mapping_name)
        if not cfg:
            logger.warning("Unknown mapping %s, falling back to xpad", mapping_name)
            cfg = MAPPINGS["xpad"]

        self._cfg = cfg
        self._create()

    def _create(self):
        if not self._active:
            return

        cfg = self._cfg
        events = {
            ec.EV_ABS: cfg["axes"],
            ec.EV_KEY: cfg["buttons"],
        }

        try:
            self._uinput = UInput(
                events=events,
                name=cfg["name"],
                bustype=cfg["bustype"],
                vendor=cfg["vendor"],
                product=cfg["product"],
                version=cfg["version"],
            )
            logger.info("Created uinput device: %s (%s)", cfg["name"], self.mapping_name)
        except Exception as e:
            logger.error("Failed to create uinput device: %s", e)
            self._uinput = None

        if self.gyro_enabled:
            try:
                gyro_events = {ec.EV_ABS: list(GYRO_AXES)}
                self._gyro_uinput = UInput(
                    events=gyro_events,
                    name="DS4 Gyro",
                    bustype=0x03,
                    vendor=0x054C,
                    product=0x09CC,
                    version=0x8111,
                    input_props=[6],
                )
            except Exception as e:
                logger.warning("Failed to create gyro device: %s", e)

    def close(self):
        if self._uinput:
            try:
                self._uinput.close()
            except Exception:
                pass
            self._uinput = None
        if self._gyro_uinput:
            try:
                self._gyro_uinput.close()
            except Exception:
                pass
            self._gyro_uinput = None

    def set_active(self, active: bool):
        if active == self._active:
            return
        self._active = active
        if active:
            self._create()
        else:
            self.close()

    def _write_ev(self, fd: int, etype: int, code: int, value: int):
        sec, usec = divmod(time.time(), 1)
        usec = int(usec * 1_000_000)
        ev = struct.pack('qqHHi', int(sec), usec, etype, code, value)
        n = os.write(fd, ev)
        if n != 24:
            logger.error("_write_ev wrote %d bytes (expected 24) type=%d code=%d val=%d", n, etype, code, value)

    def emit(self, report: DS4Report):
        if not self._active or not self._uinput:
            logger.debug("emit skipped: active=%s uinput=%s", self._active, self._uinput is not None)
            return

        if self._last_report and report == self._last_report:
            logger.debug("emit skipped: duplicate report")
            return

        fd = self._uinput.fd
        try:
            Lx = self._apply_deadzone(report.left_analog_x, 127, 5)
            Ly = self._apply_deadzone(report.left_analog_y, 127, 5)
            Rx = self._apply_deadzone(report.right_analog_x, 127, 5)
            Ry = self._apply_deadzone(report.right_analog_y, 127, 5)

            self._write_ev(fd, ec.EV_ABS, ec.ABS_X, Lx)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_Y, Ly)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_RX, Rx)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_RY, Ry)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_Z, report.l2_analog)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_RZ, report.r2_analog)

            hat_x, hat_y = DPAD_MAP.get(report.dpad, (0, 0))
            self._write_ev(fd, ec.EV_ABS, ec.ABS_HAT0X, hat_x)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_HAT0Y, hat_y)

            self._write_ev(fd, ec.EV_KEY, ec.BTN_A, int(report.button_cross))
            self._write_ev(fd, ec.EV_KEY, ec.BTN_B, int(report.button_circle))
            self._write_ev(fd, ec.EV_KEY, ec.BTN_X, int(report.button_square))
            self._write_ev(fd, ec.EV_KEY, ec.BTN_Y, int(report.button_triangle))

            self._write_ev(fd, ec.EV_KEY, ec.BTN_TL, int(report.button_l1))
            self._write_ev(fd, ec.EV_KEY, ec.BTN_TR, int(report.button_r1))
            self._write_ev(fd, ec.EV_KEY, ec.BTN_TL2, int(report.button_l2))
            self._write_ev(fd, ec.EV_KEY, ec.BTN_TR2, int(report.button_r2))

            self._write_ev(fd, ec.EV_KEY, ec.BTN_THUMBL, int(report.button_l3))
            self._write_ev(fd, ec.EV_KEY, ec.BTN_THUMBR, int(report.button_r3))

            self._write_ev(fd, ec.EV_KEY, ec.BTN_SELECT, int(report.button_share))
            self._write_ev(fd, ec.EV_KEY, ec.BTN_START, int(report.button_options))
            self._write_ev(fd, ec.EV_KEY, ec.BTN_MODE, int(report.button_ps))

            self._write_ev(fd, ec.EV_SYN, ec.SYN_REPORT, 0)

            if self._gyro_uinput and self.gyro_enabled:
                gfd = self._gyro_uinput.fd
                self._write_ev(gfd, ec.EV_ABS, ec.ABS_RX, int(report.gyro_raw_x))
                self._write_ev(gfd, ec.EV_ABS, ec.ABS_RY, int(report.gyro_raw_y))
                self._write_ev(gfd, ec.EV_ABS, ec.ABS_RZ, int(report.gyro_raw_z))
                self._write_ev(gfd, ec.EV_SYN, ec.SYN_REPORT, 0)

            self._last_report = report

        except Exception as e:
            logger.warning("uinput emit error: %s", e)

    def emit_reset(self):
        if not self._uinput:
            return
        fd = self._uinput.fd
        try:
            self._write_ev(fd, ec.EV_ABS, ec.ABS_X, AXIS_CENTER)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_Y, AXIS_CENTER)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_RX, AXIS_CENTER)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_RY, AXIS_CENTER)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_Z, 0)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_RZ, 0)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_HAT0X, 0)
            self._write_ev(fd, ec.EV_ABS, ec.ABS_HAT0Y, 0)

            for btn in self._cfg["buttons"]:
                self._write_ev(fd, ec.EV_KEY, btn, 0)

            self._write_ev(fd, ec.EV_SYN, ec.SYN_REPORT, 0)
            self._last_report = None
        except Exception:
            pass

    def _apply_deadzone(self, value: int, center: int = 127, deadzone: int = 5) -> int:
        if abs(value - center) <= deadzone:
            return center
        return value


class UinputManager:
    def __init__(self):
        self._devices: dict[str, UinputDevice] = {}
        self._active = True

    def get_or_create(self, device_id: str, mapping: str = "xpad",
                      gyro: bool = False) -> Optional[UinputDevice]:
        if device_id in self._devices:
            return self._devices[device_id]
        dev = UinputDevice(mapping, gyro=gyro)
        if dev._uinput:
            self._devices[device_id] = dev
            return dev
        return None

    def remove(self, device_id: str):
        dev = self._devices.pop(device_id, None)
        if dev:
            dev.emit_reset()
            dev.close()

    def set_active(self, active: bool):
        self._active = active
        for dev in self._devices.values():
            dev.set_active(active)

    def emit(self, device_id: str, report: DS4Report):
        dev = self._devices.get(device_id)
        if dev and self._active:
            dev.emit(report)

    def emit_reset_all(self):
        for dev in self._devices.values():
            dev.emit_reset()

    def close_all(self):
        for dev in self._devices.values():
            dev.close()
        self._devices.clear()
