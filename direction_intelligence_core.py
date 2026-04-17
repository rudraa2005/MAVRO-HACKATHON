"""Direction Intelligence Layer — pure-NumPy wrong-way detection engine.

Determines whether a vehicle is moving against the legal direction of a road
using vector dot-product analysis, temporal consistency checking, and one-way
constraint weighting.

Inputs (per tick, per vehicle):
    - Snapped road vector (vx_r, vy_r) from the Map-Matching Layer
    - Vehicle GPS position + timestamp
    - Whether the matched road is one-way

The engine maintains per-vehicle rolling buffers internally, so callers just
feed one probe per vehicle per tick and read the result.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np


EARTH_RADIUS_M = 6_371_000.0


# ── data classes ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class DirectionProbe:
    """Single vehicle observation fed to the engine."""

    vehicle_id: int
    lat: float
    lon: float
    timestamp: float
    speed_mps: float
    road_vector: tuple[float, float] | None  # (vx, vy) from map-matching
    oneway: bool
    matched_edge_id: int | None


@dataclass(slots=True)
class DirectionResult:
    """Output of the direction analysis for one vehicle."""

    vehicle_id: int
    direction_similarity: float
    wrong_way_probability: float
    is_violation: bool
    confidence: float
    motion_vector: list[float] | None
    window_size: int
    avg_wwp: float
    variance: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "direction_similarity": self.direction_similarity,
            "wrong_way_probability": self.wrong_way_probability,
            "is_violation": self.is_violation,
            "confidence": self.confidence,
            "motion_vector": self.motion_vector,
            "window_size": self.window_size,
            "avg_wwp": self.avg_wwp,
            "variance": self.variance,
        }


# ── sliding-window buffers ───────────────────────────────────────────────────


class TrajectoryBuffer:
    """Rolling buffer of the last *max_points* GPS positions for one vehicle.

    Points older than *max_age_seconds* are pruned on every insertion.
    """

    __slots__ = ("_max_points", "_max_age", "_points")

    def __init__(self, max_points: int = 10, max_age_seconds: float = 10.0) -> None:
        self._max_points = max_points
        self._max_age = max_age_seconds
        self._points: deque[tuple[float, float, float]] = deque(maxlen=max_points)

    def add(self, lat: float, lon: float, timestamp: float) -> None:
        self._points.append((lat, lon, timestamp))
        self._prune(timestamp)

    def get_points(self) -> list[tuple[float, float, float]]:
        return list(self._points)

    def size(self) -> int:
        return len(self._points)

    def _prune(self, now: float) -> None:
        while self._points and (now - self._points[0][2]) > self._max_age:
            self._points.popleft()


class WWPBuffer:
    """Rolling window of recent wrong-way probability scores.

    Used for temporal consistency: only sustained high scores trigger a
    violation, single-frame spikes are smoothed out.
    """

    __slots__ = ("_window_seconds", "_scores")

    def __init__(self, window_seconds: float = 5.0) -> None:
        self._window_seconds = window_seconds
        self._scores: deque[tuple[float, float]] = deque()

    def add(self, wwp: float, timestamp: float) -> None:
        self._scores.append((wwp, timestamp))
        self._prune(timestamp)

    @property
    def scores(self) -> deque[tuple[float, float]]:
        return self._scores

    def mean(self) -> float:
        if not self._scores:
            return 0.0
        return float(np.mean([s for s, _ in self._scores]))

    def variance(self) -> float:
        if len(self._scores) < 2:
            return 0.0
        return float(np.var([s for s, _ in self._scores]))

    def _prune(self, now: float) -> None:
        while self._scores and (now - self._scores[0][1]) > self._window_seconds:
            self._scores.popleft()


# ── coordinate helpers ────────────────────────────────────────────────────────


def _project_xy(
    lat: float, lon: float, ref_lat: float, ref_lon: float
) -> tuple[float, float]:
    """Project WGS-84 to local metres around *ref_lat / ref_lon*."""
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


# ── vector math ──────────────────────────────────────────────────────────────


def compute_motion_vector(
    points: list[tuple[float, float, float]],
) -> np.ndarray | None:
    """Return a unit-length motion vector from *points* ``(lat, lon, ts)``.

    Uses displacement from the first to the last point, projected to local
    metres.  Returns ``None`` if the displacement is negligibly small (the
    vehicle is essentially stationary over the window).
    """
    if len(points) < 2:
        return None

    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    ref_lat = sum(lats) / len(lats)
    ref_lon = sum(lons) / len(lons)

    x0, y0 = _project_xy(points[0][0], points[0][1], ref_lat, ref_lon)
    xn, yn = _project_xy(points[-1][0], points[-1][1], ref_lat, ref_lon)

    vx = xn - x0
    vy = yn - y0
    norm = math.hypot(vx, vy)
    if norm < 1e-9:
        return None

    return np.array([vx / norm, vy / norm], dtype=np.float64)


def direction_similarity(v_vehicle: np.ndarray, v_road: np.ndarray) -> float:
    """Cosine similarity (dot product of unit vectors).

    * ≈ +1 → correct direction
    * ≈  0 → perpendicular / uncertain
    * ≈ −1 → wrong direction
    """
    return float(np.dot(v_vehicle, v_road))


def raw_wwp(similarity: float) -> float:
    """Map cosine similarity ∈ [−1, +1] to wrong-way probability ∈ [0, 1]."""
    return (1.0 - similarity) / 2.0


# ── engine ────────────────────────────────────────────────────────────────────


class DirectionIntelligenceEngine:
    """Stateful per-vehicle direction analyser.

    Call :meth:`process_probe` once per GPS tick for each vehicle.  The engine
    maintains per-vehicle trajectory and WWP buffers internally so the caller
    only needs to supply the latest observation.

    Complexity is O(1) per vehicle per update (rolling buffer operations).
    """

    def __init__(
        self,
        *,
        trajectory_points: int = 10,
        trajectory_max_age_s: float = 10.0,
        wwp_window_s: float = 5.0,
        violation_threshold: float = 0.65,
        sustained_seconds: float = 2.0,
        min_speed_mps: float = 1.5,
        oneway_alpha: float = 0.75,
        twoway_alpha: float = 0.55,
        temporal_beta: float = 0.25,
    ) -> None:
        self.trajectory_points = trajectory_points
        self.trajectory_max_age_s = trajectory_max_age_s
        self.wwp_window_s = wwp_window_s
        self.violation_threshold = violation_threshold
        self.sustained_seconds = sustained_seconds
        self.min_speed_mps = min_speed_mps
        self.oneway_alpha = oneway_alpha
        self.twoway_alpha = twoway_alpha
        self.temporal_beta = temporal_beta

        self._traj: dict[int, TrajectoryBuffer] = {}
        self._wwp: dict[int, WWPBuffer] = {}

    # ── public API ────────────────────────────────────────────────────────

    def process_probe(self, probe: DirectionProbe) -> DirectionResult:
        """Analyse one vehicle's latest probe and return the direction verdict."""
        vid = probe.vehicle_id
        traj = self._get_traj(vid)
        wwp_buf = self._get_wwp(vid)

        traj.add(probe.lat, probe.lon, probe.timestamp)
        points = traj.get_points()

        # Need ≥ 2 trajectory points and a valid road vector
        if len(points) < 2 or probe.road_vector is None:
            return self._empty(vid)

        # ── speed gate ────────────────────────────────────────────────────
        dt = points[-1][2] - points[0][2]
        if dt <= 0:
            return self._empty(vid)

        x0, y0 = _project_xy(points[0][0], points[0][1], probe.lat, probe.lon)
        xn, yn = _project_xy(points[-1][0], points[-1][1], probe.lat, probe.lon)
        distance = math.hypot(xn - x0, yn - y0)
        speed_mps = distance / dt

        if speed_mps < self.min_speed_mps:
            return self._empty(vid)

        # ── Step 1: compute motion vector ─────────────────────────────────
        v_vehicle = compute_motion_vector(points)
        if v_vehicle is None:
            return self._empty(vid)

        # ── normalise road vector ─────────────────────────────────────────
        v_road = np.array(probe.road_vector, dtype=np.float64)
        road_norm = float(np.linalg.norm(v_road))
        if road_norm < 1e-9:
            return self._empty(vid)
        v_road = v_road / road_norm

        # ── Step 2: direction similarity (dot product) ────────────────────
        sim = direction_similarity(v_vehicle, v_road)

        # ── Step 3: raw wrong-way probability ─────────────────────────────
        wwp_raw_val = raw_wwp(sim)

        # ── Step 4: temporal consistency ──────────────────────────────────
        wwp_buf.add(wwp_raw_val, probe.timestamp)
        avg = wwp_buf.mean()
        var = wwp_buf.variance()
        win = len(wwp_buf.scores)

        # ── Step 5: one-way constraint ────────────────────────────────────
        alpha = self.oneway_alpha if probe.oneway else self.twoway_alpha
        beta = self.temporal_beta

        # temporal stability ∈ [0, 1] — low variance = high stability
        stability = max(0.0, 1.0 - var * 4.0)

        # ── Step 6: final probability ─────────────────────────────────────
        final_wwp = alpha * avg + beta * stability * avg
        final_wwp = min(1.0, max(0.0, final_wwp))

        # ── sustained check (reject transient spikes) ─────────────────────
        sustained = self._is_sustained(wwp_buf, probe.timestamp)

        # ── confidence ────────────────────────────────────────────────────
        point_f = min(len(points) / 5.0, 1.0)
        window_f = min(win / 3.0, 1.0)
        speed_f = min(speed_mps / 3.0, 1.0)
        conf = point_f * window_f * speed_f * (1.0 - var)
        conf = min(1.0, max(0.0, conf))
        if probe.oneway:
            conf = min(1.0, conf * 1.15)

        is_viol = (
            final_wwp >= self.violation_threshold
            and sustained
            and conf >= 0.3
        )

        return DirectionResult(
            vehicle_id=vid,
            direction_similarity=round(sim, 4),
            wrong_way_probability=round(final_wwp, 4),
            is_violation=is_viol,
            confidence=round(conf, 4),
            motion_vector=[round(float(v_vehicle[0]), 6), round(float(v_vehicle[1]), 6)],
            window_size=win,
            avg_wwp=round(avg, 4),
            variance=round(var, 6),
        )

    def seed_trajectory(
        self,
        vehicle_id: int,
        points: list[tuple[float, float, float]],
    ) -> None:
        """Pre-fill a vehicle's trajectory buffer with historical points.

        *points* is a list of ``(lat, lon, timestamp)`` tuples in
        chronological order.
        """
        traj = self._get_traj(vehicle_id)
        for lat, lon, ts in points:
            traj.add(lat, lon, ts)

    def clear_vehicle(self, vehicle_id: int) -> None:
        self._traj.pop(vehicle_id, None)
        self._wwp.pop(vehicle_id, None)

    def clear_all(self) -> None:
        self._traj.clear()
        self._wwp.clear()

    # ── internals ─────────────────────────────────────────────────────────

    def _get_traj(self, vid: int) -> TrajectoryBuffer:
        if vid not in self._traj:
            self._traj[vid] = TrajectoryBuffer(
                max_points=self.trajectory_points,
                max_age_seconds=self.trajectory_max_age_s,
            )
        return self._traj[vid]

    def _get_wwp(self, vid: int) -> WWPBuffer:
        if vid not in self._wwp:
            self._wwp[vid] = WWPBuffer(window_seconds=self.wwp_window_s)
        return self._wwp[vid]

    def _is_sustained(self, wwp_buf: WWPBuffer, now: float) -> bool:
        if not wwp_buf.scores:
            return False
        return (now - wwp_buf.scores[0][1]) >= self.sustained_seconds

    @staticmethod
    def _empty(vid: int) -> DirectionResult:
        return DirectionResult(
            vehicle_id=vid,
            direction_similarity=0.0,
            wrong_way_probability=0.0,
            is_violation=False,
            confidence=0.0,
            motion_vector=None,
            window_size=0,
            avg_wwp=0.0,
            variance=0.0,
        )
