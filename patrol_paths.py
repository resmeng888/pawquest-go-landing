"""
Fixed patrol routes around Central Park, NYC.

Each route is a polyline; dogs follow at 3–6 km/h with loop or ping-pong.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

Point = Tuple[float, float]

# 3–6 km/h in m/s
PATROL_SPEED_MIN_MS = 3.0 / 3.6
PATROL_SPEED_MAX_MS = 6.0 / 3.6

M_PER_DEG_LAT = 111_320.0


def _m_per_deg_lng(lat: float) -> float:
    return M_PER_DEG_LAT * math.cos(math.radians(lat))


def _segment_length_m(a: Point, b: Point) -> float:
    lat1, lng1 = a
    lat2, lng2 = b
    dn = (lat2 - lat1) * M_PER_DEG_LAT
    de = (lng2 - lng1) * _m_per_deg_lng((lat1 + lat2) * 0.5)
    return math.hypot(dn, de)


@dataclass(frozen=True)
class PatrolRoute:
    name: str
    waypoints: Tuple[Point, ...]
    loop: bool = True  # False = ping-pong between first and last


# Land-only bbox (Central Park + Manhattan grid — no water)
_LAND_LAT_MIN, _LAND_LAT_MAX = 40.7680, 40.7920
_LAND_LNG_MIN, _LAND_LNG_MAX = -73.9820, -73.9520


def _assert_land_points(route: PatrolRoute) -> None:
    for lat, lng in route.waypoints:
        if not (_LAND_LAT_MIN <= lat <= _LAND_LAT_MAX and _LAND_LNG_MIN <= lng <= _LAND_LNG_MAX):
            raise ValueError(f"route {route.name} waypoint ({lat}, {lng}) outside land box")


# Manhattan / Central Park street-grid style paths (all on land)
PATROL_ROUTES: Tuple[PatrolRoute, ...] = (
    PatrolRoute(
        "5th_ave_shuttle",
        ((40.7745, -73.9665), (40.7880, -73.9665)),
        loop=False,
    ),
    PatrolRoute(
        "great_lawn_loop",
        (
            (40.7782, -73.9715),
            (40.7782, -73.9605),
            (40.7718, -73.9605),
            (40.7718, -73.9715),
        ),
        loop=True,
    ),
    PatrolRoute(
        "central_park_west",
        ((40.7720, -73.9750), (40.7850, -73.9750)),
        loop=False,
    ),
    PatrolRoute(
        "east_drive",
        ((40.7730, -73.9580), (40.7860, -73.9580)),
        loop=False,
    ),
    PatrolRoute(
        "79th_transverse",
        ((40.7785, -73.9720), (40.7785, -73.9580)),
        loop=False,
    ),
    PatrolRoute(
        "bethesda_loop",
        (
            (40.7755, -73.9695),
            (40.7755, -73.9635),
            (40.7725, -73.9635),
            (40.7725, -73.9695),
        ),
        loop=True,
    ),
    PatrolRoute(
        "mall_walk",
        ((40.7728, -73.9665), (40.7798, -73.9665)),
        loop=False,
    ),
    PatrolRoute(
        "columbus_edge",
        ((40.7740, -73.9765), (40.7870, -73.9765)),
        loop=False,
    ),
    PatrolRoute(
        "park_drive_south",
        ((40.7695, -73.9700), (40.7695, -73.9620), (40.7760, -73.9620), (40.7760, -73.9700)),
        loop=True,
    ),
    PatrolRoute(
        "madison_grid",
        ((40.7735, -73.9650), (40.7805, -73.9650), (40.7805, -73.9610), (40.7735, -73.9610)),
        loop=True,
    ),
)

for _r in PATROL_ROUTES:
    _assert_land_points(_r)


class RoutePatrol:
    """Advance along a fixed route; loop or ping-pong at ends."""

    def __init__(
        self,
        route: PatrolRoute,
        *,
        speed_mps: float,
        start_offset_m: float = 0.0,
    ) -> None:
        self.route = route
        self.speed_mps = max(PATROL_SPEED_MIN_MS, min(PATROL_SPEED_MAX_MS, speed_mps))
        self._seg_lens: List[float] = []
        self._cum: List[float] = [0.0]
        wps = route.waypoints
        for i in range(len(wps) - 1):
            ln = _segment_length_m(wps[i], wps[i + 1])
            self._seg_lens.append(ln)
            self._cum.append(self._cum[-1] + ln)
        if route.loop and len(wps) > 1:
            ln = _segment_length_m(wps[-1], wps[0])
            self._seg_lens.append(ln)
            self._cum.append(self._cum[-1] + ln)
        self._total = self._cum[-1] if self._cum else 0.0
        self._dist = start_offset_m % self._total if self._total > 0 else 0.0
        self._dir = 1

    @property
    def total_length_m(self) -> float:
        return self._total

    def position(self) -> Point:
        return self._interp(self._dist)

    def step(self, dt_s: float) -> Tuple[float, float, float]:
        """Returns (lat, lng, speed_mps)."""
        if dt_s <= 0 or self._total <= 0:
            lat, lng = self.position()
            return round(lat, 6), round(lng, 6), self.speed_mps

        delta = self.speed_mps * dt_s
        if self.route.loop:
            self._dist = (self._dist + delta) % self._total
        else:
            self._dist += delta * self._dir
            while self._dist >= self._total or self._dist < 0:
                if self._dist >= self._total:
                    overshoot = self._dist - self._total
                    self._dist = self._total - overshoot
                    self._dir = -1
                elif self._dist < 0:
                    self._dist = -self._dist
                    self._dir = 1

        lat, lng = self.position()
        return round(lat, 6), round(lng, 6), self.speed_mps

    def _interp(self, dist: float) -> Point:
        if self._total <= 0:
            return self.route.waypoints[0]
        d = max(0.0, min(dist, self._total - 1e-9))
        wps = self.route.waypoints
        n_seg = len(self._seg_lens)
        for i in range(n_seg):
            if d <= self._cum[i + 1]:
                seg_len = self._seg_lens[i]
                if seg_len <= 1e-9:
                    return wps[i if i < len(wps) else 0]
                t = (d - self._cum[i]) / seg_len
                if self.route.loop and i == n_seg - 1:
                    a, b = wps[-1], wps[0]
                else:
                    a, b = wps[i], wps[i + 1]
                lat = a[0] + (b[0] - a[0]) * t
                lng = a[1] + (b[1] - a[1]) * t
                return lat, lng
        return wps[-1]


def patrol_for_dog_index(dog_index: int, *, speed_mps: float) -> RoutePatrol:
    route = PATROL_ROUTES[dog_index % len(PATROL_ROUTES)]
    probe = RoutePatrol(route, speed_mps=speed_mps)
    total = probe.total_length_m
    offset = (dog_index * 47.0) % total if total > 0 else 0.0
    return RoutePatrol(route, speed_mps=speed_mps, start_offset_m=offset)
