from __future__ import annotations

from typing import Any


def _semantic_class(vehicle: dict[str, Any]) -> str:
    return str(
        vehicle.get("semantic_class", vehicle.get("class", "normal")) or "normal"
    ).lower()


def evaluate_vehicle_risk(
    vehicle: dict[str, Any],
    settings: dict[str, float] | None = None,
) -> dict[str, Any]:
    cfg = settings or {}
    ttc_critical_s = float(cfg.get("ttc_critical_s", 2.0))
    ttc_medium_s = float(cfg.get("ttc_medium_s", 5.0))
    risk_score_high_threshold = float(cfg.get("risk_score_high_threshold", 5.0))

    """Assign a unified risk level from semantic, spatial, and memory signals."""
    temporal_state = str(vehicle.get("temporal_state", "NORMAL") or "NORMAL").upper()
    risk_score = float(vehicle.get("risk_score", 0.0) or 0.0)
    ttc = vehicle.get("ttc")
    ttc_value = float(ttc) if ttc is not None else None
    semantic_class = _semantic_class(vehicle)

    if ttc_value is not None and ttc_value < ttc_critical_s:
        risk_level = "critical"
    elif (
        risk_score > risk_score_high_threshold
        or temporal_state == "CONFIRMED"
        or semantic_class == "wrong_way"
    ):
        risk_level = "high"
    elif (
        temporal_state == "SUSPECT"
        or semantic_class == "risky"
        or (ttc_value is not None and ttc_value < ttc_medium_s)
    ):
        risk_level = "medium"
    else:
        risk_level = "low"

    vehicle["risk_level"] = risk_level
    return vehicle


def run_risk_engine(
    vehicles: list[dict[str, Any]],
    settings: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    for vehicle in vehicles:
        evaluate_vehicle_risk(vehicle, settings=settings)
    return vehicles
