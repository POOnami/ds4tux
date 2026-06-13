"""
Curses TUI for ds4tux settings and live input testing.
Pure stdlib — no external dependencies needed.
"""

from __future__ import annotations
import os
import time
import curses
from typing import Optional

from ds4tux.config import write_config as _write_config
from ds4tux.settings.client import SettingsClient, SOCKET_PATH

SLIDER_W = 40
BAR_W = 30


def _hsv_to_rgb(h: int, s: float = 1.0, v: float = 1.0) -> tuple[int, int, int]:
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    return int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)


def _rgb_to_hsv(r: int, g: int, b: int) -> tuple[int, float, float]:
    rf, gf, bf = r / 255, g / 255, b / 255
    mx = max(rf, gf, bf)
    mn = min(rf, gf, bf)
    d = mx - mn
    if d == 0:
        h = 0
    elif mx == rf:
        h = (60 * ((gf - bf) / d) + 360) % 360
    elif mx == gf:
        h = (60 * ((bf - rf) / d) + 120) % 360
    else:
        h = (60 * ((rf - gf) / d) + 240) % 360
    s = 0 if mx == 0 else d / mx
    return int(h), s, mx


def _hex_to_rgb(h: str) -> Optional[tuple[int, int, int]]:
    h = h.strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"{r:02x}{g:02x}{b:02x}".upper()


class DS4TuxTUI:
    def __init__(self, config: Optional[dict] = None):
        self._config = config or {}
        self._client: Optional[SettingsClient] = None
        self._state: dict = {}

        led = self._config.get("led", {"r": 0, "g": 0, "b": 128})
        self._hue, _, _ = _rgb_to_hsv(led["r"], led["g"], led["b"])
        self._brightness = self._config.get("led_brightness", 60)
        self._hex = _rgb_to_hex(led["r"], led["g"], led["b"])
        self._mapping = self._config.get("mapping", "xpad")
        self._device_mode = self._config.get("device_mode", "auto")
        self._enabled = True
        self._ctrl_status = "disconnected"
        self._status = ""
        self._connected = False
        self._is_usb = False

        self._focus = 0  # 0=hue, 1=bright, 2=hex, 3=mapping, 4=device, 5=driver
        self._hex_buf = list(self._hex)
        self._hex_cursor = 0
        self._dirty_led = False
        self._dirty_map = False
        self._dirty_device_mode = False
        self._dirty_enabled = False
        self._dirty = True
        self._last_bar_rgb = None

    @staticmethod
    def run(config: Optional[dict] = None):
        tui = DS4TuxTUI(config)
        tui._connect()
        try:
            curses.wrapper(tui._main)
        finally:
            if tui._client:
                tui._client.close()

    def _connect(self):
        if os.path.exists(SOCKET_PATH):
            self._client = SettingsClient()
            if self._client.connect():
                self._client._on_state = self._on_state
                self._connected = True
                self._status = "Connected"
                return
        self._status = "Daemon not running (settings saved locally)"

    def _on_state(self, state: dict):
        self._state = state
        self._connected = state.get("connected", False)
        if "enabled" in state:
            self._enabled = state["enabled"]
        if "mapping" in state:
            self._mapping = state["mapping"]
        if "device_mode" in state:
            self._device_mode = state["device_mode"]
        if "status" in state:
            self._ctrl_status = state["status"]
        elif not self._connected:
            self._ctrl_status = "disconnected"
        if "led" in state and state["connected"]:
            led = state["led"]
            r, g, b = led.get("r", 0), led.get("g", 0), led.get("b", 0)
            self._hue, _, _ = _rgb_to_hsv(r, g, b)
            self._brightness = led.get("brightness", 60)
            self._hex = _rgb_to_hex(r, g, b)
            self._hex_buf = list(self._hex)
            self._hex_cursor = len(self._hex_buf)
        self._is_usb = not state.get("bluetooth", True)
        self._dirty = True

    def _hue_rgb(self) -> tuple[int, int, int]:
        return _hsv_to_rgb(self._hue, 1.0, self._brightness / 100)

    def _send_enabled(self):
        if not self._client:
            return
        self._client.send_command({"type": "set_enabled", "enabled": self._enabled})

    def _send_led(self):
        if not self._client:
            return
        r, g, b = self._hue_rgb()
        self._client.send_command({
            "type": "set_led", "r": r, "g": g, "b": b,
            "brightness": self._brightness,
        })

    def _send_map(self):
        if not self._client:
            return
        self._client.send_command({"type": "set_mapping", "mapping": self._mapping})

    def _send_device_mode(self):
        if not self._client:
            return
        self._client.send_command({"type": "set_copycat", "mode": self._device_mode})

    def _main(self, stdscr):
        curses.curs_set(0)
        curses.use_default_colors()
        stdscr.nodelay(True)

        if curses.has_colors():
            curses.start_color()
            for i in range(8):
                curses.init_pair(i + 1, i, -1)
            if curses.can_change_color() and curses.COLORS >= 256:
                self._color_bar = curses.color_pair(100)
                try:
                    curses.init_pair(100, 0, 100)
                    curses.init_color(100, 0, 0, 1000)
                except Exception:
                    self._color_bar = None

        rows, cols = stdscr.getmaxyx()
        if rows < 24 or cols < 60:
            stdscr.addstr(0, 0, "Terminal too small (need 60x24)")
            stdscr.refresh()
            time.sleep(2)
            return

        while True:
            if self._dirty_led:
                self._send_led()
                self._dirty_led = False

            if self._dirty_map:
                self._send_map()
                self._dirty_map = False

            if self._dirty_enabled:
                self._send_enabled()
                self._dirty_enabled = False

            if self._dirty_device_mode:
                self._send_device_mode()
                self._dirty_device_mode = False

            if self._dirty:
                self._draw(stdscr)
                self._dirty = False

            key = stdscr.getch()
            if key == -1:
                pass
            elif key == ord('q'):
                break
            elif key == ord('s'):
                self._save()
                self._dirty = True
            elif key == ord('b'):
                self._blink()
                self._dirty = True
            elif key == curses.KEY_UP:
                self._focus = (self._focus - 1) % 6
                if self._focus == 2:
                    self._hex_buf = list(self._hex)
                    self._hex_cursor = len(self._hex_buf)
                self._dirty = True
            elif key == curses.KEY_DOWN:
                self._focus = (self._focus + 1) % 6
                if self._focus == 2:
                    self._hex_buf = list(self._hex)
                    self._hex_cursor = len(self._hex_buf)
                self._dirty = True
            elif key == curses.KEY_LEFT:
                self._adjust(-1)
                self._dirty = True
            elif key == curses.KEY_RIGHT:
                self._adjust(1)
                self._dirty = True
            elif key == curses.KEY_BACKSPACE or key == 127:
                if self._focus == 2 and self._hex_cursor > 0:
                    self._hex_cursor -= 1
                    self._hex_buf.pop(self._hex_cursor)
                self._dirty = True
            elif key == curses.KEY_DC:
                if self._focus == 2 and self._hex_cursor < len(self._hex_buf):
                    self._hex_buf.pop(self._hex_cursor)
                self._dirty = True
            elif key == curses.KEY_HOME:
                if self._focus == 2:
                    self._hex_cursor = 0
                self._dirty = True
            elif key == curses.KEY_END:
                if self._focus == 2:
                    self._hex_cursor = len(self._hex_buf)
                self._dirty = True
            elif 32 <= key < 127:
                ch = chr(key)
                if self._focus == 2:
                    if len(self._hex_buf) < 6 and ch in "0123456789abcdefABCDEF":
                        self._hex_buf.insert(self._hex_cursor, ch.upper())
                        self._hex_cursor += 1
                        if len(self._hex_buf) == 6:
                            self._apply_hex()
                elif self._focus == 3:
                    self._cycle_mapping()
                elif self._focus == 4:
                    self._cycle_device_mode()
                elif self._focus == 5:
                    self._toggle_enabled()
                self._dirty = True

            try:
                time.sleep(0.005)
            except KeyboardInterrupt:
                break

    def _adjust(self, direction: int):
        if self._focus == 0:
            self._hue = (self._hue + direction * 5) % 360
            self._sync_hex_from_hsv()
            self._dirty_led = True
        elif self._focus == 1:
            self._brightness = max(0, min(100, self._brightness + direction * 5))
            self._sync_hex_from_hsv()
            self._dirty_led = True

    def _sync_hex_from_hsv(self):
        r, g, b = self._hue_rgb()
        self._hex = _rgb_to_hex(r, g, b)
        self._hex_buf = list(self._hex)
        self._hex_cursor = len(self._hex_buf)

    def _apply_hex(self):
        h = "".join(self._hex_buf)
        rgb = _hex_to_rgb(h)
        if rgb:
            r, g, b = rgb
            self._hue, _, _ = _rgb_to_hsv(r, g, b)
            self._brightness = max(1, min(100, int(max(r, g, b) / 255 * 100)))
            self._hex = h
            self._dirty_led = True
        else:
            self._hex_buf = list(self._hex)
            self._hex_cursor = len(self._hex_buf)

    def _cycle_mapping(self):
        maps = ["xpad", "xboxdrv", "ds4"]
        idx = maps.index(self._mapping) if self._mapping in maps else 0
        self._mapping = maps[(idx + 1) % len(maps)]
        self._dirty_map = True
        if self._is_usb and self._mapping != "ds4":
            self._status = "USB: non-ds4 mapping may cause double inputs"
        elif self._is_usb and self._mapping == "ds4":
            self._status = ""

    def _cycle_device_mode(self):
        modes = ["auto", "clone", "genuine"]
        idx = modes.index(self._device_mode) if self._device_mode in modes else 0
        self._device_mode = modes[(idx + 1) % len(modes)]
        self._dirty_device_mode = True

    def _toggle_enabled(self):
        self._enabled = not self._enabled
        self._dirty_enabled = True

    def _send_led_off(self):
        if self._client:
            self._client.send_command({
                "type": "set_led", "r": 0, "g": 0, "b": 0,
            })

    def _blink(self):
        if self._client:
            self._client.send_command({
                "type": "blink", "on_ms": 300, "off_ms": 300, "duration": 2.0,
            })

    def _save(self):
        try:
            r, g, b = self._hue_rgb()
            cfg = {
                "mapping": self._mapping,
                "device_mode": self._device_mode,
                "led_brightness": self._brightness,
                "led": {"r": r, "g": g, "b": b},
            }
            path = _write_config(cfg)
            if self._client:
                self._client.send_command({
                    "type": "reload_config",
                    "config": cfg,
                })
            self._status = f"Config saved: {path}"
        except Exception as e:
            self._status = f"Save failed: {e}"

    def _draw(self, stdscr):
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()

        # Header
        header = " ds4tux  "
        stdscr.addstr(0, 0, header, curses.A_REVERSE)
        stdscr.addstr(0, len(header), f"  q:quit  s:save  b:blink  ↑↓:focus")

        # Status readout (right side of header)
        status_data = {
            "unpaired": ("○", curses.A_DIM, "Hold PS+Share for pairing"),
            "paired": ("●", curses.A_BOLD, "Paired — press PS to connect"),
            "pairing": ("⟳", curses.A_BOLD, "Pairing in progress..."),
            "connecting": ("⟳", curses.A_BOLD, "Connecting..."),
            "connected": ("●", curses.A_BOLD, "Connected and receiving data"),
            "reconnecting": ("⟳", curses.A_DIM, "Trying to reconnect..."),
            "disconnected": ("○", curses.A_DIM, "Not connected"),
        }
        icon, style, tooltip = status_data.get(self._ctrl_status, ("?", curses.A_NORMAL, ""))
        status_text = f" {icon} {self._ctrl_status.upper()}"
        stdscr.addstr(0, cols - len(status_text) - 2, status_text, style)

        if tooltip:
            stdscr.addstr(0, cols - len(status_text) - len(tooltip) - 4, tooltip, curses.A_DIM)

        # Color preview bar
        r, g, b = self._hue_rgb()
        if self._color_bar:
            rgb = (r, g, b)
            if rgb != self._last_bar_rgb:
                self._last_bar_rgb = rgb
                try:
                    curses.init_color(100, r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)
                except Exception:
                    pass
            stdscr.attron(self._color_bar)
            try:
                stdscr.addstr(1, 0, " " * (cols - 2))
            except Exception:
                pass
            stdscr.attroff(self._color_bar)
        else:
            stdscr.addstr(1, 0, f"{' ' * (cols - 2)}")

        # Hue slider
        hue_label = f"  Color: "
        y = 3
        stdscr.addstr(y, 0, hue_label)
        self._draw_slider(stdscr, y, len(hue_label), SLIDER_W, self._hue, 360, 0)

        # Brightness slider
        bri_label = f"  Bright:"
        y = 4
        stdscr.addstr(y, 0, bri_label)
        self._draw_slider(stdscr, y, len(bri_label), SLIDER_W, self._brightness, 100, 1)

        # Hex input
        hex_label = f"  Hex:   "
        y = 5
        stdscr.addstr(y, 0, hex_label)
        hex_str = "".join(self._hex_buf)
        padded = hex_str.ljust(6)
        if self._focus == 2:
            stdscr.addstr(y, len(hex_label), f"[{padded}]", curses.A_REVERSE)
            cx = len(hex_label) + 1 + min(self._hex_cursor, 5)
            stdscr.addstr(y, cx, padded[min(self._hex_cursor, 5)], curses.A_REVERSE | curses.A_BLINK)
        else:
            stdscr.addstr(y, len(hex_label), f" {padded} ")

        # Color preview text
        hp = f"  #{self._hex}  RGB({r},{g},{b})  hue:{self._hue} bri:{self._brightness}%"
        stdscr.addstr(y, len(hex_label) + 11, hp)

        # Mapping
        map_label = f"  Map:   "
        y = 7
        stdscr.addstr(y, 0, map_label)
        maps = ["xpad", "xboxdrv", "ds4"]
        for i, m in enumerate(maps):
            if m == self._mapping:
                if self._focus == 3:
                    stdscr.addstr(y, len(map_label) + i * 8, f"[{m:^6}]", curses.A_REVERSE)
                else:
                    stdscr.addstr(y, len(map_label) + i * 8, f" {m:^6} ", curses.A_BOLD)
            else:
                stdscr.addstr(y, len(map_label) + i * 8, f" {m:^6} ")
        if self._is_usb and self._mapping != "ds4":
            warn = "* USB: non-ds4 may cause double inputs"
            stdscr.addstr(y + 1, len(map_label), warn, curses.A_DIM)

        # Device compatibility mode
        dm_label = f"  Compatibility Mode:  "
        y = 8
        s = self._state
        dc = s.get("detected_copycat") if s else None
        detected_suffix = ""
        if dc is not None:
            detected_suffix = f"  (detected: {'copycat' if dc else 'genuine'})"
        stdscr.addstr(y, 0, dm_label)
        modes = ["auto", "clone", "genuine"]
        for i, m in enumerate(modes):
            if m == self._device_mode:
                if self._focus == 4:
                    stdscr.addstr(y, len(dm_label) + i * 8, f"[{m:^6}]", curses.A_REVERSE)
                else:
                    stdscr.addstr(y, len(dm_label) + i * 8, f" {m:^6} ", curses.A_BOLD)
            else:
                stdscr.addstr(y, len(dm_label) + i * 8, f" {m:^6} ")
        if detected_suffix:
            line_end = len(dm_label) + len(modes) * 8
            stdscr.addstr(y, line_end, detected_suffix, curses.A_DIM)

        # Enable/disable toggle
        en_label = f"  Driver: "
        y = 9
        stdscr.addstr(y, 0, en_label)
        if self._focus == 5:
            stdscr.addstr(y, len(en_label), f"[{'ENABLED' if self._enabled else 'DISABLED'}]", curses.A_REVERSE)
        else:
            stdscr.addstr(y, len(en_label), f" {'ENABLED' if self._enabled else 'DISABLED'} ",
                          curses.A_BOLD if self._enabled else curses.A_DIM)

        # Separator
        y = 10
        stdscr.addstr(y, 0, "─" * (cols - 1), curses.A_DIM)

        # Live state
        y = 11
        s = self._state
        if s and s.get("connected"):
            axes = s.get("axes", {})
            btns = s.get("buttons", {})
            bat = s.get("battery", 0)

            lx = axes.get("lx", 127)
            ly = axes.get("ly", 127)
            rx = axes.get("rx", 127)
            ry = axes.get("ry", 127)

            stdscr.addstr(y, 0, f"  Sticks:  L({lx:>3},{ly:>3})  R({rx:>3},{ry:>3})")
            y += 1

            l2 = axes.get("l2", 0)
            r2 = axes.get("r2", 0)
            l2b = "█" * int((l2 / 255) * BAR_W)
            r2b = "█" * int((r2 / 255) * BAR_W)
            stdscr.addstr(y, 0, f"  Triggers: L2 [{l2b:<{BAR_W}}] {l2:>3}   R2 [{r2b:<{BAR_W}}] {r2:>3}")
            y += 1

            def _box(yy, xx, label: str, pressed: bool):
                if pressed:
                    stdscr.addstr(yy, xx, f"[{label}]", curses.A_REVERSE)
                else:
                    stdscr.addstr(yy, xx, f" {label} ", curses.A_DIM)

            def _btngrp(yy, xx, *pairs: tuple[str, str]):
                for key, label in pairs:
                    _box(yy, xx, label, btns.get(key, False))
                    xx += len(label) + 3

            # dpad cross
            dpad = axes.get("dpad", 8)
            _box(y + 0, 0, "up", dpad in (0, 1, 7))
            _box(y + 0, 13, "^", dpad in (0, 1, 7))
            _box(y + 1, 0, "left", dpad in (5, 6, 7))
            _box(y + 1, 7, "<", dpad in (5, 6, 7))
            _box(y + 1, 13, "o", dpad == 8)
            _box(y + 1, 19, ">", dpad in (1, 2, 3))
            _box(y + 1, 25, "right", dpad in (1, 2, 3))
            _box(y + 2, 0, "down", dpad in (3, 4, 5))
            _box(y + 2, 13, "v", dpad in (3, 4, 5))
            y += 4

            _btngrp(y, 2, ("triangle", "▲"), ("circle", "●"), ("cross", "✕"), ("square", "■"))
            y += 1
            _btngrp(y, 2, ("l1", "L1"), ("r1", "R1"), ("l2", "L2"), ("r2", "R2"))
            y += 1
            _btngrp(y, 2, ("l3", "L3"), ("r3", "R3"))
            _btngrp(y, 15, ("share", "SHARE"), ("ps", "PS"), ("options", "OPTIONS"))
            y += 1

            bat_w = 20
            bat_b = "█" * int((bat / 100) * bat_w)
            chg = " ⚡" if s.get("charging", False) else ""
            cc = " (copycat)" if s.get("copycat", False) else ""
            mac = s.get("mac", "??")
            stdscr.addstr(y, 0, f"  BAT: [{bat_b:<{bat_w}}] {bat}%{chg}  {mac}{cc}")
            y += 1

            lat = s.get("latency", {})
            hz = lat.get("hz", 0)
            if hz:
                avg = lat.get("avg_ms", 0)
                jit = lat.get("jitter_ms", 0)
                _box(y, 11, "*", hz > 800)
                stdscr.addstr(y, 0, f"  Lat:     {hz:>4} Hz  avg {avg:>5.2f}ms  ±{jit:.2f}ms")
            else:
                _box(y, 11, "~", False)
                stdscr.addstr(y, 0, f"  Lat:     --- Hz")
            y += 1

            tp = s.get("touchpad", [{}, {}])
            t1 = tp[0] if len(tp) > 0 else {}
            t2 = tp[1] if len(tp) > 1 else {}
            if t1.get("active") or t2.get("active"):
                stdscr.addstr(y, 0, f"  Touch: 1({t1.get('x',0)},{t1.get('y',0)}) 2({t2.get('x',0)},{t2.get('y',0)})")
                y += 1

            gyro = s.get("gyro", {})
            gx, gy, gz = gyro.get("raw", [0, 0, 0])
            fx, fy, fz = gyro.get("filtered", [0, 0, 0])
            ax, ay, az = s.get("accel", [0, 0, 0])
            stdscr.addstr(y, 0, f"  GYRO raw: GX={gx:+6d}  GY={gy:+6d}  GZ={gz:+6d}")
            y += 1
            stdscr.addstr(y, 0, f"  ACCEL:    AX={ax:+6d}  AY={ay:+6d}  AZ={az:+6d}")
            y += 1
            stdscr.addstr(y, 0, f"  GYRO filt: GX={fx:+7.1f}  GY={fy:+7.1f}  GZ={fz:+7.1f}")
            y += 1
            y += 1
        else:
            msg = "  No controller connected"
            if self._ctrl_status in ("pairing", "connecting", "reconnecting"):
                tips = {
                    "pairing": "  Hold PS+Share on your controller",
                    "connecting": "  Press PS to connect",
                    "reconnecting": "  Press PS to reconnect",
                }
                msg = tips.get(self._ctrl_status, msg)
            stdscr.addstr(y, 0, msg)
            y += 1
            if self._state.get("usb_detected_no_sudo"):
                stdscr.addstr(y, 0, "  USB DS4 detected — requires sudo",
                              curses.A_REVERSE)
                y += 1

        # Status line
        if self._status:
            stdscr.addstr(rows - 2, 0, f"  {self._status}")
            if "OK" not in self._status and "saved" not in self._status:
                self._status = ""

        # Footer controls
        focus_names = ['Color', 'Bright', 'Hex', 'Mapping', 'Clone', 'Driver']
        stdscr.addstr(rows - 1, 0, f"  Focus: {focus_names[self._focus]}",
                      curses.A_DIM)

        stdscr.refresh()

    def _draw_slider(self, stdscr, y: int, x: int, width: int, value: int, max_val: int, idx: int):
        pos = int((value / max_val) * (width - 1))
        fill = "─" * pos
        empty = "─" * (width - 1 - pos)
        if self._focus == idx:
            stdscr.addstr(y, x, f"┃{fill}●{empty}┃", curses.A_REVERSE)
        else:
            stdscr.addstr(y, x, f"┃{fill}●{empty}┃")
