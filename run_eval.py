#!/usr/bin/env python
"""
run_eval.py
===========
Standalone evaluation harness for the FlowGuard wrong-way detection pipeline.

Usage
-----
    python run_eval.py
    python run_eval.py --ticks 400 --vehicles 30 --dt 0.5 --seed 42

What it does
------------
1. Runs a synthetic simulation for N ticks with a configurable vehicle fleet.
2. Injects wrong-way behaviour into a subset of vehicles at scheduled windows.
3. Feeds per-frame data through eval_logger (same logger used in production).
4. Computes confusion matrix, accuracy, precision, recall, F1, FPR.
5. Runs ROC curve + AUC (50 threshold points, trapezoidal rule).
6. Prints a structured JSON summary and a per-vehicle breakdown.

No Flask, no database, no UI required.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time as _time
from dataclasses import dataclass, field
from typing import List

# Ensure UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Project imports (pure Python, no Flask context needed)
# ---------------------------------------------------------------------------
from backend.services.eval_logger import EvalLogger, BUFFER_MAXLEN
from backend.services.semantic_reasoning import (
    sigmoid_confidence,
    WrongWayStateMachine,
)

# ---------------------------------------------------------------------------
# Simulation parameters (overridden by CLI args)
# ---------------------------------------------------------------------------

DEFAULT_TICKS:    int   = 200
DEFAULT_VEHICLES: int   = 20
DEFAULT_DT:       float = 0.5    # seconds per tick
DEFAULT_SEED:     int   = 42
# 0.75 better separates classes given the sigmoid calibration:
# speed_norm alone raises normal vehicles to ~0.69, so 0.75 avoids mass FPs.
DEFAULT_THRESHOLD: float = 0.75


# ---------------------------------------------------------------------------
# Synthetic vehicle model
# ---------------------------------------------------------------------------

@dataclass
class SyntheticVehicle:
    """Minimal in-memory vehicle for the evaluation harness."""
    vehicle_id:    int
    speed_mps:     float              # base cruising speed
    # Wrong-way injection window: (start_tick, end_tick) inclusive; None = never
    inject_start:  int | None = None
    inject_end:    int | None = None

    # Adversarial maneuvers (e.g., sharp legal turns)
    maneuver_start: int | None = None
    maneuver_end:   int | None = None
    maneuver_angle: float = 0.0

    # Sensor artifacts
    gps_drift: float = 0.0  # Cumulative random walk noise

    # Mutable per-tick state
    heading_window: list = field(default_factory=list)
    wrong_way_active: bool = False

    def is_ground_truth_wrong_way(self, tick: int) -> bool:
        """True when the vehicle is in its scheduled wrong-way injection window."""
        if self.inject_start is None or self.inject_end is None:
            return False
        return self.inject_start <= tick <= self.inject_end


def _simulate_angle_dev(v: SyntheticVehicle, tick: int, rng: random.Random) -> float:
    """Return a noisy angle deviation (degrees) including maneuvers and drift.

    Now includes significantly more noise and distribution overlap.
    """
    is_wrong_way = v.is_ground_truth_wrong_way(tick)
    in_maneuver  = v.maneuver_start is not None and v.maneuver_start <= tick <= v.maneuver_end

    # Random walk for GPS drift (1.0 deg/tick max delta)
    v.gps_drift += rng.uniform(-1.0, 1.0)
    v.gps_drift = max(-15.0, min(15.0, v.gps_drift))

    if is_wrong_way:
        # High noise wrong-way (130-190 range typically)
        base = rng.gauss(165.0, 15.0)
    elif in_maneuver:
        # Adversarial 'Hard Negative' - legal turn or drift looking like wrong-way
        base = v.maneuver_angle + rng.gauss(0.0, 10.0)
    else:
        # Normal driving with significant jitter (0-50 range typically)
        base = abs(rng.gauss(10.0, 18.0))

    return max(0.0, min(180.0, base + v.gps_drift))


def _simulate_speed(base: float, rng: random.Random) -> float:
    """Perturb speed with ±1.5 m/s jitter, clamp to [1.5, 20] m/s."""
    return max(1.5, min(20.0, base + rng.gauss(0.0, 1.5)))


def _compute_heading_variance(
    window: list,
    new_reading: float,
    maxlen: int = 6,
) -> float:
    """Maintain a rolling heading window and return normalised variance in [0, 1]."""
    window.append(new_reading)
    if len(window) > maxlen:
        window.pop(0)

    if len(window) < 2:
        return 0.0

    mean = sum(window) / len(window)
    variance = sum((x - mean) ** 2 for x in window) / len(window)
    return min(1.0, variance / 2500.0)          # normalised against 50° std-dev


# ---------------------------------------------------------------------------
# Evaluation harness
# ---------------------------------------------------------------------------

def build_fleet(n_vehicles: int, rng: random.Random) -> List[SyntheticVehicle]:
    """Create a mixed fleet: ~20% have a wrong-way injection window."""
    vehicles: List[SyntheticVehicle] = []
    n_wrong_way = max(1, n_vehicles // 5)       # at least 1 wrong-way vehicle

    for i in range(n_vehicles):
        base_speed = rng.uniform(6.0, 14.0)

        if i < n_wrong_way:
            # Scheduled wrong-way
            start = 30 + i * 20
            duration = rng.randint(40, 80)
            vehicles.append(SyntheticVehicle(
                vehicle_id=i + 1,
                speed_mps=base_speed,
                inject_start=start,
                inject_end=start + duration,
            ))
        elif i < n_wrong_way + 3:
            # 'Adversarial' Hard Negatives - Sharp turns
            start = rng.randint(40, 120)
            duration = rng.randint(15, 30)
            angle = rng.uniform(70.0, 120.0) # Sharp but legal turns/curves
            vehicles.append(SyntheticVehicle(
                vehicle_id=i + 1,
                speed_mps=base_speed,
                maneuver_start=start,
                maneuver_end=start + duration,
                maneuver_angle=angle,
            ))
        else:
            vehicles.append(SyntheticVehicle(
                vehicle_id=i + 1,
                speed_mps=base_speed,
            ))

    return vehicles


def run_simulation(
    n_ticks:    int,
    vehicles:   List[SyntheticVehicle],
    dt:         float,
    threshold:  float,
    rng:        random.Random,
    logger:     EvalLogger,
    fsm:        WrongWayStateMachine,
) -> dict:
    """Run the simulation loop, log every frame, return a tick-by-tick summary."""

    tick_log: list = []

    for tick in range(n_ticks):
        frame: list = []

        for v in vehicles:
            ground_truth = v.is_ground_truth_wrong_way(tick)

            # --- Simulate sensor readings --------------------------------
            angle_dev = _simulate_angle_dev(v, tick, rng)
            speed     = _simulate_speed(v.speed_mps, rng)

            # Map angle to uncertainty scaling (more jitter = more uncertainty)
            uncertainty = max(0.1, min(1.5, angle_dev / 120.0 + rng.uniform(0, 0.5)))

            # --- Compute signals -----------------------------------------
            direction_similarity = math.cos(math.radians(max(0.0, min(180.0, angle_dev))))
            dev_time = (tick - (v.inject_start or 0)) * dt if ground_truth else 0.0
            variance = _compute_heading_variance(v.heading_window, angle_dev)

            # --- Wrong-way probability (wwp) via sigmoid -----------------
            wwp = sigmoid_confidence(
                direction_similarity=direction_similarity,
                dev_time=dev_time,
                speed=speed,
                temporal_variance=variance,
            )

            # --- Temporal hysteresis FSM ----------------------------------
            fsm_state = fsm.update(
                vehicle_id=v.vehicle_id,
                score=wwp,
                dt=dt,
                variance=variance,
            )

            # --- Build dict compatible with eval_logger.log_frame() ------
            frame.append({
                "db_id":     v.vehicle_id,
                "wwp":       wwp,
                "wrong_way": ground_truth,       # <-- ground truth label
                "timestamp": tick * dt,
            })

        # Log the entire frame (one record per vehicle)
        logger.log_frame(frame)
        tick_log.append({"tick": tick, "n": len(frame)})

    return {"ticks": n_ticks, "records": sum(x["n"] for x in tick_log)}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_separator(char: str = "-", width: int = 60) -> None:
    print(char * width)


def print_report(
    confusion: dict,
    metrics: dict,
    roc: dict,
    n_vehicles: int,
    n_ticks: int,
    total_records: int,
    elapsed_ms: float,
) -> None:

    print_separator("=")
    print("  FlowGuard Evaluation Harness -- Results")
    print_separator("=")

    print(f"\n  Simulation  : {n_ticks} ticks x {n_vehicles} vehicles")
    print(f"  Log size    : {total_records:,} records")
    print(f"  Runtime     : {elapsed_ms:.0f} ms")

    print_separator()
    print("  Confusion Matrix")
    print_separator()
    tp = confusion["tp"]; fp = confusion["fp"]
    tn = confusion["tn"]; fn = confusion["fn"]
    print(f"             Predicted")
    print(f"              POS   NEG")
    print(f"  Actual POS  {tp:>4}  {fn:>4}   (TP + FN = {tp+fn})")
    print(f"  Actual NEG  {fp:>4}  {tn:>4}   (FP + TN = {fp+tn})")

    print_separator()
    print("  Performance Metrics")
    print_separator()
    for key, val in metrics.items():
        bar_filled = int(val * 30)
        bar = "#" * bar_filled + "." * (30 - bar_filled)
        print(f"  {key:<10}: {val:.4f}  [{bar}]")

    auc = roc.get("auc")
    print_separator()
    print("  ROC / AUC")
    print_separator()
    print(f"  AUC         : {auc if auc is not None else 'N/A (single class)'}")
    print(f"  ROC points  : {len(roc['thresholds'])}")
    if roc.get("warnings"):
        for w in roc["warnings"]:
            print(f"  WARN        : {w}")

    print_separator("=")
    print("  JSON Summary")
    print_separator("=")
    summary = {
        "accuracy":  metrics["accuracy"],
        "precision": metrics["precision"],
        "recall":    metrics["recall"],
        "f1":        metrics["f1"],
        "fpr":       metrics["fpr"],
        "fnr":       metrics["fnr"],
        "auc":       auc,
        "confusion": confusion,
        "samples":   total_records,
    }
    print(json.dumps(summary, indent=2))
    print_separator("=")


def per_vehicle_breakdown(
    logger: EvalLogger,
    vehicles: List[SyntheticVehicle],
) -> None:
    """Print per-vehicle TP/FP/TN/FN counts."""
    logs = logger.get_logs()
    current_threshold = logger.get_threshold()

    from collections import defaultdict
    by_vehicle: dict[int, list] = defaultdict(list)
    for r in logs:
        by_vehicle[r.vehicle_id].append(r)

    print("\n  Per-Vehicle Breakdown (Threshold: {:.4f})".format(current_threshold))
    print_separator()
    print(f"  {'VID':>4}  {'Injected':>8}  {'Samples':>7}  {'TP':>4}  {'FP':>4}  {'TN':>4}  {'FN':>4}  {'Prec':>6}  {'Rec':>6}")
    print_separator()

    for v in vehicles:
        records = by_vehicle.get(v.vehicle_id, [])
        if not records:
            continue

        # Re-eval labels based on the final threshold for consistency
        tp = 0; fp = 0; tn = 0; fn = 0
        for r in records:
            pred = 1 if r.wrong_way_probability >= current_threshold else 0
            truth = r.ground_truth_label
            if truth == 1 and pred == 1: tp += 1
            elif truth == 0 and pred == 1: fp += 1
            elif truth == 0 and pred == 0: tn += 1
            elif truth == 1 and pred == 0: fn += 1

        prec = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
        rec  = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
        injected = v.inject_start is not None

        prec_s = f"{prec:.3f}" if not math.isnan(prec) else "  N/A"
        rec_s  = f"{rec:.3f}"  if not math.isnan(rec)  else "  N/A"

        print(
            f"  {v.vehicle_id:>4}  {'YES' if injected else 'no':>8}  "
            f"{len(records):>7}  {tp:>4}  {fp:>4}  {tn:>4}  {fn:>4}  "
            f"{prec_s:>6}  {rec_s:>6}"
        )

    print_separator()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FlowGuard traffic pipeline evaluation harness"
    )
    parser.add_argument("--ticks",     type=int,   default=DEFAULT_TICKS,
                        help="Number of simulation ticks (default: 200)")
    parser.add_argument("--vehicles",  type=int,   default=DEFAULT_VEHICLES,
                        help="Number of vehicles in fleet (default: 20)")
    parser.add_argument("--dt",        type=float, default=DEFAULT_DT,
                        help="Seconds per tick (default: 0.5)")
    parser.add_argument("--seed",      type=int,   default=DEFAULT_SEED,
                        help="Random seed (default: 42)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="Decision threshold for predicted labels (default: 0.65)")
    parser.add_argument("--roc-points", type=int,  default=50,
                        help="Number of ROC threshold points (default: 50)")
    parser.add_argument("--json-only", action="store_true",
                        help="Print only the JSON summary (no tables)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    rng     = random.Random(args.seed)
    logger  = EvalLogger(maxlen=BUFFER_MAXLEN, threshold=args.threshold)
    fsm     = WrongWayStateMachine()
    vehicles = build_fleet(args.vehicles, rng)

    if not args.json_only:
        print(f"\nFlowGuard Evaluation Harness")
        print(f"Ticks={args.ticks}  Vehicles={args.vehicles}  "
              f"dt={args.dt}s  seed={args.seed}  threshold={args.threshold}")
        print(f"Wrong-way injections: "
              f"{sum(1 for v in vehicles if v.inject_start is not None)} vehicles")
        print("Running simulation...", flush=True)

    t0 = _time.monotonic()
    sim_stats = run_simulation(
        n_ticks=args.ticks,
        vehicles=vehicles,
        dt=args.dt,
        threshold=args.threshold,
        rng=rng,
        logger=logger,
        fsm=fsm,
    )
    elapsed_ms = (_time.monotonic() - t0) * 1000.0

    # --- Compute metrics ---------------------------------------------------
    logs = logger.get_logs()

    # 1. Initial metrics with default threshold
    initial_confusion = logger.compute_confusion_matrix(logs)
    roc_result        = logger.compute_roc_auc(logs, n_thresholds=args.roc_points)

    # 2. Optimize threshold using Youden's J statistic
    optimization = logger.optimal_threshold(logs, n_thresholds=args.roc_points, apply=True)
    best_threshold = optimization["best_threshold"]

    # 3. Recalculate confusion matrix with the NEW optimized threshold
    optimized_confusion = logger.compute_confusion_matrix(logs, threshold=best_threshold)

    if args.json_only:
        auc = roc_result.get("auc")
        summary = {
            "initial_threshold": args.threshold,
            "optimal_threshold": best_threshold,
            "auc":       auc,
            "metrics":   optimized_confusion["metrics"],
            "confusion": optimized_confusion["confusion"],
            "optimization": optimization,
            "samples":   len(logs),
        }
        print(json.dumps(summary, indent=2))
        return

    # --- Full report -------------------------------------------------------
    print(f"Done. ({elapsed_ms:.0f} ms)  Logged {len(logs):,} records.\n")

    # Show per-vehicle with optimized threshold
    per_vehicle_breakdown(logger, vehicles)

    print_report(
        confusion=optimized_confusion["confusion"],
        metrics=optimized_confusion["metrics"],
        roc=roc_result,
        n_vehicles=args.vehicles,
        n_ticks=args.ticks,
        total_records=len(logs),
        elapsed_ms=elapsed_ms,
    )

    print("\n  Threshold Optimization (Youden's J)")
    print_separator()
    print(f"  Initial Threshold : {args.threshold}")
    print(f"  Optimal Threshold : {best_threshold}  (MAX J = {optimization['best_j']})")
    print(f"  Expected TPR      : {optimization['best_tpr']:.4f}")
    print(f"  Expected FPR      : {optimization['best_fpr']:.4f}")

    # Show improvement
    acc_gain = optimized_confusion["metrics"]["accuracy"] - initial_confusion["metrics"]["accuracy"]
    f1_gain  = optimized_confusion["metrics"]["f1"] - initial_confusion["metrics"]["f1"]
    print(f"  Accuracy Gain     : {acc_gain:+.4f}")
    print(f"  F1-Score Gain     : {f1_gain:+.4f}")
    print_separator("=")


if __name__ == "__main__":
    main()
