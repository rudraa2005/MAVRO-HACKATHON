from __future__ import annotations

import math
from typing import Any


EARTH_RADIUS_M = 6_371_000.0
MAX_INTERACTION_DISTANCE_M = 50.0


def _project_local_xy(
    lat: float,
    lon: float,
    ref_lat: float,
    ref_lon: float,
) -> tuple[float, float]:
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


def _velocity_components(speed_mps: float, bearing_deg: float) -> tuple[float, float]:
    bearing_rad = math.radians(bearing_deg % 360.0)
    vx = speed_mps * math.sin(bearing_rad)
    vy = speed_mps * math.cos(bearing_rad)
    return vx, vy


def _risk_from_ttc(ttc: float, danger_ttc_s: float, risky_ttc_s: float) -> str:
    if ttc < danger_ttc_s:
        return "danger"
    if ttc < risky_ttc_s:
        return "risky"
    return "safe"


def _vehicle_id(vehicle: dict[str, Any]) -> int | None:
    value = vehicle.get("vehicle_id", vehicle.get("id"))
    return int(value) if value is not None else None


def compute_spatial(
    vehicles: list[dict[str, Any]],
    settings: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    cfg = settings or {}
    max_interaction_distance_m = float(
        cfg.get("max_interaction_distance_m", MAX_INTERACTION_DISTANCE_M)
    )
    danger_ttc_s = float(cfg.get("ttc_danger_s", 2.0))
    risky_ttc_s = float(cfg.get("ttc_risky_s", 5.0))

    """Compute pairwise TTC and collision risk from live positions and velocities.

    Expected per vehicle:
    - ``lat``, ``lon`` in WGS84
    - ``speed`` or ``speed_mps`` in metres/second
    - ``bearing`` in degrees
    - ``vehicle_id`` or ``id``
    """
    if not vehicles:
        return vehicles

    ref_lat = sum(float(vehicle.get("lat", 0.0)) for vehicle in vehicles) / len(vehicles)
    ref_lon = sum(float(vehicle.get("lon", 0.0)) for vehicle in vehicles) / len(vehicles)

    prepared: list[dict[str, Any]] = []
    for vehicle in vehicles:
        lat = float(vehicle.get("lat", 0.0))
        lon = float(vehicle.get("lon", 0.0))
        speed_mps = float(vehicle.get("speed", vehicle.get("speed_mps", 0.0)) or 0.0)
        bearing = float(vehicle.get("bearing", 0.0) or 0.0)
        x, y = _project_local_xy(lat=lat, lon=lon, ref_lat=ref_lat, ref_lon=ref_lon)
        vx, vy = _velocity_components(speed_mps=speed_mps, bearing_deg=bearing)

        vehicle["ttc"] = None
        vehicle["risk"] = "safe"
        vehicle["collision_with"] = None
        prepared.append(
            {
                "vehicle": vehicle,
                "id": _vehicle_id(vehicle),
                "x": x,
                "y": y,
                "vx": vx,
                "vy": vy,
            }
        )

    count = len(prepared)
    for i in range(count):
        vehicle_a = prepared[i]
        for j in range(i + 1, count):
            vehicle_b = prepared[j]

            dx = vehicle_b["x"] - vehicle_a["x"]
            dy = vehicle_b["y"] - vehicle_a["y"]
            distance = math.hypot(dx, dy)
            if distance > max_interaction_distance_m:
                continue

            dvx = vehicle_b["vx"] - vehicle_a["vx"]
            dvy = vehicle_b["vy"] - vehicle_a["vy"]
            dot = dx * dvx + dy * dvy
            if dot >= 0.0:
                continue

            rel_speed_sq = dvx * dvx + dvy * dvy
            if rel_speed_sq <= 1e-9:
                continue

            ttc = -dot / rel_speed_sq
            risk = _risk_from_ttc(ttc, danger_ttc_s=danger_ttc_s, risky_ttc_s=risky_ttc_s)
            rounded_ttc = round(ttc, 2)

            a = vehicle_a["vehicle"]
            b = vehicle_b["vehicle"]

            if a["ttc"] is None or ttc < float(a["ttc"]):
                a["ttc"] = rounded_ttc
                a["risk"] = risk
                a["collision_with"] = vehicle_b["id"]

            if b["ttc"] is None or ttc < float(b["ttc"]):
                b["ttc"] = rounded_ttc
                b["risk"] = risk
                b["collision_with"] = vehicle_a["id"]

    return vehicles


def run_spatial(vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Safe pipeline wrapper for TTC spatial tracking."""
    try:
        return compute_spatial(vehicles)
    except Exception as exc:
        print("[Spatial ERROR]", exc)
        return vehicles
