from __future__ import annotations

from collections import Counter
import re
from typing import Any

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from backend.models.road import _USE_POSTGIS

if _USE_POSTGIS:
    from geoalchemy2.shape import from_shape

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
    def search_candidates(
        self,
        query: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        import osmnx as ox

        return self._search_candidates(ox, query=query, limit=limit)

    def ingest_query(
        self,
        query: str,
        query_type: str = "auto",
        radius_m: int = 700,
        reset: bool = False,
        selection: dict[str, Any] | None = None,
    ) -> dict[str, int | float | str | bool]:
        import osmnx as ox

        normalized_query_type = (query_type or "auto").strip().lower()
        resolved = self._resolve_osm_payload(
            ox=ox,
            query=query,
            query_type=normalized_query_type,
            radius_m=radius_m,
            selection=selection,
        )
        graph = resolved["graph"]
        features = resolved["features"]
        center_lat = resolved["center_lat"]
        center_lon = resolved["center_lon"]
        scope = resolved["scope"]
        resolved_query = resolved["resolved_query"]
        selected_candidate = resolved["selected_candidate"]

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
            "selected_candidate": selected_candidate,
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
            geom_kwargs = {}
            if _USE_POSTGIS:
                geom_kwargs["geom"] = from_shape(
                    LineString([(p["lon"], p["lat"]) for p in path]),
                    srid=4326,
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
                    **geom_kwargs,
                )
            )
        return segments

    def _resolve_osm_payload(
        self,
        ox,
        query: str,
        query_type: str,
        radius_m: int,
        selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if selection is not None:
            candidate = self._normalize_candidate_selection(selection)
        else:
            search_result = self._search_candidates(ox, query=query, limit=5)
            if not search_result["candidates"]:
                message = (
                    f'Could not resolve "{query}" to an OSM location. '
                    "Try a more specific street or neighborhood."
                )
                if search_result["suggestions"]:
                    message += f" Suggestions: {', '.join(search_result['suggestions'])}."
                if search_result["attempted"]:
                    attempted = ", ".join(search_result["attempted"][:6])
                    message += f" Attempted: {attempted}."
                if search_result["errors"]:
                    message += f" Upstream detail: {search_result['errors'][0]}."
                raise IngestionError(message)
            candidate = search_result["candidates"][0]

        return self._resolve_candidate_payload(ox, candidate=candidate, radius_m=radius_m)

    def _search_candidates(
        self,
        ox,
        query: str,
        limit: int,
    ) -> dict[str, Any]:
        max_results = max(1, min(limit, 8))
        coordinate = self._parse_coordinate_query(query)
        if coordinate is not None:
            return {
                "query": query,
                "candidates": [self._coordinate_candidate(query, coordinate)],
                "attempted": [f"coordinates:{query.strip()}"],
                "errors": [],
                "suggestions": [],
            }

        candidate_queries = self._candidate_queries(query)
        attempted: list[str] = []
        errors: list[str] = []
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()

        for candidate_query in candidate_queries:
            for which_result in range(1, max_results + 1):
                attempted.append(f"search:{candidate_query}#{which_result}")
                try:
                    gdf = ox.geocode_to_gdf(candidate_query, which_result=which_result)
                except Exception as exc:
                    errors.append(f"{candidate_query}#{which_result}: {exc}")
                    break

                row = gdf.iloc[0]
                candidate = self._candidate_from_row(candidate_query, which_result, row)
                candidate_key = self._candidate_key(candidate)
                if candidate_key in seen:
                    continue

                seen.add(candidate_key)
                candidates.append(candidate)
                if len(candidates) >= max_results:
                    break
            if len(candidates) >= max_results:
                break

        return {
            "query": query,
            "candidates": candidates,
            "attempted": attempted,
            "errors": errors,
            "suggestions": self._suggestions_for(query),
        }

    def _resolve_candidate_payload(
        self,
        ox,
        candidate: dict[str, Any],
        radius_m: int,
    ) -> dict[str, Any]:
        center_lat = float(candidate["lat"])
        center_lon = float(candidate["lon"])
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
        node_frame = ox.graph_to_gdfs(graph, nodes=True, edges=False)
        center_lat, center_lon = self._graph_center(node_frame)
        return {
            "graph": graph,
            "features": features,
            "center_lat": center_lat,
            "center_lon": center_lon,
            "scope": "street_area",
            "resolved_query": candidate["display_name"],
            "selected_candidate": candidate,
        }

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

    def _candidate_from_row(
        self,
        candidate_query: str,
        which_result: int,
        row,
    ) -> dict[str, Any]:
        geometry = row.geometry
        geometry_type = geometry.geom_type if geometry is not None else "Unknown"
        osm_type = self._row_text(row, "osm_type")
        osm_id = self._coerce_optional_int(row.get("osm_id"))
        candidate = {
            "id": self._candidate_identifier(candidate_query, which_result, osm_type, osm_id),
            "candidate_query": candidate_query,
            "which_result": which_result,
            "display_name": self._row_text(row, "display_name") or candidate_query,
            "lat": float(row.get("lat")),
            "lon": float(row.get("lon")),
            "geometry_type": geometry_type,
            "match_mode": "area" if geometry_type in {"Polygon", "MultiPolygon"} else "point",
            "osm_type": osm_type,
            "osm_id": osm_id,
            "importance": float(row.get("importance") or 0.0),
            "class_name": self._row_text(row, "class"),
            "type_name": self._row_text(row, "type"),
            "bbox": {
                "north": float(row.get("bbox_north")),
                "south": float(row.get("bbox_south")),
                "east": float(row.get("bbox_east")),
                "west": float(row.get("bbox_west")),
            },
        }
        return candidate

    def _coordinate_candidate(
        self,
        query: str,
        coordinate: tuple[float, float],
    ) -> dict[str, Any]:
        lat, lon = coordinate
        return {
            "id": f"coordinates:{lat:.6f},{lon:.6f}",
            "candidate_query": query.strip(),
            "which_result": 1,
            "display_name": f"Coordinates {lat:.5f}, {lon:.5f}",
            "lat": lat,
            "lon": lon,
            "geometry_type": "Point",
            "match_mode": "point",
            "osm_type": None,
            "osm_id": None,
            "importance": 1.0,
            "class_name": "coordinates",
            "type_name": "manual",
            "bbox": {
                "north": lat,
                "south": lat,
                "east": lon,
                "west": lon,
            },
        }

    def _normalize_candidate_selection(self, selection: dict[str, Any]) -> dict[str, Any]:
        if selection.get("candidate_query") is None and selection.get("display_name") is None:
            raise IngestionError("No location candidate selected.")

        if selection.get("match_mode") == "point" and selection.get("class_name") == "coordinates":
            coordinate = self._parse_coordinate_query(selection.get("candidate_query", ""))
            if coordinate is None:
                lat = float(selection["lat"])
                lon = float(selection["lon"])
                coordinate = (lat, lon)
            return self._coordinate_candidate(selection.get("candidate_query", ""), coordinate)

        candidate_query = str(selection.get("candidate_query") or selection.get("display_name") or "")
        which_result = int(selection.get("which_result") or 1)
        display_name = selection.get("display_name") or candidate_query
        return {
            "id": selection.get("id") or self._candidate_identifier(candidate_query, which_result, selection.get("osm_type"), self._coerce_optional_int(selection.get("osm_id"))),
            "candidate_query": candidate_query,
            "which_result": which_result,
            "display_name": str(display_name),
            "lat": float(selection.get("lat")),
            "lon": float(selection.get("lon")),
            "geometry_type": selection.get("geometry_type") or "Unknown",
            "match_mode": selection.get("match_mode") or "point",
            "osm_type": selection.get("osm_type"),
            "osm_id": self._coerce_optional_int(selection.get("osm_id")),
            "importance": float(selection.get("importance") or 0.0),
            "class_name": selection.get("class_name"),
            "type_name": selection.get("type_name"),
            "bbox": selection.get("bbox") or {},
        }

    def _candidate_identifier(
        self,
        candidate_query: str,
        which_result: int,
        osm_type: str | None,
        osm_id: int | None,
    ) -> str:
        osm_type_part = osm_type or "none"
        osm_id_part = str(osm_id) if osm_id is not None else "none"
        return f"{candidate_query}|{which_result}|{osm_type_part}|{osm_id_part}"

    def _candidate_key(self, candidate: dict[str, Any]) -> str:
        if candidate.get("osm_type") and candidate.get("osm_id") is not None:
            return f"{candidate['osm_type']}:{candidate['osm_id']}"
        return candidate["id"]

    def _row_text(self, row, key: str) -> str | None:
        value = row.get(key)
        if value is None:
            return None
        return str(value)

    def _coerce_optional_int(self, value: Any) -> int | None:
        if value is None:
            return None
        try:
            if str(value).lower() == "nan":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

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
