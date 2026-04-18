from __future__ import annotations

import math
from typing import Any


EARTH_RADIUS_M = 6_371_000.0
DEFAULT_PREDICTION_STEPS = 5
DEFAULT_STEP_DT_S = 1.0
VELOCITY_GAIN = 0.45
POSITION_GAIN = 0.35


prediction_memory: dict[int, dict[str, Any]] = {}


def _project_local_xy(
    lat: float,
    lon: float,
    ref_lat: float,
    ref_lon: float,
) -> tuple[float, float]:
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


def _inverse_project_local_xy(
    x: float,
    y: float,
    ref_lat: float,
    ref_lon: float,
) -> tuple[float, float]:
    lat = ref_lat + math.degrees(y / EARTH_RADIUS_M)
    lon = ref_lon + math.degrees(
        x / (EARTH_RADIUS_M * max(math.cos(math.radians(ref_lat)), 1e-9))
    )
    return lat, lon


def _velocity_components(speed_mps: float, bearing_deg: float) -> tuple[float, float]:
    bearing_rad = math.radians(bearing_deg % 360.0)
    vx = speed_mps * math.sin(bearing_rad)
    vy = speed_mps * math.cos(bearing_rad)
    return vx, vy


def _vehicle_id(vehicle: dict[str, Any]) -> int:
    value = vehicle.get("vehicle_id", vehicle.get("id"))
    if value is None:
        raise ValueError("Vehicle is missing vehicle_id/id.")
    return int(value)


def predict_trajectory(
    vehicle: dict[str, Any],
    steps: int = DEFAULT_PREDICTION_STEPS,
    step_dt_s: float = DEFAULT_STEP_DT_S,
) -> dict[str, Any]:
    """Predict a short future trajectory using a lightweight Kalman-style update.

    Internal state vector: [x, y, vx, vy]
    Output ``future_positions`` is a list of [lat, lon] pairs for map rendering.
    """
    vehicle_id = _vehicle_id(vehicle)
    lat = float(vehicle.get("lat", 0.0))
    lon = float(vehicle.get("lon", 0.0))
    speed_mps = float(vehicle.get("speed", vehicle.get("speed_mps", 0.0)) or 0.0)
    bearing = float(vehicle.get("bearing", 0.0) or 0.0)
    timestamp = float(vehicle.get("timestamp", 0.0) or 0.0)

    measured_vx, measured_vy = _velocity_components(speed_mps=speed_mps, bearing_deg=bearing)
    previous = prediction_memory.get(vehicle_id)

    x = 0.0
    y = 0.0
    vx = measured_vx
    vy = measured_vy

    if previous is not None:
        previous_timestamp = float(previous.get("timestamp", timestamp))
        dt = max(timestamp - previous_timestamp, 0.0)

        prev_ref_lat = float(previous["ref_lat"])
        prev_ref_lon = float(previous["ref_lon"])
        meas_x_prev, meas_y_prev = _project_local_xy(
            lat=lat,
            lon=lon,
            ref_lat=prev_ref_lat,
            ref_lon=prev_ref_lon,
        )

        pred_x = float(previous["x"]) + float(previous["vx"]) * dt
        pred_y = float(previous["y"]) + float(previous["vy"]) * dt

        fused_x_prev = pred_x + POSITION_GAIN * (meas_x_prev - pred_x)
        fused_y_prev = pred_y + POSITION_GAIN * (meas_y_prev - pred_y)

        if dt > 1e-6:
            observed_vx = fused_x_prev / dt
            observed_vy = fused_y_prev / dt
            measured_vx = (measured_vx + observed_vx) / 2.0
            measured_vy = (measured_vy + observed_vy) / 2.0

        vx = float(previous["vx"]) + VELOCITY_GAIN * (measured_vx - float(previous["vx"]))
        vy = float(previous["vy"]) + VELOCITY_GAIN * (measured_vy - float(previous["vy"]))

    future_positions: list[list[float]] = []
    pred_x = x
    pred_y = y
    for _ in range(max(1, steps)):
        pred_x += vx * step_dt_s
        pred_y += vy * step_dt_s
        pred_lat, pred_lon = _inverse_project_local_xy(
            x=pred_x,
            y=pred_y,
            ref_lat=lat,
            ref_lon=lon,
        )
        future_positions.append([round(pred_lat, 7), round(pred_lon, 7)])

    prediction_memory[vehicle_id] = {
        "x": x,
        "y": y,
        "vx": vx,
        "vy": vy,
        "ref_lat": lat,
        "ref_lon": lon,
        "timestamp": timestamp,
    }

    vehicle["future_positions"] = future_positions
    vehicle["prediction_state"] = [round(x, 3), round(y, 3), round(vx, 3), round(vy, 3)]
    return vehicle


def reset_prediction_memory() -> None:
    prediction_memory.clear()
