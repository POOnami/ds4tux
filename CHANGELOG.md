# Changelog

## v0.3.3 - 2026-06-10

- **`copycat` → `device_mode`** - config key renamed to `device_mode` with values
  `auto`/`clone`/`genuine`. Old `yes`/`no` auto-migrated. `clone` forces copycat
  quirks, `genuine` forces standard, `auto` uses detection or saved per-MAC config.
- **BT genuine output format** - `build_bt_output_report_genuine()` produces the
  kernel-matching 78-byte format: valid_flag0=0x02, hw_control=0xC0,
  CRC32(seed=0xA2). `clone` mode uses the legacy 77-byte ds4drv raw format.
- **Per-MAC config persistence** - USB feature report probing now saves
  `detected_copycat` under `[device."MAC"]` in config.toml. BT `auto` mode
  looks up the saved entry. `_scan_usb_ds4_devices()` runs every 5s in the
  L2CAP loop, finding USB DS4 devices independently and saving their info.
- **BT `detected_copycat = None`** - L2CAP device sets `None` for "unknown"
  (not `False`), so the TUI doesn't show a misleading "genuine" detection.
- **Trackpad always parsed** - removed `if not copycat:` guard. Copycat
  controllers use the same touchpad layout as genuine ones.
- **TUI updates** - "Compatibility Mode" label replaces "Copycat". Value cycles
  `auto`/`clone`/`genuine`. Detected suffix shown when available, hidden when
  `None`. Ctrl+C exits cleanly instead of dumping a traceback.
- **LED fixes** - duplicate orange LED removed from `set_device()` (only
  `apply_config()` handles it). Brightness default fixed to `60`.
  Stale config on reconnect fixed: `on_connect()` reloads config from disk.
  TUI syncs hue/brightness/hex from daemon state on connection.
- **BT RGB order fix** - `build_bt_output_report_raw()` was sending G,R,B
  instead of R,G,B. Fixed.
- **USB probing persistence** - when user sets `clone`/`genuine` via TUI,
  the choice is saved per-MAC via `save_device_info()`.
- **Gyro passthrough** - `GyroProcessor` is raw passthrough (no calibration,
  no filtering). `ABS_RX`/`ABS_RY`/`ABS_RZ` with `INPUT_PROP_ACCELEROMETER`.
- **Uinput survival** - uinput device is never closed on disconnect, only
  zeroed via `emit_reset()`. Games keep their fd and see idle input.
- **Socket 0o777** - `/run/ds4tux/control.sock` world-accessible for non-root TUI.
- **BlueZ cleanup** - `bluez_profile.py` (272 lines of dead code) removed.
  `_SIGNAL_MATCHES` changed from global to instance variable.
- **Battery fix** - removed `if usb_plugged: return 100` - always reads
  actual battery register now.
- **Audio udev rule** - `50-ds4tux-audio.rules` unbinds DS4 V2 headphone jack
  from `snd-usb-audio` to prevent kernel audio device takeover.
- **Version bump** - `__init__.py` `__version__` → `"0.3.3"`.

## v0.3.2 - 2026-06-08

- **USB: auto-switch to ds4 mapping** - `on_connect()` forces `_mapping = "ds4"`
  when device is USB (not Bluetooth), minimizing double-input risk when
  Steam reads hidraw directly while ds4tux also emits via uinput.
- **TUI: inverse boxes for buttons** - replaced `●`/`○` circles with
  `[label]` `A_REVERSE` boxes. Dpad cross added. Full names SHARE/OPTIONS.
  Config save now writes `mapping` at TOML root level (was nested under
  `[led]`, making it unreadable on restart).
- **LED: brightness slider no longer triggers full-brightness flash** -
  `set_brightness()` clears `_full_brightness_until` so adjusting brightness
  level doesn't momentarily override to 100%.
- **Server state: `bluetooth` and `dpad` fields** broadcast to TUI.
- **Version bump** - `__init__.py` `__version__` → `"0.3.2"`.

## v0.3.1 - 2026-06-08

- **CTRL-hold reconnect** - bluez_l2cap.py `_start_reconnect()`
  DS4 V2 firmware has an internal ~15s INTR readiness timer that resets
  every time the CTRL channel (PSM 0x11) is closed. The original loop
  closed CTRL, slept 1s, opened CTRL, tried INTR, failed, and repeated -
  each cycle reset the timer, yielding ~40s total reconnect time.
  Fix: open CTRL once and keep it open while polling INTR every 500ms.
  The timer keeps running between polls, dropping reconnect to ~10–15s.
  Also added `ctrl.setblocking(False)` before the aliveness `recv()` check
  (missing this caused `socket.timeout` to break the inner loop).
  Verified: wall-clock reconnect before vs after the fix.

## v0.3.0 - 2026-06-08

- **Battery byte at buf[30]** - device.py `bat_off=30`
  DS4 V2 sends BT long reports (report ID 0x11, 78 bytes) by default, not
  short reports (ID 0x01, 64 bytes). The battery byte lives at buf[30] in
  long reports, not buf[10] (short report position).
  Lower nibble: 0–10 → `percent = raw * 10`. Upper nibble flags: bit4=USB,
  bit5=headphones, bit6=mic.
  Verified empirically: buf[30]=0x0a in initial session (100%), then 0x09
  in next session (90%) after draining. Stable across all IMU states and
  touch input. No other byte in the report correlates with battery drain.

- **Trackpad active bit inverted** - device.py `touch1`/`touch2`
  DS4 V2 over BT: `0x80` = idle ghost (no touch, stale position at 0,0),
  `0x00` = real touch. This is the inverse of the V1/USB convention.
  Fix: `not bool(td[0] & 0x80)`. The standard DS4 kernel driver uses
  `bool(byte & 0x80)` which would flag every ghost as an active touch.
  Verified: 2-finger tracking works correctly, double-tap gesture hides
  on-screen data as expected.

- **HIDP header 0xff parameter byte** - bluez_l2cap.py
  DS4 over BT sends HIDP Get-Report Response packets structured as:
  `[0xa1] [optional 0xff flush timeout] [report ID] [data...]`
  The original code assumed `[0xa1] [report ID]` and dropped any packet
  with an extra byte between them.
  Fix: after stripping `0xa1`, skip any additional byte before the report
  ID. Both `0xa1 0x11` and `0xa1 0xff 0x11` are now handled identically.
  Verified: previously dropped packets now parse into valid `DS4Report`s.

- **Software blink** - LEDController `update()` / `set_blink()`
  DS4 V2 ignores hardware blink registers (`blink_on`, `blink_off` in the
  output report) over Bluetooth. Setting them to non-zero has no effect.
  Fix: software-controlled blink via timer-based state machine.
  `set_blink(on_ms, off_ms, duration)` starts alternating between the
  target color and off at the requested interval. `update()` is called
  every report cycle and toggles when the timer expires. Hardware blink
  registers are always sent as 0.
  Verified: blink cycles at correct rate, terminates after duration.

- **Battery blink edge-triggered** - `__main__.py` `process_report()`
  `BatteryMonitor.update()` returns `True` only on the not-low→low
  transition (edge detection), not on every report while battery is low.
  Originally `set_blink()` was called every time `update()` returned True,
  which was every report during low battery - causing LED flicker from
  threshold bouncing.
  Fix: call `set_blink()` only when `just_low` (the edge-detected
  transition) is True.

- **BatteryMonitor hysteresis** - `actions/battery.py`
  Low-battery entry threshold: 20%. Exit threshold: 25% (`_restore_threshold`).
  Without hysteresis, a battery hovering near 20% would rapidly toggle
  low/not-low on every report due to noise in the sensor reading.
  Verified: stable low indication until battery drops below 20%, clears
  only when it reaches 25% or higher.

- **V2 detection removed** - device.py `imu_off=0` unconditionally
  The check `buf[12] == 0x10` was previously used to detect V2 controllers
  and apply a +4 IMU offset. buf[12] is the low byte of `accel_X`, which
  changes with every report and is NOT a version indicator.
  All tested controllers (PID 0x09cc, V2) use the same IMU layout at
  buf[12..23] as V1. There is no +4 shift.
  Fix: `imu_off=0` unconditionally. All V2 detection code and comments
  stripped.

- **TUI input lag** - settings/tui.py
  `curses.napms(33)` was used in the TUI draw loop as a frame-rate
  throttle. `napms()` does NOT release the Python GIL, so the daemon
  thread processing incoming reports was blocked for the full 33ms.
  Fix: `time.sleep(0.005)` - this releases the GIL, allowing the daemon
  thread to process reports between TUI draw cycles.
  Verified: report processing latency visibly improved, no input lag.

- **Reconnect startup delay** - bluez_l2cap.py `connect_loop()`
  When the device was previously disconnected (not in BlueZ's connected
  state), the 3s `_grab_l2cap()` timeout still ran before falling through
  to `_start_reconnect()`. This added 3s of unnecessary delay on every
  PS-button reconnect attempt.
  Fix: track `was_connected` from BlueZ properties. If not connected,
  skip `_grab_l2cap()` and go directly to `_start_reconnect()`.

## v0.2.0 - 2026-06-07 (initial working Bluetooth version)

- **Outgoing L2CAP before BlueZ** - bluez_l2cap.py `connect_loop()`
  The driver's reconnect loop opens an outgoing L2CAP channel to the
  DS4's PSM 0x11 before doing any BlueZ D-Bus management. This is a
  direct socket `connect()` to the controller's Bluetooth address - it
  bypasses BlueZ's incoming listener on the same PSM entirely.
  Both channels coexist without interference (different L2CAP channel
  identifiers at the kernel level). No BlueZ block/unblock cycle is
  needed to reclaim the PSM.
  `CAP_NET_ADMIN` (root or `setcap cap_net_admin=ep`) is required for
  `bind()` to privileged PSMs (< 1024).

- **Settings server before backend init** - `__main__.py`
  The Unix domain socket (`~/.cache/ds4tux/control.sock`) is created
  and listening before `init_backend()` starts the Bluetooth pairing
  or connection loop. This guarantees the TUI receives every status
  transition (pairing→connecting→connected→disconnected).

- **Copycat controller detection** - feature report probing
  Genuine DS4 controllers respond to HID feature report 0x81 with a
  7-byte reply containing their Bluetooth MAC address. Some third-party
  (copycat) controllers don't support 0x81 but do respond to fallback
  0x12 (16 bytes). If both fail, the controller is treated as a copycat.
  Runtime quirks applied to detected copycats:
  - USB output report RGB byte order: `B, R, G` instead of `R, G, B`
  - BT output report: HID CRC checksum omitted (copycats don't validate)
  - Battery: `usb_plugged` forced True (copycat battery reports unreliable)
  - Trackpad: input report trackpad data skipped (encoding unreliable)
  - Input: evdev events ignored, raw hidraw reads used instead

- **IMU layout (BT long report)** - device.py
  After stripping HIDP header + report ID + status bytes:
  - buf[12..17]: accelerometer X/Y/Z as 3 × little-endian signed 16-bit
  - buf[18..23]: gyroscope as 3 × little-endian signed 16-bit
  Gyro axis labels (pitch/yaw/roll) are unconfirmed - the 6 bytes are
  read but left as unparsed `0.0` defaults in `DS4Report`.

- **Input report format (BT long)** - device.py, README.md
  Full byte layout table documented in README. Key offsets:
  - buf[0]: sequence number (increments each report)
  - buf[1..4]: LX, LY, RX, RY (u8, 0-255)
  - buf[5]: dpad lower nibble (0-8) + face buttons upper nibble
  - buf[6]: shoulder/thumb buttons
  - buf[7]: counter (increments +4/rep) + PS/trackpad button
  - buf[8..9]: L2/R2 analog
  - buf[10..11]: timestamp le16 (~1683 increments per report)
  - buf[24]: IMU-related (changes with motion)
  - buf[25..29]: zero padding
  - buf[33]: status (bit0 always 1)
  - buf[34]: zero padding
  - buf[35..46]: trackpad (2 × 6-byte fingers)
  HIDP wire format before the data: `[0xa1] [optional 0xff] [0x11] [0xc0]`
  - stripping these leaves the above layout at relative offset 0.

- **Output report format (BT)** - device.py `build_bt_output_report_raw()`
  Sent on CTRL channel (PSM 0x11):
  `[0x52, 0x11]` HIDP Set-Report header + 77 byte body:
  - byte 0: flags (0x80 small-motor, 0x00, 0xFF)
  - byte 3: R
  - byte 4: G
  - byte 5: B
  - bytes 6-7: rumble L/R
  - bytes 8-9: blink_on/blink_off (ignored by V2 over BT)
  - remaining: zeros

- **hid_playstation MUST be blacklisted**
  The kernel `hid-playstation` driver races with the userspace driver for
  LED control, rumble, and input. The kernel driver grabs the HID device
  first, making it unavailable to userspace, and applies its own output
  report defaults (full-brightness white LED). Blacklisting via modprobe
  or `--block-kmod` flag is required.

- **hidp kernel module NOT loaded**
  BlueZ handles HID profiles internally via its `plugins/hid.c` (not the
  kernel `hidp` module). No `/dev/hidraw*` device appears for Bluetooth
  connections. All DS4 BT communication uses raw L2CAP sockets on
  PSM 0x11 (CTRL) and PSM 0x13 (INTR). The `hidp` module must not be
  loaded (or must be blacklisted) to avoid BlueZ falling back to kernel
  HIDP transport.

- **Dpad mapping** - uinput.py `DPAD_MAP`
  dpad value 0-8 maps to ABS_HAT0X/ABS_HAT0Y:
  - 0: (0,-1) up, 1: (1,-1) NE, 2: (1,0) right, 3: (1,1) SE
  - 4: (0,1) down, 5: (-1,1) SW, 6: (-1,0) left, 7: (-1,-1) NW
  - 8: (0,0) neutral
  This matches the xpad kernel driver convention. Dpad is NOT duplicated
  onto any analog axes. Gyro axes (ABS_RUDDER/ABS_WHEEL/ABS_THROTTLE)
  are on a separate uinput device and not created by default.
