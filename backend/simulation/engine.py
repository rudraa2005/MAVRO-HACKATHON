from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from math import fabs
from typing import NamedTuple

from flask import Flask

from backend.extensions import db
from backend.models import RoadSegment, Vehicle, VehicleHistory
from backend.services.geo import (
    add_noise,
    cumulative_path_lengths,
    interpolate_path_position,
    path_bearing_at,
)


BEHAVIOR_SPEED_FACTORS = {
    "calm": 0.72,
    "normal": 0.88,
    "aggressive": 1.0,
}

BEHAVIOR_GAP_FACTORS = {
    "calm": 1.28,
    "normal": 1.0,
    "aggressive": 0.72,
}


class RouteOption(NamedTuple):
    segment_id: int
    direction: int
    wrong_way: bool


@dataclass(slots=True)
class SegmentRuntime:
    id: int
    start_node_id: int
    end_node_id: int
    oneway: bool
    length_m: float
    geometry: list[dict[str, float]]
    cumulative_lengths: list[float]
    speed_limit_mps: float


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

    def start(self, app: Flask, force: bool = False) -> None:
        with self._lock:
            self._app = app
            self._rng = random.Random(app.config["SIMULATION_RANDOM_SEED"])
            self.refresh_network(app)

            with app.app_context():
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
                    self._wrong_way_options.setdefault(runtime.end_node_id, []).append(
                        RouteOption(runtime.id, -1, True)
                    )

    def reseed_demo_fleet(self, app: Flask) -> None:
        with app.app_context():
            now = time.time()
            VehicleHistory.query.delete()
            Vehicle.query.delete()
            db.session.commit()
            self.refresh_network(app)
            self._ensure_vehicle_pool(now, vehicles=[])

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
        vehicles = Vehicle.query.order_by(Vehicle.id).all()
        self._ensure_vehicle_pool(now, vehicles=vehicles)
        vehicles = Vehicle.query.order_by(Vehicle.id).all()
        if not vehicles:
            return

        self._top_up_wrong_way_vehicles(vehicles, now)
        history_rows: list[VehicleHistory] = []

        for vehicle in vehicles:
            self._advance_vehicle(vehicle, now)
            history_rows.append(
                VehicleHistory(
                    vehicle_id=vehicle.id,
                    road_segment_id=vehicle.road_segment_id,
                    lat=vehicle.lat,
                    lon=vehicle.lon,
                    speed_mps=vehicle.speed_mps,
                    bearing=vehicle.bearing,
                    timestamp=vehicle.timestamp,
                )
            )

        db.session.add_all(history_rows)
        db.session.commit()

    def _ensure_vehicle_pool(
        self,
        now: float,
        vehicles: list[Vehicle] | None = None,
    ) -> None:
        if self._app is None or not self._segments:
            return

        target_count = self._app.config["VEHICLE_COUNT"]
        current_vehicles = vehicles or Vehicle.query.order_by(Vehicle.id).all()
        missing = max(target_count - len(current_vehicles), 0)
        if missing == 0:
            return

        new_vehicles: list[Vehicle] = []
        segment_pool = list(self._segments.values())
        behavior_weights = [0.25, 0.55, 0.20]
        behaviors = list(BEHAVIOR_SPEED_FACTORS.keys())

        for _ in range(missing):
            segment = self._rng.choice(segment_pool)
            behavior = self._rng.choices(behaviors, weights=behavior_weights, k=1)[0]
            direction = 1
            if not segment.oneway and self._rng.random() < 0.35:
                direction = -1
            progress = self._rng.uniform(0.0, segment.length_m)
            speed_mps = self._compute_speed(segment, behavior)
            lat, lon, bearing = self._state_from_segment(segment, progress, direction)
            lat, lon = add_noise(
                lat, lon, self._app.config["GPS_NOISE_METERS"], rng=self._rng
            )
            new_vehicles.append(
                Vehicle(
                    road_segment_id=segment.id,
                    lat=lat,
                    lon=lon,
                    speed_mps=speed_mps,
                    bearing=bearing,
                    timestamp=now,
                    direction=direction,
                    progress_m=progress,
                    wrong_way=False,
                    behavior=behavior,
                )
            )

        db.session.add_all(new_vehicles)
        db.session.commit()

    def _top_up_wrong_way_vehicles(self, vehicles: list[Vehicle], now: float) -> None:
        if self._app is None:
            return

        target = min(self._app.config["WRONG_WAY_COUNT"], len(vehicles))
        active = [vehicle for vehicle in vehicles if vehicle.wrong_way]
        missing = max(target - len(active), 0)
        if missing == 0:
            return

        eligible = [vehicle for vehicle in vehicles if not vehicle.wrong_way]
        self._rng.shuffle(eligible)
        for vehicle in eligible[:missing]:
            self._activate_wrong_way(vehicle, now)

    def _activate_wrong_way(self, vehicle: Vehicle, now: float) -> None:
        oneway_segments = [segment for segment in self._segments.values() if segment.oneway]
        if not oneway_segments or self._app is None:
            return

        segment = self._rng.choice(oneway_segments)
        vehicle.road_segment_id = segment.id
        vehicle.direction = -1
        vehicle.progress_m = max(segment.length_m - 1.0, segment.length_m * 0.75)
        vehicle.wrong_way = True
        vehicle.wrong_way_until = now + self._app.config["WRONG_WAY_DURATION_SECONDS"]
        vehicle.speed_mps = max(self._compute_speed(segment, vehicle.behavior) * 0.85, 4.5)

        lat, lon, bearing = self._state_from_segment(
            segment, vehicle.progress_m, vehicle.direction
        )
        vehicle.lat, vehicle.lon = add_noise(
            lat, lon, self._app.config["GPS_NOISE_METERS"], rng=self._rng
        )
        vehicle.bearing = bearing
        vehicle.timestamp = now

    def trigger_wrong_way_demo(
        self,
        app: Flask,
        segment_id: int | None = None,
        vehicle_id: int | None = None,
        duration_seconds: float | None = None,
    ) -> dict[str, int | float | bool | list[dict[str, float]]]:
        with app.app_context():
            self.refresh_network(app)
            if not self._segments:
                raise ValueError("No road network loaded. Ingest a street area first.")

            segment = self._select_demo_segment(segment_id)
            if segment is None:
                raise ValueError("No one-way road found in the current area.")

            now = time.time()
            vehicles = Vehicle.query.order_by(Vehicle.id).all()

            # BOOST DENSITY: Ensure at least 40 vehicles for a "busy" scenario
            if len(vehicles) < 40:
                original_count = app.config["VEHICLE_COUNT"]
                app.config["VEHICLE_COUNT"] = 40
                self._ensure_vehicle_pool(now, vehicles=vehicles)
                app.config["VEHICLE_COUNT"] = original_count
                vehicles = Vehicle.query.order_by(Vehicle.id).all()

            vehicle = self._select_demo_vehicle(vehicles, vehicle_id)
            if vehicle is None:
                raise ValueError("No vehicle available for the demo scenario.")

            for candidate in vehicles:
                candidate.wrong_way = False
                candidate.wrong_way_until = None

            demo_duration = duration_seconds or app.config["WRONG_WAY_DURATION_SECONDS"]
            vehicle.road_segment_id = segment.id
            vehicle.direction = -1
            vehicle.progress_m = max(segment.length_m * 0.85, min(segment.length_m, 12.0))
            vehicle.wrong_way = True
            vehicle.wrong_way_until = now + demo_duration
            vehicle.speed_mps = max(self._compute_speed(segment, vehicle.behavior) * 0.8, 5.0)

            # ORCHESTRATE COLLISION: Find another vehicle and put it on the same segment moving forward
            target_vehicle = next((v for v in vehicles if v.id != vehicle.id and not v.wrong_way), None)
            if target_vehicle:
                target_vehicle.road_segment_id = segment.id
                target_vehicle.direction = 1
                target_vehicle.progress_m = 0.0 # Start at the beginning of segment
                target_vehicle.speed_mps = max(self._compute_speed(segment, target_vehicle.behavior) * 0.9, 6.0)

                t_lat, t_lon, t_bearing = self._state_from_segment(segment, target_vehicle.progress_m, 1)
                target_vehicle.lat, target_vehicle.lon = add_noise(t_lat, t_lon, 2.0, rng=self._rng)
                target_vehicle.bearing = t_bearing
                target_vehicle.timestamp = now

            lat, lon, bearing = self._state_from_segment(
                segment,
                vehicle.progress_m,
                vehicle.direction,
            )
            vehicle.lat, vehicle.lon = add_noise(
                lat, lon, app.config["GPS_NOISE_METERS"], rng=self._rng
            )
            vehicle.bearing = bearing
            vehicle.timestamp = now

            # Ensure both are in history
            h_rows = [
                VehicleHistory(
                    vehicle_id=vehicle.id,
                    road_segment_id=vehicle.road_segment_id,
                    lat=vehicle.lat,
                    lon=vehicle.lon,
                    speed_mps=vehicle.speed_mps,
                    bearing=vehicle.bearing,
                    timestamp=vehicle.timestamp,
                )
            ]
            if target_vehicle:
                h_rows.append(
                    VehicleHistory(
                        vehicle_id=target_vehicle.id,
                        road_segment_id=target_vehicle.road_segment_id,
                        lat=target_vehicle.lat,
                        lon=target_vehicle.lon,
                        speed_mps=target_vehicle.speed_mps,
                        bearing=target_vehicle.bearing,
                        timestamp=target_vehicle.timestamp,
                    )
                )

            db.session.add_all(h_rows)
            db.session.commit()

            return {
                "vehicle_id": vehicle.id,
                "target_vehicle_id": target_vehicle.id if target_vehicle else None,
                "road_segment_id": segment.id,
                "duration_seconds": demo_duration,
                "wrong_way": True,
                "geometry": segment.geometry,
                "length_m": segment.length_m,
            }

    def predict_vehicle_path(
        self,
        vehicle: Vehicle,
        horizon_seconds: float = 14.0,
        step_seconds: float = 1.0,
    ) -> list[dict[str, float | int]]:
        segment = self._segments.get(vehicle.road_segment_id)
        if segment is None:
            return []

        speed_mps = max(float(vehicle.speed_mps or 0.0), 0.1)
        progress_m = float(vehicle.progress_m or 0.0)
        direction = 1 if vehicle.direction >= 0 else -1
        wrong_way = bool(vehicle.wrong_way)
        elapsed = 0.0
        points: list[dict[str, float | int]] = []

        while elapsed <= horizon_seconds:
            current_segment = self._segments.get(segment.id)
            if current_segment is None:
                break

            lat, lon, bearing = self._state_from_segment(
                current_segment,
                progress_m,
                direction,
            )
            points.append(
                {
                    "t": round(elapsed, 2),
                    "lat": lat,
                    "lon": lon,
                    "bearing": round(bearing, 2),
                    "speed": round(speed_mps, 2),
                    "road_segment_id": current_segment.id,
                }
            )

            elapsed += step_seconds
            distance_to_walk = speed_mps * step_seconds
            progress_m, segment, direction, wrong_way = self._project_along_network(
                current_segment,
                progress_m,
                direction,
                distance_to_walk,
                wrong_way,
            )

        return points

    def recommended_gap_seconds(self, behavior: str) -> float:
        return round(2.2 * BEHAVIOR_GAP_FACTORS.get(behavior, 1.0), 2)

    def _advance_vehicle(self, vehicle: Vehicle, now: float) -> None:
        segment = self._segments.get(vehicle.road_segment_id)
        if segment is None:
            self._reset_vehicle(vehicle, now)
            return

        dt = max(min(now - vehicle.timestamp, 2.0), self._app.config["SIMULATION_INTERVAL_SECONDS"])
        vehicle.speed_mps = self._compute_speed(segment, vehicle.behavior)
        if vehicle.wrong_way:
            vehicle.speed_mps = max(vehicle.speed_mps * 0.9, 4.0)

        remaining_distance = vehicle.speed_mps * dt

        while remaining_distance > 0:
            current_segment = self._segments[vehicle.road_segment_id]
            if vehicle.direction >= 0:
                remaining_on_segment = current_segment.length_m - vehicle.progress_m
                step = min(remaining_distance, remaining_on_segment)
                vehicle.progress_m += step
                remaining_distance -= step
                reached_boundary = vehicle.progress_m >= current_segment.length_m - 1e-6
                boundary_node = current_segment.end_node_id
            else:
                remaining_on_segment = vehicle.progress_m
                step = min(remaining_distance, remaining_on_segment)
                vehicle.progress_m -= step
                remaining_distance -= step
                reached_boundary = vehicle.progress_m <= 1e-6
                boundary_node = current_segment.start_node_id

            if not reached_boundary:
                break

            if not self._transition_vehicle(vehicle, boundary_node, now):
                self._reset_vehicle(vehicle, now)
                break

        segment = self._segments[vehicle.road_segment_id]
        lat, lon, bearing = self._state_from_segment(
            segment, vehicle.progress_m, vehicle.direction
        )
        vehicle.lat, vehicle.lon = add_noise(
            lat, lon, self._app.config["GPS_NOISE_METERS"], rng=self._rng
        )
        vehicle.bearing = bearing
        vehicle.timestamp = now

    def _transition_vehicle(self, vehicle: Vehicle, node_id: int, now: float) -> bool:
        prefer_wrong_way = bool(
            vehicle.wrong_way
            and vehicle.wrong_way_until is not None
            and vehicle.wrong_way_until > now
        )
        next_option = self._choose_next_option(
            node_id,
            current_segment_id=vehicle.road_segment_id,
            prefer_wrong_way=prefer_wrong_way,
        )

        if next_option is None and vehicle.wrong_way:
            vehicle.wrong_way = False
            vehicle.wrong_way_until = None
            next_option = self._choose_next_option(
                node_id,
                current_segment_id=vehicle.road_segment_id,
                prefer_wrong_way=False,
            )

        if next_option is None:
            return False

        next_segment = self._segments[next_option.segment_id]
        vehicle.road_segment_id = next_segment.id
        vehicle.direction = next_option.direction
        vehicle.progress_m = 0.0 if next_option.direction >= 0 else next_segment.length_m
        vehicle.wrong_way = next_option.wrong_way
        if not next_option.wrong_way:
            vehicle.wrong_way_until = None
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
            filtered = [option for option in candidates if option.segment_id != current_segment_id]
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

            next_option = self._choose_projected_next_option(
                boundary_node,
                current_segment.id,
                current_direction,
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

    def _choose_projected_next_option(
        self,
        node_id: int,
        current_segment_id: int,
        current_direction: int,
        prefer_wrong_way: bool,
    ) -> RouteOption | None:
        candidates: list[RouteOption] = []
        if prefer_wrong_way:
            candidates.extend(self._wrong_way_options.get(node_id, []))
        candidates.extend(self._normal_options.get(node_id, []))
        candidates = [
            option for option in candidates if option.segment_id != current_segment_id
        ]
        if not candidates:
            return None

        current_segment = self._segments[current_segment_id]
        current_bearing = path_bearing_at(
            current_segment.geometry,
            current_segment.cumulative_lengths,
            current_segment.length_m if current_direction >= 0 else 0.0,
            current_direction,
        )

        def score(option: RouteOption) -> float:
            candidate = self._segments[option.segment_id]
            candidate_bearing = path_bearing_at(
                candidate.geometry,
                candidate.cumulative_lengths,
                0.0 if option.direction >= 0 else candidate.length_m,
                option.direction,
            )
            delta = fabs(((candidate_bearing - current_bearing + 180.0) % 360.0) - 180.0)
            wrong_way_penalty = 24.0 if option.wrong_way and not prefer_wrong_way else 0.0
            return delta + wrong_way_penalty

        return min(candidates, key=score)

    def _reset_vehicle(self, vehicle: Vehicle, now: float) -> None:
        segment = self._rng.choice(list(self._segments.values()))
        vehicle.road_segment_id = segment.id
        vehicle.direction = 1 if segment.oneway or self._rng.random() < 0.5 else -1
        vehicle.progress_m = self._rng.uniform(0.0, segment.length_m)
        vehicle.wrong_way = False
        vehicle.wrong_way_until = None
        vehicle.speed_mps = self._compute_speed(segment, vehicle.behavior)
        lat, lon, bearing = self._state_from_segment(segment, vehicle.progress_m, vehicle.direction)
        vehicle.lat, vehicle.lon = add_noise(
            lat, lon, self._app.config["GPS_NOISE_METERS"], rng=self._rng
        )
        vehicle.bearing = bearing
        vehicle.timestamp = now

    def _select_demo_segment(self, segment_id: int | None) -> SegmentRuntime | None:
        if segment_id is not None:
            return self._segments.get(segment_id)
        oneway_segments = [segment for segment in self._segments.values() if segment.oneway]
        if not oneway_segments:
            return None
        return max(oneway_segments, key=lambda segment: segment.length_m)

    def _select_demo_vehicle(
        self,
        vehicles: list[Vehicle],
        vehicle_id: int | None,
    ) -> Vehicle | None:
        if vehicle_id is not None:
            for vehicle in vehicles:
                if vehicle.id == vehicle_id:
                    return vehicle
        for vehicle in vehicles:
            if not vehicle.wrong_way:
                return vehicle
        return vehicles[0] if vehicles else None

    def _state_from_segment(
        self,
        segment: SegmentRuntime,
        progress_m: float,
        direction: int,
    ) -> tuple[float, float, float]:
        distance = min(max(progress_m, 0.0), segment.length_m)
        lat, lon = interpolate_path_position(segment.geometry, segment.cumulative_lengths, distance)
        bearing = path_bearing_at(segment.geometry, segment.cumulative_lengths, distance, direction)
        return lat, lon, bearing

    def _compute_speed(self, segment: SegmentRuntime, behavior: str) -> float:
        behavior_factor = BEHAVIOR_SPEED_FACTORS.get(behavior, 0.88)
        base_speed = max(segment.speed_limit_mps * behavior_factor, 5.0)
        varied = base_speed * self._rng.uniform(0.92, 1.08)
        return round(min(varied, 20.0), 2)


simulation_engine = VehicleSimulationEngine()
