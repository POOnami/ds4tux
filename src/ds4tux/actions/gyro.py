from __future__ import annotations


class GyroProcessor:
    """Passthrough: sends raw gyro values to the output fields."""

    def process(self, raw_x: int, raw_y: int, raw_z: int) -> tuple[float, float, float]:
        return float(raw_x), float(raw_y), float(raw_z)

    def reset(self):
        pass
