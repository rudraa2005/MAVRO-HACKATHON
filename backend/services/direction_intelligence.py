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

import threading
import time
from typing import Any

from backend.extensions import db
from backend.models import RoadSegment, Vehicle, VehicleHistory
from backend.services.map_matching import map_matching_service
from direction_intelligence_core import DirectionIntelligenceEngine, DirectionProbe


class DirectionIntelligenceService:
    def __init__(self) -> None:
        self._engine = DirectionIntelligenceEngine()
        self._lock = threading.Lock()
        self._road_oneway_cache: dict[int, bool] = {}
        self._seeded_vehicles: set[int] = set()

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

        # Step 1: map-match all vehicles
        mm_result = map_matching_service.match_live_vehicles(
            candidate_limit=candidate_limit,
            distance_threshold_m=distance_threshold_m,
            max_jump_speed_mps=max_jump_speed_mps,
            limit=limit,
        )
        matches = mm_result["matches"]
        mm_stats = mm_result["stats"]

        # Step 2: load current vehicles
        vehicles = {v.id: v for v in Vehicle.query.order_by(Vehicle.id).all()}

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
            direction_results.append(result.to_dict())

        di_elapsed_ms = (time.perf_counter() - di_started) * 1000.0
        total_elapsed_ms = (time.perf_counter() - started) * 1000.0

        return {
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

    def invalidate_cache(self) -> None:
        with self._lock:
            self._engine.clear_all()
            self._road_oneway_cache.clear()
            self._seeded_vehicles.clear()

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


direction_intelligence_service = DirectionIntelligenceService()
