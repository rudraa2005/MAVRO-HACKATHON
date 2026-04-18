from __future__ import annotations

import math
import threading
import time
from typing import Any

from sqlalchemy import func

from backend.extensions import db
from backend.models import RoadSegment, Vehicle, VehicleHistory
from map_matching_core import GPSProbe, MapMatchingIndex, PreviousMatchState


class MapMatchingService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._index: MapMatchingIndex | None = None
        self._network_signature: tuple[int, int] | None = None
        self._previous_state: dict[str, PreviousMatchState] = {}

    def match_payloads(
        self,
        payloads: list[dict[str, Any]],
        candidate_limit: int = 5,
        distance_threshold_m: float = 30.0,
        max_jump_speed_mps: float = 60.0,
        update_state_cache: bool = True,
    ) -> list[dict[str, Any]]:
        index = self._ensure_index()
        results: list[dict[str, Any]] = []

        for offset, payload in enumerate(payloads):
            probe = GPSProbe.from_payload(payload, fallback_vehicle_id=offset + 1)
            previous_state = self._previous_state.get(str(probe.vehicle_id))
            result = index.match_probe(
                probe=probe,
                candidate_limit=candidate_limit,
                distance_threshold_m=distance_threshold_m,
                previous_state=previous_state,
                max_jump_speed_mps=max_jump_speed_mps,
            )
            if update_state_cache and result.matched_edge_id is not None:
                self._previous_state[str(probe.vehicle_id)] = PreviousMatchState(
                    edge_id=result.matched_edge_id,
                    lat=probe.lat,
                    lon=probe.lon,
                    timestamp=probe.timestamp,
                    speed_mps=probe.speed_mps,
                )
            results.append(result.to_dict())

        return results

    def match_live_vehicles(
        self,
        candidate_limit: int = 5,
        distance_threshold_m: float = 30.0,
        max_jump_speed_mps: float = 60.0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        query = Vehicle.query.order_by(Vehicle.id)
        if limit is not None:
            query = query.limit(max(1, min(limit, 5000)))

        payloads = [vehicle.to_dict() for vehicle in query.all()]
        started_at = time.perf_counter()
        matches = self.match_payloads(
            payloads=payloads,
            candidate_limit=candidate_limit,
            distance_threshold_m=distance_threshold_m,
            max_jump_speed_mps=max_jump_speed_mps,
            update_state_cache=True,
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        matched_count = sum(1 for match in matches if match["matched_edge_id"] is not None)
        return {
            "matches": matches,
            "stats": {
                "points": len(payloads),
                "matched": matched_count,
                "elapsed_ms": round(elapsed_ms, 3),
                "avg_ms_per_point": round(elapsed_ms / max(len(payloads), 1), 4),
            },
        }

    def benchmark(
        self,
        points_count: int = 1000,
        candidate_limit: int = 5,
        distance_threshold_m: float = 30.0,
        max_jump_speed_mps: float = 60.0,
    ) -> dict[str, Any]:
        sample_rows = VehicleHistory.query.order_by(VehicleHistory.timestamp.desc()).limit(5000).all()
        payloads = [
            {
                "vehicle_id": row.vehicle_id,
                "lat": row.lat,
                "lon": row.lon,
                "timestamp": row.timestamp,
                "speed": row.speed_mps,
                "heading": row.bearing,
            }
            for row in reversed(sample_rows)
        ]

        if not payloads:
            payloads = [vehicle.to_dict() for vehicle in Vehicle.query.order_by(Vehicle.id).all()]

        if not payloads:
            raise ValueError("No GPS points available. Start the simulation or load vehicle history first.")

        if len(payloads) < points_count:
            repeats = math.ceil(points_count / len(payloads))
            payloads = (payloads * repeats)[:points_count]
        else:
            payloads = payloads[:points_count]

        started_at = time.perf_counter()
        matches = self.match_payloads(
            payloads=payloads,
            candidate_limit=candidate_limit,
            distance_threshold_m=distance_threshold_m,
            max_jump_speed_mps=max_jump_speed_mps,
            update_state_cache=False,
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        matched_count = sum(1 for match in matches if match["matched_edge_id"] is not None)

        return {
            "points": len(payloads),
            "matched": matched_count,
            "elapsed_ms": round(elapsed_ms, 3),
            "avg_ms_per_point": round(elapsed_ms / max(len(payloads), 1), 4),
            "candidate_limit": candidate_limit,
            "distance_threshold_m": distance_threshold_m,
            "postgis_candidate_sql": self.postgis_candidate_sql(
                candidate_limit=candidate_limit,
                distance_threshold_m=distance_threshold_m,
            ),
        }

    def invalidate_cache(self) -> None:
        with self._lock:
            self._index = None
            self._network_signature = None
            self._previous_state.clear()

    def postgis_candidate_sql(
        self,
        candidate_limit: int = 5,
        distance_threshold_m: float = 30.0,
    ) -> str:
        return f"""
WITH probe AS (
  SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) AS geom
)
SELECT
  rs.id AS edge_id,
  ST_Distance(rs.geom::geography, probe.geom::geography) AS distance_m,
  ST_AsText(ST_ClosestPoint(rs.geom, probe.geom)) AS snapped_wkt,
  rs.bearing,
  rs.oneway
FROM road_segments rs, probe
WHERE ST_DWithin(rs.geom::geography, probe.geom::geography, {float(distance_threshold_m):.1f})
ORDER BY rs.geom <-> probe.geom
LIMIT {int(max(1, min(candidate_limit, 8)))};
""".strip()

    def _ensure_index(self) -> MapMatchingIndex:
        signature = self._network_signature_value()
        if self._index is not None and signature == self._network_signature:
            return self._index

        with self._lock:
            signature = self._network_signature_value()
            if self._index is not None and signature == self._network_signature:
                return self._index

            segments = RoadSegment.query.order_by(RoadSegment.id).all()
            self._index = MapMatchingIndex.from_road_segments(segments)
            self._network_signature = signature
            self._previous_state.clear()
            return self._index

    def _network_signature_value(self) -> tuple[int, int]:
        count = db.session.query(func.count(RoadSegment.id)).scalar() or 0
        max_id = db.session.query(func.max(RoadSegment.id)).scalar() or 0
        return int(count), int(max_id)


map_matching_service = MapMatchingService()
