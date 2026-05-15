import asyncio
import json
import math
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from collections import deque

from behavior_map import behavior_for_activity_level
from patrol_paths import (
    PATROL_SPEED_MAX_MS,
    PATROL_SPEED_MIN_MS,
    RoutePatrol,
    patrol_for_dog_index,
)


@dataclass
class DogState:
    dog_id: str
    lat: float
    lng: float
    battery: float
    speed_mps: float
    heading_deg: float
    steps: int = 0
    online: bool = True
    sound_until_ts: float = 0.0
    history: Deque[Tuple[float, float]] = None  # type: ignore[assignment]
    vibration: float = 0.0
    accel_x: int = 0
    accel_y: int = 0
    accel_z: int = 0
    behavior_label: str = "Resting"
    behavior_emoji: str = "💤"
    last_idle_drain_ts: float = field(default_factory=time.time)
    _battery_step_bucket: int = 0
    _walk_m_frac: float = 0.0
    cruise_target_mps: float = 0.0


def _wrap_heading(deg: float) -> float:
    return deg % 360.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# Central Park, NYC — map center for UI defaults
PARK_CENTER_LAT = 40.7812
PARK_CENTER_LNG = -73.9665

# Mock DB — in-memory position log (future SQL migration).
data_history: List[dict] = []
_DATA_HISTORY_MAX = 50_000


def _coord6(x: float) -> float:
    return round(float(x), 6)


def _record_data_history(dog_id: str, lat: float, lng: float, ts: Optional[float] = None) -> None:
    """Append a GPS sample with timestamp; trim oldest when over cap."""
    data_history.append(
        {
            "dog_id": dog_id,
            "lat": _coord6(lat),
            "lng": _coord6(lng),
            "ts": float(ts if ts is not None else time.time()),
        }
    )
    overflow = len(data_history) - _DATA_HISTORY_MAX
    if overflow > 0:
        del data_history[:overflow]


class DogSimulator:
    def __init__(
        self,
        n: int = 100,
        center_lat: float = PARK_CENTER_LAT,
        center_lng: float = PARK_CENTER_LNG,
        radius_m: float = 320.0,
        seed: Optional[int] = 42,
    ) -> None:
        self._rng = random.Random(seed)
        self._dogs: Dict[str, DogState] = {}
        self._critical_dogs: Set[str] = set()
        self._offline_dogs: Set[str] = set()
        self._patrol: Dict[str, RoutePatrol] = {}

        for i in range(n):
            dog_id = f"dog_{i+1:03d}"
            speed_mps = self._rng.uniform(PATROL_SPEED_MIN_MS, PATROL_SPEED_MAX_MS)
            walker = patrol_for_dog_index(i, speed_mps=speed_mps)
            lat, lng = walker.position()
            self._patrol[dog_id] = walker

            if self._rng.random() < 0.06:
                start_battery = self._rng.uniform(38.0, 62.0)
            else:
                start_battery = self._rng.uniform(72.0, 100.0)

            self._dogs[dog_id] = DogState(
                dog_id=dog_id,
                lat=lat,
                lng=lng,
                battery=start_battery,
                speed_mps=speed_mps,
                heading_deg=0.0,
                steps=0,
                cruise_target_mps=speed_mps,
            )
            self._dogs[dog_id].history = deque([(lat, lng)], maxlen=50)

        # --- Inject anomalies (soft demo: one mildly low battery, one offline) ---
        ids = list(self._dogs.keys())
        self._rng.shuffle(ids)
        if ids:
            critical_id = ids[0]
            self._critical_dogs.add(critical_id)
            self._dogs[critical_id].battery = self._rng.uniform(20.0, 28.0)
        if len(ids) >= 2 and self._rng.random() < 0.85:
            offline_id = ids[1]
            self._offline_dogs.add(offline_id)
            self._dogs[offline_id].online = False

        self._lock = threading.RLock()
        self._imei_to_dog: Dict[str, str] = {}
        self._hardware_dogs: Set[str] = set()

    def bind_imei(self, imei: str) -> str:
        """Bind 365GPS IMEI (BCD string) to a dog slot for TCP-fed telemetry."""
        with self._lock:
            if imei in self._imei_to_dog:
                return self._imei_to_dog[imei]
            suffix = int(imei[-3:]) % 100
            if suffix == 0:
                suffix = 100
            dog_id = f"dog_{suffix:03d}"
            self._imei_to_dog[imei] = dog_id
            self._hardware_dogs.add(dog_id)
            return dog_id

    def apply_tcp_gps(self, dog_id: str, lat: float, lng: float, speed_kmh: float) -> None:
        with self._lock:
            dog = self._dogs.get(dog_id)
            if not dog:
                return
            dog.lat, dog.lng = _coord6(lat), _coord6(lng)
            sk = _clamp(float(speed_kmh), 3.0, 6.0)
            dog.speed_mps = sk / 3.6
            if dog.history is not None:
                dog.history.append((dog.lat, dog.lng))
            _record_data_history(dog_id, dog.lat, dog.lng)

    def apply_tcp_status(
        self,
        dog_id: str,
        battery_pct: float,
        steps: int,
        *,
        accel: Optional[Tuple[int, int, int]] = None,
        vibration: Optional[float] = None,
    ) -> None:
        with self._lock:
            dog = self._dogs.get(dog_id)
            if not dog:
                return
            dog.battery = _clamp(float(battery_pct), 0.0, 100.0)
            dog.steps = int(steps) & 0xFFFFF
            if accel is not None and vibration is not None:
                ax, ay, az = accel
                dog.accel_x, dog.accel_y, dog.accel_z = int(ax), int(ay), int(az)
                dog.vibration = float(vibration)
                dog.behavior_label, dog.behavior_emoji = behavior_for_activity_level(dog.vibration)
            else:
                dog.behavior_label, dog.behavior_emoji = behavior_for_activity_level(dog.vibration)

    def step(self, dt_s: float) -> List[dict]:
        with self._lock:
            out: List[dict] = []
            now = time.time()
            for dog in self._dogs.values():
                if dog.dog_id in self._offline_dogs:
                    out.append(
                        {
                            "id": dog.dog_id,
                            "lat": _coord6(dog.lat),
                            "lng": _coord6(dog.lng),
                            "battery": round(_clamp(dog.battery, 0.0, 100.0), 1),
                            "speed": 0.0,
                            "online": False,
                            "sound": dog.sound_until_ts > now,
                            "history": [[_coord6(la), _coord6(ln)] for (la, ln) in (dog.history or [])],
                            "steps": dog.steps,
                            "vibration": round(dog.vibration, 1),
                            "behavior_label": dog.behavior_label,
                            "behavior_emoji": dog.behavior_emoji,
                        }
                    )
                    continue

                if dog.dog_id in self._hardware_dogs:
                    out.append(
                        {
                            "id": dog.dog_id,
                            "lat": _coord6(dog.lat),
                            "lng": _coord6(dog.lng),
                            "battery": round(_clamp(dog.battery, 0.0, 100.0), 1),
                            "speed": round(dog.speed_mps * 3.6, 1),
                            "online": True,
                            "sound": dog.sound_until_ts > now,
                            "history": [[_coord6(la), _coord6(ln)] for (la, ln) in (dog.history or [])],
                            "steps": dog.steps,
                            "vibration": round(dog.vibration, 1),
                            "behavior_label": dog.behavior_label,
                            "behavior_emoji": dog.behavior_emoji,
                        }
                    )
                    continue

                walker = self._patrol.get(dog.dog_id)
                if walker is None:
                    speed_mps = self._rng.uniform(PATROL_SPEED_MIN_MS, PATROL_SPEED_MAX_MS)
                    walker = patrol_for_dog_index(int(dog.dog_id.split("_")[-1]) - 1, speed_mps=speed_mps)
                    self._patrol[dog.dog_id] = walker

                lat, lng, spd = walker.step(dt_s)
                dog.lat, dog.lng = lat, lng
                dog.speed_mps = spd
                meters = spd * dt_s if dt_s > 0 else 0.0
                if dt_s > 0:
                    if dog.history is not None:
                        dog.history.append((lat, lng))
                    _record_data_history(dog.dog_id, lat, lng, ts=now)

                # Battery: −1% per ~100 steps of walked distance (~0.65 m/step), not per-second % drain.
                meters_per_step = 0.65
                dog._walk_m_frac += meters
                whole_steps = int(dog._walk_m_frac / meters_per_step)
                if whole_steps > 0:
                    dog._walk_m_frac -= whole_steps * meters_per_step
                    dog._battery_step_bucket += whole_steps
                    dog.steps = (dog.steps + whole_steps) & 0xFFFFF
                    while dog._battery_step_bucket >= 100:
                        dog._battery_step_bucket -= 100
                        dog.battery = _clamp(dog.battery - 1.0, 0.0, 100.0)

                # ~Every 2 minutes: small random idle drain (0.1%).
                if now - dog.last_idle_drain_ts >= 120.0:
                    dog.last_idle_drain_ts = now
                    if self._rng.random() < 0.55:
                        dog.battery = _clamp(dog.battery - 0.1, 0.0, 100.0)

                if dog.dog_id in self._critical_dogs:
                    dog.battery = _clamp(dog.battery, 18.0, 30.0)

                # Simulated collars: no IMU — infer a soft activity level from speed for the three-tier map.
                sim_activity = dog.speed_mps * 8_200.0
                lvl = max(dog.vibration, sim_activity)
                dog.behavior_label, dog.behavior_emoji = behavior_for_activity_level(lvl)

                out.append(
                    {
                        "id": dog.dog_id,
                        "lat": _coord6(dog.lat),
                        "lng": _coord6(dog.lng),
                        "battery": round(_clamp(dog.battery, 0.0, 100.0), 1),
                        "speed": round(dog.speed_mps * 3.6, 1),
                        "online": True,
                        "sound": dog.sound_until_ts > now,
                        "history": [[_coord6(la), _coord6(ln)] for (la, ln) in (dog.history or [])],
                        "steps": dog.steps,
                        "vibration": round(dog.vibration, 1),
                        "behavior_label": dog.behavior_label,
                        "behavior_emoji": dog.behavior_emoji,
                    }
                )
            return out

    def play_sound(self, dog_id: str, seconds: float = 5.0) -> bool:
        with self._lock:
            dog = self._dogs.get(dog_id)
            if not dog:
                return False
            dog.sound_until_ts = max(dog.sound_until_ts, time.time() + seconds)
            return True


app = FastAPI(title="PawQuest v2 Backend")

ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROOT / "index.html"

if (ROOT / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(ROOT / "assets")), name="assets")


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(str(INDEX_PATH))


class Hub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._clients_lock = asyncio.Lock()
        self._simulators: Dict[WebSocket, DogSimulator] = {}

    async def connect(self, ws: WebSocket, simulator: DogSimulator) -> None:
        await ws.accept()
        async with self._clients_lock:
            self._clients.add(ws)
            self._simulators[ws] = simulator

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._clients_lock:
            self._clients.discard(ws)
            self._simulators.pop(ws, None)

    async def broadcast_tick(self, dt_s: float) -> None:
        async with self._clients_lock:
            clients = list(self._clients)

        if not clients:
            return

        ts_ms = int(time.time() * 1000)
        dogs = get_shared_simulator().step(dt_s)
        msg = json.dumps({"type": "tick", "ts": ts_ms, "dogs": dogs}, separators=(",", ":"))
        payloads: Dict[WebSocket, str] = {ws: msg for ws in clients}

        # Send concurrently; remove dead sockets.
        async def _send_one(sock: WebSocket, msg: str) -> None:
            await sock.send_text(msg)

        tasks = []
        for ws, msg in payloads.items():
            tasks.append(asyncio.create_task(_send_one(ws, msg)))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for ws, r in zip(payloads.keys(), results):
                if isinstance(r, Exception):
                    await self.disconnect(ws)

    async def broadcast_sensor(self, payload: dict) -> None:
        """Push a single hardware sensor frame to all WebSocket clients (TCP path)."""
        async with self._clients_lock:
            clients = list(self._clients)
        if not clients:
            return
        text = json.dumps(payload, separators=(",", ":"))

        async def _send_one(sock: WebSocket, msg: str) -> None:
            await sock.send_text(msg)

        tasks = [asyncio.create_task(_send_one(ws, text)) for ws in clients]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        async with self._clients_lock:
            for ws, r in zip(clients, results):
                if isinstance(r, Exception):
                    await self.disconnect(ws)


hub = Hub()

_shared_simulator: Optional[DogSimulator] = None
_shared_sim_lock = threading.Lock()


def get_shared_simulator(
    n: int = 100,
    center_lat: float = PARK_CENTER_LAT,
    center_lng: float = PARK_CENTER_LNG,
    radius_m: float = 320.0,
    seed: int = 42,
) -> DogSimulator:
    """Single process-wide simulator (Web UI + TCP gateway share state)."""
    global _shared_simulator
    with _shared_sim_lock:
        if _shared_simulator is None:
            _shared_simulator = DogSimulator(
                n=n,
                center_lat=center_lat,
                center_lng=center_lng,
                radius_m=radius_m,
                seed=seed,
            )
        return _shared_simulator


@app.on_event("startup")
async def _startup() -> None:
    async def _loop() -> None:
        tick_s = 1.0
        while True:
            try:
                await hub.broadcast_tick(tick_s)
            except Exception:
                # Keep demo resilient; per-socket errors are handled in broadcast_tick.
                pass
            await asyncio.sleep(tick_s)

    asyncio.create_task(_loop())

    import os

    if os.environ.get("ENABLE_TCP_GATEWAY", "").lower() in ("1", "true", "yes"):
        try:
            from tcp_gateway import serve_tcp_gateway

            asyncio.create_task(serve_tcp_gateway())
            print("[TCP] 365GPS gateway scheduled on :6063 (ENABLE_TCP_GATEWAY=1)", flush=True)
        except Exception as exc:
            print("[TCP] gateway failed to start:", exc, flush=True)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    # Optional query params: center=lat,lng  seed=int  n=int  radius=float(meters)
    qp = parse_qs(ws.scope.get("query_string", b"").decode("utf-8"))
    center = qp.get("center", ["40.7812,-73.9665"])[0]
    seed = qp.get("seed", ["42"])[0]
    n = qp.get("n", ["100"])[0]
    radius = qp.get("radius", ["480"])[0]
    try:
        center_lat_str, center_lng_str = center.split(",", 1)
        center_lat = float(center_lat_str)
        center_lng = float(center_lng_str)
    except Exception:
        center_lat, center_lng = PARK_CENTER_LAT, PARK_CENTER_LNG

    try:
        seed_i = int(seed)
    except Exception:
        seed_i = 42

    try:
        n_i = int(n)
    except Exception:
        n_i = 100

    try:
        radius_m = float(radius)
    except Exception:
        radius_m = 480.0

    simulator = get_shared_simulator(
        n=n_i,
        center_lat=center_lat,
        center_lng=center_lng,
        radius_m=radius_m,
        seed=seed_i,
    )
    await hub.connect(ws, simulator)

    # Send an initial snapshot immediately so markers appear without waiting for first tick.
    try:
        dogs = simulator.step(0.0)
        await ws.send_text(json.dumps({"type": "snapshot", "ts": int(time.time() * 1000), "dogs": dogs}))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            if msg.get("action") == "play_sound":
                dog_id = msg.get("dog_id")
                if isinstance(dog_id, str) and simulator.play_sound(dog_id, seconds=5.0):
                    print(f"[CMD] 向 {dog_id} 發送蜂鳴器指令成功！", flush=True)
                    # Optional ack
                    await ws.send_text(json.dumps({"type": "ack", "action": "play_sound", "dog_id": dog_id}))
    except WebSocketDisconnect:
        await hub.disconnect(ws)
    except Exception:
        await hub.disconnect(ws)


if __name__ == "__main__":
    import os
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main_v2:app", host="0.0.0.0", port=port, reload=True)
