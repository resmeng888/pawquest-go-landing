"""
GT06 / 365GPS 0x13 status packet — extract battery, steps, optional raw accelerometer XYZ.

Layout of ``info`` (protocol payload inside the short frame, after 0x13 byte):

- Byte 0: battery % (0–100)
- Bytes 1–3: step count, 24-bit big-endian (lower 20 bits used here)
- Bytes 4–5: acceleration X, int16 big-endian signed (optional)
- Bytes 6–7: Y
- Bytes 8–9: Z

Packets with only the first 4 bytes are supported (no IMU); vibration is then omitted (caller
should not overwrite prior motion state).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Parsed0x13Status:
    battery_pct: float
    steps: int
    accel_x: Optional[int]
    accel_y: Optional[int]
    accel_z: Optional[int]
    """Euclidean magnitude of (x,y,z) when axes present; ``None`` when no axes in frame."""
    vibration: Optional[float]


def parse_0x13_status_info(info: bytes) -> Optional[Parsed0x13Status]:
    if len(info) < 4:
        return None
    battery_pct = float(min(100, info[0]))
    steps = int.from_bytes(info[1:4], "big") & 0xFFFFF
    if len(info) >= 10:
        ax = int.from_bytes(info[4:6], "big", signed=True)
        ay = int.from_bytes(info[6:8], "big", signed=True)
        az = int.from_bytes(info[8:10], "big", signed=True)
        vib = float(math.hypot(math.hypot(float(ax), float(ay)), float(az)))
        return Parsed0x13Status(battery_pct, steps, ax, ay, az, vib)
    return Parsed0x13Status(battery_pct, steps, None, None, None, None)
