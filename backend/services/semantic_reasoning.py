"""
semantic_reasoning.py
=====================
Classifies vehicle behaviour and computes wrong-way confidence scores.

Key additions
-------------
* sigmoid_confidence()       -- smooth probabilistic scorer (no hard constants)
* WrongWayStateMachine       -- per-vehicle temporal hysteresis FSM
* wrong_way_fsm              -- module-level singleton registry
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPEED_THRESHOLD: float = 8.0          # m/s  (~28 km/h)

# Hysteresis FSM thresholds
SUSPECT_THRESHOLD:  float = 0.50      # score must exceed this to enter SUSPECT
CONFIRM_THRESHOLD:  float = 0.70      # score must exceed this to enter CONFIRMED
EXIT_THRESHOLD:     float = 0.40      # score must stay below this to revert

# Minimum *sustained* durations (seconds) for each transition
SUSPECT_MIN_DURATION:  float = 1.0   # NORMAL  -> SUSPECT  : score > 0.5 for 1.0 s
CONFIRM_MIN_DURATION:  float = 2.5   # SUSPECT -> CONFIRMED: score > 0.7 for 2.5 s
EXIT_MIN_DURATION:     float = 2.0   # CONFIRMED -> SUSPECT: score < 0.4 for 2.0 s
SUSPECT_EXIT_DURATION: float = 1.5   # SUSPECT  -> NORMAL  : score < 0.5 for 1.5 s

# Variance suppression: if normalised heading variance exceeds this, CONFIRMED
# is suppressed back to SUSPECT (GPS noise / roundabout artefact guard)
VARIANCE_SUPPRESS_THRESHOLD: float = 0.55


# ---------------------------------------------------------------------------
# Smooth probabilistic confidence scorer (shared with engine.py)
# ---------------------------------------------------------------------------

def sigmoid_confidence(
    direction_similarity: float,
    dev_time: float,
    speed: float,
    temporal_variance: float = 0.0,
) -> float:
    """Return a wrong-way confidence score in [0, 1] via sigmoid.

    Inputs
    ------
    direction_similarity : cosine similarity of vehicle heading vs road [-1, 1]
        +1 = perfectly aligned, -1 = perfectly opposed
    dev_time             : seconds the vehicle has sustained a deviation [0, inf)
    speed                : vehicle speed in m/s [0, inf)
    temporal_variance    : heading/direction variance, normalised to [0, 1]
        higher = more unstable / noisy signal (suppresses confidence)

    Formula
    -------
    direction_mismatch = 1 - direction_similarity      in [0, 2]
    speed_norm         = speed / 10                    in [0, inf)
    time_norm          = min(dev_time / 5, 1.0)        in [0, 1]

    logit = 2.5 * direction_mismatch
          + 1.2 * time_norm
          + 0.8 * speed_norm
          - 1.0 * temporal_variance

    confidence = sigmoid(logit) = 1 / (1 + exp(-logit))
    """
    direction_mismatch = 1.0 - float(max(-1.0, min(1.0, direction_similarity)))
    speed_norm         = float(speed) / 10.0
    time_norm          = min(float(dev_time) / 5.0, 1.0)
    variance_term      = float(max(0.0, temporal_variance))

    logit = (
        2.5 * direction_mismatch
        + 1.2 * time_norm
        + 0.8 * speed_norm
        - 1.0 * variance_term
    )

    # Numerically stable sigmoid (avoids overflow for large |logit|)
    if logit >= 0:
        confidence = 1.0 / (1.0 + math.exp(-logit))
    else:
        e = math.exp(logit)
        confidence = e / (1.0 + e)

    return round(float(max(0.0, min(1.0, confidence))), 4)


# ---------------------------------------------------------------------------
# Temporal hysteresis state machine
# ---------------------------------------------------------------------------

class WrongWayFSMState:
    """States for the wrong-way finite state machine."""
    NORMAL    = "NORMAL"
    SUSPECT   = "SUSPECT"
    CONFIRMED = "CONFIRMED"


@dataclass
class VehicleFSM:
    """Per-vehicle FSM record.  All times in seconds (wall-clock or sim-time)."""

    state: str = WrongWayFSMState.NORMAL

    # Time spent continuously above/below the active threshold
    time_above_suspect:  float = 0.0   # how long score has been > SUSPECT_THRESHOLD
    time_above_confirm:  float = 0.0   # how long score has been > CONFIRM_THRESHOLD
    time_below_exit:     float = 0.0   # how long score has been < EXIT_THRESHOLD
    time_below_suspect:  float = 0.0   # how long score has been < SUSPECT_THRESHOLD


class WrongWayStateMachine:
    """Per-vehicle temporal hysteresis FSM registry.

    Logic
    -----
    NORMAL
      score > SUSPECT_THRESHOLD for >= SUSPECT_MIN_DURATION
        => SUSPECT

    SUSPECT
      score > CONFIRM_THRESHOLD for >= CONFIRM_MIN_DURATION
        => CONFIRMED
      score < SUSPECT_THRESHOLD for >= SUSPECT_EXIT_DURATION
        => NORMAL  (abort early — not sustained enough)

    CONFIRMED
      score < EXIT_THRESHOLD for >= EXIT_MIN_DURATION
        => SUSPECT  (hysteresis: hard to leave once confirmed)
      variance > VARIANCE_SUPPRESS_THRESHOLD
        => suppressed to SUSPECT for this frame (state not written back)

    No single-frame transitions are possible because every transition requires
    a *sustained* duration accumulator to reach its minimum.
    """

    def __init__(self) -> None:
        self._vehicles: Dict[int, VehicleFSM] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        vehicle_id: int,
        score: float,
        dt: float,
        variance: float = 0.0,
    ) -> str:
        """Update FSM for one vehicle and return the effective state string.

        Parameters
        ----------
        vehicle_id : unique int identifier
        score      : current wrong-way confidence in [0, 1]
        dt         : elapsed seconds since last call (simulation tick interval)
        variance   : normalised heading variance in [0, 1]

        Returns
        -------
        One of: WrongWayFSMState.NORMAL | SUSPECT | CONFIRMED
        """
        fsm = self._vehicles.setdefault(vehicle_id, VehicleFSM())
        dt  = max(0.0, float(dt))

        # ── Accumulate time counters ──────────────────────────────────
        if score > SUSPECT_THRESHOLD:
            fsm.time_above_suspect += dt
            fsm.time_below_suspect  = 0.0
        else:
            fsm.time_above_suspect  = 0.0
            fsm.time_below_suspect += dt

        if score > CONFIRM_THRESHOLD:
            fsm.time_above_confirm += dt
        else:
            fsm.time_above_confirm  = 0.0

        if score < EXIT_THRESHOLD:
            fsm.time_below_exit += dt
        else:
            fsm.time_below_exit  = 0.0

        # ── State transitions ─────────────────────────────────────────
        current = fsm.state

        if current == WrongWayFSMState.NORMAL:
            if fsm.time_above_suspect >= SUSPECT_MIN_DURATION:
                fsm.state = WrongWayFSMState.SUSPECT
                # Reset confirm accumulator on entry to SUSPECT
                fsm.time_above_confirm = 0.0

        elif current == WrongWayFSMState.SUSPECT:
            if fsm.time_above_confirm >= CONFIRM_MIN_DURATION:
                fsm.state = WrongWayFSMState.CONFIRMED
                fsm.time_below_exit = 0.0
            elif fsm.time_below_suspect >= SUSPECT_EXIT_DURATION:
                # Score dropped: abort back to NORMAL
                fsm.state = WrongWayFSMState.NORMAL
                fsm.time_above_suspect = 0.0
                fsm.time_above_confirm = 0.0

        elif current == WrongWayFSMState.CONFIRMED:
            if fsm.time_below_exit >= EXIT_MIN_DURATION:
                # Hysteresis exit: drop back only to SUSPECT, not NORMAL directly.
                # Reset time_below_suspect so the SUSPECT->NORMAL abort timer
                # doesn't fire immediately in the same tick.
                fsm.state = WrongWayFSMState.SUSPECT
                fsm.time_above_confirm  = 0.0
                fsm.time_below_exit     = 0.0
                fsm.time_below_suspect  = 0.0   # prevents instant SUSPECT->NORMAL

        # ── Variance suppression ──────────────────────────────────────
        # High sensor noise masks a CONFIRMED state for this frame only.
        # The underlying fsm.state is NOT changed — suppression is transient.
        effective_state = fsm.state
        if (
            effective_state == WrongWayFSMState.CONFIRMED
            and float(variance) > VARIANCE_SUPPRESS_THRESHOLD
        ):
            effective_state = WrongWayFSMState.SUSPECT

        return effective_state

    def get_state(self, vehicle_id: int) -> str:
        """Return the current FSM state for a vehicle (NORMAL if unseen)."""
        return self._vehicles.get(vehicle_id, VehicleFSM()).state

    def reset(self, vehicle_id: int) -> None:
        """Clear FSM state for a vehicle (e.g. on reset/reseed)."""
        self._vehicles.pop(vehicle_id, None)

    def clear_all(self) -> None:
        """Clear all vehicle states (e.g. on simulation stop)."""
        self._vehicles.clear()


# Module-level singleton — imported by engine.py and direction_intelligence.py
wrong_way_fsm = WrongWayStateMachine()


# ---------------------------------------------------------------------------
# Core semantic reasoning (uses FSM for wrong-way classification)
# ---------------------------------------------------------------------------

def semantic_reasoning(vehicles: List[dict], dt: float) -> List[dict]:
    """Classify vehicle behaviour and compute smooth confidence scores.

    Updates each vehicle dict in-place.  Fields written:
        class, confidence, reason, severity, is_high_risk,
        wrong_way_state (NORMAL | SUSPECT | CONFIRMED)
    """
    for vehicle in vehicles:
        vid = vehicle.get("id", 0)

        # 1. Maintain temporal deviation accumulator
        if "deviation_time" not in vehicle:
            vehicle["deviation_time"] = 0.0

        if vehicle.get("wrong_way_flag", False):
            vehicle["deviation_time"] += dt
        else:
            vehicle["deviation_time"] = max(0.0, vehicle["deviation_time"] - dt)

        dev_time = vehicle["deviation_time"]

        # 2. Compute speed
        vx    = vehicle.get("vx", 0.0)
        vy    = vehicle.get("vy", 0.0)
        speed = math.sqrt(vx**2 + vy**2)

        # 3. Normalise angle (radians -> degrees if needed)
        angle_dev = vehicle.get("angle_dev", 0.0)
        if 0.0 < angle_dev <= math.pi:
            angle_dev = math.degrees(angle_dev)

        # 4. Stationary edge case
        if speed == 0.0:
            vehicle["class"]           = "normal"
            vehicle["wrong_way_state"] = wrong_way_fsm.update(vid, 0.0, dt, 0.0)
            vehicle["confidence"]      = sigmoid_confidence(
                direction_similarity=1.0,
                dev_time=0.0,
                speed=0.0,
                temporal_variance=float(vehicle.get("variance", 0.0)),
            )
            vehicle["reason"]      = "Vehicle is stationary"
            vehicle["is_high_risk"] = False
            vehicle["severity"]    = "low"
            continue

        # 5. Classification rules
        classification = "normal"
        reason         = "Normal driving behavior"

        if angle_dev > 150 and dev_time > 3:
            classification = "wrong_way"
            reason = f"Sustained opposite direction >3s (dev: {dev_time:.1f}s)"
        elif angle_dev > 150 and dev_time <= 2:
            classification = "u_turn"
            reason = "High angle deviation but short duration; possible U-turn"
        elif speed > SPEED_THRESHOLD and angle_dev > 30:
            classification = "risky"
            reason = "High speed with significant angular deviation"
        elif angle_dev > 150 and 2 < dev_time <= 3:
            classification = "risky"
            reason = "Transitioning to wrong-way behavior"

        # Low-speed wrong-way filter
        if classification == "wrong_way" and speed < 2:
            classification = "risky"
            reason = "Low-speed reverse movement (not critical)"

        # 6. Sigmoid confidence score
        direction_similarity = math.cos(math.radians(max(0.0, min(180.0, angle_dev))))
        temporal_variance    = float(vehicle.get("variance", 0.0))
        confidence = sigmoid_confidence(
            direction_similarity=direction_similarity,
            dev_time=dev_time,
            speed=speed,
            temporal_variance=temporal_variance,
        )

        # 7. Temporal hysteresis FSM
        #    FSM state overrides the rule-based classification for wrong_way:
        #    only CONFIRMED state triggers is_high_risk.
        fsm_state = wrong_way_fsm.update(
            vehicle_id=vid,
            score=confidence,
            dt=dt,
            variance=temporal_variance,
        )

        # Map FSM state -> classification override
        if classification == "wrong_way" and fsm_state != WrongWayFSMState.CONFIRMED:
            # Rule-based said wrong_way but FSM hasn't confirmed yet — demote
            classification = "risky"
            reason = f"Wrong-way candidate ({fsm_state.lower()}, awaiting confirmation)"
        elif fsm_state == WrongWayFSMState.CONFIRMED and classification in {"risky", "normal"}:
            # FSM has confirmed wrong-way even if brief rule-based dip occurred
            classification = "wrong_way"
            reason = "Confirmed wrong-way by temporal hysteresis"

        # 8. Severity
        if classification == "wrong_way":
            severity = "high"
        elif classification in {"risky", "u_turn"}:
            severity = "medium"
        else:
            severity = "low"

        # 9. Write output
        vehicle["class"]           = classification
        vehicle["confidence"]      = confidence
        vehicle["reason"]          = reason
        vehicle["severity"]        = severity
        vehicle["is_high_risk"]    = classification == "wrong_way"
        vehicle["wrong_way_state"] = fsm_state

        print(
            f"[Semantic] ID:{vid} -> {classification} "
            f"| conf={confidence:.3f} | fsm={fsm_state} ({reason})"
        )

    return vehicles


def run_semantic(vehicles: List[dict], dt: float) -> List[dict]:
    """Integration-safe wrapper.  Usage: vehicles = run_semantic(vehicles, dt=0.5)"""
    try:
        return semantic_reasoning(vehicles, dt)
    except Exception as exc:
        print("[Semantic ERROR]", exc)
        return vehicles
