"""
scenario_generator.py
=====================
Generates synthetic hard-negative and true-positive scenarios as a DataFrame.

This module is completely standalone  --  it does NOT touch the simulation engine,
Flask routes, or any live system component.  It synthesises tabular rows that
match the column schema of the DatasetLogger CSV so they can be concatenated
directly with real simulation data before training.

Each scenario produces >= 300 samples with Gaussian noise on every numeric field.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLES_PER_SCENARIO = 350          # >= 300 as requested
TICK_DT = 0.5                       # seconds per simulated tick
RNG_SEED = 2024

# Schema must match DatasetLogger header exactly:
# vehicle_id, timestamp, speed, bearing, dev_angle,
# anomaly_score, wrong_way_prob, risk_score, gps_quality, intent, label
_COLUMNS = [
    "vehicle_id", "timestamp", "speed", "bearing", "dev_angle",
    "anomaly_score", "wrong_way_prob", "risk_score",
    "gps_quality", "intent", "label",
    # Extra columns consumed by clean_features.py 
    "road_type",            # 'service' | 'urban' | 'highway'
    "diversion_flag",       # 0 | 1
    "sustained_duration_s", # how long bearing has been deviated
]


def _noise(rng: np.random.Generator, shape, scale: float = 1.0) -> np.ndarray:
    """Small Gaussian noise  --  clipped so it never flips sign of important fields."""
    return rng.normal(0.0, scale, size=shape)


def _base_timestamps(n: int, t0: float) -> np.ndarray:
    return t0 + np.arange(n) * TICK_DT


# ---------------------------------------------------------------------------
# Scenario builders  --  each returns a DataFrame chunk
# ---------------------------------------------------------------------------

def _uturn(rng: np.random.Generator, vid_start: int, t0: float) -> pd.DataFrame:
    n = SAMPLES_PER_SCENARIO
    rows = []
    ticks_per_uturn = int(4.5 / TICK_DT)  # Extend to 4.5 seconds
    num_events = n // ticks_per_uturn

    for ev in range(num_events):
        vid = vid_start + ev
        ts_base = t0 + ev * 15.0 
        for i in range(ticks_per_uturn):
            frac = i / max(ticks_per_uturn - 1, 1)
            # More aggressive non-linear deviation with higher noise
            dev = 178.0 * (1.0 - frac**0.6) + 12.0 * (frac**0.6) + rng.normal(0, 18.0)
            dev = max(0.0, min(180.0, dev))
            speed = rng.uniform(3.0, 8.0)
            # Randomize road type (Fix 2)
            road_type = rng.choice(["service", "urban", "highway"], p=[0.25, 0.60, 0.15])
            rows.append({
                "vehicle_id": vid,
                "timestamp": ts_base + i * TICK_DT,
                "speed": round(speed + rng.normal(0, 0.4), 2),
                "bearing": round(rng.uniform(0, 360), 1),
                "dev_angle": round(dev, 1),
                "anomaly_score": round(rng.uniform(0.1, 0.6), 3),
                "wrong_way_prob": round(rng.uniform(0.05, 0.5), 3),
                "risk_score": round(rng.uniform(0.05, 0.4), 3),
                "gps_quality": round(rng.uniform(0.4, 0.9), 3),
                "intent": "UTURN",
                "label": 0,
                "road_type": road_type,
                "diversion_flag": 0,
                "sustained_duration_s": round(i * TICK_DT, 2),
            })

    # Pad to target if needed
    while len(rows) < n:
        base = rows[rng.integers(0, len(rows))].copy()
        base["dev_angle"] = round(max(0, base["dev_angle"] + rng.normal(0, 5)), 1)
        rows.append(base)

    return pd.DataFrame(rows[:n])


def _service_road(rng: np.random.Generator, vid_start: int, t0: float) -> pd.DataFrame:
    """Service road parallel: bearing_deviation 155-165, road_type=service.  Label=0."""
    n = SAMPLES_PER_SCENARIO
    rows = []
    for i in range(n):
        dev = rng.uniform(155.0, 165.0) + rng.normal(0, 3.0)
        dev = max(0.0, min(180.0, dev))
        speed = rng.uniform(2.0, 7.0)
        road_type = rng.choice(["service", "urban", "highway"], p=[0.25, 0.60, 0.15])
        rows.append({
            "vehicle_id": vid_start + i % 50,
            "timestamp": t0 + i * TICK_DT,
            "speed": round(speed + rng.normal(0, 0.4), 2),
            "bearing": round(rng.uniform(0, 360), 1),
            "dev_angle": round(dev, 1),
            "anomaly_score": round(rng.uniform(0.1, 0.45), 3),
            "wrong_way_prob": round(rng.uniform(0.1, 0.5), 3),
            "risk_score": round(rng.uniform(0.05, 0.25), 3),
            "gps_quality": round(rng.uniform(0.4, 0.9), 3),
            "intent": "SERVICE_ROAD",
            "label": 0,
            "road_type": road_type,
            "diversion_flag": 0,
            "sustained_duration_s": round(rng.uniform(0.5, 8.0), 2),
        })
    return pd.DataFrame(rows)


def _gps_spike(rng: np.random.Generator, vid_start: int, t0: float) -> pd.DataFrame:
    """GPS spike: single frame >160 deviation, next frame <20.  Label=0."""
    n = SAMPLES_PER_SCENARIO
    rows = []
    pairs = n // 2
    for p in range(pairs):
        vid = vid_start + p % 60
        ts = t0 + p * 2.0
        speed = rng.uniform(4.0, 14.0)

        # Spike frame
        road_type = rng.choice(["service", "urban", "highway"], p=[0.25, 0.60, 0.15])
        rows.append({
            "vehicle_id": vid,
            "timestamp": ts,
            "speed": round(speed + rng.normal(0, 0.5), 2),
            "bearing": round(rng.uniform(0, 360), 1),
            "dev_angle": round(rng.uniform(160.0, 180.0) + rng.normal(0, 3), 1),
            "anomaly_score": round(rng.uniform(0.3, 0.7), 3),
            "wrong_way_prob": round(rng.uniform(0.4, 0.8), 3),
            "risk_score": round(rng.uniform(0.1, 0.4), 3),
            "gps_quality": round(rng.uniform(0.2, 0.5), 3),
            "intent": "GPS_SPIKE",
            "label": 0,
            "road_type": road_type,
            "diversion_flag": 0,
            "sustained_duration_s": round(TICK_DT, 2),
        })
        # Correction frame
        rows.append({
            "vehicle_id": vid,
            "timestamp": ts + TICK_DT,
            "speed": round(speed + rng.normal(0, 0.5), 2),
            "bearing": round(rng.uniform(0, 360), 1),
            "dev_angle": round(max(0, rng.uniform(2.0, 18.0) + rng.normal(0, 3)), 1),
            "anomaly_score": round(rng.uniform(0.05, 0.25), 3),
            "wrong_way_prob": round(rng.uniform(0.01, 0.15), 3),
            "risk_score": round(rng.uniform(0.02, 0.15), 3),
            "gps_quality": round(rng.uniform(0.6, 1.0), 3),
            "intent": "GPS_SPIKE",
            "label": 0,
            "road_type": road_type,
            "diversion_flag": 0,
            "sustained_duration_s": 0.0,
        })

    return pd.DataFrame(rows[:n])


def _traffic_jam(rng: np.random.Generator, vid_start: int, t0: float) -> pd.DataFrame:
    """Traffic jam: speed<1.8, bearing oscillates 25.  Label=0."""
    n = SAMPLES_PER_SCENARIO
    rows = []
    for i in range(n):
        speed = rng.uniform(0.3, 1.8)
        base_bearing = rng.uniform(0, 360)
        oscillation = rng.uniform(-25, 25)
        dev = abs(oscillation) + rng.normal(0, 8.0)
        dev = max(0.0, min(100.0, dev))
        road_type = rng.choice(["service", "urban", "highway"], p=[0.25, 0.60, 0.15])
        rows.append({
            "vehicle_id": vid_start + i % 40,
            "timestamp": t0 + i * TICK_DT,
            "speed": round(max(0.1, speed + rng.normal(0, 0.2)), 2),
            "bearing": round((base_bearing + oscillation) % 360, 1),
            "dev_angle": round(dev, 1),
            "anomaly_score": round(rng.uniform(0.05, 0.3), 3),
            "wrong_way_prob": round(rng.uniform(0.01, 0.2), 3),
            "risk_score": round(rng.uniform(0.01, 0.15), 3),
            "gps_quality": round(rng.uniform(0.3, 0.8), 3),
            "intent": "TRAFFIC_JAM",
            "label": 0,
            "road_type": road_type,
            "diversion_flag": 0,
            "sustained_duration_s": 0.0,
        })
    return pd.DataFrame(rows)


def _legal_diversion(rng: np.random.Generator, vid_start: int, t0: float) -> pd.DataFrame:
    """Legal diversion: bearing_dev 140-170, diversion_flag=True.  Label=0."""
    n = SAMPLES_PER_SCENARIO
    rows = []
    for i in range(n):
        dev = rng.uniform(140.0, 170.0) + rng.normal(0, 5.0)
        dev = max(0.0, min(180.0, dev))
        speed = rng.uniform(3.0, 12.0)
        road_type = rng.choice(["service", "urban", "highway"], p=[0.25, 0.60, 0.15])
        rows.append({
            "vehicle_id": vid_start + i % 50,
            "timestamp": t0 + i * TICK_DT,
            "speed": round(speed + rng.normal(0, 0.6), 2),
            "bearing": round(rng.uniform(0, 360), 1),
            "dev_angle": round(dev, 1),
            "anomaly_score": round(rng.uniform(0.1, 0.5), 3),
            "wrong_way_prob": round(rng.uniform(0.1, 0.6), 3),
            "risk_score": round(rng.uniform(0.05, 0.3), 3),
            "gps_quality": round(rng.uniform(0.5, 1.0), 3),
            "intent": "DIVERSION",
            "label": 0,
            "road_type": road_type,
            "diversion_flag": 1,
            "sustained_duration_s": round(rng.uniform(1.0, 10.0), 2),
        })
    return pd.DataFrame(rows)


def _true_wrong_way(rng: np.random.Generator, vid_start: int, t0: float) -> pd.DataFrame:
    """True wrong-way: bearing_dev>150 sustained >4s, speed>3 m/s.  Label=1."""
    n = SAMPLES_PER_SCENARIO
    rows = []
    # Each event is 8-16 ticks (4-8 seconds) of sustained wrong-way
    ticks_per_event = 12  # ~6 seconds
    num_events = n // ticks_per_event

    for ev in range(num_events):
        vid = vid_start + ev
        ts_base = t0 + ev * 20.0
        speed_base = rng.uniform(4.0, 15.0)
        dev_base = rng.uniform(155.0, 178.0)

        for i in range(ticks_per_event):
            elapsed = i * TICK_DT
            # Add more jitter to the deviation to overlap with noise
            dev = dev_base + rng.normal(0, 10.0) 
            dev = max(130.0, min(180.0, dev))
            speed = speed_base + rng.normal(0, 2.0)
            speed = max(1.0, speed)
            road_type = rng.choice(["service", "urban", "highway"], p=[0.25, 0.60, 0.15])
            rows.append({
                "vehicle_id": vid,
                "timestamp": ts_base + elapsed,
                "speed": round(speed, 2),
                "bearing": round(rng.uniform(0, 360), 1),
                "dev_angle": round(dev, 1),
                "anomaly_score": round(rng.uniform(0.1, 0.9), 3),
                "wrong_way_prob": round(rng.uniform(0.2, 0.98), 3),
                "risk_score": round(rng.uniform(0.1, 0.85), 3),
                "gps_quality": round(rng.uniform(0.3, 0.9), 3),
                "intent": "WRONG_WAY",
                "label": 1,
                "road_type": road_type,
                "diversion_flag": 0,
                "sustained_duration_s": round(elapsed, 2),
            })

    # Pad remaining with even more noisy samples
    while len(rows) < n:
        base = rows[rng.integers(0, len(rows))].copy()
        base["dev_angle"] = round(max(120, base["dev_angle"] + rng.normal(0, 15)), 1)
        base["sustained_duration_s"] = round(rng.uniform(3.0, 12.0), 2)
        rows.append(base)

    return pd.DataFrame(rows[:n])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_all_scenarios(seed: int = RNG_SEED, t0: float | None = None) -> pd.DataFrame:
    """Generate a complete hard-negative + true-positive dataset.

    Parameters
    ----------
    seed : RNG seed for reproducibility
    t0   : Base timestamp.  If None, defaults to 1 hour ago.
           Pass the midpoint of your real data's timestamp range to interleave
           synthetic scenarios with real data for proper time-aware splitting.

    Returns a DataFrame with the standard eval columns PLUS the extra
    columns (road_type, diversion_flag, sustained_duration_s).

    Scenarios produced (>=300 samples each):
        - U-turn           (label=0)
        - Service road      (label=0)
        - GPS spike         (label=0)
        - Traffic jam        (label=0)
        - Legal diversion    (label=0)
        - True wrong-way     (label=1)
    """
    rng = np.random.default_rng(seed)
    if t0 is None:
        t0 = time.time() - 3600.0

    chunks = [
        _uturn(rng, vid_start=9000, t0=t0 + rng.uniform(-43200, 43200)),
        _service_road(rng, vid_start=9100, t0=t0 + rng.uniform(-43200, 43200)),
        _gps_spike(rng, vid_start=9200, t0=t0 + rng.uniform(-43200, 43200)),
        _traffic_jam(rng, vid_start=9300, t0=t0 + rng.uniform(-43200, 43200)),
        _legal_diversion(rng, vid_start=9400, t0=t0 + rng.uniform(-43200, 43200)),
        _true_wrong_way(rng, vid_start=9500, t0=t0 + rng.uniform(-43200, 43200)),
    ]

    df = pd.concat(chunks, ignore_index=True)

    # Ensure column order matches _COLUMNS
    for col in _COLUMNS:
        if col not in df.columns:
            df[col] = 0
    df = df[_COLUMNS]

    return df


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    df = generate_all_scenarios()
    print(f"Generated {len(df)} scenario samples")
    print(f"Label distribution:\n{df['label'].value_counts().to_string()}")
    print(f"\nScenario distribution (by intent):")
    print(df["intent"].value_counts().to_string())
    print(f"\ndev_angle stats by label:")
    print(df.groupby("label")["dev_angle"].describe().to_string())
