from __future__ import annotations

from collections import Counter
import re
from typing import Any

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from backend.extensions import db
from backend.models import POI, RoadSegment, Vehicle, VehicleHistory
from backend.services.geo import (
    compute_bearing,
    flatten_osm_value,
    line_to_path,
    parse_osm_bool,
    representative_lat_lon,
)


DEFAULT_SPEED_LIMITS_KPH = {
    "motorway": 80,
    "trunk": 70,
    "primary": 60,
    "secondary": 50,
    "tertiary": 40,
    "residential": 30,
    "service": 20,
    "unclassified": 30,
}

QUERY_ALIASES = {
    "parry's corner": [
        "Parrys, George Town, Chennai, Tamil Nadu, India",
        "George Town, Chennai, Tamil Nadu, India",
    ],
    "parrys corner": [
        "Parrys, George Town, Chennai, Tamil Nadu, India",
        "George Town, Chennai, Tamil Nadu, India",
    ],
    "t nagar": [
        "T. Nagar, Chennai, Tamil Nadu, India",
    ],
    "anna salai": [
        "Anna Salai, Chennai, Tamil Nadu, India",
    ],
}


class IngestionError(ValueError):
    pass


class OSMIngestionService:
    def ingest_query(
        self,
        query: str,
        query_type: str = "auto",
        radius_m: int = 700,
        reset: bool = False,
    ) -> dict[str, int | float | str | bool]:
        import osmnx as ox

        normalized_query_type = (query_type or "auto").strip().lower()
        resolved = self._resolve_osm_payload(
            ox=ox,
            query=query,
            query_type=normalized_query_type,
            radius_m=radius_m,
        )
        graph = resolved["graph"]
        features = resolved["features"]
        center_lat = resolved["center_lat"]
        center_lon = resolved["center_lon"]
        scope = resolved["scope"]
        resolved_query = resolved["resolved_query"]

        node_frame, edge_frame = ox.graph_to_gdfs(graph, nodes=True, edges=True)
        edge_rows = edge_frame.reset_index()
        node_lookup = {
            int(node_id): {
                "lat": float(row.y),
                "lon": float(row.x),
                "street_count": row.get("street_count", 0),
            }
            for node_id, row in node_frame.iterrows()
        }

        if reset:
            VehicleHistory.query.delete()
            Vehicle.query.delete()
            POI.query.delete()
            RoadSegment.query.delete()
            db.session.commit()

        road_segments = self._build_road_segments(edge_rows, node_lookup)
        db.session.add_all(road_segments)
        db.session.flush()

        pois = self._build_pois(features, node_frame, road_segments)
        db.session.add_all(pois)
        self._update_poi_density(road_segments, pois)
        db.session.commit()

        return {
            "query": query,
            "resolved_query": resolved_query,
            "scope": scope,
            "radius_m": radius_m,
            "road_segments": len(road_segments),
            "oneway_segments": sum(1 for segment in road_segments if segment.oneway),
            "pois": len(pois),
            "intersections": sum(1 for poi in pois if poi.poi_type == "intersection"),
            "center_lat": center_lat,
            "center_lon": center_lon,
        }

    def ingest_place(self, place_name: str, reset: bool = False) -> dict[str, int | float | str | bool]:
        return self.ingest_query(
            query=place_name,
            query_type="place",
            radius_m=700,
            reset=reset,
        )

    def _build_road_segments(
        self,
        edge_rows,
        node_lookup: dict[int, dict[str, float | int]],
    ) -> list[RoadSegment]:
        segments: list[RoadSegment] = []
        for row in edge_rows.itertuples(index=False):
            start_node = node_lookup[int(row.u)]
            end_node = node_lookup[int(row.v)]
            path = line_to_path(
                getattr(row, "geometry", None),
                (float(start_node["lat"]), float(start_node["lon"])),
                (float(end_node["lat"]), float(end_node["lon"])),
            )

            road_class = flatten_osm_value(getattr(row, "highway", None))
            speed_limit_mps = self._speed_limit_mps(
                getattr(row, "maxspeed", None),
                road_class,
            )
            segments.append(
                RoadSegment(
                    osm_way_id=self._safe_int(getattr(row, "osmid", None)),
                    start_node_id=int(row.u),
                    end_node_id=int(row.v),
                    start_lat=path[0]["lat"],
                    start_lon=path[0]["lon"],
                    end_lat=path[-1]["lat"],
                    end_lon=path[-1]["lon"],
                    bearing=compute_bearing(
                        path[0]["lat"],
                        path[0]["lon"],
                        path[-1]["lat"],
                        path[-1]["lon"],
                    ),
                    oneway=parse_osm_bool(getattr(row, "oneway", False)),
                    length_m=float(getattr(row, "length", 0.0)),
                    geometry=path,
                    road_class=road_class,
                    speed_limit_mps=speed_limit_mps,
                )
            )
        return segments

    def _resolve_osm_payload(
        self,
        ox,
        query: str,
        query_type: str,
        radius_m: int,
    ) -> dict[str, Any]:
        coordinate = self._parse_coordinate_query(query)
        if query_type != "place" and coordinate is not None:
            center_lat, center_lon = coordinate
            graph = ox.graph_from_point(
                (center_lat, center_lon),
                dist=radius_m,
                network_type="drive",
                simplify=True,
            )
            features = ox.features_from_point(
                (center_lat, center_lon),
                self._poi_tags(),
                dist=radius_m,
            )
            node_frame, _ = ox.graph_to_gdfs(graph, nodes=True, edges=False)
            center_lat, center_lon = self._graph_center(node_frame)
            return {
                "graph": graph,
                "features": features,
                "center_lat": center_lat,
                "center_lon": center_lon,
                "scope": "street_area",
                "resolved_query": query.strip(),
            }

        candidates = self._candidate_queries(query)
        attempted: list[str] = []
        last_error: Exception | None = None

        if query_type == "place":
            modes = ("place",)
        else:
            modes = ("point", "place")

        for mode in modes:
            for candidate in candidates:
                attempted.append(f"{mode}:{candidate}")
                try:
                    if mode == "place":
                        graph = ox.graph_from_place(
                            candidate,
                            network_type="drive",
                            simplify=True,
                        )
                        features = ox.features_from_place(candidate, self._poi_tags())
                    else:
                        center_lat, center_lon = ox.geocode(candidate)
                        graph = ox.graph_from_point(
                            (center_lat, center_lon),
                            dist=radius_m,
                            network_type="drive",
                            simplify=True,
                        )
                        features = ox.features_from_point(
                            (center_lat, center_lon),
                            self._poi_tags(),
                            dist=radius_m,
                        )

                    node_frame, _ = ox.graph_to_gdfs(graph, nodes=True, edges=False)
                    center_lat, center_lon = self._graph_center(node_frame)
                    return {
                        "graph": graph,
                        "features": features,
                        "center_lat": center_lat,
                        "center_lon": center_lon,
                        "scope": "place" if mode == "place" else "street_area",
                        "resolved_query": candidate,
                    }
                except Exception as exc:
                    last_error = exc

        suggestion_list = self._suggestions_for(query)
        message = (
            f'Could not resolve "{query}" to an OSM location. '
            "Try a more specific street or neighborhood."
        )
        if suggestion_list:
            message += f" Suggestions: {', '.join(suggestion_list)}."
        if attempted:
            message += f" Attempted: {', '.join(attempted[:6])}."
        if last_error is not None:
            message += f" Upstream detail: {last_error}."
        raise IngestionError(message)

    def _build_pois(self, features, node_frame, road_segments: list[RoadSegment]) -> list[POI]:
        road_lines = [
            LineString([(point["lon"], point["lat"]) for point in segment.geometry])
            for segment in road_segments
        ]
        tree = STRtree(road_lines) if road_lines else None
        pois: list[POI] = []

        for index, row in features.iterrows():
            poi_type = self._classify_poi(row)
            if not poi_type:
                continue
            lat, lon = representative_lat_lon(row.geometry)
            road_segment_id = self._nearest_road_segment_id(tree, road_segments, lat, lon)
            pois.append(
                POI(
                    osm_feature_id=self._feature_identifier(index),
                    poi_type=poi_type,
                    lat=lat,
                    lon=lon,
                    nearest_road_segment_id=road_segment_id,
                )
            )

        if "street_count" in node_frame.columns:
            intersections = node_frame[node_frame["street_count"].fillna(0) >= 3]
        else:
            intersections = node_frame.iloc[0:0]

        for node_id, row in intersections.iterrows():
            lat = float(row.y)
            lon = float(row.x)
            pois.append(
                POI(
                    osm_feature_id=f"intersection:{node_id}",
                    poi_type="intersection",
                    lat=lat,
                    lon=lon,
                    nearest_road_segment_id=self._nearest_road_segment_id(
                        tree, road_segments, lat, lon
                    ),
                )
            )

        return pois

    def _update_poi_density(self, road_segments: list[RoadSegment], pois: list[POI]) -> None:
        counts = Counter(
            poi.nearest_road_segment_id for poi in pois if poi.nearest_road_segment_id
        )
        for segment in road_segments:
            count = counts.get(segment.id, 0)
            segment.poi_density = count / max(segment.length_m / 1000.0, 0.1)

    def _nearest_road_segment_id(
        self,
        tree: STRtree | None,
        road_segments: list[RoadSegment],
        lat: float,
        lon: float,
    ) -> int | None:
        if not road_segments or tree is None:
            return None
        nearest_index = tree.nearest(Point(lon, lat))
        if nearest_index is None:
            return None
        return road_segments[int(nearest_index)].id

    def _classify_poi(self, row: Any) -> str | None:
        if row.get("shop"):
            return "shop"
        if row.get("highway") == "traffic_signals":
            return "signal"
        if row.get("amenity") == "parking":
            return "parking"
        return None

    def _poi_tags(self) -> dict[str, Any]:
        return {
            "shop": True,
            "amenity": ["parking"],
            "highway": ["traffic_signals"],
        }

    def _speed_limit_mps(self, raw_maxspeed: Any, road_class: str | None) -> float:
        kph_value = self._parse_maxspeed(raw_maxspeed)
        if kph_value is None:
            kph_value = DEFAULT_SPEED_LIMITS_KPH.get(road_class or "", 35)
        return round(kph_value / 3.6, 2)

    def _parse_maxspeed(self, raw_maxspeed: Any) -> float | None:
        raw_value = flatten_osm_value(raw_maxspeed)
        if not raw_value:
            return None

        match = re.search(r"(\d+(?:\.\d+)?)", raw_value)
        if match:
            value = float(match.group(1))
            if "mph" in raw_value.lower():
                return round(value * 1.60934, 2)
            return value
        return None

    def _safe_int(self, value: Any) -> int | None:
        if isinstance(value, list) and value:
            value = value[0]
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _feature_identifier(self, index: Any) -> str:
        if isinstance(index, tuple):
            return ":".join(str(item) for item in index)
        return str(index)

    def _candidate_queries(self, query: str) -> list[str]:
        base_query = query.strip()
        if not base_query:
            raise IngestionError("Query is empty. Enter a street, neighborhood, or place.")

        candidates: list[str] = [base_query]
        normalized = re.sub(r"[’']", "", base_query)
        if normalized != base_query:
            candidates.append(normalized)

        primary_label = normalized.split(",")[0].strip().lower()
        alias_keys = {normalized.lower(), primary_label}
        for alias_key in alias_keys:
            for alias in QUERY_ALIASES.get(alias_key, []):
                candidates.append(alias)

        lower = normalized.lower()
        if "chennai" not in lower:
            candidates.append(f"{normalized}, Chennai, Tamil Nadu, India")
        elif "tamil nadu" not in lower and "india" in lower:
            candidates.append(
                re.sub(
                    r",\s*india\b",
                    ", Tamil Nadu, India",
                    normalized,
                    flags=re.IGNORECASE,
                )
            )
        elif "tamil nadu" not in lower:
            candidates.append(f"{normalized}, Tamil Nadu, India")
        elif "india" not in lower:
            candidates.append(f"{normalized}, India")

        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            compact = re.sub(r"\s+", " ", candidate).strip(" ,")
            key = compact.lower()
            if not compact or key in seen:
                continue
            deduped.append(compact)
            seen.add(key)
        return deduped

    def _suggestions_for(self, query: str) -> list[str]:
        normalized = re.sub(r"[’']", "", query.strip()).lower()
        primary_label = normalized.split(",")[0].strip()
        if normalized in QUERY_ALIASES:
            return QUERY_ALIASES[normalized]
        if primary_label in QUERY_ALIASES:
            return QUERY_ALIASES[primary_label]
        return [
            "Anna Salai, Chennai, Tamil Nadu, India",
            "T. Nagar, Chennai, Tamil Nadu, India",
            "George Town, Chennai, Tamil Nadu, India",
        ]

    def _graph_center(self, node_frame) -> tuple[float, float]:
        return float(node_frame.y.mean()), float(node_frame.x.mean())

    def _parse_coordinate_query(self, query: str) -> tuple[float, float] | None:
        match = re.fullmatch(
            r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*",
            query,
        )
        if not match:
            return None

        lat = float(match.group(1))
        lon = float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise IngestionError("Coordinates are out of range. Use lat,lon in WGS84.")
        return lat, lon


osm_ingestion_service = OSMIngestionService()
