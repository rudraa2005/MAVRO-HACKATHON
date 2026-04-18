from __future__ import annotations

from typing import Any


vehicle_memory: dict[int, dict[str, Any]] = {}

_MAX_HISTORY = 50
_SUSPECT_INCREMENT = 0.25
_NORMAL_DECAY = 0.15


def update_memory(
    vehicles: list[dict[str, Any]],
    settings: dict[str, float | int] | None = None,
) -> list[dict[str, Any]]:
    """Update persistent per-vehicle behavioral memory in O(n).

    Expected vehicle fields:
    - ``vehicle_id`` or ``id``
    - ``temporal_state`` in {"NORMAL", "SUSPECT", "CONFIRMED"}
    - ``sustained_duration_s``
    - ``wrong_way_flag`` (optional)
    - ``timestamp`` (optional)
    """
    cfg = settings or {}
    max_history = int(cfg.get("max_history", _MAX_HISTORY))
    suspect_increment = float(cfg.get("suspect_increment", _SUSPECT_INCREMENT))
    normal_decay = float(cfg.get("normal_decay", _NORMAL_DECAY))

    for vehicle in vehicles:
        vehicle_id = int(vehicle.get("vehicle_id", vehicle.get("id")))
        timestamp = float(vehicle.get("timestamp", 0.0) or 0.0)
        temporal_state = str(vehicle.get("temporal_state", "NORMAL")).upper()
        sustained_duration_s = float(vehicle.get("sustained_duration_s", 0.0) or 0.0)
        wrong_way_flag = bool(
            vehicle.get(
                "wrong_way_flag",
                temporal_state == "CONFIRMED" or vehicle.get("is_violation", False),
            )
        )

        memory = vehicle_memory.setdefault(
            vehicle_id,
            {
                "violation_count": 0,
                "last_seen": timestamp,
                "risk_score": 0.0,
                "history": [],
            },
        )

        memory["last_seen"] = timestamp

        if temporal_state == "CONFIRMED":
            memory["violation_count"] += 1

        base_score = memory["violation_count"] * 0.5 + sustained_duration_s * 0.3

        if temporal_state == "SUSPECT":
            risk_score = max(memory["risk_score"] + suspect_increment, base_score)
        elif temporal_state == "NORMAL":
            risk_score = max(0.0, memory["risk_score"] - normal_decay)
        else:
            risk_score = base_score

        risk_score = round(min(max(risk_score, 0.0), 10.0), 3)
        memory["risk_score"] = risk_score
        memory["history"].append(
            {
                "timestamp": timestamp,
                "temporal_state": temporal_state,
                "sustained_duration_s": sustained_duration_s,
                "wrong_way_flag": wrong_way_flag,
                "risk_score": risk_score,
                "violation_count": memory["violation_count"],
            }
        )
        if len(memory["history"]) > max_history:
            del memory["history"][:-max_history]

        vehicle["wrong_way_flag"] = wrong_way_flag
        vehicle["risk_score"] = risk_score
        vehicle["violation_count"] = memory["violation_count"]

    return vehicles


def reset_vehicle_memory() -> None:
    vehicle_memory.clear()
