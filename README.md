# ds4tux

DualShock 4 userspace driver for Linux that actually works. Built because ds4drv has been broken for years. Handles genuine and fake / clone / copycat controllers identically: Pair once, press PS to reconnect. Live TUI for input testing, LED control and Xinput emulation

## Install
    copy this line into your terminal:
    curl -sSL https://raw.githubusercontent.com/POOnami/ds4tux/main/install.sh | sudo sh

    Don't forget to start the service! 
    sudo systemctl start ds4tux   (systemd)
    sudo rc-service ds4tux start  (openrc)

    then `ds4tux --settings` to test inputs

## Features
- PS button auto-reconnect. Doesnt need pair mode every time (sudo/service)
- LED control for clone controllers (less battery drain)
- Live TUI for input testing, LED, mapping (`ds4tux --settings`)
- USB charging with bluetooth connection (lower latency)
- Systemd, OpenRC init script, udev rules included
- BlueZ D-Bus integration - no fight with kernel hid_playstation

## Usage
    ds4tux                        # auto (BT or USB)
    ds4tux --settings             # live TUI
    ds4tux --led ff0000           # red LED
    ds4tux --copycat clone         # force clone mode
    ds4tux --block-kmod           # blacklist hid_playstation

## Known issues
- No rumble
- Multiple controllers not implemented 
- PS Reconnect requires root/sudo (works via service) 
- Genuine/Clone detection over USB, saved per-MAC; toggle in TUI if needed

## Untested
- Genuine Sony DualShock 4
- BlueZ experimental mode (`-E`) needed for PS button reconnect
- Gyro is raw passthrough (no calibration)
- USB mode (low priority, hardware limited latency)

## Docs
Developer notes, protocol details: `docs/technical.md`

## License
GPL v3
