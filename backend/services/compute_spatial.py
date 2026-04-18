from __future__ import annotations

from collections import defaultdict, deque
import math
from typing import Any

import numpy as np


EARTH_RADIUS_M = 6_371_000.0
MAX_INTERACTION_DISTANCE_M = 50.0
DEFAULT_COLLISION_DISTANCE_M = 4.0
DEFAULT_MONTE_CARLO_SIMS = 75
DEFAULT_MONTE_CARLO_HORIZON_S = 4.0
DEFAULT_MONTE_CARLO_STEPS = 6
DEFAULT_REACTION_TIME_S = 1.0
ACTION_NAMES = ("brake", "accelerate", "swerve_left", "swerve_right")


def _project_local_xy(
    lat: float,
    lon: float,
    ref_lat: float,
    ref_lon: float,
) -> tuple[float, float]:
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


def _project_local_xy_array(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    ref_lat: float,
    ref_lon: float,
) -> tuple[np.ndarray, np.ndarray]:
    x = np.radians(longitudes - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = np.radians(latitudes - ref_lat) * EARTH_RADIUS_M
    return x, y


def _velocity_components(speed_mps: float, bearing_deg: float) -> tuple[float, float]:
    bearing_rad = math.radians(bearing_deg % 360.0)
    vx = speed_mps * math.sin(bearing_rad)
    vy = speed_mps * math.cos(bearing_rad)
    return vx, vy


def _velocity_components_array(
    speeds_mps: np.ndarray,
    bearings_deg: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    bearing_rad = np.radians(np.mod(bearings_deg, 360.0))
    vx = speeds_mps * np.sin(bearing_rad)
    vy = speeds_mps * np.cos(bearing_rad)
    return vx, vy


def _risk_from_ttc(ttc: float) -> str:
    if ttc < 2.0:
        return "danger"
    if ttc < 5.0:
        return "risky"
    return "safe"


def _vehicle_id(vehicle: dict[str, Any]) -> int | None:
    value = vehicle.get("vehicle_id", vehicle.get("id"))
    return int(value) if value is not None else None


def _vehicle_seed(vehicle_a: dict[str, Any], vehicle_b: dict[str, Any]) -> int:
    id_a = int(_vehicle_id(vehicle_a) or 0)
    id_b = int(_vehicle_id(vehicle_b) or 0)
    low = min(id_a, id_b)
    high = max(id_a, id_b)
    return ((low * 73_856_093) ^ (high * 19_349_663)) & 0xFFFFFFFF


def _pair_monte_carlo_metrics(
    vehicle_a: dict[str, Any],
    vehicle_b: dict[str, Any],
    n: int = DEFAULT_MONTE_CARLO_SIMS,
    collision_distance_m: float = DEFAULT_COLLISION_DISTANCE_M,
    horizon_s: float = DEFAULT_MONTE_CARLO_HORIZON_S,
    steps: int = DEFAULT_MONTE_CARLO_STEPS,
) -> tuple[float, float]:
    rng = np.random.default_rng(_vehicle_seed(vehicle_a, vehicle_b))
    times = np.linspace(0.5, horizon_s, max(2, steps))

    # Fix 1: Scale noise with speed (faster = more uncertainty)
    speed_a = float(vehicle_a.get("speed", vehicle_a.get("speed_mps", 0.0)) or 0.0)
    speed_b = float(vehicle_b.get("speed", vehicle_b.get("speed_mps", 0.0)) or 0.0)
    
    noise_scale_a = 0.1 + 0.02 * speed_a
    noise_scale_b = 0.1 + 0.02 * speed_b

    noise_ax = rng.normal(0.0, noise_scale_a, size=(n, 1))
    noise_ay = rng.normal(0.0, noise_scale_a, size=(n, 1))
    noise_bx = rng.normal(0.0, noise_scale_b, size=(n, 1))
    noise_by = rng.normal(0.0, noise_scale_b, size=(n, 1))

    # Fix 2: Dynamic collision radius based on combined speed
    base_radius = collision_distance_m
    dynamic_radius = base_radius + 0.1 * (speed_a + speed_b)

    pos_a_x = float(vehicle_a["x"]) + (float(vehicle_a["vx"]) + noise_ax) * times
    pos_a_y = float(vehicle_a["y"]) + (float(vehicle_a["vy"]) + noise_ay) * times
    pos_b_x = float(vehicle_b["x"]) + (float(vehicle_b["vx"]) + noise_bx) * times
    pos_b_y = float(vehicle_b["y"]) + (float(vehicle_b["vy"]) + noise_by) * times

    distances = np.sqrt((pos_b_x - pos_a_x) ** 2 + (pos_b_y - pos_a_y) ** 2)
    
    # Fix 3: Smooth collision detection
    # Using exponential decay for a less "harsh" binary cutoff
    collision_prob_matrix = np.exp(-distances / dynamic_radius)
    collision_hits = np.any(collision_prob_matrix > 0.5, axis=1)
    
    collision_probability = float(np.mean(collision_hits))

    if collision_probability > 0.05:
        print(f"[MC DEBUG] Pair A:{vehicle_a.get('id')} B:{vehicle_b.get('id')} | Sims: {n} | Prob: {collision_probability:.3f} | Radius: {dynamic_radius:.1f}m")

    simulated_positions = np.stack(
        [pos_a_x, pos_a_y, pos_b_x, pos_b_y],
        axis=-1,
    )
    uncertainty = float(np.var(simulated_positions))
    return collision_probability, uncertainty


def monte_carlo_collision(
    vehicle_a: dict[str, Any],
    vehicle_b: dict[str, Any],
    n: int = DEFAULT_MONTE_CARLO_SIMS,
) -> float:
    """Estimate probabilistic collision likelihood under uncertainty."""
    collision_probability, _ = _pair_monte_carlo_metrics(vehicle_a, vehicle_b, n=n)
    return collision_probability


def _simulate_action_collision(
    vehicle: dict[str, Any],
    counterpart: dict[str, Any],
    new_velocity: tuple[float, float],
    horizon_s: float = DEFAULT_MONTE_CARLO_HORIZON_S,
    steps: int = DEFAULT_MONTE_CARLO_STEPS,
    collision_distance_m: float = DEFAULT_COLLISION_DISTANCE_M,
) -> bool:
    times = np.linspace(0.5, horizon_s, max(2, steps))
    vx, vy = new_velocity
    x = float(vehicle["x"]) + vx * times
    y = float(vehicle["y"]) + vy * times
    other_x = float(counterpart["x"]) + float(counterpart["vx"]) * times
    other_y = float(counterpart["y"]) + float(counterpart["vy"]) * times
    distances = np.sqrt((other_x - x) ** 2 + (other_y - y) ** 2)
    return bool(np.any(distances < collision_distance_m))


def simulate_actions(
    vehicle: dict[str, Any],
    counterpart: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Simulate driver interventions to determine avoidability."""
    if counterpart is None:
        return {
            "safe_actions": list(ACTION_NAMES),
            "unsafe_actions": [],
        }

    vx = float(vehicle.get("vx", 0.0))
    vy = float(vehicle.get("vy", 0.0))
    actions = {
        "brake": (vx * 0.5, vy * 0.5),
        "accelerate": (vx * 1.2, vy * 1.2),
        "swerve_left": (-vy, vx),
        "swerve_right": (vy, -vx),
    }

    safe_actions: list[str] = []
    unsafe_actions: list[str] = []
    for action_name, action_velocity in actions.items():
        collision = _simulate_action_collision(
            vehicle=vehicle,
            counterpart=counterpart,
            new_velocity=action_velocity,
        )
        if collision:
            unsafe_actions.append(action_name)
        else:
            safe_actions.append(action_name)

    return {
        "safe_actions": safe_actions,
        "unsafe_actions": unsafe_actions,
    }


def _cluster_sizes(collision_graph: dict[int, list[int]]) -> dict[int, int]:
    cluster_sizes: dict[int, int] = {}
    seen: set[int] = set()

    for vehicle_id in collision_graph:
        if vehicle_id in seen:
            continue
        queue: deque[int] = deque([vehicle_id])
        component: list[int] = []
        seen.add(vehicle_id)

        while queue:
            current = queue.popleft()
            component.append(current)
            for neighbor in collision_graph[current]:
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                queue.append(neighbor)

        size = max(len(component), 1)
        for node in component:
            cluster_sizes[node] = size

    return cluster_sizes


def _should_replace_pair(
    vehicle: dict[str, Any],
    *,
    candidate_probability: float,
    candidate_ttc: float,
) -> bool:
    current_probability = float(vehicle.get("collision_probability", 0.0) or 0.0)
    current_ttc = vehicle.get("ttc")
    current_ttc_value = float(current_ttc) if current_ttc is not None else math.inf

    if candidate_probability > current_probability + 1e-9:
        return True
    if abs(candidate_probability - current_probability) <= 1e-9 and candidate_ttc < current_ttc_value:
        return True
    return vehicle.get("collision_with") is None


def compute_spatial(vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute TTC, probabilistic collision risk, and interaction structure.

    This stage upgrades the spatial model into a hybrid physics and simulation
    module while remaining lightweight enough for edge deployment.
    """
    if not vehicles:
        return vehicles

    latitudes = np.array([float(vehicle.get("lat", 0.0)) for vehicle in vehicles], dtype=float)
    longitudes = np.array([float(vehicle.get("lon", 0.0)) for vehicle in vehicles], dtype=float)
    speeds = np.array(
        [float(vehicle.get("speed", vehicle.get("speed_mps", 0.0)) or 0.0) for vehicle in vehicles],
        dtype=float,
    )
    bearings = np.array([float(vehicle.get("bearing", 0.0) or 0.0) for vehicle in vehicles], dtype=float)

    ref_lat = float(np.mean(latitudes))
    ref_lon = float(np.mean(longitudes))
    xs, ys = _project_local_xy_array(latitudes, longitudes, ref_lat=ref_lat, ref_lon=ref_lon)
    vxs, vys = _velocity_components_array(speeds_mps=speeds, bearings_deg=bearings)

    prepared: list[dict[str, Any]] = []
    vehicle_lookup: dict[int, dict[str, Any]] = {}
    collision_graph: defaultdict[int, list[int]] = defaultdict(list)

    for index, vehicle in enumerate(vehicles):
        vehicle_id = _vehicle_id(vehicle)
        x = float(xs[index])
        y = float(ys[index])
        vx = float(vxs[index])
        vy = float(vys[index])

        vehicle["x"] = round(x, 3)
        vehicle["y"] = round(y, 3)
        vehicle["vx"] = round(vx, 3)
        vehicle["vy"] = round(vy, 3)
        vehicle["ttc"] = None
        vehicle["risk"] = "safe"
        vehicle["collision_with"] = None
        vehicle["distance"] = None
        vehicle["relative_speed"] = 0.0
        vehicle["collision_probability"] = 0.0
        vehicle["uncertainty"] = 0.0
        vehicle["collision_neighbors"] = []
        vehicle["cluster_size"] = 1
        vehicle["safe_actions"] = list(ACTION_NAMES)
        vehicle["unsafe_actions"] = []
        vehicle["time_to_action"] = None

        prepared_vehicle = {
            "vehicle": vehicle,
            "id": vehicle_id,
            "x": x,
            "y": y,
            "vx": vx,
            "vy": vy,
        }
        prepared.append(prepared_vehicle)
        if vehicle_id is not None:
            vehicle_lookup[vehicle_id] = prepared_vehicle
            collision_graph[vehicle_id]

    if len(prepared) == 1:
        return vehicles

    x_matrix = xs[np.newaxis, :] - xs[:, np.newaxis]
    y_matrix = ys[np.newaxis, :] - ys[:, np.newaxis]
    distance_matrix = np.hypot(x_matrix, y_matrix)

    dvx_matrix = vxs[np.newaxis, :] - vxs[:, np.newaxis]
    dvy_matrix = vys[np.newaxis, :] - vys[:, np.newaxis]
    dot_matrix = x_matrix * dvx_matrix + y_matrix * dvy_matrix
    rel_speed_sq_matrix = dvx_matrix * dvx_matrix + dvy_matrix * dvy_matrix

    upper_mask = np.triu(np.ones(distance_matrix.shape, dtype=bool), k=1)
    valid_mask = (
        upper_mask
        & (distance_matrix <= MAX_INTERACTION_DISTANCE_M)
        & (dot_matrix < 0.0)
        & (rel_speed_sq_matrix > 1e-9)
    )

    ttc_matrix = np.full(distance_matrix.shape, np.inf, dtype=float)
    ttc_matrix[valid_mask] = -dot_matrix[valid_mask] / rel_speed_sq_matrix[valid_mask]

    for i, j in np.argwhere(valid_mask):
        ttc = float(ttc_matrix[i, j])
        distance = float(distance_matrix[i, j])
        risk = _risk_from_ttc(ttc)
        relative_speed = float(math.sqrt(rel_speed_sq_matrix[i, j]))

        prepared_a = prepared[i]
        prepared_b = prepared[j]
        vehicle_a = prepared_a["vehicle"]
        vehicle_b = prepared_b["vehicle"]

        collision_probability, uncertainty = _pair_monte_carlo_metrics(
            prepared_a,
            prepared_b,
            n=DEFAULT_MONTE_CARLO_SIMS,
        )

        if _should_replace_pair(
            vehicle_a,
            candidate_probability=collision_probability,
            candidate_ttc=ttc,
        ):
            vehicle_a["ttc"] = round(ttc, 2)
            vehicle_a["risk"] = risk
            vehicle_a["distance"] = round(distance, 2)
            vehicle_a["relative_speed"] = round(relative_speed, 3)
            vehicle_a["collision_with"] = prepared_b["id"]
            vehicle_a["collision_probability"] = round(collision_probability, 4)
            vehicle_a["uncertainty"] = round(uncertainty, 4)

        if _should_replace_pair(
            vehicle_b,
            candidate_probability=collision_probability,
            candidate_ttc=ttc,
        ):
            vehicle_b["ttc"] = round(ttc, 2)
            vehicle_b["risk"] = risk
            vehicle_b["distance"] = round(distance, 2)
            vehicle_b["relative_speed"] = round(relative_speed, 3)
            vehicle_b["collision_with"] = prepared_a["id"]
            vehicle_b["collision_probability"] = round(collision_probability, 4)
            vehicle_b["uncertainty"] = round(uncertainty, 4)

        if prepared_a["id"] is not None and prepared_b["id"] is not None:
            collision_graph[prepared_a["id"]].append(prepared_b["id"])
            collision_graph[prepared_b["id"]].append(prepared_a["id"])

    cluster_sizes = _cluster_sizes(collision_graph)

    for vehicle in vehicles:
        vehicle_id = _vehicle_id(vehicle)
        if vehicle_id is not None:
            neighbors = sorted(set(collision_graph[vehicle_id]))
            vehicle["collision_neighbors"] = neighbors
            vehicle["cluster_size"] = cluster_sizes.get(vehicle_id, 1)

        counterpart_id = vehicle.get("collision_with")
        counterpart = vehicle_lookup.get(int(counterpart_id)) if counterpart_id is not None else None
        action_result = simulate_actions(vehicle, counterpart=counterpart)
        vehicle["safe_actions"] = action_result["safe_actions"]
        vehicle["unsafe_actions"] = action_result["unsafe_actions"]

        if vehicle["ttc"] is not None:
            vehicle["time_to_action"] = round(float(vehicle["ttc"]) - DEFAULT_REACTION_TIME_S, 2)

    return vehicles


def run_spatial(vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Safe pipeline wrapper for hybrid spatial tracking."""
    try:
        return compute_spatial(vehicles)
    except Exception as exc:
        print("[Spatial ERROR]", exc)
        return vehicles
