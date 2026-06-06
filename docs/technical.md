# ds4tux

DualShock 4 userspace driver for Linux - reliable, copycat-aware, Steam-friendly.

## Features

- **BlueZ D-Bus** integration - no kernel driver race, no LED fight, no double input
- **Copycat controller support** - automatic detection, output report quirks, fallback feature reports

- **Auto-reconnect** - pair once, press PS button to reconnect
- **TUI settings** - `ds4tux --settings` for live input testing, LED control, mapping, modes
- **OpenRC / systemd** - proper service lifecycle
- **Keepalive** - prevents connection dropouts
- **LED battery save** - dim or off by default (kernel defaults to full-brightness white)

## Install

```sh
curl -sSL https://raw.githubusercontent.com/anomalyco/ds4tux/main/install.sh | sudo sh
```

Or if you're on Arch Linux, build the PKGBUILD from the repo.

### udev rules (for non-root access)

```sh
sudo cp udev/50-ds4tux.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

### OpenRC (Alpine, Gentoo, etc.)

```sh
sudo cp openrc/ds4tux /etc/init.d/
sudo rc-update add ds4tux default
sudo rc-service ds4tux start
```

## Usage

```sh
ds4tux                              # Auto mode (BlueZ for BT, hidraw for USB)
ds4tux --settings                   # Live settings TUI
ds4tux --led 00ff00                 # Green LED
ds4tux --mapping ds4                 # Native DS4 mapping
ds4tux --copycat yes                 # Force copycat mode
ds4tux --block-kmod                  # Blacklist hid_playstation (root)
ds4tux --gyro                        # Enable gyro uinput device
```

### Bluetooth setup

1. Enable BlueZ experimental mode for PS button reconnect:
   ```sh
   sudo systemctl edit bluetooth
   ```
   Add:
   ```
   [Service]
   ExecStart=
   ExecStart=/usr/lib/bluetooth/bluetoothd -E
   ```
   Then `sudo systemctl restart bluetooth`

2. Pair your DS4 once (Share+PS), then press PS to reconnect after that.

## Config

`~/.config/ds4tux/config.toml`

```toml
[led]
r = 0
g = 0
b = 128

led_brightness = 60
mapping = "xpad"
copycat = "auto"
keepalive_interval = 5.0
auto_reconnect = true
```

## Requirements

- Linux kernel 6.2+ (for `hid-playstation` driver)
- BlueZ 5.14+
- Python 3.10+
- `python-evdev`, `pyudev`, `python-gobject` (PyGObject)
- Optional (for TUI): `python-gobject`, `python-gi-cairo`

## Developer Notes

### Bluetooth reconnection

The DS4 reconnects via raw L2CAP (PSM 0x11 / 0x13).  The driver does an
outgoing `l2cap_connect()` to the controller's PSM 0x11 - an independent
L2CAP channel from BlueZ's incoming listener on the same PSM.  Both coexist
without interference, so no BlueZ block/unblock cycle is needed.

The reconnect loop simply tries `l2cap_connect()` with a 2-second timeout
every 3 seconds.  The connect blocks until either the DS4 responds to the
host's page scan or the timeout expires.

For first-time pairing, BlueZ's `pair_ds4()` establishes the Bluetooth link
key.  After that, the controller is disconnected from BlueZ and subsequent
PS button presses are handled by the reconnect loop.

See `src/ds4tux/backend/bluez_l2cap.py` for the full timing parameters and
reconnect loop.

Requires `CAP_NET_ADMIN` (root or setcap) for `connect()` to PSM < 1024.

### Input report byte layout

DS4 input reports are 78 bytes (BT) or 64 bytes (USB).  After stripping
headers, the layout differs between V1 (CUH-ZCT1) and V2 (CUH-ZCT2):

| Field          | V1 offset (buf) | V2 offset (buf) | Range                     |
|----------------|------------------|------------------|---------------------------|
| LX, LY, RX, RY | 1-4              | 1-4              | 0-255 (127 centre)        |
| D-pad + face   | 5                | 5                | bitfield                  |
| Shoulders      | 6                | 6                | bitfield                  |
| PS + trackpad  | 7                | 7                | bitfield                  |
| L2, R2 analog  | 8-9              | 8-9              | 0-255                     |
| V1 battery     | 10               | -                | nibble 0-10               |
| Timestamp      | -                | 10-11            | le16 (~7/tick)            |
| V2 battery     | -                | 13               | nibble 0-10               |
| IMU (accel)    | 12-17            | 16-21            | 3 × le16 signed           |
| IMU (gyro)     | 18-23            | 22-27            | 3 × le16 signed           |
| Trackpad       | 35-46            | 39-50            | 2 × finger, 1920×943      |

V2 detection: `buf[12] == 0x10` (always 0x00 in V1).  IMU and trackpad offsets
differ between V1 and V2 (see offsets above).

Battery encoding: raw nibble 0-10 → `percent = raw * 10`.

Full reference in `src/ds4tux/device.py`.

### Bluetooth L2CAP send (output report)

Wire format on CTRL channel (PSM 0x11):
```
[0x52, 0x11]  HIDP Set-Report header
[77 bytes]    body: flags(0x80, 0x00, 0xFF), R, G, B, rumble, blink
```
See `build_bt_output_report_raw()` in `src/ds4tux/device.py`.

### Copycat controller support

Third-party DS4 clones are detected automatically via USB feature report probing:

1. **Feature report 0x81** (7 bytes) - genuine DS4 responds with its MAC.
2. **Fallback 0x12** (16 bytes) - some copycats respond here instead.
3. **Both fail** → treated as copycat.

Detected copycat controllers trigger these runtime quirks:

- **USB output report**: RGB byte order is `B, R, G` instead of `R, G, B`.
- **BT output report**: HID CRC checksum is omitted (copycats don't validate it).
- **Battery**: `usb_plugged` forced `True` - copycats' charge indicator is unreliable (raw battery level still reported).
- **Trackpad**: Input report trackpad data is skipped (copycat encoding is unreliable).
- **Input**: evdev events are ignored; raw hidraw reads used instead.

**Bluetooth note**: copycat detection is USB-only (hidraw feature reports). The
BT L2CAP path does not probe, and the output report format is the same
regardless of copycat status. This driver was developed and tested with copycat
controllers only - genuine DS4 over BT is unverified.

The `--copycat` flag forces or disables the mode:

```
--copycat auto   detection-based (default)
--copycat yes    always treat as copycat
--copycat no     never treat as copycat
```

Config equivalent: `copycat = "auto"` in `~/.config/ds4tux/config.toml`.

## License

GPL v3
