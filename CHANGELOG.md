# Changelog

## v0.3.4 - 2026-06-13

- updated Charge logic, added led indicator animation
- TUI dpad: text buttons + wider glyph spacing
- bugfixes

## v0.3.3 - 2026-06-10

- config: copycat → device_mode, per-MAC persistence
- BT: genuine output format, trackpad always parsed
- TUI: Compatibility Mode, auto/clone/genuine cycle
- gyro passthrough, uinput survives reconnect
- BlueZ cleanup, battery fix, audio udev rule

## v0.3.2 - 2026-06-08

- USB auto-switch to ds4 mapping
- TUI: inverse buttons, dpad cross
- LED: brightness no longer flashes full
- server: bluetooth + dpad fields

## v0.3.1 - 2026-06-08

- CTRL-hold reconnect: 40s → 10-15s

## v0.3.0 - 2026-06-08

- battery byte at buf[30] for BT long reports
- trackpad active bit inverted (DS4 V2 BT)
- HIDP 0xff header byte fix
- software blink (V2 ignores HW blink over BT)
- BatteryMonitor hysteresis
- V2 detection removed, TUI GIL fix
- reconnect startup delay removed

## v0.2.0 - 2026-06-07 (initial working Bluetooth version)

- outgoing L2CAP before BlueZ for reconnect
- copycat detection via feature report probing
- IMU layout, input/output report formats
- hid_playstation blacklisted, hidp not loaded
- settings server before backend init
- dpad mapping
