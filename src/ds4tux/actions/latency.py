from __future__ import annotations
import logging
import statistics

logger = logging.getLogger("ds4tux.latency")


class LatencyMonitor:
    def __init__(self, window: int = 100):
        self.window = window
        self.intervals: list[float] = []
        self.prev_time: float = 0.0
        self.report_count: int = 0

    def update(self, report):
        now = report.timestamp

        if self.prev_time > 0:
            dt = (now - self.prev_time) * 1000.0
            self.intervals.append(dt)
            if len(self.intervals) > self.window:
                self.intervals.pop(0)

        self.prev_time = now
        self.report_count += 1

        if self.report_count % 1000 == 0 and len(self.intervals) > 1:
            stats = self.get_stats()
            logger.info(
                "LAT: %4d Hz  avg %5.2fms  max %5.2fms  ±%4.2fms",
                stats["hz"], stats["avg_ms"],
                stats["max_ms"], stats["jitter_ms"],
            )

    def get_stats(self) -> dict:
        if len(self.intervals) < 2:
            return {"hz": 0, "avg_ms": 0.0, "max_ms": 0.0, "min_ms": 0.0, "jitter_ms": 0.0}
        avg = statistics.mean(self.intervals)
        hz = 1000.0 / avg if avg > 0 else 0
        jitter = statistics.pstdev(self.intervals) if len(self.intervals) > 1 else 0.0
        return {
            "hz": int(round(hz)),
            "avg_ms": round(avg, 2),
            "max_ms": round(max(self.intervals), 2),
            "min_ms": round(min(self.intervals), 2),
            "jitter_ms": round(jitter, 2),
        }
