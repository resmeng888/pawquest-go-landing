"""
PawQuest — 365GPS / GT06-style TCP gateway (78 78 … 0D 0A).

Status (0x13) IMU fields are parsed by ``gt06_0x13_parse.parse_0x13_status_info``; when
X/Y/Z are present, vibration magnitude is pushed to browsers immediately via
``main_v2.hub.broadcast_sensor`` (WebSocket ``type: "sensor"``), in addition to the regular
tick snapshot from ``DogSimulator``.

Integrates parsed telemetry into ``main_v2.get_shared_simulator()`` so the same
``DogSimulator`` instance backs the Web UI when the API process loads this module.

Run embedded with the HTTP server::

    ENABLE_TCP_GATEWAY=1 python3 -m uvicorn main_v2:app --host 0.0.0.0 --port 8000

Or standalone (separate in-memory simulator — for protocol smoke tests only)::

    python3 tcp_gateway.py
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from gt06_0x13_parse import parse_0x13_status_info

# Login ACK required by hardware (exact bytes).
LOGIN_ACK = bytes.fromhex("787801010d0a")


def decode_bcd_imei(raw: bytes) -> str:
    """Decode 8-byte BCD terminal ID / IMEI (two decimal digits per byte)."""
    digits: list[str] = []
    for byte in raw[:8]:
        hi = (byte >> 4) & 0x0F
        lo = byte & 0x0F
        if hi <= 9:
            digits.append(str(hi))
        if lo <= 9:
            digits.append(str(lo))
    s = "".join(digits).lstrip("0")
    return s or "0"


def raw_coord_to_decimal(raw: int) -> float:
    """
    365GPS-style coordinate: raw uint32 / 30000 → 度分 (DDMM.mmmm style), then decimal degrees.
    Absolute magnitude; apply hemisphere when decoding for NYC.
    """
    x = float(raw) / 30000.0
    degrees = int(x // 100)
    minutes = x - degrees * 100.0
    return degrees + minutes / 60.0


def raw_lng_to_decimal_west(raw: int) -> float:
    """Longitude in GT06 is absolute; PawQuest US deployment = Western hemisphere."""
    return -abs(raw_coord_to_decimal(raw))


def _split_gt06_body(body: bytes) -> Optional[tuple[int, bytes]]:
    """
    Short packet: 78 78 | L | protocol | info | SN(2) | CRC(2) | 0D 0A
    L = length from protocol byte through CRC inclusive.
    """
    if len(body) < 5:
        return None
    pkt_len = body[0]
    if len(body) < 1 + pkt_len:
        return None
    block = body[1 : 1 + pkt_len]
    if len(block) < 5:
        return None
    proto = block[0]
    info = block[1:-4]
    return proto, info


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    from behavior_map import behavior_for_activity_level
    from main_v2 import get_shared_simulator, hub

    sim = get_shared_simulator()
    peer = writer.get_extra_info("peername")
    buf = bytearray()
    session_dog_id: Optional[str] = None

    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buf.extend(chunk)

            while True:
                start = buf.find(b"\x78\x78")
                if start < 0:
                    buf.clear()
                    break
                if start > 0:
                    del buf[:start]
                tail = buf.find(b"\x0d\x0a", 2)
                if tail < 0:
                    break
                packet = bytes(buf[: tail + 2])
                del buf[: tail + 2]

                if not packet.startswith(b"\x78\x78") or not packet.endswith(b"\x0d\x0a"):
                    continue

                inner = packet[2:-2]
                parsed = _split_gt06_body(inner)
                if parsed is None:
                    print(f"[365GPS] {peer} short/malformed frame ({len(packet)} B)", flush=True)
                    continue

                proto, info = parsed

                if proto == 0x01:
                    if len(info) < 8:
                        print(f"[365GPS] {peer} login without 8-byte IMEI", flush=True)
                        continue
                    imei = decode_bcd_imei(info[:8])
                    session_dog_id = sim.bind_imei(imei)
                    writer.write(LOGIN_ACK)
                    await writer.drain()
                    print(f"[365GPS] {peer} LOGIN IMEI={imei} -> {session_dog_id}", flush=True)
                    continue

                dog = session_dog_id
                if dog is None:
                    print(f"[365GPS] {peer} proto=0x{proto:02x} before login, ignored", flush=True)
                    continue

                if proto == 0x10:
                    if len(info) < 9:
                        print(f"[365GPS] {peer} 0x10 info too short ({len(info)})", flush=True)
                        continue
                    lat_raw = int.from_bytes(info[0:4], "big")
                    lng_raw = int.from_bytes(info[4:8], "big")
                    speed_kmh = float(info[8])
                    lat = raw_coord_to_decimal(lat_raw)
                    lng = raw_lng_to_decimal_west(lng_raw)
                    sim.apply_tcp_gps(dog, lat, lng, speed_kmh)
                    print(
                        f"[365GPS] LOC {dog} lat={lat:.6f} lng={lng:.6f} spd={speed_kmh:.1f}km/h",
                        flush=True,
                    )
                    continue

                if proto == 0x13:
                    parsed = parse_0x13_status_info(info)
                    if parsed is None:
                        print(f"[365GPS] {peer} 0x13 info too short ({len(info)})", flush=True)
                        continue
                    if parsed.vibration is not None and parsed.accel_x is not None:
                        sim.apply_tcp_status(
                            dog,
                            parsed.battery_pct,
                            parsed.steps,
                            accel=(parsed.accel_x, parsed.accel_y, parsed.accel_z),
                            vibration=parsed.vibration,
                        )
                        label, emoji = behavior_for_activity_level(parsed.vibration)
                        await hub.broadcast_sensor(
                            {
                                "type": "sensor",
                                "ts": int(time.time() * 1000),
                                "dog_id": dog,
                                "x": parsed.accel_x,
                                "y": parsed.accel_y,
                                "z": parsed.accel_z,
                                "vibration": round(parsed.vibration, 2),
                                "label": label,
                                "emoji": emoji,
                            }
                        )
                        print(
                            f"[365GPS] STAT {dog} batt={parsed.battery_pct:.0f}% steps={parsed.steps} "
                            f"acc=({parsed.accel_x},{parsed.accel_y},{parsed.accel_z}) "
                            f"|v|={parsed.vibration:.1f} {emoji}{label}",
                            flush=True,
                        )
                    else:
                        sim.apply_tcp_status(dog, parsed.battery_pct, parsed.steps)
                        print(
                            f"[365GPS] STAT {dog} batt={parsed.battery_pct:.0f}% steps={parsed.steps} (no IMU)",
                            flush=True,
                        )
                    continue

                print(f"[365GPS] {peer} unhandled proto=0x{proto:02x}", flush=True)

    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        print(f"[365GPS] disconnected {peer}", flush=True)


async def serve_tcp_gateway(host: str = "0.0.0.0", port: int = 6063) -> None:
    server = await asyncio.start_server(_handle_client, host=host, port=port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    print(f"[365GPS] listening TCP {addrs}", flush=True)
    async with server:
        await server.serve_forever()


def main() -> None:
    from main_v2 import get_shared_simulator

    get_shared_simulator()
    print(
        "[365GPS] Standalone gateway: simulator is only in this process. "
        "For live map integration run: ENABLE_TCP_GATEWAY=1 uvicorn main_v2:app …",
        flush=True,
    )
    try:
        asyncio.run(serve_tcp_gateway())
    except KeyboardInterrupt:
        print("[365GPS] shutdown", flush=True)


if __name__ == "__main__":
    main()
