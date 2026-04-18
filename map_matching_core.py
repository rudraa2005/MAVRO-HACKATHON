from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from shapely.geometry import LineString, Point, box
from shapely.strtree import STRtree


EARTH_RADIUS_M = 6_371_000.0


@dataclass
class GPSProbe:
    vehicle_id: str | int | None
    lat: float
    lon: float
    timestamp: float | None
    speed_mps: float
    heading: float

    @classmethod
    def from_payload(cls, payload: dict[str, Any], fallback_vehicle_id: int) -> "GPSProbe":
        vehicle_id = payload.get("vehicle_id", payload.get("id", fallback_vehicle_id))
        lat = float(payload["lat"])
        lon = float(payload["lon"])
        timestamp_value = payload.get("timestamp")
        timestamp = float(timestamp_value) if timestamp_value is not None else None
        speed_value = payload.get("speed", payload.get("speed_mps", 0.0))
        heading_value = payload.get("heading", payload.get("bearing", 0.0))
        return cls(
            vehicle_id=vehicle_id,
            lat=lat,
            lon=lon,
            timestamp=timestamp,
            speed_mps=float(speed_value or 0.0),
            heading=float(heading_value or 0.0) % 360.0,
        )


@dataclass
class PreviousMatchState:
    edge_id: int
    lat: float
    lon: float
    timestamp: float | None
    speed_mps: float


@dataclass
class IndexedEdge:
    edge_id: int
    oneway: bool
    length_m: float
    speed_limit_mps: float | None
    lonlat_line: LineString
    local_line: LineString
    ref_lat: float
    ref_lon: float


@dataclass
class CandidateScore:
    edge_id: int
    distance_error_m: float
    distance_along_edge_m: float
    snapped_lat: float
    snapped_lon: float
    road_vector: tuple[float, float]
    matched_bearing: float
    heading_diff: float
    score: float


@dataclass
class MapMatchResult:
    vehicle_id: str | int | None
    matched_edge_id: int | None
    snapped_point: list[float] | None
    distance_error: float | None
    road_vector: list[float] | None
    confidence_score: float
    distance_along_edge: float | None
    heading_diff: float | None
    matched_bearing: float | None
    rejected_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "matched_edge_id": self.matched_edge_id,
            "snapped_point": self.snapped_point,
            "distance_error": self.distance_error,
            "road_vector": self.road_vector,
            "confidence_score": self.confidence_score,
            "distance_along_edge": self.distance_along_edge,
            "heading_diff": self.heading_diff,
            "matched_bearing": self.matched_bearing,
            "rejected_reason": self.rejected_reason,
        }


class MapMatchingIndex:
    def __init__(self, edges: list[IndexedEdge]) -> None:
        self._edges = edges
        self._geometries = [edge.lonlat_line for edge in edges]
        self._geometry_index = {id(geometry): index for index, geometry in enumerate(self._geometries)}
        self._tree = STRtree(self._geometries) if self._geometries else None

    @classmethod
    def from_dicts(cls, segments: list[dict[str, Any]]) -> "MapMatchingIndex":
        indexed_edges: list[IndexedEdge] = []
        for segment in segments:
            geometry = segment.get("geometry") or [
                {"lat": segment["start"][0], "lon": segment["start"][1]},
                {"lat": segment["end"][0], "lon": segment["end"][1]},
            ]
            edge = cls._build_indexed_edge(
                edge_id=int(segment["id"]),
                oneway=bool(segment.get("oneway", False)),
                length_m=float(segment.get("length", segment.get("length_m", 0.0)) or 0.0),
                speed_limit_mps=segment.get("speed_limit_mps"),
                geometry=geometry,
            )
            if edge is not None:
                indexed_edges.append(edge)
        return cls(indexed_edges)

    @classmethod
    def from_road_segments(cls, segments: list[Any]) -> "MapMatchingIndex":
        indexed_edges: list[IndexedEdge] = []
        for segment in segments:
            geometry = getattr(segment, "geometry", None) or [
                {"lat": float(segment.start_lat), "lon": float(segment.start_lon)},
                {"lat": float(segment.end_lat), "lon": float(segment.end_lon)},
            ]
            edge = cls._build_indexed_edge(
                edge_id=int(segment.id),
                oneway=bool(segment.oneway),
                length_m=float(segment.length_m or 0.0),
                speed_limit_mps=getattr(segment, "speed_limit_mps", None),
                geometry=geometry,
            )
            if edge is not None:
                indexed_edges.append(edge)
        return cls(indexed_edges)

    def match_probe(
        self,
        probe: GPSProbe,
        candidate_limit: int = 5,
        distance_threshold_m: float = 30.0,
        previous_state: PreviousMatchState | None = None,
        max_jump_speed_mps: float = 60.0,
    ) -> MapMatchResult:
        if self._tree is None:
            return MapMatchResult(
                vehicle_id=probe.vehicle_id,
                matched_edge_id=None,
                snapped_point=None,
                distance_error=None,
                road_vector=None,
                confidence_score=0.0,
                distance_along_edge=None,
                heading_diff=None,
                matched_bearing=None,
                rejected_reason="no_road_network",
            )

        candidate_edges = self._candidate_edges(
            lat=probe.lat,
            lon=probe.lon,
            candidate_limit=max(1, min(candidate_limit, 8)),
            distance_threshold_m=max(distance_threshold_m, 5.0),
        )
        if not candidate_edges:
            return MapMatchResult(
                vehicle_id=probe.vehicle_id,
                matched_edge_id=None,
                snapped_point=None,
                distance_error=None,
                road_vector=None,
                confidence_score=0.0,
                distance_along_edge=None,
                heading_diff=None,
                matched_bearing=None,
                rejected_reason="no_candidate_edges",
            )

        scored_candidates: list[CandidateScore] = []
        for edge in candidate_edges:
            scored = self._score_candidate(
                edge=edge,
                probe=probe,
                distance_threshold_m=distance_threshold_m,
                previous_state=previous_state,
            )
            if scored is not None:
                scored_candidates.append(scored)

        if not scored_candidates:
            return MapMatchResult(
                vehicle_id=probe.vehicle_id,
                matched_edge_id=None,
                snapped_point=None,
                distance_error=None,
                road_vector=None,
                confidence_score=0.0,
                distance_along_edge=None,
                heading_diff=None,
                matched_bearing=None,
                rejected_reason="distance_threshold_exceeded",
            )

        scored_candidates.sort(key=lambda candidate: candidate.score)
        best = scored_candidates[0]

        if self._is_sudden_jump(
            probe=probe,
            previous_state=previous_state,
            best_candidate=best,
            max_jump_speed_mps=max_jump_speed_mps,
            distance_threshold_m=distance_threshold_m,
        ):
            return MapMatchResult(
                vehicle_id=probe.vehicle_id,
                matched_edge_id=None,
                snapped_point=None,
                distance_error=None,
                road_vector=None,
                confidence_score=0.0,
                distance_along_edge=None,
                heading_diff=None,
                matched_bearing=None,
                rejected_reason="jump_threshold_exceeded",
            )

        second_best_score = scored_candidates[1].score if len(scored_candidates) > 1 else None
        confidence = self._confidence_for(best.score, second_best_score)

        return MapMatchResult(
            vehicle_id=probe.vehicle_id,
            matched_edge_id=best.edge_id,
            snapped_point=[best.snapped_lat, best.snapped_lon],
            distance_error=round(best.distance_error_m, 3),
            road_vector=[round(best.road_vector[0], 6), round(best.road_vector[1], 6)],
            confidence_score=round(confidence, 4),
            distance_along_edge=round(best.distance_along_edge_m, 3),
            heading_diff=round(best.heading_diff, 3),
            matched_bearing=round(best.matched_bearing, 3),
        )

    def _candidate_edges(
        self,
        lat: float,
        lon: float,
        candidate_limit: int,
        distance_threshold_m: float,
    ) -> list[IndexedEdge]:
        if self._tree is None:
            return []

        seen_indexes: set[int] = set()
        search_radius = max(distance_threshold_m, 20.0)

        for _ in range(4):
            bbox = box(
                lon - _lon_delta(search_radius, lat),
                lat - _lat_delta(search_radius),
                lon + _lon_delta(search_radius, lat),
                lat + _lat_delta(search_radius),
            )
            for index in self._normalize_query_indexes(self._tree.query(bbox)):
                seen_indexes.add(index)
            if len(seen_indexes) >= candidate_limit:
                break
            search_radius *= 2.0

        if not seen_indexes:
            nearest = self._tree.nearest(Point(lon, lat))
            if nearest is not None:
                seen_indexes.add(self._normalize_single_index(nearest))

        candidate_edges = [self._edges[index] for index in seen_indexes]
        candidate_edges.sort(
            key=lambda edge: self._point_to_edge_distance_m(lat=lat, lon=lon, edge=edge)
        )
        return candidate_edges[:candidate_limit]

    def _score_candidate(
        self,
        edge: IndexedEdge,
        probe: GPSProbe,
        distance_threshold_m: float,
        previous_state: PreviousMatchState | None,
    ) -> CandidateScore | None:
        point_x, point_y = _project_local_xy(
            lat=probe.lat,
            lon=probe.lon,
            ref_lat=edge.ref_lat,
            ref_lon=edge.ref_lon,
        )
        local_point = Point(point_x, point_y)
        distance_error_m = edge.local_line.distance(local_point)
        if distance_error_m > distance_threshold_m:
            return None

        distance_along_edge_m = edge.local_line.project(local_point)
        snapped_local = edge.local_line.interpolate(distance_along_edge_m)
        snapped_lat, snapped_lon = _inverse_project_local_xy(
            x=snapped_local.x,
            y=snapped_local.y,
            ref_lat=edge.ref_lat,
            ref_lon=edge.ref_lon,
        )

        forward_vector, forward_bearing = _line_direction_at(edge.local_line, distance_along_edge_m, 1)
        heading_forward = _heading_difference(probe.heading, forward_bearing)

        road_vector = forward_vector
        matched_bearing = forward_bearing
        heading_diff = heading_forward

        if not edge.oneway:
            reverse_vector = (-forward_vector[0], -forward_vector[1])
            reverse_bearing = (forward_bearing + 180.0) % 360.0
            heading_reverse = _heading_difference(probe.heading, reverse_bearing)
            if heading_reverse < heading_forward:
                road_vector = reverse_vector
                matched_bearing = reverse_bearing
                heading_diff = heading_reverse

        normalized_distance = min(distance_error_m / max(distance_threshold_m, 1.0), 3.0)
        normalized_heading = heading_diff / 180.0
        speed_penalty = 0.0
        if edge.speed_limit_mps is not None and probe.speed_mps > edge.speed_limit_mps * 1.15:
            speed_penalty = min(
                (probe.speed_mps - edge.speed_limit_mps * 1.15) / max(edge.speed_limit_mps, 1.0),
                2.0,
            )
        oneway_penalty = 0.15 * normalized_heading if edge.oneway else 0.0

        score = (
            0.68 * normalized_distance
            + 0.27 * normalized_heading
            + 0.05 * speed_penalty
            + oneway_penalty
        )
        if previous_state is not None and previous_state.edge_id == edge.edge_id:
            score *= 0.82

        return CandidateScore(
            edge_id=edge.edge_id,
            distance_error_m=distance_error_m,
            distance_along_edge_m=min(distance_along_edge_m, edge.length_m or distance_along_edge_m),
            snapped_lat=snapped_lat,
            snapped_lon=snapped_lon,
            road_vector=road_vector,
            matched_bearing=matched_bearing,
            heading_diff=heading_diff,
            score=score,
        )

    def _point_to_edge_distance_m(self, lat: float, lon: float, edge: IndexedEdge) -> float:
        point_x, point_y = _project_local_xy(lat=lat, lon=lon, ref_lat=edge.ref_lat, ref_lon=edge.ref_lon)
        return edge.local_line.distance(Point(point_x, point_y))

    def _is_sudden_jump(
        self,
        probe: GPSProbe,
        previous_state: PreviousMatchState | None,
        best_candidate: CandidateScore,
        max_jump_speed_mps: float,
        distance_threshold_m: float,
    ) -> bool:
        if previous_state is None or previous_state.timestamp is None or probe.timestamp is None:
            return False

        dt = probe.timestamp - previous_state.timestamp
        if dt <= 0:
            return False

        travel_distance_m = haversine_distance_m(
            previous_state.lat,
            previous_state.lon,
            probe.lat,
            probe.lon,
        )
        travel_speed_mps = travel_distance_m / dt
        if travel_speed_mps <= max_jump_speed_mps:
            return False

        if best_candidate.edge_id == previous_state.edge_id:
            return False

        return travel_distance_m > distance_threshold_m * 4.0

    def _confidence_for(self, best_score: float, second_best_score: float | None) -> float:
        base_confidence = max(0.0, min(1.0, 1.0 - best_score / 1.35))
        if second_best_score is None:
            return base_confidence
        separation = max(second_best_score - best_score, 0.0)
        return max(0.0, min(1.0, base_confidence * min(1.0, 0.7 + separation)))

    def _normalize_query_indexes(self, result: Any) -> list[int]:
        if result is None:
            return []
        indexes: list[int] = []
        for item in result:
            indexes.append(self._normalize_single_index(item))
        return indexes

    def _normalize_single_index(self, value: Any) -> int:
        if isinstance(value, int):
            return value
        if hasattr(value, "item") and not hasattr(value, "geom_type"):
            return int(value.item())
        return self._geometry_index[id(value)]

    @classmethod
    def _build_indexed_edge(
        cls,
        edge_id: int,
        oneway: bool,
        length_m: float,
        speed_limit_mps: float | None,
        geometry: list[dict[str, float]],
    ) -> IndexedEdge | None:
        lonlat_coords = _geometry_to_lonlat_coords(geometry)
        if len(lonlat_coords) < 2:
            return None

        ref_lat = sum(lat for _, lat in lonlat_coords) / len(lonlat_coords)
        ref_lon = sum(lon for lon, _ in lonlat_coords) / len(lonlat_coords)
        local_coords = [
            _project_local_xy(lat=lat, lon=lon, ref_lat=ref_lat, ref_lon=ref_lon)
            for lon, lat in lonlat_coords
        ]
        local_line = LineString(local_coords)
        if local_line.length <= 0:
            return None

        return IndexedEdge(
            edge_id=edge_id,
            oneway=bool(oneway),
            length_m=float(length_m or local_line.length),
            speed_limit_mps=float(speed_limit_mps) if speed_limit_mps is not None else None,
            lonlat_line=LineString(lonlat_coords),
            local_line=local_line,
            ref_lat=ref_lat,
            ref_lon=ref_lon,
        )


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_M * c


def _geometry_to_lonlat_coords(geometry: list[dict[str, float]]) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    for point in geometry:
        lon = float(point["lon"])
        lat = float(point["lat"])
        if coords and math.isclose(coords[-1][0], lon) and math.isclose(coords[-1][1], lat):
            continue
        coords.append((lon, lat))
    return coords


def _project_local_xy(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


def _inverse_project_local_xy(x: float, y: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    lat = ref_lat + math.degrees(y / EARTH_RADIUS_M)
    lon = ref_lon + math.degrees(x / (EARTH_RADIUS_M * max(math.cos(math.radians(ref_lat)), 1e-9)))
    return lat, lon


def _line_direction_at(line: LineString, distance_m: float, direction: int) -> tuple[tuple[float, float], float]:
    line_length = line.length
    if line_length <= 0:
        return (1.0, 0.0), 90.0

    delta = min(max(line_length * 0.02, 2.0), 8.0)
    start_distance = max(distance_m - delta, 0.0)
    end_distance = min(distance_m + delta, line_length)
    if math.isclose(start_distance, end_distance):
        start_distance = max(distance_m - 1.0, 0.0)
        end_distance = min(distance_m + 1.0, line_length)

    start_point = line.interpolate(start_distance)
    end_point = line.interpolate(end_distance)
    vector_x = end_point.x - start_point.x
    vector_y = end_point.y - start_point.y
    norm = math.hypot(vector_x, vector_y)
    if norm <= 0:
        return (1.0, 0.0), 90.0

    unit_x = vector_x / norm
    unit_y = vector_y / norm
    if direction < 0:
        unit_x *= -1.0
        unit_y *= -1.0

    bearing = (math.degrees(math.atan2(unit_x, unit_y)) + 360.0) % 360.0
    return (unit_x, unit_y), bearing


def _heading_difference(vehicle_heading: float, road_bearing: float) -> float:
    return abs((vehicle_heading - road_bearing + 180.0) % 360.0 - 180.0)


def _lat_delta(distance_m: float) -> float:
    return math.degrees(distance_m / EARTH_RADIUS_M)


def _lon_delta(distance_m: float, lat: float) -> float:
    return math.degrees(distance_m / (EARTH_RADIUS_M * max(math.cos(math.radians(lat)), 1e-9)))
