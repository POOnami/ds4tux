"""
LED control action. Manages the controller lightbar state,
including dimming for battery life and optional blinking.
"""

from __future__ import annotations
import time
import threading
import logging

logger = logging.getLogger("ds4tux.led")

LED_MIN = 0
LED_MAX = 255
DEFAULT_LED_R = 0
DEFAULT_LED_G = 0
DEFAULT_LED_B = 128
DEFAULT_BRIGHTNESS = 60


class LEDController:
    def __init__(self, r: int = DEFAULT_LED_R, g: int = DEFAULT_LED_G,
                 b: int = DEFAULT_LED_B, brightness: int = DEFAULT_BRIGHTNESS):
        self._lock = threading.Lock()
        self._target_r = r
        self._target_g = g
        self._target_b = b
        self._brightness = max(0, min(100, brightness))
        self._current_r = 0
        self._current_g = 0
        self._current_b = 0
        self._dirty = True
        self._write_fn = None
        self._sw_blink_active = False
        self._sw_blink_on_ms = 0
        self._sw_blink_off_ms = 0
        self._sw_blink_until = 0.0
        self._sw_blink_state = True
        self._sw_blink_next_toggle = 0.0
        self._full_brightness_until = 0.0

    def set_color(self, r: int, g: int, b: int):
        with self._lock:
            self._target_r = max(0, min(255, r))
            self._target_g = max(0, min(255, g))
            self._target_b = max(0, min(255, b))
            self._full_brightness_until = time.time() + 1.0
            self._dirty = True

    def set_brightness(self, pct: int):
        with self._lock:
            self._brightness = max(1, min(100, pct))
            self._full_brightness_until = 0.0
            self._dirty = True

    def set_blink(self, on_ms: int, off_ms: int, duration: float = 0):
        with self._lock:
            self._sw_blink_active = True
            self._sw_blink_on_ms = max(10, on_ms)
            self._sw_blink_off_ms = max(10, off_ms)
            if duration > 0:
                self._sw_blink_until = time.time() + duration
            else:
                self._sw_blink_until = float('inf')
            self._sw_blink_state = True
            self._sw_blink_next_toggle = time.time() + on_ms / 1000.0
            self._dirty = True

    def stop_blink(self):
        with self._lock:
            self._sw_blink_active = False
            self._sw_blink_until = 0.0
            self._dirty = True

    def set_write_fn(self, fn):
        with self._lock:
            self._write_fn = fn

    @property
    def is_blinking(self) -> bool:
        with self._lock:
            return self._sw_blink_active and time.time() < self._sw_blink_until

    def update(self) -> bool:
        with self._lock:
            now = time.time()

            if self._full_brightness_until and now >= self._full_brightness_until:
                self._full_brightness_until = 0.0
                self._dirty = True

            if self._sw_blink_active:
                if now >= self._sw_blink_until:
                    self._sw_blink_active = False
                    self._dirty = True
                elif now >= self._sw_blink_next_toggle:
                    self._sw_blink_state = not self._sw_blink_state
                    if self._sw_blink_state:
                        self._sw_blink_next_toggle = now + self._sw_blink_on_ms / 1000.0
                    else:
                        self._sw_blink_next_toggle = now + self._sw_blink_off_ms / 1000.0
                    self._dirty = True

            if not self._dirty:
                return False

            if self._sw_blink_active and not self._sw_blink_state:
                r, g, b = 0, 0, 0
            else:
                pct = 100 if now < self._full_brightness_until else self._brightness
                r = (self._target_r * pct) // 100
                g = (self._target_g * pct) // 100
                b = (self._target_b * pct) // 100

            if (r, g, b) == (self._current_r, self._current_g, self._current_b) and not self._dirty:
                return False

            self._current_r, self._current_g, self._current_b = r, g, b

            if self._write_fn:
                if not self._write_fn(r, g, b, 0, 0):
                    return False

            self._dirty = False
            return True
