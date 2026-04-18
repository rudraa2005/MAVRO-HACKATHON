from __future__ import annotations

import math
from typing import Any


def semantic_reasoning(
    vehicles: list[dict[str, Any]],
    dt: float,
    settings: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    cfg = settings or {}
    wrong_way_angle_deg = float(cfg.get("wrong_way_angle_deg", 150.0))
    u_turn_max_seconds = float(cfg.get("u_turn_max_seconds", 2.0))
    wrong_way_min_seconds = float(cfg.get("wrong_way_min_seconds", 3.0))
    risky_speed_threshold_mps = float(cfg.get("risky_speed_threshold_mps", 8.0))

    for vehicle in vehicles:
        temporal_state = str(vehicle.get("temporal_state", "NORMAL") or "NORMAL").upper()
        sustained_duration_s = float(vehicle.get("sustained_duration_s", 0.0) or 0.0)
        speed_mps = float(vehicle.get("speed", vehicle.get("speed_mps", 0.0)) or 0.0)
        direction_similarity = float(vehicle.get("direction_similarity", 1.0) or 1.0)
        direction_similarity = max(-1.0, min(1.0, direction_similarity))
        angle_dev_deg = math.degrees(math.acos(direction_similarity))

        wrong_way_flag = bool(vehicle.get("is_violation", False) or temporal_state == "CONFIRMED")
        if temporal_state == "CONFIRMED" or (
            angle_dev_deg >= wrong_way_angle_deg and sustained_duration_s >= wrong_way_min_seconds
        ):
            classification = "wrong_way"
            reason = "Sustained opposite-direction movement."
        elif angle_dev_deg >= wrong_way_angle_deg and sustained_duration_s <= u_turn_max_seconds:
            classification = "u_turn"
            reason = "High direction deviation with short duration."
        elif speed_mps >= risky_speed_threshold_mps and angle_dev_deg >= 30.0:
            classification = "risky"
            reason = "Elevated speed with directional instability."
        elif temporal_state == "SUSPECT":
            classification = "risky"
            reason = "Temporal layer flagged suspect movement."
        else:
            classification = "normal"
            reason = "Nominal movement pattern."

        confidence = float(vehicle.get("confidence", 0.0) or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        if classification == "wrong_way":
            severity = "high"
        elif classification in {"risky", "u_turn"}:
            severity = "medium"
        else:
            severity = "low"

        vehicle["angle_dev"] = round(angle_dev_deg, 3)
        vehicle["wrong_way_flag"] = wrong_way_flag
        vehicle["class"] = classification
        vehicle["semantic_class"] = classification if classification != "u_turn" else "risky"
        vehicle["reason"] = reason
        vehicle["severity"] = severity
        vehicle["confidence"] = round(confidence, 4)
        vehicle["is_high_risk"] = classification == "wrong_way"
        vehicle["deviation_time"] = max(0.0, sustained_duration_s if wrong_way_flag else max(sustained_duration_s - dt, 0.0))

    return vehicles


def run_semantic(
    vehicles: list[dict[str, Any]],
    dt: float,
    settings: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    try:
        return semantic_reasoning(vehicles, dt, settings=settings)
    except Exception:
        return vehicles
