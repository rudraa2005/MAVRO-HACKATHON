"""Direction Intelligence Service — database-aware wrapper.

Connects the pure-NumPy :class:`DirectionIntelligenceEngine` to the Flask /
SQLAlchemy layer.  On each call to :meth:`analyze_live_vehicles` the service:

1. Runs map-matching for all live vehicles (reuses ``map_matching_service``).
2. Looks up one-way status for matched edges.
3. Seeds trajectory buffers for newly seen vehicles from ``vehicle_history``.
4. Feeds each vehicle probe to the engine and collects results.

The response includes *both* the map-match data and direction analysis so the
frontend only needs a single poll endpoint.
"""

from __future__ import annotations

from copy import deepcopy
import threading
import time
from typing import Any

from backend.extensions import db
from backend.models import RoadSegment, Vehicle, VehicleHistory
from backend.services.anomaly_memory import reset_vehicle_memory, update_memory
from backend.services.compute_spatial import compute_spatial
from backend.services.decision import run_decision
from backend.services.map_matching import map_matching_service
from backend.services.prediction import predict_trajectory, reset_prediction_memory
from backend.services.risk_engine import run_risk_engine
from direction_intelligence_core import DirectionIntelligenceEngine, DirectionProbe


class DirectionIntelligenceService:
    def __init__(self) -> None:
        self._engine = DirectionIntelligenceEngine()
        self._lock = threading.Lock()
        self._road_oneway_cache: dict[int, bool] = {}
        self._seeded_vehicles: set[int] = set()
        self._last_signature: tuple[Any, ...] | None = None
        self._last_result: dict[str, Any] | None = None

    # ── public API ────────────────────────────────────────────────────────

    def analyze_live_vehicles(
        self,
        candidate_limit: int = 5,
        distance_threshold_m: float = 30.0,
        max_jump_speed_mps: float = 60.0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Run map-matching + direction analysis for every live vehicle.

        Returns a dict with ``matches`` (map-match data), ``direction``
        (per-vehicle direction results), and ``stats``.
        """
        started = time.perf_counter()
        query = Vehicle.query.order_by(Vehicle.id)
        if limit is not None:
            query = query.limit(max(1, min(limit, 5000)))
        vehicle_rows = query.all()
        vehicles = {vehicle.id: vehicle for vehicle in vehicle_rows}

        signature = self._snapshot_signature(
            vehicles=vehicle_rows,
            candidate_limit=candidate_limit,
            distance_threshold_m=distance_threshold_m,
            max_jump_speed_mps=max_jump_speed_mps,
            limit=limit,
        )
        cached = self._cached_result(signature)
        if cached is not None:
            return cached

        payloads = [vehicle.to_dict() for vehicle in vehicle_rows]

        # Step 1: map-match all vehicles
        mm_started = time.perf_counter()
        matches = map_matching_service.match_payloads(
            payloads=payloads,
            candidate_limit=candidate_limit,
            distance_threshold_m=distance_threshold_m,
            max_jump_speed_mps=max_jump_speed_mps,
            update_state_cache=True,
        )
        mm_elapsed_ms = (time.perf_counter() - mm_started) * 1000.0
        mm_stats = {
            "points": len(payloads),
            "matched": sum(1 for match in matches if match["matched_edge_id"] is not None),
            "elapsed_ms": round(mm_elapsed_ms, 3),
            "avg_ms_per_point": round(mm_elapsed_ms / max(len(payloads), 1), 4),
        }

        # Step 3: seed trajectory buffers for new vehicles
        self._seed_new_vehicles(vehicles)

        # Step 4: run direction analysis per vehicle
        di_started = time.perf_counter()
        direction_results: list[dict[str, Any]] = []
        violations = 0

        for match in matches:
            vid = match.get("vehicle_id")
            vehicle = vehicles.get(vid)
            if vehicle is None:
                continue

            edge_id = match.get("matched_edge_id")
            oneway = self._is_oneway(edge_id) if edge_id else False

            probe = DirectionProbe(
                vehicle_id=vid,
                lat=vehicle.lat,
                lon=vehicle.lon,
                timestamp=vehicle.timestamp,
                speed_mps=vehicle.speed_mps,
                road_vector=(
                    tuple(match["road_vector"]) if match.get("road_vector") else None
                ),
                oneway=oneway,
                matched_edge_id=edge_id,
            )

            result = self._engine.process_probe(probe)
            if result.is_violation:
                violations += 1
            result_dict = result.to_dict()
            semantic_class = "normal"
            if result.is_violation:
                semantic_class = "wrong_way"
            elif result_dict["temporal_state"] == "SUSPECT":
                semantic_class = "risky"
            result_dict.update(
                {
                    "id": vehicle.id,
                    "lat": vehicle.lat,
                    "lon": vehicle.lon,
                    "speed": vehicle.speed_mps,
                    "bearing": vehicle.bearing,
                    "timestamp": vehicle.timestamp,
                    "road_segment_id": vehicle.road_segment_id,
                    "wrong_way": vehicle.wrong_way,
                    "behavior": vehicle.behavior,
                    "semantic_class": semantic_class,
                    "class": semantic_class,
                }
            )
            direction_results.append(result_dict)

        direction_results = update_memory(direction_results)
        direction_results = compute_spatial(direction_results)
        direction_results = [predict_trajectory(vehicle) for vehicle in direction_results]
        direction_results = run_risk_engine(direction_results)
        direction_results = run_decision(direction_results)

        di_elapsed_ms = (time.perf_counter() - di_started) * 1000.0
        total_elapsed_ms = (time.perf_counter() - started) * 1000.0

        result = {
            "matches": matches,
            "direction": direction_results,
            "stats": {
                "vehicles": len(matches),
                "violations": violations,
                "total_elapsed_ms": round(total_elapsed_ms, 3),
                "direction_elapsed_ms": round(di_elapsed_ms, 3),
                "match_stats": mm_stats,
            },
        }
        self._store_cached_result(signature, result)
        return deepcopy(result)

    def invalidate_cache(self) -> None:
        with self._lock:
            self._engine.clear_all()
            self._road_oneway_cache.clear()
            self._seeded_vehicles.clear()
            self._last_signature = None
            self._last_result = None
            reset_vehicle_memory()
            reset_prediction_memory()

    # ── internals ─────────────────────────────────────────────────────────

    def _is_oneway(self, edge_id: int) -> bool:
        if edge_id in self._road_oneway_cache:
            return self._road_oneway_cache[edge_id]
        road = db.session.get(RoadSegment, edge_id)
        oneway = bool(road.oneway) if road else False
        self._road_oneway_cache[edge_id] = oneway
        return oneway

    def _seed_new_vehicles(self, vehicles: dict[int, Vehicle]) -> None:
        """Pre-fill trajectory buffers from recent history for new vehicles."""
        for vid in vehicles:
            if vid in self._seeded_vehicles:
                continue
            self._seeded_vehicles.add(vid)
            history = (
                VehicleHistory.query
                .filter_by(vehicle_id=vid)
                .order_by(VehicleHistory.timestamp.desc())
                .limit(10)
                .all()
            )
            if history:
                points = [
                    (h.lat, h.lon, h.timestamp) for h in reversed(history)
                ]
                self._engine.seed_trajectory(vid, points)

    def _snapshot_signature(
        self,
        vehicles: list[Vehicle],
        candidate_limit: int,
        distance_threshold_m: float,
        max_jump_speed_mps: float,
        limit: int | None,
    ) -> tuple[Any, ...]:
        vehicle_signature = tuple(
            (
                vehicle.id,
                round(vehicle.timestamp, 6),
                round(vehicle.lat, 7),
                round(vehicle.lon, 7),
                round(vehicle.speed_mps, 4),
                round(vehicle.bearing, 4),
                vehicle.road_segment_id,
            )
            for vehicle in vehicles
        )
        return (
            candidate_limit,
            round(distance_threshold_m, 4),
            round(max_jump_speed_mps, 4),
            limit,
            vehicle_signature,
        )

    def _cached_result(self, signature: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._lock:
            if signature != self._last_signature or self._last_result is None:
                return None
            return deepcopy(self._last_result)

    def _store_cached_result(
        self,
        signature: tuple[Any, ...],
        result: dict[str, Any],
    ) -> None:
        with self._lock:
            self._last_signature = signature
            self._last_result = deepcopy(result)


direction_intelligence_service = DirectionIntelligenceService()
