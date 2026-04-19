from __future__ import annotations

import random
import statistics
import threading
import time
from collections import defaultdict
from collections import deque
from dataclasses import dataclass, field
from math import fabs
import math
from typing import NamedTuple

from flask import Flask

from backend.extensions import db
from backend.models import RoadSegment, Vehicle, VehicleHistory
from backend.services.geo import (
    add_noise,
    cumulative_path_lengths,
    haversine_distance_m,
    interpolate_path_position,
    path_bearing_at,
)
from backend.services.eval_logger import eval_logger
from backend.eval.dataset_logger import eval_dataset_logger
from backend.services.semantic_reasoning import sigmoid_confidence


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BEHAVIOR_SPEED_FACTORS = {
    "calm": 1.1,
    "normal": 1.4,
    "aggressive": 1.8,
}

BEHAVIOR_GAP_FACTORS = {
    "calm": 0.8,
    "normal": 0.6,
    "aggressive": 0.4,
}

SAFE_DISTANCE_M = 12.0
INTERSECTION_THRESHOLD = 0.85   # fraction of edge where slowdown starts
INTERSECTION_BRAKE = 0.6
SPEED_JITTER_MPS = 1.5
WRONG_WAY_SPEED_BOOST = 1.2


class RouteOption(NamedTuple):
    segment_id: int
    direction: int
    wrong_way: bool


@dataclass
class SegmentRuntime:
    id: int
    start_node_id: int
    end_node_id: int
    oneway: bool
    length_m: float
    geometry: list[dict[str, float]]
    cumulative_lengths: list[float]
    speed_limit_mps: float
    road_class: str
    bearing: float


# ---------------------------------------------------------------------------
# In-memory per-vehicle state (hot path — avoids DB reads every tick)
# ---------------------------------------------------------------------------

@dataclass
class VehicleLive:
    db_id: int
    segment_id: int
    progress_m: float
    direction: int
    speed_mps: float
    behavior: str
    wrong_way: bool
    wrong_way_until: float | None
    lat: float
    lon: float
    bearing: float
    timestamp: float
    # ML metrics (computed each tick)
    anomaly_score: float = 0.0
    risk_score: float = 0.0
    wwp: float = 0.0
    ttc: float | None = None
    maneuverability: float = 1.0
    nearby_count: int = 0
    closest_distance_m: float | None = None
    state: str = "normal"
    confidence: float = 0.0
    lateral_offset: float = 0.0
    target_speed_mps: float = 0.0
    demo_focus: bool = False
    heading_deg: float = 0.0
    heading_smooth_deg: float = 0.0
    road_bearing_deg: float = 0.0
    angle_diff_deg: float = 0.0
    gps_stability: str = "MEDIUM"
    edge_case: str = "NONE"
    reference: bool = False
    _heading_window: deque[float] = field(default_factory=lambda: deque(maxlen=6))
    _suspect_since: float | None = None
    _last_lat: float | None = None
    _last_lon: float | None = None
    dirty: bool = True  # whether the vehicle changed since last API read


# ---------------------------------------------------------------------------
# Analytics ring buffer — stores per-tick snapshots for timeseries
# ---------------------------------------------------------------------------

@dataclass
class TickSnapshot:
    t: float
    vehicles: list[dict]  # compact per-vehicle metrics


class AnalyticsBuffer:
    def __init__(self, max_length: int = 120) -> None:
        self._buffer: list[TickSnapshot] = []
        self._max = max_length

    def push(self, snapshot: TickSnapshot) -> None:
        self._buffer.append(snapshot)
        if len(self._buffer) > self._max:
            self._buffer = self._buffer[-self._max:]

    def get_all(self) -> list[TickSnapshot]:
        return list(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()


# ---------------------------------------------------------------------------
# Simulation Engine
# ---------------------------------------------------------------------------

class VehicleSimulationEngine:
    def __init__(self) -> None:
        self._app: Flask | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._rng = random.Random(42)
        self._segments: dict[int, SegmentRuntime] = {}
        self._normal_options: dict[int, list[RouteOption]] = {}
        self._wrong_way_options: dict[int, list[RouteOption]] = {}
        # In-memory vehicle pool
        self._vehicles: dict[int, VehicleLive] = {}
        self._seq = 0  # monotonic sequence for delta tracking
        self._analytics = AnalyticsBuffer()
        self._tick_count = 0
        self._reference_vehicle_id: int | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, app: Flask, force: bool = False) -> None:
        with self._lock:
            self._app = app
            self._rng = random.Random(app.config["SIMULATION_RANDOM_SEED"])
            self.refresh_network(app)

            with app.app_context():
                self._sync_from_db()
                self._ensure_vehicle_pool(time.time())

            if self._started and not force:
                return

            if self._thread and self._thread.is_alive():
                self._started = True
                return

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="flowguard-simulator",
                daemon=True,
            )
            self._thread.start()
            self._started = True

    def is_running(self) -> bool:
        return bool(self._started and self._thread and self._thread.is_alive())

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=2.0)
            self._thread = None
            self._started = False
            self._stop_event = threading.Event()

    def clear_fleet(self, app: Flask) -> None:
        with app.app_context():
            VehicleHistory.query.delete()
            Vehicle.query.delete()
            db.session.commit()
        self._vehicles.clear()
        self._analytics.clear()
        self._tick_count = 0

    def refresh_network(self, app: Flask) -> None:
        with app.app_context():
            segments = RoadSegment.query.order_by(RoadSegment.id).all()
            self._segments = {}
            self._normal_options = {}
            self._wrong_way_options = {}

            for segment in segments:
                runtime = SegmentRuntime(
                    id=segment.id,
                    start_node_id=segment.start_node_id,
                    end_node_id=segment.end_node_id,
                    oneway=segment.oneway,
                    length_m=max(segment.length_m, 0.1),
                    geometry=segment.geometry,
                    cumulative_lengths=cumulative_path_lengths(segment.geometry),
                    speed_limit_mps=segment.speed_limit_mps or 12.0,
                    road_class=segment.road_class or "residential",
                    bearing=segment.bearing or 0.0,
                )
                self._segments[runtime.id] = runtime
                self._normal_options.setdefault(runtime.start_node_id, []).append(
                    RouteOption(runtime.id, 1, False)
                )
                if not runtime.oneway:
                    self._normal_options.setdefault(runtime.end_node_id, []).append(
                        RouteOption(runtime.id, -1, False)
                    )
                else:
                    # Rare natural wrong-way (0.2% chance)
                    self._wrong_way_options.setdefault(runtime.end_node_id, []).append(
                        RouteOption(runtime.id, -1, True)
                    )

    def reseed_demo_fleet(self, app: Flask) -> None:
        with app.app_context():
            now = time.time()
            VehicleHistory.query.delete()
            Vehicle.query.delete()
            db.session.commit()
            self._vehicles.clear()
            self._analytics.clear()
            self._tick_count = 0
            self.refresh_network(app)
            self._ensure_vehicle_pool(now)

    def set_vehicle_count(self, app: Flask, count: int) -> None:
        """Adjust target vehicle count (from density slider)."""
        max_v = app.config.get("MAX_VEHICLES", 40)
        app.config["VEHICLE_COUNT"] = min(max(count, 5), max_v)

    # ------------------------------------------------------------------
    # API accessors
    # ------------------------------------------------------------------

    def get_vehicles_snapshot(self) -> list[dict]:
        """Return all vehicles as dicts for API consumption."""
        result = []
        for v in self._vehicles.values():
            result.append(self._vehicle_to_api(v))
        return result

    def get_analytics_timeseries(self) -> list[dict]:
        """Return analytics ring buffer for charts."""
        snapshots = self._analytics.get_all()
        return [{"t": s.t, "vehicles": s.vehicles} for s in snapshots]

    def get_risk_vehicles(self) -> list[dict]:
        """Return vehicles with elevated risk for the risk monitor."""
        high_risk = []
        for v in self._vehicles.values():
            if v.risk_score >= 0.25 or v.wrong_way:
                high_risk.append(self._vehicle_to_api(v))
        high_risk.sort(key=lambda x: -x["risk_score"])
        return high_risk

    def recommended_gap_seconds(self, behavior: str) -> float:
        return round(2.2 * BEHAVIOR_GAP_FACTORS.get(behavior, 1.0), 2)

    # ------------------------------------------------------------------
    # Wrong-way demo trigger
    # ------------------------------------------------------------------

    def trigger_wrong_way_demo(
        self,
        app: Flask,
        segment_id: int | None = None,
        vehicle_id: int | None = None,
        duration_seconds: float | None = None,
    ) -> dict:
        with app.app_context():
            self.refresh_network(app)
            if not self._segments:
                raise ValueError("No road network loaded. Ingest a street area first.")

            segment = self._select_demo_segment(segment_id)
            if segment is None:
                raise ValueError("No one-way road found in the current area.")

            now = time.time()

            # Ensure enough vehicles
            if len(self._vehicles) < 40:
                original_count = app.config["VEHICLE_COUNT"]
                app.config["VEHICLE_COUNT"] = 40
                self._ensure_vehicle_pool(now)
                app.config["VEHICLE_COUNT"] = original_count

            # Select target vehicle
            target_v = self._select_demo_vehicle(vehicle_id)
            if target_v is None:
                raise ValueError("No vehicle available for the demo scenario.")

            # Clear existing wrong-way flags
            for v in self._vehicles.values():
                v.wrong_way = False
                v.wrong_way_until = None
                v.state = "normal"
                v.reference = False

            demo_duration = duration_seconds or app.config["WRONG_WAY_DURATION_SECONDS"]

            # Set wrong-way vehicle
            target_v.segment_id = segment.id
            # Clear existing demo focus
            for v in self._vehicles.values():
                v.demo_focus = False

            target_v.direction = -1
            target_v.progress_m = max(segment.length_m * 0.85, min(segment.length_m, 12.0))
            target_v.wrong_way = True
            target_v.wrong_way_until = now + demo_duration
            target_v.state = "wrong_way"
            target_v.demo_focus = True
            target_v.confidence = 0.7
            target_v.speed_mps = max(
                self._compute_speed(segment, target_v.behavior) * WRONG_WAY_SPEED_BOOST, 5.0
            )
            lat, lon, bearing = self._state_from_segment(
                segment, target_v.progress_m, target_v.direction
            )
            target_v.lat, target_v.lon = lat, lon
            target_v.bearing = bearing
            target_v.timestamp = now
            target_v.dirty = True

            # Orchestrate collision: put another vehicle on same segment, opposite direction
            oncoming_v = None
            for v in self._vehicles.values():
                if v.db_id != target_v.db_id and not v.wrong_way:
                    oncoming_v = v
                    break

            if oncoming_v:
                oncoming_v.segment_id = segment.id
                oncoming_v.direction = 1
                oncoming_v.progress_m = 0.0
                oncoming_v.speed_mps = max(
                    self._compute_speed(segment, oncoming_v.behavior) * 0.9, 6.0
                )
                t_lat, t_lon, t_bearing = self._state_from_segment(segment, 0.0, 1)
                oncoming_v.lat, oncoming_v.lon = t_lat, t_lon
                oncoming_v.bearing = t_bearing
                oncoming_v.timestamp = now
                oncoming_v.dirty = True
                oncoming_v.reference = True
                self._reference_vehicle_id = oncoming_v.db_id

            # Flush to DB
            self._flush_to_db()

            return {
                "vehicle_id": target_v.db_id,
                "target_vehicle_id": oncoming_v.db_id if oncoming_v else None,
                "road_segment_id": segment.id,
                "duration_seconds": demo_duration,
                "wrong_way": True,
                "geometry": segment.geometry,
                "length_m": segment.length_m,
            }

    # ------------------------------------------------------------------
    # Prediction (for ML layer)
    # ------------------------------------------------------------------

    def predict_vehicle_path(
        self,
        vehicle_id: int,
        horizon_seconds: float = 14.0,
        step_seconds: float = 1.0,
    ) -> list[dict]:
        v = self._vehicles.get(vehicle_id)
        if v is None:
            return []

        segment = self._segments.get(v.segment_id)
        if segment is None:
            return []

        speed_mps = max(v.speed_mps, 0.1)
        progress_m = v.progress_m
        direction = 1 if v.direction >= 0 else -1
        wrong_way = v.wrong_way
        elapsed = 0.0
        points: list[dict] = []

        while elapsed <= horizon_seconds:
            current_segment = self._segments.get(segment.id)
            if current_segment is None:
                break

            lat, lon, bearing = self._state_from_segment(
                current_segment, progress_m, direction
            )
            points.append({
                "t": round(elapsed, 2),
                "lat": lat,
                "lon": lon,
                "bearing": round(bearing, 2),
                "speed": round(speed_mps, 2),
                "road_segment_id": current_segment.id,
            })

            elapsed += step_seconds
            distance_to_walk = speed_mps * step_seconds
            progress_m, segment, direction, wrong_way = self._project_along_network(
                current_segment, progress_m, direction, distance_to_walk, wrong_way
            )

        return points

    # ------------------------------------------------------------------
    # Main simulation loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        if self._app is None:
            return

        interval = self._app.config["SIMULATION_INTERVAL_SECONDS"]
        while not self._stop_event.wait(interval):
            with self._app.app_context():
                try:
                    self._tick()
                except Exception:
                    db.session.rollback()
                    self._app.logger.exception("Simulation tick failed")

    def _tick(self) -> None:
        if not self._segments:
            return

        now = time.time()
        self._tick_count += 1

        # Ensure pool is at target size
        self._ensure_vehicle_pool(now)

        if not self._vehicles:
            return

        # Build spatial index: vehicles grouped by segment for car-following
        by_segment: dict[int, list[VehicleLive]] = defaultdict(list)
        for v in self._vehicles.values():
            by_segment[v.segment_id].append(v)

        # Sort vehicles on each segment by progress for car-following
        for seg_id, vlist in by_segment.items():
            vlist.sort(key=lambda x: x.progress_m)

        # Compute segment densities
        segment_density: dict[int, float] = {}
        for seg_id, vlist in by_segment.items():
            seg = self._segments.get(seg_id)
            if seg and seg.length_m > 0:
                segment_density[seg_id] = len(vlist) / (seg.length_m / 100.0)
            else:
                segment_density[seg_id] = 0.0

        # Advance each vehicle
        for v in list(self._vehicles.values()):
            self._advance_vehicle(v, now, by_segment, segment_density)

        # Compute ML metrics for all vehicles
        self._compute_all_metrics(now, by_segment)

        # Record analytics snapshot
        snap_vehicles = []
        for v in self._vehicles.values():
            snap_vehicles.append({
                "id": v.db_id,
                "db_id": v.db_id,
                "wwp": round(v.wwp, 3),
                "ttc": round(v.ttc, 1) if v.ttc is not None else None,
                "risk": round(v.risk_score, 3),
                "anomaly": round(v.anomaly_score, 3),
                "wrong_way": v.wrong_way,
                "speed": round(v.speed_mps, 2),
                "bearing": round(v.heading_deg, 1),
                "angle_diff": round(v.angle_diff_deg, 1),
                "gps_quality": 1.0 if v.gps_stability == "HIGH" else 0.7 if v.gps_stability == "MEDIUM" else 0.4,
                "intent": getattr(v, "intent_classification", "UNKNOWN"),
                "ground_truth_wrong_way": v.wrong_way,
                "timestamp": now,
            })
        self._analytics.push(TickSnapshot(t=now, vehicles=snap_vehicles))

        # Log ground-truth-labeled data for evaluation pipeline
        eval_logger.log_frame(snap_vehicles)
        eval_dataset_logger.log_frame(snap_vehicles)

        # Flush to DB
        self._flush_to_db()

    # ------------------------------------------------------------------
    # Vehicle advancement (microscopic model)
    # ------------------------------------------------------------------

    def _advance_vehicle(
        self,
        v: VehicleLive,
        now: float,
        by_segment: dict[int, list[VehicleLive]],
        segment_density: dict[int, float],
    ) -> None:
        segment = self._segments.get(v.segment_id)
        if segment is None:
            self._reset_vehicle(v, now)
            return

        dt = max(
            min(now - v.timestamp, 2.0),
            self._app.config["SIMULATION_INTERVAL_SECONDS"],
        )
        dt = max(dt, 1e-3)

        # --- Compute target speed ---
        base_speed = self._compute_speed(segment, v.behavior)

        # 1. Speed jitter
        jitter = self._rng.uniform(-SPEED_JITTER_MPS, SPEED_JITTER_MPS)
        target_speed = max(base_speed + jitter, 2.0)

        # 2. Density-speed relation (fundamental diagram)
        density = segment_density.get(v.segment_id, 0.0)
        density_factor = max(0.3, 1.0 - density * 0.15)
        target_speed *= density_factor

        # 3. Intersection slowdown
        fraction = v.progress_m / max(segment.length_m, 0.1)
        if v.direction >= 0 and fraction > INTERSECTION_THRESHOLD:
            target_speed *= INTERSECTION_BRAKE
        elif v.direction < 0 and fraction < (1.0 - INTERSECTION_THRESHOLD):
            target_speed *= INTERSECTION_BRAKE

        # 4. Car-following: check distance to vehicle ahead on same segment
        segment_vehicles = by_segment.get(v.segment_id, [])
        if len(segment_vehicles) > 1:
            front_speed = self._car_following_speed(v, segment_vehicles)
            if front_speed is not None:
                target_speed = min(target_speed, front_speed)

        # 5. Wrong-way speed
        if v.wrong_way:
            target_speed = max(target_speed * 0.9, 4.0)
            # Check if wrong-way expired
            if v.wrong_way_until is not None and now > v.wrong_way_until:
                v.wrong_way = False
                v.wrong_way_until = None
                v.state = "normal"

        # Smooth speed transitions (inertia)
        v.speed_mps = v.speed_mps * 0.8 + target_speed * 0.2
        v.speed_mps = round(min(max(v.speed_mps, 2.0), 20.0), 2)

        # Lateral drift (lane behavior)
        v.lateral_offset += self._rng.uniform(-0.05, 0.05)
        v.lateral_offset = max(-1.2, min(1.2, v.lateral_offset))

        # --- Move along network ---
        remaining_distance = v.speed_mps * dt

        while remaining_distance > 0:
            current_segment = self._segments.get(v.segment_id)
            if current_segment is None:
                self._reset_vehicle(v, now)
                return

            if v.direction >= 0:
                remaining_on_segment = current_segment.length_m - v.progress_m
                step = min(remaining_distance, remaining_on_segment)
                v.progress_m += step
                remaining_distance -= step
                reached_boundary = v.progress_m >= current_segment.length_m - 1e-6
                boundary_node = current_segment.end_node_id
            else:
                remaining_on_segment = v.progress_m
                step = min(remaining_distance, remaining_on_segment)
                v.progress_m -= step
                remaining_distance -= step
                reached_boundary = v.progress_m <= 1e-6
                boundary_node = current_segment.start_node_id

            if not reached_boundary:
                break

            if not self._transition_vehicle(v, boundary_node, now):
                self._reset_vehicle(v, now)
                break

        # Update position
        segment = self._segments.get(v.segment_id)
        if segment is None:
            return

        lat, lon, bearing = self._state_from_segment(
            segment, v.progress_m, v.direction
        )
        noise_m = self._app.config["GPS_NOISE_METERS"]
        v.lat, v.lon = add_noise(lat, lon, noise_m, rng=self._rng)
        v.bearing = bearing
        v.timestamp = now
        v.dirty = True

    def _car_following_speed(
        self,
        v: VehicleLive,
        segment_vehicles: list[VehicleLive],
    ) -> float | None:
        """Find the vehicle immediately ahead and apply car-following constraint."""
        best_gap = float("inf")
        front_speed = None

        for other in segment_vehicles:
            if other.db_id == v.db_id:
                continue
            # Only consider vehicles moving in the same direction
            if other.direction != v.direction:
                continue

            if v.direction >= 0:
                gap = other.progress_m - v.progress_m
            else:
                gap = v.progress_m - other.progress_m

            if 0 < gap < best_gap:
                best_gap = gap
                front_speed = other.speed_mps

        if best_gap < SAFE_DISTANCE_M and front_speed is not None:
            return min(front_speed, v.speed_mps)

        return None

    # ------------------------------------------------------------------
    # ML metric computation
    # ------------------------------------------------------------------

    def _compute_all_metrics(
        self,
        now: float,
        by_segment: dict[int, list[VehicleLive]],
    ) -> None:
        vehicle_list = list(self._vehicles.values())

        # Precompute pairwise distances for nearby/TTC
        for v in vehicle_list:
            v.nearby_count = 0
            v.closest_distance_m = None
            v.ttc = None

        # Spatial metrics
        for i, v1 in enumerate(vehicle_list):
            for v2 in vehicle_list[i + 1:]:
                dist = haversine_distance_m(v1.lat, v1.lon, v2.lat, v2.lon)

                # Nearby count (within 50m)
                if dist < 50.0:
                    v1.nearby_count += 1
                    v2.nearby_count += 1

                # Closest distance
                if v1.closest_distance_m is None or dist < v1.closest_distance_m:
                    v1.closest_distance_m = dist
                if v2.closest_distance_m is None or dist < v2.closest_distance_m:
                    v2.closest_distance_m = dist

                # TTC: same segment, opposite directions, approaching
                if v1.segment_id == v2.segment_id and v1.direction != v2.direction:
                    closing_speed = v1.speed_mps + v2.speed_mps
                    if closing_speed > 0.5:
                        gap = abs(v1.progress_m - v2.progress_m)
                        ttc_val = gap / closing_speed
                        if ttc_val < 30.0:
                            if v1.ttc is None or ttc_val < v1.ttc:
                                v1.ttc = ttc_val
                            if v2.ttc is None or ttc_val < v2.ttc:
                                v2.ttc = ttc_val

        # Per-vehicle ML scores
        for v in vehicle_list:
            segment = self._segments.get(v.segment_id)
            if segment is None:
                continue

            self._update_heading_and_confidence(v, segment, now)

            # --- Anomaly score ---
            speed_dev = 0.0
            if segment:
                expected_speed = segment.speed_limit_mps * BEHAVIOR_SPEED_FACTORS.get(v.behavior, 0.88)
                if expected_speed > 0:
                    speed_dev = abs(v.speed_mps - expected_speed) / expected_speed

            wrong_way_factor = 1.0 if v.wrong_way else 0.0
            heading_instability = 0.0
            if len(v._heading_window) > 1:
                heading_var = statistics.pvariance(list(v._heading_window))
                heading_instability = min(1.0, heading_var / 1800.0)
            neighborhood_pressure = min(v.nearby_count / 6.0, 1.0)
            v.anomaly_score = round(
                min(
                    1.0,
                    0.25 * speed_dev
                    + 0.35 * wrong_way_factor
                    + 0.20 * v.confidence
                    + 0.12 * heading_instability
                    + 0.08 * neighborhood_pressure,
                ),
                3,
            )

            # --- WWP derived from confidence (probabilistic) ---
            v.wwp = round(min(max(v.confidence, 0.0), 1.0), 3)

            # --- Risk score ---
            ttc_factor = 0.0
            if v.ttc is not None and v.ttc < 15.0:
                ttc_factor = max(0.0, 1.0 - v.ttc / 15.0)

            proximity_factor = 0.0
            if v.closest_distance_m is not None and v.closest_distance_m < 30.0:
                proximity_factor = max(0.0, 1.0 - v.closest_distance_m / 30.0)

            v.risk_score = round(
                min(1.0,
                    0.30 * v.anomaly_score
                    + 0.35 * ttc_factor
                    + 0.15 * proximity_factor
                    + 0.20 * wrong_way_factor),
                3,
            )

            # --- Maneuverability ---
            v.maneuverability = round(
                max(0.0, 1.0 - min(v.nearby_count / 6.0, 0.6) - proximity_factor * 0.3),
                3,
            )

            # --- State ---
            if v.edge_case in {"ROUNDABOUT", "GPS_GAP"}:
                v.state = "normal"
            elif v.wrong_way:
                v.state = "wrong_way"
            elif v.confidence >= 0.75 and v.angle_diff_deg >= 150.0 and (segment.oneway or v.wrong_way):
                v.state = "wrong_way"
            elif v.confidence >= 0.55 or v.angle_diff_deg > 100.0:
                v.state = "suspicious"
            else:
                v.state = "normal"

    # ------------------------------------------------------------------
    # Vehicle pool management
    # ------------------------------------------------------------------

    def _sync_from_db(self) -> None:
        """Load existing vehicles from DB into in-memory state."""
        self._vehicles.clear()
        vehicles = Vehicle.query.order_by(Vehicle.id).all()
        for v in vehicles:
            self._vehicles[v.id] = VehicleLive(
                db_id=v.id,
                segment_id=v.road_segment_id,
                progress_m=v.progress_m,
                direction=v.direction,
                speed_mps=v.speed_mps,
                behavior=v.behavior,
                wrong_way=v.wrong_way,
                wrong_way_until=v.wrong_way_until,
                lat=v.lat,
                lon=v.lon,
                bearing=v.bearing,
                timestamp=v.timestamp,
                confidence=v.wwp,
                heading_deg=v.bearing,
                heading_smooth_deg=v.bearing,
                road_bearing_deg=0.0,
                angle_diff_deg=0.0,
                gps_stability="MEDIUM",
                edge_case="NONE",
                reference=(v.id == self._reference_vehicle_id),
            )
        if self._reference_vehicle_id is None and self._vehicles:
            self._reference_vehicle_id = next(iter(self._vehicles.keys()))
        for live in self._vehicles.values():
            live.reference = (live.db_id == self._reference_vehicle_id)

    def _ensure_vehicle_pool(self, now: float) -> None:
        if self._app is None or not self._segments:
            return

        target_count = self._app.config["VEHICLE_COUNT"]
        current_count = len(self._vehicles)
        
        # If we have too many, remove some
        if current_count > target_count:
            to_remove = current_count - target_count
            # Get list of candidates (exclude reference vehicle)
            candidates = [vid for vid in self._vehicles.keys() if vid != self._reference_vehicle_id]
            for i in range(min(to_remove, len(candidates))):
                vid = candidates[i]
                v_live = self._vehicles.pop(vid, None)
                if v_live:
                    db_v = db.session.get(Vehicle, v_live.db_id)
                    if db_v:
                        db.session.delete(db_v)
            db.session.commit()
            return

        missing = max(target_count - len(self._vehicles), 0)
        if self._reference_vehicle_id is None and self._vehicles:
            self._reference_vehicle_id = next(iter(self._vehicles.keys()))
        for vehicle in self._vehicles.values():
            vehicle.reference = (vehicle.db_id == self._reference_vehicle_id)
        if missing == 0:
            return

        segment_pool = list(self._segments.values())
        behavior_weights = [0.25, 0.55, 0.20]
        behaviors = list(BEHAVIOR_SPEED_FACTORS.keys())
        new_db_vehicles: list[Vehicle] = []

        for _ in range(missing):
            segment = self._rng.choice(segment_pool)
            behavior = self._rng.choices(behaviors, weights=behavior_weights, k=1)[0]
            direction = 1
            if not segment.oneway and self._rng.random() < 0.30:
                direction = -1
            
            # Very rare natural wrong way spawn (0.1%)
            natural_ww = False
            if segment.oneway and self._rng.random() < 0.001:
                direction = -1
                natural_ww = True
            progress = self._rng.uniform(0.0, segment.length_m)
            speed_mps = self._compute_speed(segment, behavior)
            lat, lon, bearing = self._state_from_segment(segment, progress, direction)
            noise_m = self._app.config["GPS_NOISE_METERS"]
            lat, lon = add_noise(lat, lon, noise_m, rng=self._rng)

            db_v = Vehicle(
                road_segment_id=segment.id,
                lat=lat,
                lon=lon,
                speed_mps=speed_mps,
                bearing=bearing,
                timestamp=now,
                direction=direction,
                progress_m=progress,
                wrong_way=natural_ww,
                behavior=behavior,
                state="normal",
                anomaly_score=0.0,
                risk_score=0.0,
                wwp=0.0,
                ttc=None,
                maneuverability=1.0,
                nearby_count=0,
                closest_distance_m=None,
            )
            new_db_vehicles.append(db_v)

        if new_db_vehicles:
            db.session.add_all(new_db_vehicles)
            db.session.commit()

            # Add to in-memory pool
            for db_v in new_db_vehicles:
                self._vehicles[db_v.id] = VehicleLive(
                    db_id=db_v.id,
                    segment_id=db_v.road_segment_id,
                    progress_m=db_v.progress_m,
                    direction=db_v.direction,
                    speed_mps=db_v.speed_mps,
                    behavior=db_v.behavior,
                    wrong_way=db_v.wrong_way,
                    wrong_way_until=None,
                    lat=db_v.lat,
                    lon=db_v.lon,
                    bearing=db_v.bearing,
                    timestamp=now,
                    confidence=0.0,
                    heading_deg=db_v.bearing,
                    heading_smooth_deg=db_v.bearing,
                    road_bearing_deg=0.0,
                    angle_diff_deg=0.0,
                    gps_stability="MEDIUM",
                    edge_case="NONE",
                    reference=(db_v.id == self._reference_vehicle_id),
                )

        if self._reference_vehicle_id is None and self._vehicles:
            self._reference_vehicle_id = next(iter(self._vehicles.keys()))
        for vehicle in self._vehicles.values():
            vehicle.reference = (vehicle.db_id == self._reference_vehicle_id)

    def _flush_to_db(self) -> None:
        """Bulk-write in-memory vehicle state to DB."""
        history_rows: list[VehicleHistory] = []

        for v in self._vehicles.values():
            db_v = db.session.get(Vehicle, v.db_id)
            if db_v is None:
                continue

            db_v.road_segment_id = v.segment_id
            db_v.lat = v.lat
            db_v.lon = v.lon
            db_v.speed_mps = v.speed_mps
            db_v.bearing = v.bearing
            db_v.timestamp = v.timestamp
            db_v.direction = v.direction
            db_v.progress_m = v.progress_m
            db_v.wrong_way = v.wrong_way
            db_v.wrong_way_until = v.wrong_way_until
            db_v.state = v.state
            db_v.anomaly_score = v.anomaly_score
            db_v.risk_score = v.risk_score
            db_v.wwp = v.wwp
            db_v.ttc = v.ttc
            db_v.maneuverability = v.maneuverability
            db_v.nearby_count = v.nearby_count
            db_v.closest_distance_m = v.closest_distance_m

            history_rows.append(
                VehicleHistory(
                    vehicle_id=v.db_id,
                    road_segment_id=v.segment_id,
                    lat=v.lat,
                    lon=v.lon,
                    speed_mps=v.speed_mps,
                    bearing=v.bearing,
                    timestamp=v.timestamp,
                )
            )
            v.dirty = False

        db.session.add_all(history_rows)
        db.session.commit()
        self._seq += 1

    # ------------------------------------------------------------------
    # Transitions & helpers
    # ------------------------------------------------------------------

    def _transition_vehicle(self, v: VehicleLive, node_id: int, now: float) -> bool:
        prefer_wrong_way = bool(
            v.wrong_way and v.wrong_way_until is not None and v.wrong_way_until > now
        )
        next_option = self._choose_next_option(
            node_id,
            current_segment_id=v.segment_id,
            prefer_wrong_way=prefer_wrong_way,
        )

        if next_option is None and v.wrong_way:
            v.wrong_way = False
            v.wrong_way_until = None
            v.state = "normal"
            next_option = self._choose_next_option(
                node_id,
                current_segment_id=v.segment_id,
                prefer_wrong_way=False,
            )

        if next_option is None:
            return False

        next_segment = self._segments[next_option.segment_id]
        v.segment_id = next_segment.id
        v.direction = next_option.direction
        v.progress_m = 0.0 if next_option.direction >= 0 else next_segment.length_m
        v.wrong_way = next_option.wrong_way
        if not next_option.wrong_way:
            v.wrong_way_until = None
            v.state = "normal"
        return True

    def _choose_next_option(
        self,
        node_id: int,
        current_segment_id: int,
        prefer_wrong_way: bool,
    ) -> RouteOption | None:
        candidate_groups = []
        if prefer_wrong_way:
            candidate_groups.append(self._wrong_way_options.get(node_id, []))
        candidate_groups.append(self._normal_options.get(node_id, []))

        for candidates in candidate_groups:
            filtered = [o for o in candidates if o.segment_id != current_segment_id]
            pool = filtered or candidates
            if pool:
                return self._rng.choice(pool)
        return None

    def _project_along_network(
        self,
        segment: SegmentRuntime,
        progress_m: float,
        direction: int,
        distance_m: float,
        wrong_way: bool,
    ) -> tuple[float, SegmentRuntime, int, bool]:
        current_segment = segment
        current_progress = progress_m
        current_direction = direction
        current_wrong_way = wrong_way
        remaining = max(distance_m, 0.0)

        while remaining > 0:
            if current_direction >= 0:
                available = current_segment.length_m - current_progress
                step = min(remaining, available)
                current_progress += step
                boundary_node = current_segment.end_node_id
            else:
                available = current_progress
                step = min(remaining, available)
                current_progress -= step
                boundary_node = current_segment.start_node_id

            remaining -= step
            reached_boundary = available <= step + 1e-6
            if not reached_boundary:
                break

            next_option = self._choose_next_option(
                boundary_node,
                current_segment.id,
                current_wrong_way,
            )
            if next_option is None:
                current_progress = (
                    current_segment.length_m if current_direction >= 0 else 0.0
                )
                break

            current_segment = self._segments[next_option.segment_id]
            current_direction = next_option.direction
            current_wrong_way = next_option.wrong_way
            current_progress = (
                0.0 if current_direction >= 0 else current_segment.length_m
            )

        return current_progress, current_segment, current_direction, current_wrong_way

    def _reset_vehicle(self, v: VehicleLive, now: float) -> None:
        segment = self._rng.choice(list(self._segments.values()))
        v.segment_id = segment.id
        v.direction = 1 if segment.oneway or self._rng.random() < 0.5 else -1
        v.progress_m = self._rng.uniform(0.0, segment.length_m)
        v.wrong_way = False
        v.wrong_way_until = None
        v.state = "normal"
        v.speed_mps = self._compute_speed(segment, v.behavior)
        lat, lon, bearing = self._state_from_segment(segment, v.progress_m, v.direction)
        noise_m = self._app.config["GPS_NOISE_METERS"]
        v.lat, v.lon = add_noise(lat, lon, noise_m, rng=self._rng)
        v.bearing = bearing
        v.timestamp = now
        v.dirty = True
        v.reference = (v.db_id == self._reference_vehicle_id)

    def _select_demo_segment(self, segment_id: int | None) -> SegmentRuntime | None:
        if segment_id is not None:
            return self._segments.get(segment_id)
        oneway_segments = [s for s in self._segments.values() if s.oneway]
        if not oneway_segments:
            return None
        return max(oneway_segments, key=lambda s: s.length_m)

    def _select_demo_vehicle(self, vehicle_id: int | None) -> VehicleLive | None:
        if vehicle_id is not None:
            return self._vehicles.get(vehicle_id)
        for v in self._vehicles.values():
            if not v.wrong_way:
                return v
        return next(iter(self._vehicles.values()), None)

    def _state_from_segment(
        self,
        segment: SegmentRuntime,
        progress_m: float,
        direction: int,
    ) -> tuple[float, float, float]:
        distance = min(max(progress_m, 0.0), segment.length_m)
        lat, lon = interpolate_path_position(
            segment.geometry, segment.cumulative_lengths, distance
        )
        bearing = path_bearing_at(
            segment.geometry, segment.cumulative_lengths, distance, direction
        )
        return lat, lon, bearing

    def _compute_speed(self, segment: SegmentRuntime, behavior: str) -> float:
        behavior_factor = BEHAVIOR_SPEED_FACTORS.get(behavior, 1.2)
        # Higher base speed floor for faster feel
        base_speed = max(segment.speed_limit_mps * behavior_factor, 12.0)
        varied = base_speed * self._rng.uniform(0.95, 1.1)
        return round(min(varied, 35.0), 2)

    def _vehicle_to_api(self, v: VehicleLive) -> dict:
        seg = self._segments.get(v.segment_id)
        return {
            "id": v.db_id,
            "lat": v.lat,
            "lon": v.lon,
            "speed": v.speed_mps,
            "bearing": v.bearing,
            "timestamp": v.timestamp,
            "road_segment_id": v.segment_id,
            "wrong_way": v.wrong_way,
            "behavior": v.behavior,
            "state": v.state,
            "anomaly_score": round(v.anomaly_score, 3),
            "risk_score": round(v.risk_score, 3),
            "wwp": round(v.wwp, 3),
            "ttc": round(v.ttc, 1) if v.ttc is not None else None,
            "maneuverability": round(v.maneuverability, 3),
            "nearby_count": v.nearby_count,
            "closest_distance_m": (
                round(v.closest_distance_m, 1)
                if v.closest_distance_m is not None
                else None
            ),
            "road_class": seg.road_class if seg else None,
            "confidence": round(v.confidence, 3),
            "heading": round(v.heading_smooth_deg, 2),
            "road_bearing": round(v.road_bearing_deg, 2),
            "angle_diff": round(v.angle_diff_deg, 2),
            "gps_stability": v.gps_stability,
            "edge_case": v.edge_case,
            "reference": bool(v.reference),
            "demo_focus": v.demo_focus,
            "kinematics": {
                "braking_distance": round((v.speed_mps**2) / (2 * 0.7 * 9.81), 2),
                "heading_drift": self._compute_heading_drift(v),
                "lateral_offset": round(v.lateral_offset, 2)
            }
        }

    def _circular_mean_deg(self, values: list[float]) -> float:
        if not values:
            return 0.0
        sin_sum = sum(math.sin(math.radians(v)) for v in values)
        cos_sum = sum(math.cos(math.radians(v)) for v in values)
        angle = math.degrees(math.atan2(sin_sum, cos_sum))
        return (angle + 360.0) % 360.0

    def _compute_heading_drift(self, v: VehicleLive) -> float:
        window = list(v._heading_window)
        if len(window) < 2:
            return 0.0
        c_mean = self._circular_mean_deg(window)
        deviations = [min(abs(h - c_mean), 360.0 - abs(h - c_mean)) for h in window]
        return round(statistics.fmean(deviations), 2)

    def _update_heading_and_confidence(self, v: VehicleLive, segment: SegmentRuntime, now: float) -> None:
        if v._last_lat is None or v._last_lon is None:
            v._last_lat, v._last_lon = v.lat, v.lon
            v.heading_deg = v.bearing
        else:
            dx = v.lon - v._last_lon
            dy = v.lat - v._last_lat
            if abs(dx) + abs(dy) > 1e-10:
                heading = math.degrees(math.atan2(dy, dx))
                v.heading_deg = (heading + 360.0) % 360.0
            v._last_lat, v._last_lon = v.lat, v.lon

        v._heading_window.append(v.heading_deg)
        v.heading_smooth_deg = self._circular_mean_deg(list(v._heading_window)[-3:])

        # ML Feature: angle_diff_deg should be relative to the BASE legal segment bearing
        # to ensure wrong-way vehicles maintain a distinct ~180 degree deviation signature.
        v.road_bearing_deg = segment.bearing
        angle_diff = abs(v.heading_smooth_deg - segment.bearing)
        v.angle_diff_deg = min(angle_diff, 360.0 - angle_diff)

        # BUG FIX: If the road is TWO-WAY, going in the opposite direction (B+180) is also legal.
        if not segment.oneway:
            opp_bearing = (segment.bearing + 180.0) % 360.0
            opp_diff = abs(v.heading_smooth_deg - opp_bearing)
            opp_diff_deg = min(opp_diff, 360.0 - opp_diff)
            v.angle_diff_deg = min(v.angle_diff_deg, opp_diff_deg)

        # Edge-case handling
        edge_case = "NONE"
        if segment.road_class and "roundabout" in segment.road_class:
            edge_case = "ROUNDABOUT"
        elif 60.0 < v.angle_diff_deg < 120.0:
            edge_case = "INTERSECTION_TURN"
        elif segment.road_class in {"motorway", "trunk"}:
            edge_case = "DIVIDED_HIGHWAY"

        dt = now - v.timestamp
        if dt > (self._app.config["SIMULATION_INTERVAL_SECONDS"] * 2.5):
            edge_case = "GPS_GAP"
        v.edge_case = edge_case

        # Duration score (how long suspicious angle is sustained)
        if v.angle_diff_deg > 100.0:
            if v._suspect_since is None:
                v._suspect_since = now
        else:
            v._suspect_since = None
        duration = (now - v._suspect_since) if v._suspect_since is not None else 0.0

        # Stability score from heading variance
        if len(v._heading_window) > 1:
            heading_var = float(statistics.pvariance(list(v._heading_window)))
        else:
            heading_var = 0.0
        stability_score = max(0.0, min(1.0, 1.0 - (heading_var / 2500.0)))
        v.gps_stability = "HIGH" if stability_score > 0.75 else "MEDIUM" if stability_score > 0.45 else "LOW"

        dir_score     = max(0.0, min(1.0, v.angle_diff_deg / 180.0))
        duration_score = max(0.0, min(1.0, duration / 5.0))

        # direction_similarity = cos(angle_diff) ∈ [-1, 1]
        direction_similarity = math.cos(math.radians(min(v.angle_diff_deg, 180.0)))

        # Normalised heading variance ∈ [0, 1] (used as temporal_variance suppressor)
        variance_norm = max(0.0, min(1.0, heading_var / 2500.0))

        # Edge-case attenuation: scale dev_time and speed inputs so sigmoid
        # naturally reduces confidence without any ad-hoc multipliers.
        effective_duration = duration
        effective_speed    = v.speed_mps
        if edge_case == "INTERSECTION_TURN":
            effective_duration *= 0.4         # short duration counts less at intersections
            effective_speed    *= 0.4
        elif edge_case in {"ROUNDABOUT", "GPS_GAP"}:
            effective_duration  = 0.0         # treat as no evidence
            effective_speed     = 0.0
        elif edge_case == "DIVIDED_HIGHWAY":
            effective_duration *= 0.85

        # Two-way roads: require stronger sustained evidence
        if not segment.oneway:
            effective_duration *= 0.55

        # Compute smooth sigmoid confidence
        confidence = sigmoid_confidence(
            direction_similarity=direction_similarity,
            dev_time=effective_duration,
            speed=effective_speed,
            temporal_variance=variance_norm,
        )

        # Wrong-way override: ramp confidence up using elapsed time as dev_time input
        if v.wrong_way:
            total = float(self._app.config.get("WRONG_WAY_DURATION_SECONDS", 30))
            if v.wrong_way_until is not None:
                elapsed = max(0.0, total - max(v.wrong_way_until - now, 0.0))
            else:
                elapsed = total
            boosted = sigmoid_confidence(
                direction_similarity=-1.0,       # maximum opposition
                dev_time=elapsed,
                speed=v.speed_mps,
                temporal_variance=variance_norm,
            )
            confidence = max(confidence, boosted)

        v.confidence = max(0.0, min(1.0, confidence))



simulation_engine = VehicleSimulationEngine()
