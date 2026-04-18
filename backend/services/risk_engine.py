from __future__ import annotations

from typing import Any


def _semantic_class(vehicle: dict[str, Any]) -> str:
    return str(
        vehicle.get("semantic_class", vehicle.get("class", "normal")) or "normal"
    ).lower()


def evaluate_vehicle_risk(vehicle: dict[str, Any]) -> dict[str, Any]:
    """Assign a unified risk level from semantic, spatial, and memory signals."""
    temporal_state = str(vehicle.get("temporal_state", "NORMAL") or "NORMAL").upper()
    risk_score = float(vehicle.get("risk_score", 0.0) or 0.0)
    ttc = vehicle.get("ttc")
    ttc_value = float(ttc) if ttc is not None else None
    semantic_class = _semantic_class(vehicle)

    if ttc_value is not None and ttc_value < 2.0:
        risk_level = "critical"
    elif (
        risk_score > 5.0
        or temporal_state == "CONFIRMED"
        or semantic_class == "wrong_way"
    ):
        risk_level = "high"
    elif (
        temporal_state == "SUSPECT"
        or semantic_class == "risky"
        or (ttc_value is not None and ttc_value < 5.0)
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
