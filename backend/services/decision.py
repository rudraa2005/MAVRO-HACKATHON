from __future__ import annotations

from typing import List


def decision_layer(vehicles: List[dict]) -> List[dict]:
    """
    Final decision layer for alert-ready vehicle objects.

    Inputs:
    - collision_probability
    - risk_score_refined
    - temporal_state

    Output:
    - alert in {"SAFE", "WARNING", "HIGH_ALERT", "COLLISION_ALERT"}
    """
    for vehicle in vehicles:
        collision_probability = float(vehicle.get("collision_probability", 0.0) or 0.0)
        ml_collision_probability = float(vehicle.get("ml_collision_probability", 0.0) or 0.0)
        refined_score = float(vehicle.get("risk_score_refined", 0.0) or 0.0)
        anomaly_score = float(vehicle.get("anomaly_score", 0.0) or 0.0)
        temporal_state = str(vehicle.get("temporal_state", "NORMAL") or "NORMAL").upper()

        if collision_probability > 0.7 or ml_collision_probability > 0.7:
            vehicle["alert"] = "COLLISION_ALERT"
        elif refined_score > 6.0 or ml_collision_probability > 0.55:
            vehicle["alert"] = "HIGH_ALERT"
        elif temporal_state == "SUSPECT" or anomaly_score > 0.58:
            vehicle["alert"] = "WARNING"
        else:
            vehicle["alert"] = "SAFE"

    return vehicles


def run_decision(vehicles: List[dict]) -> List[dict]:
    """Safe pipeline wrapper for the decision engine."""
    try:
        return decision_layer(vehicles)
    except Exception as exc:
        print("[Decision ERROR]", exc)
        return vehicles


def debug_print_pipeline(vehicles: List[dict]) -> None:
    """Utility function to show the upgraded pipeline state."""
    print("=" * 72)
    for vehicle in vehicles:
        print(
            f"ID:{vehicle.get('id', 'N/A')} | "
            f"risk_refined:{round(vehicle.get('risk_score_refined', 0.0), 2)} | "
            f"collision_prob:{round(vehicle.get('collision_probability', 0.0), 2)} | "
            f"time_to_action:{vehicle.get('time_to_action', 'None')} | "
            f"alert:{vehicle.get('alert', 'missing')}"
        )
    print("=" * 72)
