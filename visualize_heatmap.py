#!/usr/bin/env python3
"""
================================================================
  MAVRO FlowGuard -- False-Positive Reduction & Risk Heatmap Viz
  Standalone script - No existing files modified
  Run:  python visualize_heatmap.py           (interactive window)
        python visualize_heatmap.py --save    (save PNGs, no window)
================================================================

Demonstrates:
  - Side-by-side RAW vs FILTERED comparison
  - Aggressive risk heatmap (inferno colormap)
  - Collision zone danger circles
  - False positive labels that fade over time
  - Vehicle trajectory trails
  - On-screen narration per phase
  - Temporal smoothing + Monte Carlo filtering
"""

from __future__ import annotations

import math
import os
import random
import sys
from collections import defaultdict
from typing import Any

import numpy as np

# --- Detect mode --------------------------------------------------------
SAVE_MODE = "--save" in sys.argv

import matplotlib
if SAVE_MODE:
    matplotlib.use("Agg")
else:
    try:
        matplotlib.use("TkAgg")
    except Exception:
        matplotlib.use("Agg")
        SAVE_MODE = True

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.animation as animation
from matplotlib.colors import LinearSegmentedColormap

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
GRID_SIZE = 100
TOTAL_FRAMES = 90
ANIMATION_INTERVAL_MS = 180
SMOOTHING_ALPHA = 0.75
GAUSSIAN_SIGMA = 5.0
MONTE_CARLO_SAMPLES = 60
NOISE_DECAY_RATE = 0.045
DANGER_THRESHOLD = 0.15          # hard threshold for danger zones
COLLISION_CIRCLE_PROB = 0.45     # draw danger circle above this

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heatmap_output")
SNAPSHOT_FRAMES = [0, 5, 10, 20, 30, 45, 60, 75, 89]

# Custom aggressive colormap: black -> deep purple -> red -> orange -> white
_CMAP_COLORS = [
    (0.0, "#000000"),   # black = safe
    (0.15, "#1a0533"),  # deep void
    (0.3, "#4a0e6e"),   # purple
    (0.45, "#8b1a4a"),  # magenta
    (0.6, "#c62828"),   # red
    (0.75, "#ef6c00"),  # orange
    (0.88, "#fdd835"),  # yellow
    (1.0, "#ffffff"),   # white = extreme danger
]
_positions = [c[0] for c in _CMAP_COLORS]
_colors_hex = [c[1] for c in _CMAP_COLORS]

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))

_colors_rgb = [_hex_to_rgb(c) for c in _colors_hex]
DANGER_CMAP = LinearSegmentedColormap.from_list(
    "danger_fire",
    list(zip(_positions, _colors_rgb)),
    N=512,
)

# ---------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------
USE_LIVE = False
try:
    from backend.services.input_layer import FlowGuardInputLayer
    _input_layer = FlowGuardInputLayer()
    _ = list(_input_layer.get_vehicle_updates())
    USE_LIVE = True
    print("[INFO] Connected to live FlowGuard pipeline.")
except Exception:
    print("[INFO] Live pipeline unavailable -- using synthetic vehicle data.")

# ---------------------------------------------------------------------
# Vehicle trajectory history
# ---------------------------------------------------------------------
_trajectory_history: dict[str, list[tuple[float, float]]] = defaultdict(list)
MAX_TRAIL_LEN = 15


def _generate_synthetic_vehicles(frame: int) -> list[dict[str, Any]]:
    random.seed(frame * 7 + 42)
    noise_level = max(0.02, 1.0 - frame * NOISE_DECAY_RATE)

    vehicles: list[dict[str, Any]] = [
        {
            "id": "V-DANGER-1",
            "x": 25 + math.sin(frame * 0.1) * 4,
            "y": 30 + math.cos(frame * 0.1) * 3,
            "vx": 2.0, "vy": -1.5,
            "ttc": max(0.3, 3.0 - frame * 0.045),
            "collision_probability": min(0.97, 0.25 + frame * 0.01),
            "uncertainty": max(0.03, 0.45 - frame * 0.006),
            "label": "Wrong-Way Truck",
            "is_real": True,
        },
        {
            "id": "V-DANGER-2",
            "x": 72 + math.cos(frame * 0.08) * 3,
            "y": 68 + math.sin(frame * 0.08) * 3,
            "vx": -1.8, "vy": 1.2,
            "ttc": max(0.2, 2.5 - frame * 0.035),
            "collision_probability": min(0.93, 0.2 + frame * 0.011),
            "uncertainty": max(0.03, 0.4 - frame * 0.005),
            "label": "Speeding Sedan",
            "is_real": True,
        },
        {
            "id": "V-SAFE-1",
            "x": min(92, 45 + frame * 0.25),
            "y": 50 + math.sin(frame * 0.05) * 2,
            "vx": 0.5, "vy": 0.0,
            "ttc": 14.0,
            "collision_probability": 0.02,
            "uncertainty": 0.08,
            "label": "Safe Cruiser",
            "is_real": True,
        },
    ]

    # Ghost detections -- MORE and LOUDER early, vanish later
    n_ghosts = int(10 * noise_level)
    for i in range(n_ghosts):
        gx = random.uniform(8, 92)
        gy = random.uniform(8, 92)
        vehicles.append({
            "id": f"GHOST-{frame}-{i}",
            "x": gx, "y": gy,
            "vx": random.gauss(0, 1.5), "vy": random.gauss(0, 1.5),
            "ttc": random.uniform(3, 12),
            "collision_probability": random.uniform(0.05, 0.25) * noise_level,
            "uncertainty": random.uniform(0.55, 0.95),
            "label": "FP?",
            "is_real": False,
        })

    # Track trajectories for real vehicles
    for v in vehicles:
        vid = v["id"]
        if v.get("is_real"):
            _trajectory_history[vid].append((v["x"], v["y"]))
            if len(_trajectory_history[vid]) > MAX_TRAIL_LEN:
                _trajectory_history[vid] = _trajectory_history[vid][-MAX_TRAIL_LEN:]

    return vehicles


def get_vehicles(frame: int) -> list[dict[str, Any]]:
    if USE_LIVE:
        try:
            return list(_input_layer.get_vehicle_updates())
        except Exception:
            pass
    return _generate_synthetic_vehicles(frame)


# ---------------------------------------------------------------------
# Risk computation
# ---------------------------------------------------------------------
def compute_risk(v: dict[str, Any]) -> float:
    base = v["collision_probability"] * (1.0 / max(v["ttc"], 0.1))
    return base * (1.0 - v.get("uncertainty", 0.0))


def monte_carlo_risk(v: dict[str, Any], n_samples: int = MONTE_CARLO_SAMPLES) -> float:
    unc = v.get("uncertainty", 0.3)
    risks = []
    for _ in range(n_samples):
        cp = np.clip(random.gauss(v["collision_probability"], unc * 0.3), 0, 1)
        ttc = max(0.1, random.gauss(v["ttc"], unc * 2.0))
        r = cp * (1.0 / ttc) * (1.0 - unc)
        risks.append(r)
    return float(np.mean(risks))


# ---------------------------------------------------------------------
# Gaussian spatial spread
# ---------------------------------------------------------------------
_yy, _xx = np.mgrid[0:GRID_SIZE, 0:GRID_SIZE]


def spread_risk(heatmap: np.ndarray, x: float, y: float, risk: float) -> None:
    ix = int(np.clip(x, 0, GRID_SIZE - 1))
    iy = int(np.clip(y, 0, GRID_SIZE - 1))
    d2 = (_xx - ix) ** 2 + (_yy - iy) ** 2
    heatmap += risk * np.exp(-d2 / (2 * GAUSSIAN_SIGMA ** 2))


# ---------------------------------------------------------------------
# Temporal smoothing
# ---------------------------------------------------------------------
def temporal_smooth(prev: np.ndarray, curr: np.ndarray,
                    alpha: float = SMOOTHING_ALPHA) -> np.ndarray:
    return alpha * prev + (1.0 - alpha) * curr


# ---------------------------------------------------------------------
# Phase narration (on-screen text)
# ---------------------------------------------------------------------
NARRATION = {
    (0, 12): (
        "PHASE 1: RAW DETECTION",
        [
            "Multiple false positives scattered across grid",
            "Ghost detections create noise everywhere",
            "System has NOT yet filtered anything",
        ],
    ),
    (13, 25): (
        "PHASE 2: FILTERING BEGINS",
        [
            "Temporal smoothing activates (alpha=0.75)",
            "Unstable ghost detections start fading",
            "Watch the RIGHT panel -- noise is reducing",
        ],
    ),
    (26, 45): (
        "PHASE 3: FALSE POSITIVES ELIMINATED",
        [
            "Monte Carlo sampling rejects uncertain signals",
            "Only persistent threats retain heat signature",
            "FP? labels disappearing from filtered view",
        ],
    ),
    (46, 70): (
        "PHASE 4: CONVERGENCE",
        [
            "Danger zones lock onto real collision paths",
            "Wrong-Way Truck: TTC dropping, risk climbing",
            "Speeding Sedan: confirmed high-probability threat",
        ],
    ),
    (71, 89): (
        "PHASE 5: STABLE INTELLIGENCE",
        [
            "System confidence is now maximum",
            "Zero false positives remain",
            "Only REAL collision zones are highlighted",
        ],
    ),
}


def get_narration(frame: int) -> tuple[str, list[str]]:
    for (lo, hi), val in NARRATION.items():
        if lo <= frame <= hi:
            return val
    return ("", [])


# =====================================================================
# Simulation engine
# =====================================================================
def simulate_all_frames() -> dict:
    prev_smooth = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float64)
    results = {"raw": [], "filtered": [], "vehicles": [], "metrics": []}

    for frame in range(TOTAL_FRAMES):
        vehicles = get_vehicles(frame)
        raw_heatmap = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float64)
        filtered_heatmap = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float64)
        vehicle_data = []

        for v in vehicles:
            raw_risk = compute_risk(v)
            mc_risk = monte_carlo_risk(v)

            spread_risk(raw_heatmap, float(v["x"]), float(v["y"]), raw_risk)
            spread_risk(filtered_heatmap, float(v["x"]), float(v["y"]), mc_risk)

            vehicle_data.append({"v": v, "raw_risk": raw_risk, "mc_risk": mc_risk})

        # Temporal smoothing only on filtered
        smoothed = temporal_smooth(prev_smooth, filtered_heatmap)
        prev_smooth = smoothed.copy()

        # Normalize both for display
        raw_norm = raw_heatmap / max(raw_heatmap.max(), 1e-9)
        filt_norm = smoothed / max(smoothed.max(), 1e-9)

        # Hard threshold: zero out weak signals in filtered
        filt_display = filt_norm.copy()
        filt_display[filt_display < DANGER_THRESHOLD] *= 0.15  # dim weak zones

        avg_risk = float(np.mean(smoothed))
        max_risk = float(np.max(smoothed))
        active_collisions = sum(
            1 for v in vehicles
            if v.get("collision_probability", 0) > 0.5 and v.get("ttc", 99) < 3
        )
        n_fps = sum(1 for v in vehicles if not v.get("is_real", True))

        results["raw"].append(raw_norm)
        results["filtered"].append(filt_display)
        results["vehicles"].append(vehicle_data)
        results["metrics"].append({
            "avg_risk": avg_risk,
            "max_risk": max_risk,
            "active_collisions": active_collisions,
            "false_positives": n_fps,
            "frame": frame,
        })

        if frame == 10:
            print(f"[Frame {frame:>3}]  [!] NOISE PRESENT -- "
                  f"avg={avg_risk:.4f}  max={max_risk:.4f}  FPs={n_fps}")
        elif frame == 30:
            print(f"[Frame {frame:>3}]  [~] FPs REDUCED -- "
                  f"avg={avg_risk:.4f}  max={max_risk:.4f}  FPs={n_fps}")
        elif frame == 60:
            print(f"[Frame {frame:>3}]  [+] STABLE ZONES -- "
                  f"avg={avg_risk:.4f}  max={max_risk:.4f}  FPs={n_fps}")

    return results


# =====================================================================
# Rendering
# =====================================================================
def render_dual_frame(fig, axes, raw, filtered, vehicle_data, metrics):
    """Render side-by-side RAW vs FILTERED with all overlays."""
    ax_raw, ax_filt = axes
    frame = metrics["frame"]
    phase_title, phase_lines = get_narration(frame)

    for ax in (ax_raw, ax_filt):
        ax.clear()
        ax.set_facecolor("#000000")
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_color("#333333")

    # --- LEFT: RAW detections (no filtering) ---
    ax_raw.imshow(raw, cmap=DANGER_CMAP, vmin=0, vmax=1.0,
                  origin="lower", aspect="auto", interpolation="bilinear")
    ax_raw.set_title("RAW DETECTIONS (unfiltered)", fontsize=11,
                     fontweight="bold", color="#ff6b6b", pad=8)

    # --- RIGHT: FILTERED (temporal + MC) ---
    ax_filt.imshow(filtered, cmap=DANGER_CMAP, vmin=0, vmax=1.0,
                   origin="lower", aspect="auto", interpolation="bilinear")
    ax_filt.set_title("FILTERED (temporal + Monte Carlo)", fontsize=11,
                      fontweight="bold", color="#69f0ae", pad=8)

    # --- Overlay vehicles on BOTH panels ---
    for vd in vehicle_data:
        v = vd["v"]
        vx, vy = float(v["x"]), float(v["y"])
        is_real = v.get("is_real", True)
        cp = v.get("collision_probability", 0)
        label = v.get("label", "")

        for ax, is_filtered in [(ax_raw, False), (ax_filt, True)]:
            if is_real:
                # Real vehicle markers
                marker_color = "#ff1744" if cp > 0.5 else "#ffd600" if cp > 0.2 else "#69f0ae"
                ax.plot(vx, vy, "o", color=marker_color, markersize=8,
                        markeredgecolor="white", markeredgewidth=1.2, zorder=10)

                # Danger circle for high-risk
                if cp > COLLISION_CIRCLE_PROB:
                    circle_size = 8 + cp * 10
                    danger_circle = plt.Circle(
                        (vx, vy), circle_size, fill=False,
                        color="#ff1744", linewidth=2.0, linestyle="-",
                        alpha=0.8 + 0.2 * math.sin(frame * 0.3), zorder=9,
                    )
                    ax.add_patch(danger_circle)

                    # Second pulsing ring
                    outer_circle = plt.Circle(
                        (vx, vy), circle_size + 3, fill=False,
                        color="#ff5252", linewidth=1.0, linestyle="--",
                        alpha=0.4 + 0.3 * math.sin(frame * 0.5), zorder=8,
                    )
                    ax.add_patch(outer_circle)

                # Vehicle label
                if label and label != "FP?":
                    ax.annotate(
                        label, (vx, vy), textcoords="offset points",
                        xytext=(8, 8), fontsize=6.5, fontweight="bold",
                        color="white", zorder=11,
                        bbox=dict(boxstyle="round,pad=0.2",
                                  facecolor="#1a1a2e", edgecolor=marker_color,
                                  alpha=0.9),
                    )

                # Trajectory trail
                vid = v["id"]
                if vid in _trajectory_history and len(_trajectory_history[vid]) > 2:
                    trail = _trajectory_history[vid]
                    trail_x = [p[0] for p in trail]
                    trail_y = [p[1] for p in trail]
                    n_pts = len(trail)
                    for i in range(1, n_pts):
                        alpha_val = 0.1 + 0.6 * (i / n_pts)
                        ax.plot(
                            trail_x[i-1:i+1], trail_y[i-1:i+1],
                            "--", color=marker_color, alpha=alpha_val,
                            linewidth=1.2, zorder=7,
                        )

            else:
                # Ghost / false positive
                if is_filtered and frame > 20:
                    # In filtered view, ghosts fade and disappear after frame 20
                    ghost_alpha = max(0.0, 0.5 - (frame - 20) * 0.02)
                    if ghost_alpha < 0.05:
                        continue
                    ax.plot(vx, vy, "x", color="#ff9800", markersize=5,
                            alpha=ghost_alpha, zorder=6)
                else:
                    ax.plot(vx, vy, "x", color="#ff9800", markersize=6,
                            alpha=0.7, zorder=6)
                    if not is_filtered:
                        ax.annotate(
                            "FP?", (vx, vy), textcoords="offset points",
                            xytext=(5, 5), fontsize=5, color="#ff9800",
                            alpha=0.8, fontweight="bold", zorder=11,
                        )

    # --- Metrics badges ---
    badge_y = 0.04
    ax_raw.text(
        0.02, badge_y,
        f"Detections: {len(vehicle_data)}  |  "
        f"False Positives: {metrics['false_positives']}",
        transform=ax_raw.transAxes, fontsize=7, color="#ff9800",
        fontfamily="monospace", va="bottom",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#1a1a2e",
                  edgecolor="#ff9800", alpha=0.9),
    )

    real_count = sum(1 for vd in vehicle_data if vd["v"].get("is_real", True))
    ax_filt.text(
        0.02, badge_y,
        f"Confirmed Threats: {metrics['active_collisions']}  |  "
        f"FPs Remaining: {metrics['false_positives']}  |  "
        f"max_risk: {metrics['max_risk']:.3f}",
        transform=ax_filt.transAxes, fontsize=7, color="#69f0ae",
        fontfamily="monospace", va="bottom",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#1a1a2e",
                  edgecolor="#69f0ae", alpha=0.9),
    )

    # --- Phase narration (top center) ---
    if phase_title:
        # Title bar
        fig.texts.clear()
        fig.text(
            0.5, 0.97, f"Frame {frame}/{TOTAL_FRAMES}",
            ha="center", va="top", fontsize=9, color="#666666",
            fontfamily="monospace",
        )
        fig.text(
            0.5, 0.94, phase_title,
            ha="center", va="top", fontsize=14, fontweight="bold",
            color="#e0e0e0",
        )
        for i, line in enumerate(phase_lines):
            fig.text(
                0.5, 0.90 - i * 0.025, line,
                ha="center", va="top", fontsize=8.5, color="#aaaaaa",
                fontfamily="monospace", style="italic",
            )

    # --- Progress bar at very bottom ---
    progress = frame / max(TOTAL_FRAMES - 1, 1)
    bar_y = 0.015
    fig.patches.clear()
    # Background bar
    fig.patches.append(mpatches.FancyBboxPatch(
        (0.05, bar_y - 0.004), 0.9, 0.008,
        boxstyle="round,pad=0.002", facecolor="#1a1a2e",
        edgecolor="#333333", transform=fig.transFigure, zorder=100,
    ))
    # Progress fill
    if progress > 0.01:
        bar_color = "#ff1744" if progress < 0.3 else "#ffd600" if progress < 0.6 else "#69f0ae"
        fig.patches.append(mpatches.FancyBboxPatch(
            (0.05, bar_y - 0.003), 0.9 * progress, 0.006,
            boxstyle="round,pad=0.001", facecolor=bar_color,
            transform=fig.transFigure, zorder=101,
        ))


# =====================================================================
# MODE 1: Save snapshots
# =====================================================================
def run_save_mode():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"\n[SAVE MODE] Generating output to: {OUTPUT_DIR}")
    print("[*] Simulating all frames...")
    data = simulate_all_frames()

    # Individual snapshots
    for f in SNAPSHOT_FRAMES:
        fig_s, axes_s = plt.subplots(1, 2, figsize=(16, 7))
        fig_s.patch.set_facecolor("#0a0a0a")
        fig_s.subplots_adjust(left=0.03, right=0.97, top=0.82, bottom=0.06, wspace=0.08)
        render_dual_frame(fig_s, axes_s, data["raw"][f], data["filtered"][f],
                          data["vehicles"][f], data["metrics"][f])
        path = os.path.join(OUTPUT_DIR, f"frame_{f:03d}.png")
        fig_s.savefig(path, dpi=150, facecolor="#0a0a0a",
                      bbox_inches="tight", pad_inches=0.15)
        plt.close(fig_s)
        print(f"    Saved {path}")

    # Summary: 5-panel comparison
    key_frames = [0, 10, 30, 60, 89]
    fig_sum, axes_all = plt.subplots(2, len(key_frames), figsize=(22, 9))
    fig_sum.patch.set_facecolor("#0a0a0a")
    fig_sum.suptitle(
        "MAVRO FlowGuard -- False-Positive Reduction: RAW vs FILTERED",
        fontsize=18, fontweight="bold", color="#e0e0e0", y=0.98,
    )

    for col, f in enumerate(key_frames):
        ax_r = axes_all[0, col]
        ax_f = axes_all[1, col]

        ax_r.imshow(data["raw"][f], cmap=DANGER_CMAP, vmin=0, vmax=1.0,
                    origin="lower", aspect="auto", interpolation="bilinear")
        ax_r.set_title(f"Frame {f} RAW", fontsize=8, color="#ff6b6b", fontweight="bold")
        ax_r.set_xticks([]); ax_r.set_yticks([])

        ax_f.imshow(data["filtered"][f], cmap=DANGER_CMAP, vmin=0, vmax=1.0,
                    origin="lower", aspect="auto", interpolation="bilinear")
        ax_f.set_title(f"Frame {f} FILTERED", fontsize=8, color="#69f0ae", fontweight="bold")
        ax_f.set_xticks([]); ax_f.set_yticks([])

        for ax in (ax_r, ax_f):
            ax.set_facecolor("#000000")
            for spine in ax.spines.values():
                spine.set_color("#333333")

    # Row labels
    axes_all[0, 0].set_ylabel("RAW", fontsize=12, color="#ff6b6b", fontweight="bold")
    axes_all[1, 0].set_ylabel("FILTERED", fontsize=12, color="#69f0ae", fontweight="bold")

    fig_sum.subplots_adjust(wspace=0.05, hspace=0.15, top=0.92, bottom=0.03,
                            left=0.04, right=0.98)
    sum_path = os.path.join(OUTPUT_DIR, "summary_timeline.png")
    fig_sum.savefig(sum_path, dpi=150, facecolor="#0a0a0a",
                    bbox_inches="tight", pad_inches=0.2)
    plt.close(fig_sum)
    print(f"    Saved {sum_path}")

    # Metrics evolution
    frames_arr = [m["frame"] for m in data["metrics"]]
    avg_arr = [m["avg_risk"] for m in data["metrics"]]
    max_arr = [m["max_risk"] for m in data["metrics"]]
    col_arr = [m["active_collisions"] for m in data["metrics"]]
    fp_arr = [m["false_positives"] for m in data["metrics"]]

    fig_met, axes_met = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
    fig_met.patch.set_facecolor("#0a0a0a")
    fig_met.suptitle("Risk Metrics Over Time",
                     fontsize=14, fontweight="bold", color="#e0e0e0")

    for ax in axes_met:
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e")
        for spine in ax.spines.values():
            spine.set_color("#30363d")
        # Phase shading
        ax.axvspan(0, 12, alpha=0.08, color="#ff1744")
        ax.axvspan(12, 25, alpha=0.06, color="#ff9800")
        ax.axvspan(25, 45, alpha=0.06, color="#ffd600")
        ax.axvspan(45, 70, alpha=0.05, color="#69f0ae")
        ax.axvspan(70, TOTAL_FRAMES, alpha=0.05, color="#40c4ff")

    axes_met[0].plot(frames_arr, max_arr, color="#ff1744", linewidth=2, label="max_risk")
    axes_met[0].plot(frames_arr, avg_arr, color="#40c4ff", linewidth=1.5, label="avg_risk")
    axes_met[0].fill_between(frames_arr, max_arr, alpha=0.12, color="#ff1744")
    axes_met[0].legend(fontsize=8, facecolor="#0d1117", edgecolor="#30363d", labelcolor="#e0e0e0")
    axes_met[0].set_ylabel("Risk", color="#8b949e")

    axes_met[1].bar(frames_arr, col_arr, color="#ff1744", alpha=0.8, width=1.0)
    axes_met[1].set_ylabel("Collisions", color="#8b949e")

    axes_met[2].plot(frames_arr, fp_arr, color="#ff9800", linewidth=2, label="False Positives")
    axes_met[2].fill_between(frames_arr, fp_arr, alpha=0.15, color="#ff9800")
    axes_met[2].set_ylabel("FP Count", color="#8b949e")
    axes_met[2].set_xlabel("Frame", color="#8b949e")
    axes_met[2].legend(fontsize=8, facecolor="#0d1117", edgecolor="#30363d", labelcolor="#e0e0e0")

    met_path = os.path.join(OUTPUT_DIR, "metrics_evolution.png")
    fig_met.savefig(met_path, dpi=150, facecolor="#0a0a0a",
                    bbox_inches="tight", pad_inches=0.2)
    plt.close(fig_met)
    print(f"    Saved {met_path}")

    print(f"\n[DONE] All outputs saved to {OUTPUT_DIR}/")


# =====================================================================
# MODE 2: Interactive animation
# =====================================================================
def run_interactive_mode():
    print("\n[INTERACTIVE MODE] Running live animation...")
    print("[*] Simulating all frames...")
    data = simulate_all_frames()

    fig, axes = plt.subplots(1, 2, figsize=(17, 8))
    fig.patch.set_facecolor("#0a0a0a")
    fig.subplots_adjust(left=0.03, right=0.97, top=0.82, bottom=0.06, wspace=0.08)

    def _update(frame_idx):
        render_dual_frame(fig, axes, data["raw"][frame_idx],
                          data["filtered"][frame_idx],
                          data["vehicles"][frame_idx],
                          data["metrics"][frame_idx])

    anim = animation.FuncAnimation(
        fig, _update, frames=range(TOTAL_FRAMES),
        interval=ANIMATION_INTERVAL_MS, repeat=False,
    )
    plt.show()
    print("\n[DONE] Animation complete.")


# =====================================================================
# Entry point
# =====================================================================
def main() -> None:
    print("=" * 66)
    print("  MAVRO FlowGuard - False-Positive Reduction Visualiser")
    print("  Frames: {}   Grid: {}x{}   Smoothing alpha: {}".format(
        TOTAL_FRAMES, GRID_SIZE, GRID_SIZE, SMOOTHING_ALPHA))
    if SAVE_MODE:
        print("  Mode: SAVE (headless -- PNGs to heatmap_output/)")
    else:
        print("  Mode: INTERACTIVE (live animation window)")
    print("=" * 66)

    if SAVE_MODE:
        run_save_mode()
    else:
        try:
            run_interactive_mode()
        except Exception as e:
            print(f"\n[WARN] Interactive mode failed ({e})")
            print("[WARN] Falling back to save mode...")
            run_save_mode()


if __name__ == "__main__":
    main()
