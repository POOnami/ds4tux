"""
DualShock 4 HID protocol implementation.
Handles input/output/feature reports for both USB and Bluetooth transports,
including copycat controller quirks and fallback paths.

────────────────────────────────────────────────────────────────────────────────
INPUT REPORT LAYOUT — Bluetooth (long report, PSM 0x13 interrupt channel)
────────────────────────────────────────────────────────────────────────────────

Raw wire bytes on L2CAP (before any stripping):
  0      0xa0     HIDP Data-Input header     (stripped by bluez_l2cap.py)
  1      0x11     HID report ID              0x11 = DS4 BT input
  2      0x00     HIDP status byte           0x00 = success
  3      seq      Sequence number            increments each report, wraps 0..255

parse_input_report() strips bytes 0-2 via buf = data[2:], so buf[0] = seq.

Buf offsets after stripping (is_bluetooth=True, has_hidp_status=True):

  Off  Var               Description                     Range / encoding
  ───  ────────────────  ──────────────────────────────  ───────────────────────
  0    seq               Sequence number                 0..255
  1    LX                Left stick X                    0..255 (center 127)
  2    LY                Left stick Y                    0..255 (center 127)
  3    RX                Right stick X                   0..255 (center 127)
  4    RY                Right stick Y                   0..255 (center 127)
  5                      D-pad + face buttons            bitfield
                          bits 3:0  D-pad direction      0=up,1=ne,2=right,3=se,
                                                          4=down,5=sw,6=left,7=nw,
                                                          8=neutral
                          bit 4     Square
                          bit 5     Cross
                          bit 6     Circle
                          bit 7     Triangle
  6                      Shoulder + centre buttons       bitfield
                          bit 0     L1
                          bit 1     R1
                          bit 2     L2
                          bit 3     R2
                          bit 4     Share
                          bit 5     Options
                          bit 6     L3
                          bit 7     R3
  7                      PS + trackpad button            bitfield
                          bit 0     PS button
                          bit 1     Trackpad click
  8    L2                L2 trigger analog               0..255
  9    R2                R2 trigger analog               0..255

  ─── V1 (CUH-ZCT1, PID 0x05c4) continues at offset 10: ───
  10                     Battery + flags                 bitfield
                          bits 3:0  battery level        0..10 (→ raw*10 = %)
                          bit 4     USB plugged
                          bit 5     Headphones plugged
                          bit 6     Microphone plugged
  11                     reserved (0x00)
  12-23                  IMU data                        6 × le16 signed
                            12-13  accel X
                            14-15  accel Y
                            16-17  accel Z
                            18-19  gyro pitch
                            20-21  gyro yaw
                            22-23  gyro roll
                          Range: -32768..32767 (after sign fix)
  24-34                  reserved / unknown
  35-46                  Trackpad data                   12 bytes (6 per finger)
                            35/41  [touch:1, id:7]
                            36/42  X low 8 bits
                            37/43  [X hi nib : Y hi nib]
                            38/44  Y low 8 bits
                          Touch resolution: 1920 × 943
  47-77                  reserved / unknown              (to 78 bytes total)

  ─── V2 (CUH-ZCT2, PID 0x09cc) — 3 extra timestamp+reserved bytes before battery: ───
  10-11                  Timestamp                       le16, increments ~7/tick
  12                     reserved                        0x10 (detection flag)
  13                     Battery + flags                 same encoding as V1 at off 10
                           bits 3:0  battery level        0..10
                           bit 4     USB plugged
                           bit 5     Headphones plugged
                           bit 6     Microphone plugged
  14-15                  reserved (0x00, 0x00)
  16-27                  IMU data                        same struct as V1, shifted +4
                             16-17  accel X
                             18-19  accel Y
                             20-21  accel Z
                             22-23  gyro pitch
                             24-25  gyro yaw
                             26-27  gyro roll
  28-38                  reserved / unknown
  39-50                  Trackpad data                   shifted +4 vs V1
                             39/45  [touch:1, id:7]
                             40/46  X low 8 bits
                             41/47  [X hi nib : Y hi nib]
                             42/48  Y low 8 bits
  51-77                  reserved / unknown

  V2 detection: buf[12] == 0x10  (V1 has 0x00 here).
  imu_off = 4 for V2 to shift IMU and trackpad parsing (+4 bytes before V2 IMU vs V1).
  Note: In BT long reports the IMU and trackpad data share the same
  *physical* bytes in the report (they overlap in the buf).  This is
  by design — do not expect independent locations.

────────────────────────────────────────────────────────────────────────────────
INPUT REPORT LAYOUT — USB (and BT short report 0x01 on USB transport)
────────────────────────────────────────────────────────────────────────────────

After stripping report ID (0x01) only: buf = data[1:], so=0 (no seq byte).

  Off  Var               Description                     Range / encoding
  ───  ────────────────  ──────────────────────────────  ───────────────────────
  0    LX                Left stick X                    0..255
  1    LY                Left stick Y                    0..255
  2    RX                Right stick X                   0..255
  3    RY                Right stick Y                   0..255
  4-6                   Same button bitfields as BT     (no seq byte, same order)
  7    L2                L2 trigger analog               0..255
  8    R2                R2 trigger analog               0..255
  9-29                  reserved / unknown
  30                     Battery + flags                 same encoding as BT V1 at off 10
  31-34                  reserved / unknown
  35-46                  Trackpad data                   12 bytes (same format as BT)
  47-63                  reserved / unknown              (to 64 bytes USB report)

────────────────────────────────────────────────────────────────────────────────
BATTERY ENCODING
────────────────────────────────────────────────────────────────────────────────
  raw nibble 0-10 → battery_percent = raw * 10   (e.g. 5 → 50 %)
  raw nibble 10  → 100 % (controller reports 10 when full)
  usb_plugged    → is_charging = True
  Copycat controllers: usb_plugged fixed True (fake charge status).

────────────────────────────────────────────────────────────────────────────────
OUTPUT REPORT (to L2CAP CTRL channel)
────────────────────────────────────────────────────────────────────────────────
  Wire: [0x52, 0x11] HIDP Set-Report header  +  77-byte body (build_bt_output_report_raw)
  77-byte body:
    Off  Size  Description
      0   1     0x80  (HIDP flags)
      1   1     0x00  (reserved / zero)
      2   1     0xFF  (rumble timing)
      3-4  2    0x00  (reserved)
      5   1     motor_right (small, 0-255)
      6   1     motor_left  (large, 0-255)
      7   1     G
      8   1     R
      9   1     B
     10   1     blink_on  (10ms units)
     11   1     blink_off (10ms units)
     12-76  65  zeros/padding
"""

from __future__ import annotations
import struct
import time
import logging
import binascii
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ds4tux")

# DS4 vendor/product IDs
SONY_VID = 0x054c
DS4_V1_PID = 0x05c4
DS4_V2_PID = 0x09cc

# Report IDs
REPORT_ID_USB = 0x01
REPORT_ID_BT = 0x11
REPORT_ID_USB_OUTPUT = 0x05
REPORT_ID_BT_OUTPUT = 0x11
FEATURE_REPORT_MAC = 0x81
FEATURE_REPORT_MAC_FALLBACK = 0x12

# Report sizes
USB_REPORT_SIZE = 64
USB_OUTPUT_REPORT_SIZE = 32
BT_REPORT_SIZE = 78
BT_OUTPUT_REPORT_SIZE = 78
FEATURE_REPORT_MAC_SIZE = 7
FEATURE_REPORT_MAC_FALLBACK_SIZE = 16

# Battery constants
BATTERY_MAX = 10  # DS4 reports 0-10
BATTERY_CAPACITY_MAX = 100


@dataclass
class DS4Report:
    timestamp: float = 0.0

    left_analog_x: int = 127
    left_analog_y: int = 127
    right_analog_x: int = 127
    right_analog_y: int = 127

    l2_analog: int = 0
    r2_analog: int = 0

    dpad: int = 8  # 8 = neutral

    button_cross: bool = False
    button_circle: bool = False
    button_square: bool = False
    button_triangle: bool = False

    button_l1: bool = False
    button_r1: bool = False
    button_l2: bool = False
    button_r2: bool = False

    button_l3: bool = False
    button_r3: bool = False

    button_share: bool = False
    button_options: bool = False
    button_ps: bool = False
    button_trackpad: bool = False

    battery: int = 0
    usb_plugged: bool = False
    mic_plugged: bool = False
    headphones_plugged: bool = False

    gyro_pitch: float = 0.0
    gyro_roll: float = 0.0
    gyro_yaw: float = 0.0
    gyro_raw_x: int = 0
    gyro_raw_y: int = 0
    gyro_raw_z: int = 0
    gyro_filtered_x: float = 0.0
    gyro_filtered_y: float = 0.0
    gyro_filtered_z: float = 0.0
    accel_x: int = 0
    accel_y: int = 0
    accel_z: int = 0

    trackpad_touch1: bool = False
    trackpad_touch2: bool = False
    trackpad_x1: int = 0
    trackpad_y1: int = 0
    trackpad_x2: int = 0
    trackpad_y2: int = 0

    copycat: bool = False

    def copy(self) -> DS4Report:
        return DS4Report(**{k: v for k, v in self.__dict__.items()})

    @property
    def battery_percent(self) -> int:
        return max(0, min(100, int((self.battery / BATTERY_MAX) * 100)))

    @property
    def is_charging(self) -> bool:
        return self.usb_plugged


def parse_input_report(data: bytes, is_bluetooth: bool = False, copycat: bool = False) -> Optional[DS4Report]:
    if not data or len(data) < 2:
        return None

    r = DS4Report()
    r.timestamp = time.time()
    r.copycat = copycat

    imu_off = 0

    if is_bluetooth:
        if data[0] == REPORT_ID_BT:
            buf = data[2:]  # skip report ID + HIDP status byte → [seq, left_x, ...]
            has_hidp_status = True
            so = 1  # BT has a leading seq byte at buf[0]
        elif data[0] == REPORT_ID_USB:
            buf = data[2:]  # skip report ID + HIDP status byte → [left_x, ...]
            has_hidp_status = False
            so = 0  # USB short reports over BT have no seq byte
        else:
            return None
        if len(buf) < 64:
            return None
        if has_hidp_status:
            # BT long report (report ID 0x11)
            #   buf[0]  = seq
            #   buf[1..4]  = LX, LY, RX, RY
            #   buf[5]  = dpad+face buttons
            #   buf[6]  = shoulder/thumb buttons
            #   buf[7]  = counter + PS/trackpad buttons
            #   buf[8]  = L2 analog
            #   buf[9]  = R2 analog
            #   buf[10..11] = timestamp le16
            #   buf[12..23] = IMU (accel X/Y/Z, gyro pitch/yaw/roll — 6 × le16)
            #   buf[24] = IMU-related (changes with motion)
            #   buf[25..29] = zeros (padding)
            #   buf[30] = battery (lower nibble 0-10, upper nibble flags)
            #   buf[31..32] = zeros
            #   buf[33] = 0x01 (status byte, bit0 set)
            #   buf[34] = zeros
            #   buf[35..46] = trackpad (12 bytes)
            bat_off = 30
            r.battery = (buf[bat_off] & 0x0F) if len(buf) > bat_off else 0
            r.usb_plugged = bool(buf[bat_off] & 0x10) if len(buf) > bat_off else False
            r.mic_plugged = bool(buf[bat_off] & 0x40) if len(buf) > bat_off else False
            r.headphones_plugged = bool(buf[bat_off] & 0x20) if len(buf) > bat_off else False
        else:
            r.battery = (buf[30] & 0x0F) if len(buf) > 30 else 0
            r.usb_plugged = bool(buf[30] & 0x10) if len(buf) > 30 else False
            r.mic_plugged = bool(buf[30] & 0x40) if len(buf) > 30 else False
            r.headphones_plugged = bool(buf[30] & 0x20) if len(buf) > 30 else False
    else:
        if data[0] != REPORT_ID_USB:
            return None
        buf = data[1:]  # skip report ID → [left_x, ...]
        so = 0  # USB HID has no seq byte
        # USB HID report, after stripping report_id (0x01):
        #   buf[0]  = left_analog_x     (LX)
        #   buf[1]  = left_analog_y     (LY)
        #   buf[2]  = right_analog_x    (RX)
        #   buf[3]  = right_analog_y    (RY)
        #   ...same field order as BT but NO seq byte (so=0)
        #   buf[30] = battery + status
        r.battery = (buf[30] & 0x0F) if len(buf) > 30 else 0
        r.usb_plugged = bool(buf[30] & 0x10) if len(buf) > 30 else False
        r.mic_plugged = bool(buf[30] & 0x40) if len(buf) > 30 else False
        r.headphones_plugged = bool(buf[30] & 0x20) if len(buf) > 30 else False

    if copycat:
        r.usb_plugged = True

    # Standard DS4 input report layout — sticks and buttons align at same
    # positions after stripping headers, except BT has a leading seq byte.
    # Same order for both; `so` (1 for BT, 0 for USB) accounts for the seq byte.
    r.left_analog_x = buf[0 + so] if len(buf) > 0 + so else 127
    r.left_analog_y = buf[1 + so] if len(buf) > 1 + so else 127
    r.right_analog_x = buf[2 + so] if len(buf) > 2 + so else 127
    r.right_analog_y = buf[3 + so] if len(buf) > 3 + so else 127

    if len(buf) > 4 + so:
        b = buf[4 + so]
        r.dpad = (b & 0x0F)
        r.button_square = bool(b & 0x10)
        r.button_cross = bool(b & 0x20)
        r.button_circle = bool(b & 0x40)
        r.button_triangle = bool(b & 0x80)

    if len(buf) > 5 + so:
        b2 = buf[5 + so]
        r.button_l1 = bool(b2 & 0x01)
        r.button_r1 = bool(b2 & 0x02)
        r.button_l2 = bool(b2 & 0x04)
        r.button_r2 = bool(b2 & 0x08)
        r.button_share = bool(b2 & 0x10)
        r.button_options = bool(b2 & 0x20)
        r.button_l3 = bool(b2 & 0x40)
        r.button_r3 = bool(b2 & 0x80)

    if len(buf) > 6 + so:
        b3 = buf[6 + so]
        r.button_ps = bool(b3 & 0x01)
        r.button_trackpad = bool(b3 & 0x02)

    r.l2_analog = buf[7 + so] if len(buf) > 7 + so else 0
    r.r2_analog = buf[8 + so] if len(buf) > 8 + so else 0

    if len(buf) > 17 + imu_off:
        r.accel_x = buf[12 + imu_off] | (buf[13 + imu_off] << 8)
        r.accel_y = buf[14 + imu_off] | (buf[15 + imu_off] << 8)
        r.accel_z = buf[16 + imu_off] | (buf[17 + imu_off] << 8)
        if r.accel_x > 32767: r.accel_x -= 65536
        if r.accel_y > 32767: r.accel_y -= 65536
        if r.accel_z > 32767: r.accel_z -= 65536

    if len(buf) > 23 + imu_off:
        r.gyro_raw_x = buf[18 + imu_off] | (buf[19 + imu_off] << 8)
        r.gyro_raw_y = buf[20 + imu_off] | (buf[21 + imu_off] << 8)
        r.gyro_raw_z = buf[22 + imu_off] | (buf[23 + imu_off] << 8)
        if r.gyro_raw_x > 32767: r.gyro_raw_x -= 65536
        if r.gyro_raw_y > 32767: r.gyro_raw_y -= 65536
        if r.gyro_raw_z > 32767: r.gyro_raw_z -= 65536

    # Trackpad at buf[35..46] for BT long reports (same position for V1/V2)
    if is_bluetooth:
        touch_base = 35 if not has_hidp_status else (35 + imu_off)
    else:
        touch_base = 35

    if len(buf) > touch_base + 12:
        td = buf[touch_base:touch_base + 12]
        r.trackpad_touch1 = not bool(td[0] & 0x80)  # inverted: 0=active
        r.trackpad_x1 = ((td[2] & 0x0F) << 8) | td[1]
        r.trackpad_y1 = (td[3] << 4) | ((td[2] & 0xF0) >> 4)
        r.trackpad_touch2 = not bool(td[6] & 0x80)
        r.trackpad_x2 = ((td[8] & 0x0F) << 8) | td[7]
        r.trackpad_y2 = (td[9] << 4) | ((td[8] & 0xF0) >> 4)

    return r


def build_bt_output_report_raw(
    r: int = 0, g: int = 0, b: int = 0,
    blink_on: int = 0, blink_off: int = 0,
    motor_left: int = 0, motor_right: int = 0,
) -> bytes:
    """77-byte BT output report body for raw L2CAP (ds4drv / clone format).
    Send on CTRL socket with HIDP header [0x52, 0x11] prepended."""
    pkt = bytearray(77)
    pkt[0] = 0x80
    pkt[2] = 0xFF
    pkt[5] = motor_right
    pkt[6] = motor_left
    pkt[7] = r
    pkt[8] = g
    pkt[9] = b
    pkt[10] = blink_on
    pkt[11] = blink_off
    return bytes(pkt)


def build_bt_output_report_genuine(
    r: int = 0, g: int = 0, b: int = 0,
    blink_on: int = 0, blink_off: int = 0,
    motor_left: int = 0, motor_right: int = 0,
) -> bytes:
    """78-byte BT output report matching the kernel's genuine DS4 format.
    Uses valid_flag system and CRC32. Send with HIDP header [0x52, 0x11]."""
    buf = bytearray(78)
    buf[0] = REPORT_ID_BT_OUTPUT
    buf[1] = 0xC0  # DS4_OUTPUT_HWCTL_HID | DS4_OUTPUT_HWCTL_CRC32
    buf[2] = 0x00  # audio_control
    buf[3] = 0x00  # valid_flag0
    buf[4] = 0x00  # valid_flag1
    buf[5] = 0x00  # reserved
    buf[6] = motor_right
    buf[7] = motor_left
    if r or g or b or blink_on or blink_off:
        buf[3] |= 0x02  # DS4_OUTPUT_VALID_FLAG0_LED
        buf[8] = r
        buf[9] = g
        buf[10] = b
        buf[11] = blink_on
        buf[12] = blink_off
        if blink_on or blink_off:
            buf[3] |= 0x04  # DS4_OUTPUT_VALID_FLAG0_LED_BLINK
    if motor_left or motor_right:
        buf[3] |= 0x01  # DS4_OUTPUT_VALID_FLAG0_MOTOR
    # CRC32: seed=0xA2, crc32_le over buf[:74], inverted
    crc = binascii.crc32(bytes([0xA2]), 0xFFFFFFFF)
    crc = binascii.crc32(buf[:74], crc)
    crc ^= 0xFFFFFFFF
    buf[74] = crc & 0xFF
    buf[75] = (crc >> 8) & 0xFF
    buf[76] = (crc >> 16) & 0xFF
    buf[77] = (crc >> 24) & 0xFF
    return bytes(buf)


def build_usb_output_report(
    copycat: bool,
    r: int = 0, g: int = 0, b: int = 128,
    blink_on: int = 0, blink_off: int = 0,
    motor_left: int = 0, motor_right: int = 0,
) -> bytes:
    """32-byte USB output report."""
    buf = bytearray(32)
    buf[0] = REPORT_ID_USB_OUTPUT
    valid = 0x00
    if motor_left or motor_right:
        valid |= 0x02
    if r or g or b or blink_on or blink_off:
        valid |= 0x04
    buf[1] = valid
    buf[2] = 0x00
    buf[3] = motor_left
    buf[4] = motor_right
    if copycat:
        buf[5] = b
        buf[6] = r
        buf[7] = g
    else:
        buf[5] = r
        buf[6] = g
        buf[7] = b
    buf[8] = blink_on
    buf[9] = blink_off
    return bytes(buf)


def build_output_report(
    copycat: bool,
    is_bluetooth: bool,
    r: int = 0,
    g: int = 0,
    b: int = 128,
    blink_on: int = 0,
    blink_off: int = 0,
    motor_left: int = 0,
    motor_right: int = 0,
) -> bytes:
    if is_bluetooth:
        if copycat:
            raw = build_bt_output_report_raw(r, g, b, blink_on, blink_off, motor_left, motor_right)
            return raw + b'\x00'
        return build_bt_output_report_genuine(r, g, b, blink_on, blink_off, motor_left, motor_right)
    return build_usb_output_report(copycat, r, g, b, blink_on, blink_off, motor_left, motor_right)


def read_feature_report(fd, report_id: int, size: int) -> Optional[bytes]:
    import fcntl
    import struct
    from ds4tux.exceptions import DeviceError

    HIDIOCGFEATURE = 0xC0064807
    buf = bytearray(size)
    buf[0] = report_id
    try:
        packed = struct.pack('Ii', report_id, size)
        fcntl.ioctl(fd, HIDIOCGFEATURE, packed)
        ret = fcntl.ioctl(fd, HIDIOCGFEATURE, buf)
        if isinstance(ret, bytes):
            return ret
        return bytes(buf)
    except (IOError, OSError) as e:
        raise DeviceError(f"Feature report 0x{report_id:02x} failed: {e}")


def detect_copycat(fd_hidraw) -> tuple[bool, Optional[bytes]]:
    mac: Optional[bytes] = None
    copycat = False

    try:
        buf = read_feature_report(fd_hidraw, FEATURE_REPORT_MAC, FEATURE_REPORT_MAC_SIZE)
        if buf and len(buf) >= 7:
            mac = buf[1:7]
            logger.info("Genuine DS4 detected, MAC: %s", ":".join(f"{b:02x}" for b in mac))
    except Exception:
        try:
            buf = read_feature_report(fd_hidraw, FEATURE_REPORT_MAC_FALLBACK, FEATURE_REPORT_MAC_FALLBACK_SIZE)
            if buf and len(buf) >= 7:
                mac = buf[1:7]
                copycat = True
                logger.warning("Copycat DS4 detected (feature report 0x81 failed, used 0x12 fallback)")
        except Exception:
            copycat = True
            logger.warning("Copycat DS4 detected (no feature report available)")

    return copycat, mac
