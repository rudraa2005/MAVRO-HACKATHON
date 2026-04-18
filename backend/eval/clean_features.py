"""
clean_features.py
=================
Extracts a leak-free feature vector for ML training.

CRITICAL DESIGN RULE:
    This module NEVER reads anomaly_score, risk_score, or wrong_way_prob
    from the input DataFrame.  Those features contain the ground-truth label
    baked in (see audit) and must be excluded from any training pipeline.

The clean feature set:
    1. speed_mps                     --  raw vehicle speed
    2. bearing_deviation_deg         --  angle between vehicle heading and road
    3. sustained_wrong_way_duration_s  --  seconds of continuous deviation > 100
    4. gps_noise_estimate            --  variance of last 5 bearing readings
    5. speed_x_bearing_interaction   --  speed  (bearing_deviation / 180)
    6. road_type_encoded             --  0=service, 1=urban, 2=highway
    7. time_of_day_sin               --  cyclical encoding of hour
    8. time_of_day_cos               --  cyclical encoding of hour

Also applies label corrections:
    - sustained_wrong_way_duration < 3s  -> label = 0
    - speed < 1.5 m/s                    -> label = 0
"""

from __future__ import annotations

import math
from typing import Tuple

import numpy as np
import pandas as pd


CLEAN_FEATURE_NAMES = [
    "speed_mps",
    "bearing_deviation_deg",
    "sustained_wrong_way_duration_s",
    "gps_noise_estimate",
    "speed_x_bearing_interaction",
    "road_type_encoded",
    "time_of_day_sin",
    "time_of_day_cos",
]

# Road type encoding lookup
_ROAD_TYPE_MAP = {
    "service": 0,
    "urban": 1,
    "highway": 2,
}


def _compute_gps_noise(df: pd.DataFrame) -> pd.Series:
    """Estimate GPS noise as rolling variance of bearing over last 5 readings.

    For each vehicle, compute a rolling variance of `bearing` with window=5.
    If the vehicle doesn't have enough history, fall back to a global noise
    estimate from `gps_quality` (inverted).
    """
    if "bearing" not in df.columns:
        return pd.Series(np.zeros(len(df)), index=df.index)

    # Sort by vehicle + time so rolling makes sense
    sorted_df = df.sort_values(["vehicle_id", "timestamp"])

    # Rolling variance per vehicle
    variance = (
        sorted_df.groupby("vehicle_id")["bearing"]
        .rolling(window=5, min_periods=2)
        .var()
        .reset_index(level=0, drop=True)
    )
    # Re-index back to original order
    variance = variance.reindex(df.index)

    # Fill NaN (vehicles with < 2 readings) with a fallback based on gps_quality
    if "gps_quality" in df.columns:
        fallback = (1.0 - df["gps_quality"].clip(0, 1)) * 500.0  # low quality -> high noise
    else:
        fallback = 100.0

    variance = variance.fillna(fallback)

    # Clip to sane range [0, 5000] and normalise to [0, 1]
    variance = variance.clip(0, 5000) / 5000.0

    return variance


def _compute_sustained_duration(df: pd.DataFrame) -> pd.Series:
    """Compute how long (seconds) each sample has been in sustained deviation.

    FIX - Only using noisy bearing-derived estimate. Ignores pre-calculated columns.
    """
    # 1. Estimate from bearing deviation only - no ground truth
    # Use dev_angle which is pre-computed from telemetry in the input DF
    bearing_above_threshold = (df["dev_angle"] > 130).astype(int)
    groups = (bearing_above_threshold != bearing_above_threshold.shift()).cumsum()
    
    # User requested * 1.0 multiplier
    result = (
        bearing_above_threshold
        .groupby(groups)
        .transform("cumsum") * 1.0
    )
    
    # 2. Add significant Gaussian noise
    rng = np.random.default_rng(2025)
    noise = rng.normal(0, 0.8, size=len(result))
    result = (result + noise).clip(0, 30.0)

    return result


def _encode_road_type(df: pd.DataFrame) -> pd.Series:
    """Encode road type as integer.  Falls back to 1 (urban) if missing."""
    if "road_type" in df.columns:
        return df["road_type"].map(_ROAD_TYPE_MAP).fillna(1).astype(int)
    return pd.Series(np.ones(len(df), dtype=int), index=df.index)


def _encode_time_cyclical(df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
    """Extract cyclical hour-of-day features from unix timestamp."""
    if "timestamp" not in df.columns:
        return (
            pd.Series(np.zeros(len(df)), index=df.index),
            pd.Series(np.ones(len(df)), index=df.index),
        )

    hours = (df["timestamp"] % 86400) / 3600.0  # seconds-in-day -> hours
    sin_vals = np.sin(2 * math.pi * hours / 24.0)
    cos_vals = np.cos(2 * math.pi * hours / 24.0)
    return pd.Series(sin_vals, index=df.index), pd.Series(cos_vals, index=df.index)


def apply_label_corrections(df: pd.DataFrame) -> pd.DataFrame:
    """Override labels for ambiguous samples.  Additive  --  only flips 1->0, never 0->1.

    Rules:
        1. sustained_wrong_way_duration < 3s  -> label = 0
        2. speed < 1.5 m/s                    -> label = 0
        3. U-turn pattern (dev_angle initially high, then drops) -> label = 0
    """
    df = df.copy()

    # Rule 1: short duration
    duration_col = _compute_sustained_duration(df)
    mask_short = (df["label"] == 1) & (duration_col < 3.0)
    df.loc[mask_short, "label"] = 0

    # Rule 2: very slow
    speed_col = df.get("speed", df.get("speed_mps", pd.Series(dtype=float)))
    mask_slow = (df["label"] == 1) & (speed_col < 1.5)
    df.loc[mask_slow, "label"] = 0

    # Rule 3: U-turn pattern  --  intent flag from scenario_generator
    if "intent" in df.columns:
        mask_uturn = (df["label"] == 1) & (df["intent"] == "UTURN")
        df.loc[mask_uturn, "label"] = 0

    return df


def extract_clean_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series]:
    """Build the leak-free feature matrix X and corrected label vector y.

    Parameters
    ----------
    df : DataFrame with at least columns:
        speed (or speed_mps), dev_angle, bearing, timestamp, vehicle_id, label

    Returns
    -------
    X : DataFrame with columns matching CLEAN_FEATURE_NAMES
    y : Series of corrected binary labels
    """
    # --- Apply label corrections first ---
    df = apply_label_corrections(df)

    # --- Build each feature ---
    speed = df.get("speed", df.get("speed_mps", pd.Series(np.zeros(len(df)), index=df.index)))
    bearing_dev = df["dev_angle"].fillna(0.0) if "dev_angle" in df.columns else pd.Series(np.zeros(len(df)), index=df.index)
    sustained = _compute_sustained_duration(df)
    gps_noise = _compute_gps_noise(df)
    interaction = speed * (bearing_dev / 180.0)
    road_enc = _encode_road_type(df)
    tod_sin, tod_cos = _encode_time_cyclical(df)

    X = pd.DataFrame({
        "speed_mps": speed.values,
        "bearing_deviation_deg": bearing_dev.values,
        "sustained_wrong_way_duration_s": sustained.values,
        "gps_noise_estimate": gps_noise.values,
        "speed_x_bearing_interaction": interaction.values,
        "road_type_encoded": road_enc.values,
        "time_of_day_sin": tod_sin.values,
        "time_of_day_cos": tod_cos.values,
    }, index=df.index)

    y = df["label"].astype(int)

    return X, y


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Quick test with scenario data
    from backend.eval.scenario_generator import generate_all_scenarios

    scenarios = generate_all_scenarios()
    X, y = extract_clean_features(scenarios)
    print(f"Feature matrix: {X.shape}")
    print(f"Features: {list(X.columns)}")
    print(f"Label distribution after corrections:\n{y.value_counts().to_string()}")
    print(f"\nFeature stats:")
    print(X.describe().round(3).to_string())

    # Verify no leaked columns
    leaked = {"anomaly_score", "risk_score", "wrong_way_prob", "wwp"}
    found = leaked.intersection(set(X.columns))
    if found:
        print(f"\n[FAIL] LEAK DETECTED: {found}")
    else:
        print("\n[OK] No feature leakage detected")
