"""
Battery monitoring action.
Reads battery level from reports and tracks charge status.
"""

from __future__ import annotations
import time
import logging
from ds4tux.device import DS4Report

logger = logging.getLogger("ds4tux.battery")


class BatteryMonitor:
    def __init__(self, low_threshold: int = 20, critical_threshold: int = 5,
                 restore_threshold: int = 25):
        self._low_threshold = low_threshold
        self._critical_threshold = critical_threshold
        self._restore_threshold = restore_threshold
        self._level = 100
        self._charging = False
        self._was_low = False
        self._was_critical = False
        self._prev_low = False
        self._last_report_time = 0.0
        self._last_warn_time = 0.0
        self._warn_interval = 5.0

    @property
    def level(self) -> int:
        return self._level

    @property
    def charging(self) -> bool:
        return self._charging

    @property
    def low(self) -> bool:
        if self._charging:
            return False
        if self._was_low:
            return self._level <= self._restore_threshold
        return self._level <= self._low_threshold

    @property
    def critical(self) -> bool:
        if self._charging:
            return False
        if self._was_critical:
            return self._level <= self._critical_threshold + 5
        return self._level <= self._critical_threshold

    def update(self, report: DS4Report) -> bool:
        self._level = report.battery_percent
        self._charging = report.is_charging
        self._last_report_time = time.time()

        now = time.time()
        just_low = False

        became_low = not self._prev_low and self.low
        self._prev_low = self.low

        if self.low:
            self._was_low = True
        else:
            self._was_low = False

        if self.critical:
            self._was_critical = True
        elif not self.low:
            self._was_critical = False

        if became_low:
            if now - self._last_warn_time >= self._warn_interval:
                self._last_warn_time = now
                level_label = "critical" if self.critical else "low"
                logger.warning("Battery %s: %d%%", level_label, self._level)
                just_low = True

        return just_low
