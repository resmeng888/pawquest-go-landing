# -*- coding: utf-8 -*-
"""
PawQuest — 365GPS mock collar: fixed patrol along Central Park routes.

- This process drives one hardware slot (dog_001) on a fixed Manhattan grid path.
- All 100 UI dogs use the same route library via ``patrol_paths`` in ``main_v2.py``.
- No random walk; waypoints are land-only; speed 3–6 km/h.
"""
from __future__ import annotations

import random
import socket
import struct
import time

from patrol_paths import PATROL_ROUTES, PATROL_SPEED_MAX_MS, PATROL_SPEED_MIN_MS, RoutePatrol

MOCK_DOG_ID = "dog_001"
MOCK_ROUTE_INDEX = 0  # 5th Ave shuttle
TICK_S = 1.0
HOST = "127.0.0.1"
PORT = 6063

LOGIN_HEX = "78 78 0D 01 01 23 45 67 89 01 23 45 00 01 00 01 0D 0A"
STATUS_HEX = "78 78 0F 13 58 00 00 01 10 00 08 00 04 00 00 01 00 00 0D 0A"


def _crc16_itu(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def decimal_to_raw_coord(dec: float) -> int:
    dec = abs(float(dec))
    degrees = int(dec)
    minutes = (dec - degrees) * 60.0
    x = degrees * 100.0 + minutes
    return int(round(x * 30000.0)) & 0xFFFFFFFF


def build_gt06_frame(proto: int, info: bytes, serial: int) -> bytes:
    sn = struct.pack(">H", serial & 0xFFFF)
    core = bytes([proto]) + info
    crc = _crc16_itu(core + sn)
    length = len(core) + 4
    return b"\x78\x78" + bytes([length]) + core + sn + struct.pack(">H", crc) + b"\x0d\x0a"


def build_gps_frame(lat: float, lon: float, speed_kmh: int, serial: int) -> bytes:
    lat_raw = decimal_to_raw_coord(lat)
    lon_raw = decimal_to_raw_coord(lon)
    info = (
        struct.pack(">I", lat_raw)
        + struct.pack(">I", lon_raw)
        + bytes([max(0, min(255, int(speed_kmh)))])
    )
    return build_gt06_frame(0x10, info, serial)


def send_hex(sock: socket.socket, hex_string: str, desc: str) -> None:
    sock.sendall(bytes.fromhex(hex_string.replace(" ", "")))
    print(f"TX {desc}", flush=True)


def patrol_log(dog_id: str, route_name: str, lat: float, lon: float, speed_kmh: float) -> None:
    mph = speed_kmh * 0.621371
    print(
        f"[Patrol] Dog ID: {dog_id} | Route: {route_name} | "
        f"Lat: {lat:.6f} | Lon: {lon:.6f} | "
        f"{mph:.1f} mph ({speed_kmh:.1f} km/h)",
        flush=True,
    )


def main() -> None:
    rng = random.Random(7)
    speed_mps = rng.uniform(PATROL_SPEED_MIN_MS, PATROL_SPEED_MAX_MS)
    route = PATROL_ROUTES[MOCK_ROUTE_INDEX]
    walker = RoutePatrol(route, speed_mps=speed_mps)
    lat, lon = walker.position()
    serial = 1

    print("PawQuest mock — fixed patrol mode", flush=True)
    print(f"  Route: {route.name} | speed ~{speed_mps * 3.6:.1f} km/h\n", flush=True)

    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((HOST, PORT))
        print(f"Connected {HOST}:{PORT}\n", flush=True)

        send_hex(sock, LOGIN_HEX, "login 0x01")
        time.sleep(0.4)

        while True:
            lat, lon, spd = walker.step(TICK_S)
            speed_kmh = max(3.0, min(6.0, spd * 3.6))

            patrol_log(MOCK_DOG_ID, route.name, lat, lon, speed_kmh)
            sock.sendall(build_gps_frame(lat, lon, int(round(speed_kmh)), serial))
            serial = (serial + 1) & 0xFFFF

            send_hex(sock, STATUS_HEX, "status 0x13")
            time.sleep(TICK_S)

    except KeyboardInterrupt:
        print("\nMock stopped.", flush=True)
    except OSError as exc:
        print(f"Connect failed: {exc}", flush=True)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    main()
