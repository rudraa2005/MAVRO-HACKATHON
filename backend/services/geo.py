from __future__ import annotations

import math
import random
from bisect import bisect_right
from typing import Any

from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge


EARTH_RADIUS_M = 6_371_000


def compute_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)

    x = math.sin(delta_lambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - (
        math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    )
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


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


def add_noise(
    lat: float,
    lon: float,
    max_noise_m: float,
    rng: random.Random | None = None,
) -> tuple[float, float]:
    if max_noise_m <= 0:
        return lat, lon

    active_rng = rng or random
    distance = active_rng.uniform(0.0, max_noise_m)
    bearing = active_rng.uniform(0.0, 360.0)
    return move_coordinate(lat, lon, distance, bearing)


def move_coordinate(lat: float, lon: float, distance_m: float, bearing_deg: float) -> tuple[float, float]:
    angular_distance = distance_m / EARTH_RADIUS_M
    bearing = math.radians(bearing_deg)
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)

    new_lat = math.asin(
        math.sin(lat_rad) * math.cos(angular_distance)
        + math.cos(lat_rad) * math.sin(angular_distance) * math.cos(bearing)
    )
    new_lon = lon_rad + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat_rad),
        math.cos(angular_distance) - math.sin(lat_rad) * math.sin(new_lat),
    )
    return math.degrees(new_lat), math.degrees(new_lon)


def parse_osm_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, list):
        return any(parse_osm_bool(item) for item in value)
    if value is None:
        return False
    return str(value).strip().lower() in {"yes", "true", "1", "forward", "backward"}


def flatten_osm_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value)


def line_to_path(
    geometry: LineString | MultiLineString | None,
    fallback_start: tuple[float, float],
    fallback_end: tuple[float, float],
) -> list[dict[str, float]]:
    if geometry is None:
        return [
            {"lat": fallback_start[0], "lon": fallback_start[1]},
            {"lat": fallback_end[0], "lon": fallback_end[1]},
        ]

    if isinstance(geometry, MultiLineString):
        geometry = linemerge(geometry)
        if isinstance(geometry, MultiLineString):
            longest = max(geometry.geoms, key=lambda geom: geom.length)
            geometry = LineString(longest.coords)

    return [{"lat": lat, "lon": lon} for lon, lat in geometry.coords]


def representative_lat_lon(geometry: Any) -> tuple[float, float]:
    if isinstance(geometry, Point):
        return geometry.y, geometry.x
    point = geometry.representative_point()
    return point.y, point.x


def cumulative_path_lengths(path: list[dict[str, float]]) -> list[float]:
    distances = [0.0]
    running_total = 0.0
    for index in range(1, len(path)):
        prev = path[index - 1]
        current = path[index]
        running_total += haversine_distance_m(
            prev["lat"],
            prev["lon"],
            current["lat"],
            current["lon"],
        )
        distances.append(running_total)
    return distances


def interpolate_path_position(
    path: list[dict[str, float]],
    cumulative_lengths: list[float],
    distance_m: float,
) -> tuple[float, float]:
    if not path:
        raise ValueError("Path is empty")
    if len(path) == 1 or distance_m <= 0:
        return path[0]["lat"], path[0]["lon"]
    if distance_m >= cumulative_lengths[-1]:
        return path[-1]["lat"], path[-1]["lon"]

    upper_index = bisect_right(cumulative_lengths, distance_m)
    lower_index = max(0, upper_index - 1)
    lower_distance = cumulative_lengths[lower_index]
    upper_distance = cumulative_lengths[upper_index]
    lower_point = path[lower_index]
    upper_point = path[upper_index]

    if math.isclose(upper_distance, lower_distance):
        return upper_point["lat"], upper_point["lon"]

    ratio = (distance_m - lower_distance) / (upper_distance - lower_distance)
    lat = lower_point["lat"] + (upper_point["lat"] - lower_point["lat"]) * ratio
    lon = lower_point["lon"] + (upper_point["lon"] - lower_point["lon"]) * ratio
    return lat, lon


def path_bearing_at(
    path: list[dict[str, float]],
    cumulative_lengths: list[float],
    distance_m: float,
    direction: int,
) -> float:
    if len(path) < 2:
        return 0.0

    clamped = min(max(distance_m, 0.0), cumulative_lengths[-1])
    upper_index = min(max(bisect_right(cumulative_lengths, clamped), 1), len(path) - 1)
    lower_index = upper_index - 1

    if direction >= 0:
        start = path[lower_index]
        end = path[upper_index]
    else:
        start = path[upper_index]
        end = path[lower_index]

    return compute_bearing(start["lat"], start["lon"], end["lat"], end["lon"])
