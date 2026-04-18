from __future__ import annotations

from typing import Any


REACTION_TIME_S = 1.0


def _semantic_class(vehicle: dict[str, Any]) -> str:
    return str(
        vehicle.get("semantic_class", vehicle.get("class", "normal")) or "normal"
    ).lower()


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric


def compute_risk_score(vehicle: dict[str, Any]) -> float:
    """Continuous hybrid risk score without hard TTC buckets."""
    refined_score = 0.0

    ttc_value = _float_or_none(vehicle.get("ttc"))
    if ttc_value is not None:
        refined_score += (1.0 / max(ttc_value, 0.1)) * 2.0

    speed_value = _float_or_none(vehicle.get("speed", vehicle.get("speed_mps", 0.0))) or 0.0
    refined_score += speed_value / 10.0

    temporal_state = str(vehicle.get("temporal_state", "NORMAL") or "NORMAL").upper()
    if temporal_state == "CONFIRMED":
        refined_score += 2.0
    elif temporal_state == "SUSPECT":
        refined_score += 1.0

    semantic_class = _semantic_class(vehicle)
    if semantic_class == "wrong_way":
        refined_score += 3.0

    distance_value = _float_or_none(vehicle.get("distance", 50.0))
    if distance_value is not None and distance_value < 10.0:
        refined_score += 2.0

    return round(refined_score, 4)


def evaluate_vehicle_risk(vehicle: dict[str, Any]) -> dict[str, Any]:
    """Assign a unified risk level from semantic, spatial, memory, and probability signals."""
    temporal_state = str(vehicle.get("temporal_state", "NORMAL") or "NORMAL").upper()
    semantic_class = _semantic_class(vehicle)
    memory_risk_score = float(vehicle.get("risk_score", 0.0) or 0.0)
    collision_probability = float(vehicle.get("collision_probability", 0.0) or 0.0)
    ml_collision_probability = float(vehicle.get("ml_collision_probability", 0.0) or 0.0)

    refined_score = compute_risk_score(vehicle)
    vehicle["risk_score_refined"] = refined_score

    ttc_value = _float_or_none(vehicle.get("ttc"))
    if vehicle.get("time_to_action") is None and ttc_value is not None:
        vehicle["time_to_action"] = round(ttc_value - REACTION_TIME_S, 2)

    time_to_action = _float_or_none(vehicle.get("time_to_action"))

    if (
        collision_probability > 0.7
        or ml_collision_probability > 0.75
        or (time_to_action is not None and time_to_action <= 0.5)
    ):
        risk_level = "critical"
    elif (
        refined_score > 6.0
        or ml_collision_probability > 0.55
        or memory_risk_score > 5.0
        or temporal_state == "CONFIRMED"
        or semantic_class == "wrong_way"
    ):
        risk_level = "high"
    elif (
        refined_score > 3.0
        or ml_collision_probability > 0.35
        or collision_probability > 0.3
        or temporal_state == "SUSPECT"
        or semantic_class == "risky"
    ):
        risk_level = "medium"
    else:
        risk_level = "low"

    vehicle["risk_level"] = risk_level
    return vehicle


def run_risk_engine(vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for vehicle in vehicles:
        evaluate_vehicle_risk(vehicle)
    return vehicles
