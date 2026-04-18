#!/usr/bin/env python3
"""
================================================================
  MAVRO FlowGuard -- LIVE Risk Heatmap Visualization
  Uses REAL pipeline data (Flask + DB + full enrichment)
  Standalone script - No existing files modified
  Run:  python visualize_heatmap.py           (live animation)
        python visualize_heatmap.py --save    (save snapshots)
================================================================

Pipeline flow per frame:
  DB vehicles -> direction_intelligence -> compute_spatial
  -> ML signals -> prediction -> risk_engine -> decision
  -> THIS visualization

Data fields used from pipeline:
  lat, lon, vx, vy, speed, bearing, class, risk_score_refined,
  collision_probability, ttc, alert, temporal_state, uncertainty
"""

from __future__ import annotations

import math
import os
import sys
import time
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
POLL_INTERVAL_S = 1.0           # match frontend 1000ms polling
SMOOTHING_ALPHA = 0.75
GAUSSIAN_SIGMA = 5.0
DANGER_THRESHOLD = 0.15
COLLISION_CIRCLE_PROB = 0.45
MAX_TRAIL_LEN = 20
MAX_SAVE_FRAMES = 60            # frames to capture in save mode

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heatmap_output")

# Custom aggressive colormap
_CMAP_COLORS = [
    (0.0, "#000000"),
    (0.15, "#1a0533"),
    (0.3, "#4a0e6e"),
    (0.45, "#8b1a4a"),
    (0.6, "#c62828"),
    (0.75, "#ef6c00"),
    (0.88, "#fdd835"),
    (1.0, "#ffffff"),
]

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))

DANGER_CMAP = LinearSegmentedColormap.from_list(
    "danger_fire",
    list(zip([c[0] for c in _CMAP_COLORS], [_hex_to_rgb(c[1]) for c in _CMAP_COLORS])),
    N=512,
)

# ---------------------------------------------------------------------
# Flask app bootstrap (required for DB access)
# ---------------------------------------------------------------------
print("[*] Bootstrapping Flask application context...")

try:
    from backend import create_app
    from backend.services.input_layer import FlowGuardInputLayer

    app = create_app()
    app_context = app.app_context()
    app_context.push()

    input_layer = FlowGuardInputLayer()
    USE_LIVE = True
    print("[OK] Flask app context active -- using REAL pipeline data")
except Exception as e:
    print(f"[FAIL] Could not bootstrap Flask app: {e}")
    print("[FAIL] This script requires the backend to be properly configured.")
    print("[FAIL] Make sure your database is running and .env is configured.")
    sys.exit(1)


# ---------------------------------------------------------------------
# Trajectory history
# ---------------------------------------------------------------------
_trajectory_history: dict[Any, list[tuple[float, float]]] = defaultdict(list)


# ---------------------------------------------------------------------
# Fetch real vehicles from pipeline
# ---------------------------------------------------------------------
def fetch_pipeline_vehicles() -> list[dict[str, Any]]:
    """
    Calls get_vehicle_updates() which triggers the FULL pipeline:
    direction_intelligence -> compute_spatial -> ML -> prediction
    -> risk_engine -> decision

    Returns enriched vehicle dicts with all risk fields.
    """
    try:
        vehicles = list(input_layer.get_vehicle_updates())
        if not vehicles:
            print("[WARN] No vehicles returned from pipeline (DB may be empty)")
            return []

        # Track trajectories
        for v in vehicles:
            vid = v.get("id", v.get("vehicle_id"))
            lat = float(v.get("lat", 0))
            lon = float(v.get("lon", 0))
            if vid is not None:
                _trajectory_history[vid].append((lon, lat))
                if len(_trajectory_history[vid]) > MAX_TRAIL_LEN:
                    _trajectory_history[vid] = _trajectory_history[vid][-MAX_TRAIL_LEN:]

        return vehicles
    except Exception as e:
        print(f"[ERROR] Pipeline fetch failed: {e}")
        return []


# ---------------------------------------------------------------------
# Lat/Lon -> Grid XY conversion
# ---------------------------------------------------------------------
# We maintain a running bounding box that expands as we see more data
_bbox = {"lat_min": None, "lat_max": None, "lon_min": None, "lon_max": None}
_BBOX_PADDING = 0.0005  # ~55m padding to prevent edge clipping


def _update_bbox(vehicles: list[dict]) -> None:
    """Expand bounding box to include all vehicle positions."""
    if not vehicles:
        return

    lats = [float(v.get("lat", 0)) for v in vehicles]
    lons = [float(v.get("lon", 0)) for v in vehicles]

    new_lat_min = min(lats) - _BBOX_PADDING
    new_lat_max = max(lats) + _BBOX_PADDING
    new_lon_min = min(lons) - _BBOX_PADDING
    new_lon_max = max(lons) + _BBOX_PADDING

    if _bbox["lat_min"] is None:
        _bbox["lat_min"] = new_lat_min
        _bbox["lat_max"] = new_lat_max
        _bbox["lon_min"] = new_lon_min
        _bbox["lon_max"] = new_lon_max
    else:
        _bbox["lat_min"] = min(_bbox["lat_min"], new_lat_min)
        _bbox["lat_max"] = max(_bbox["lat_max"], new_lat_max)
        _bbox["lon_min"] = min(_bbox["lon_min"], new_lon_min)
        _bbox["lon_max"] = max(_bbox["lon_max"], new_lon_max)


def latlon_to_grid(lat: float, lon: float) -> tuple[int, int]:
    """Convert geographic coordinates to grid indices."""
    if _bbox["lat_min"] is None:
        return GRID_SIZE // 2, GRID_SIZE // 2

    lat_range = max(_bbox["lat_max"] - _bbox["lat_min"], 1e-7)
    lon_range = max(_bbox["lon_max"] - _bbox["lon_min"], 1e-7)

    x = (lon - _bbox["lon_min"]) / lon_range
    y = (lat - _bbox["lat_min"]) / lat_range

    x_idx = int(np.clip(x * (GRID_SIZE - 1), 0, GRID_SIZE - 1))
    y_idx = int(np.clip(y * (GRID_SIZE - 1), 0, GRID_SIZE - 1))
    return x_idx, y_idx


def latlon_to_grid_float(lat: float, lon: float) -> tuple[float, float]:
    """Convert geographic coordinates to continuous grid coordinates."""
    if _bbox["lat_min"] is None:
        return GRID_SIZE / 2.0, GRID_SIZE / 2.0

    lat_range = max(_bbox["lat_max"] - _bbox["lat_min"], 1e-7)
    lon_range = max(_bbox["lon_max"] - _bbox["lon_min"], 1e-7)

    x = (lon - _bbox["lon_min"]) / lon_range * (GRID_SIZE - 1)
    y = (lat - _bbox["lat_min"]) / lat_range * (GRID_SIZE - 1)

    return float(np.clip(x, 0, GRID_SIZE - 1)), float(np.clip(y, 0, GRID_SIZE - 1))


# ---------------------------------------------------------------------
# Risk computation from REAL pipeline signals
# ---------------------------------------------------------------------
def compute_vehicle_risk(v: dict[str, Any]) -> float:
    """
    Composite risk from real pipeline outputs:
      0.5 * collision_probability
    + 0.3 * (1 / TTC)
    + 0.2 * risk_score_refined
    """
    cp = float(v.get("collision_probability", 0) or 0)
    ttc = float(v.get("ttc", 999) or 999)
    rsr = float(v.get("risk_score_refined", 0) or 0)

    risk = (
        0.5 * cp
        + 0.3 * (1.0 / max(ttc, 0.1))
        + 0.2 * min(rsr / 10.0, 1.0)  # normalize refined score to [0,1]
    )
    return risk


# ---------------------------------------------------------------------
# Gaussian spatial spread (vectorised)
# ---------------------------------------------------------------------
_yy, _xx = np.mgrid[0:GRID_SIZE, 0:GRID_SIZE]


def spread_risk(heatmap: np.ndarray, x: float, y: float, risk: float) -> None:
    """Add Gaussian blob of risk centred at (x, y)."""
    ix = int(np.clip(x, 0, GRID_SIZE - 1))
    iy = int(np.clip(y, 0, GRID_SIZE - 1))
    d2 = (_xx - ix) ** 2 + (_yy - iy) ** 2
    heatmap += risk * np.exp(-d2 / (2 * GAUSSIAN_SIGMA ** 2))


# ---------------------------------------------------------------------
# Temporal smoothing
# ---------------------------------------------------------------------
_prev_heatmap = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float64)


def temporal_smooth(curr: np.ndarray) -> np.ndarray:
    global _prev_heatmap
    smoothed = SMOOTHING_ALPHA * _prev_heatmap + (1.0 - SMOOTHING_ALPHA) * curr
    _prev_heatmap = smoothed.copy()
    return smoothed


# ---------------------------------------------------------------------
# Build one frame of data from pipeline
# ---------------------------------------------------------------------
def build_frame(frame_num: int) -> dict[str, Any]:
    """Fetch vehicles, build heatmaps, compute metrics."""
    vehicles = fetch_pipeline_vehicles()

    if not vehicles:
        return {
            "raw": np.zeros((GRID_SIZE, GRID_SIZE)),
            "filtered": np.zeros((GRID_SIZE, GRID_SIZE)),
            "vehicles": [],
            "metrics": {
                "frame": frame_num,
                "count": 0,
                "avg_risk": 0, "max_risk": 0,
                "active_collisions": 0, "alerts": {},
            },
        }

    _update_bbox(vehicles)

    raw_heatmap = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float64)
    filtered_heatmap = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.float64)
    vehicle_data = []

    for v in vehicles:
        lat = float(v.get("lat", 0))
        lon = float(v.get("lon", 0))
        gx, gy = latlon_to_grid_float(lat, lon)

        raw_risk = compute_vehicle_risk(v)
        spread_risk(raw_heatmap, gx, gy, raw_risk)
        spread_risk(filtered_heatmap, gx, gy, raw_risk)

        vehicle_data.append({
            "v": v,
            "gx": gx,
            "gy": gy,
            "risk": raw_risk,
        })

    # Normalize raw (no smoothing)
    raw_max = raw_heatmap.max()
    raw_norm = raw_heatmap / max(raw_max, 1e-9)

    # Temporal smoothing on filtered
    smoothed = temporal_smooth(filtered_heatmap)
    filt_max = smoothed.max()
    filt_norm = smoothed / max(filt_max, 1e-9)

    # Hard threshold: dim weak signals
    filt_display = filt_norm.copy()
    filt_display[filt_display < DANGER_THRESHOLD] *= 0.15

    # Metrics
    risk_scores = [float(v.get("risk_score_refined", 0) or 0) for v in vehicles]
    avg_risk = sum(risk_scores) / max(len(risk_scores), 1)
    max_risk_val = max(risk_scores) if risk_scores else 0

    alert_counts = defaultdict(int)
    for v in vehicles:
        alert_counts[v.get("alert", "SAFE")] += 1

    active_collisions = sum(
        1 for v in vehicles
        if float(v.get("collision_probability", 0) or 0) > 0.5
        and float(v.get("ttc", 999) or 999) < 3
    )

    return {
        "raw": raw_norm,
        "filtered": filt_display,
        "vehicles": vehicle_data,
        "metrics": {
            "frame": frame_num,
            "count": len(vehicles),
            "avg_risk": round(avg_risk, 3),
            "max_risk": round(max_risk_val, 3),
            "active_collisions": active_collisions,
            "alerts": dict(alert_counts),
        },
    }


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------
def render_dual_frame(fig, axes, data: dict):
    """Render side-by-side RAW vs FILTERED with all overlays."""
    ax_raw, ax_filt = axes
    metrics = data["metrics"]
    frame = metrics["frame"]

    for ax in (ax_raw, ax_filt):
        ax.clear()
        ax.set_facecolor("#000000")
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_color("#333333")

    # --- Heatmaps ---
    ax_raw.imshow(data["raw"], cmap=DANGER_CMAP, vmin=0, vmax=1.0,
                  origin="lower", aspect="auto", interpolation="bilinear")
    ax_raw.set_title("RAW DETECTIONS (unfiltered)", fontsize=11,
                     fontweight="bold", color="#ff6b6b", pad=8)

    ax_filt.imshow(data["filtered"], cmap=DANGER_CMAP, vmin=0, vmax=1.0,
                   origin="lower", aspect="auto", interpolation="bilinear")
    ax_filt.set_title("FILTERED (temporal + pipeline)", fontsize=11,
                      fontweight="bold", color="#69f0ae", pad=8)

    # --- Vehicle overlays ---
    for vd in data["vehicles"]:
        v = vd["v"]
        gx, gy = vd["gx"], vd["gy"]
        risk = vd["risk"]
        cp = float(v.get("collision_probability", 0) or 0)
        alert = v.get("alert", "SAFE")
        sem_class = v.get("class", v.get("semantic_class", "normal"))
        vid = v.get("id", v.get("vehicle_id"))
        ttc_val = v.get("ttc")

        # Color by alert level
        if alert == "COLLISION_ALERT":
            marker_color = "#ff1744"
            marker_size = 10
        elif alert == "HIGH_ALERT":
            marker_color = "#ff6d00"
            marker_size = 8
        elif alert == "WARNING":
            marker_color = "#ffd600"
            marker_size = 7
        else:
            marker_color = "#69f0ae"
            marker_size = 5

        for ax in (ax_raw, ax_filt):
            ax.plot(gx, gy, "o", color=marker_color, markersize=marker_size,
                    markeredgecolor="white", markeredgewidth=1.0, zorder=10)

            # Danger circle for high collision probability
            if cp > COLLISION_CIRCLE_PROB:
                circle_r = 6 + cp * 12
                danger_circle = plt.Circle(
                    (gx, gy), circle_r, fill=False,
                    color="#ff1744", linewidth=2.0,
                    alpha=0.7 + 0.3 * math.sin(frame * 0.4), zorder=9,
                )
                ax.add_patch(danger_circle)

                # Outer pulsing ring
                outer = plt.Circle(
                    (gx, gy), circle_r + 3, fill=False,
                    color="#ff5252", linewidth=1.0, linestyle="--",
                    alpha=0.3 + 0.3 * math.sin(frame * 0.6), zorder=8,
                )
                ax.add_patch(outer)

            # Vehicle label
            label_parts = []
            if sem_class and sem_class != "normal":
                label_parts.append(sem_class.replace("_", " ").title())
            if alert in ("COLLISION_ALERT", "HIGH_ALERT"):
                label_parts.append(alert.replace("_", " "))
            if ttc_val is not None and float(ttc_val) < 5:
                label_parts.append(f"TTC:{float(ttc_val):.1f}s")

            if label_parts:
                label_text = " | ".join(label_parts)
                ax.annotate(
                    label_text, (gx, gy), textcoords="offset points",
                    xytext=(8, 8), fontsize=5.5, fontweight="bold",
                    color="white", zorder=11,
                    bbox=dict(boxstyle="round,pad=0.2",
                              facecolor="#1a1a2e", edgecolor=marker_color,
                              alpha=0.9),
                )

            # Trajectory trail
            if vid in _trajectory_history and len(_trajectory_history[vid]) > 2:
                trail = _trajectory_history[vid]
                # Convert trail lat/lon to grid coords
                trail_grid = [latlon_to_grid_float(p[1], p[0]) for p in trail]
                n_pts = len(trail_grid)
                for i in range(1, n_pts):
                    a_val = 0.1 + 0.6 * (i / n_pts)
                    ax.plot(
                        [trail_grid[i-1][0], trail_grid[i][0]],
                        [trail_grid[i-1][1], trail_grid[i][1]],
                        "--", color=marker_color, alpha=a_val,
                        linewidth=1.0, zorder=7,
                    )

    # --- Metrics badges ---
    alerts = metrics.get("alerts", {})
    collision_alerts = alerts.get("COLLISION_ALERT", 0)
    high_alerts = alerts.get("HIGH_ALERT", 0)
    warnings = alerts.get("WARNING", 0)
    safe_count = alerts.get("SAFE", 0)

    ax_raw.text(
        0.02, 0.04,
        f"Vehicles: {metrics['count']}  |  "
        f"Avg Risk: {metrics['avg_risk']}  |  Max Risk: {metrics['max_risk']}",
        transform=ax_raw.transAxes, fontsize=7, color="#ff9800",
        fontfamily="monospace", va="bottom",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#1a1a2e",
                  edgecolor="#ff9800", alpha=0.9),
    )

    ax_filt.text(
        0.02, 0.04,
        f"COLLISION: {collision_alerts}  HIGH: {high_alerts}  "
        f"WARN: {warnings}  SAFE: {safe_count}  |  "
        f"Active Collisions: {metrics['active_collisions']}",
        transform=ax_filt.transAxes, fontsize=7, color="#69f0ae",
        fontfamily="monospace", va="bottom",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#1a1a2e",
                  edgecolor="#69f0ae", alpha=0.9),
    )

    # --- Title ---
    fig.texts.clear()
    fig.text(
        0.5, 0.97,
        f"MAVRO FlowGuard -- LIVE Risk Map  |  "
        f"Frame {frame}  |  Vehicles: {metrics['count']}  |  "
        f"Poll: {POLL_INTERVAL_S}s",
        ha="center", va="top", fontsize=12, fontweight="bold", color="#e0e0e0",
    )

    # Alert summary bar
    if collision_alerts > 0:
        status_color = "#ff1744"
        status = f"!! {collision_alerts} COLLISION ALERT(S) ACTIVE !!"
    elif high_alerts > 0:
        status_color = "#ff6d00"
        status = f"! {high_alerts} HIGH ALERT(S) !"
    elif warnings > 0:
        status_color = "#ffd600"
        status = f"{warnings} warning(s) detected"
    else:
        status_color = "#69f0ae"
        status = "All vehicles SAFE"

    fig.text(
        0.5, 0.935, status,
        ha="center", va="top", fontsize=10, fontweight="bold",
        color=status_color, fontfamily="monospace",
    )

    # Coordinate info
    if _bbox["lat_min"] is not None:
        fig.text(
            0.5, 0.91,
            f"Bounds: [{_bbox['lat_min']:.5f}, {_bbox['lon_min']:.5f}] "
            f"-> [{_bbox['lat_max']:.5f}, {_bbox['lon_max']:.5f}]",
            ha="center", va="top", fontsize=7, color="#666666",
            fontfamily="monospace",
        )


# =====================================================================
# MODE 1: Save snapshots (headless)
# =====================================================================
def run_save_mode():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"\n[SAVE MODE] Capturing {MAX_SAVE_FRAMES} frames to: {OUTPUT_DIR}")

    for frame_num in range(MAX_SAVE_FRAMES):
        print(f"  [Frame {frame_num:>3}/{MAX_SAVE_FRAMES}] Polling pipeline...", end="")
        data = build_frame(frame_num)

        m = data["metrics"]
        print(f"  vehicles={m['count']}  "
              f"avg_risk={m['avg_risk']}  "
              f"collisions={m['active_collisions']}  "
              f"alerts={m.get('alerts', {})}")

        # Save every 5th frame as snapshot
        if frame_num % 5 == 0 or frame_num == MAX_SAVE_FRAMES - 1:
            fig_s, axes_s = plt.subplots(1, 2, figsize=(16, 7))
            fig_s.patch.set_facecolor("#0a0a0a")
            fig_s.subplots_adjust(left=0.03, right=0.97, top=0.85, bottom=0.06, wspace=0.08)
            render_dual_frame(fig_s, axes_s, data)
            path = os.path.join(OUTPUT_DIR, f"live_frame_{frame_num:03d}.png")
            fig_s.savefig(path, dpi=150, facecolor="#0a0a0a",
                          bbox_inches="tight", pad_inches=0.15)
            plt.close(fig_s)
            print(f"    -> Saved {path}")

        time.sleep(POLL_INTERVAL_S)

    print(f"\n[DONE] Snapshots saved to {OUTPUT_DIR}/")


# =====================================================================
# MODE 2: Interactive real-time animation
# =====================================================================
def run_interactive_mode():
    print("\n[INTERACTIVE MODE] Starting live visualization...")
    print(f"  Polling interval: {POLL_INTERVAL_S}s (synced with frontend)")
    print("  Close the window to stop.\n")

    fig, axes = plt.subplots(1, 2, figsize=(17, 8))
    fig.patch.set_facecolor("#0a0a0a")
    fig.subplots_adjust(left=0.03, right=0.97, top=0.85, bottom=0.04, wspace=0.08)

    frame_counter = [0]

    def _update(_frame_unused):
        data = build_frame(frame_counter[0])
        m = data["metrics"]
        print(f"  [Frame {frame_counter[0]:>3}] "
              f"vehicles={m['count']}  "
              f"avg_risk={m['avg_risk']}  "
              f"max_risk={m['max_risk']}  "
              f"collisions={m['active_collisions']}")
        render_dual_frame(fig, axes, data)
        frame_counter[0] += 1

    anim = animation.FuncAnimation(
        fig, _update,
        interval=int(POLL_INTERVAL_S * 1000),
        cache_frame_data=False,
    )
    plt.show()
    print("\n[DONE] Visualization stopped.")


# =====================================================================
# Entry point
# =====================================================================
def main() -> None:
    print("=" * 66)
    print("  MAVRO FlowGuard - LIVE Risk Heatmap Visualiser")
    print("  Grid: {}x{}  |  Smoothing alpha: {}  |  Poll: {}s".format(
        GRID_SIZE, GRID_SIZE, SMOOTHING_ALPHA, POLL_INTERVAL_S))
    if SAVE_MODE:
        print("  Mode: SAVE (capture {} frames to heatmap_output/)".format(MAX_SAVE_FRAMES))
    else:
        print("  Mode: INTERACTIVE (real-time, synced with UI)")
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
