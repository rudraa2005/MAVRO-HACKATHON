#!/usr/bin/env python
"""
train_logistic.py
=================
Improved training pipeline for LogisticRiskModel.

Improvements over the baseline train_synthetic()
-------------------------------------------------
* Structured dataset with 5 realistic driving scenarios
* 80/20 stratified train/validation split
* Per-epoch train + validation loss tracking
* Early stopping (patience = 5 epochs on val loss)
* L2 regularisation to reduce overfitting
* Saves weights to  instance/logistic_risk_model.npz

Usage
-----
    python train_logistic.py
    python train_logistic.py --epochs 1000 --lr 0.05 --patience 10 --seed 7
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Ensure UTF-8 output on Windows
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Feature names (must match LogisticRiskModel column order in ml_intelligence)
# ---------------------------------------------------------------------------
FEATURE_NAMES = [
    "inv_ttc",           # 1 / time-to-collision
    "speed",             # m/s
    "relative_speed",    # closing speed m/s
    "angle_dev",         # degrees
    "temporal_state",    # 0=NORMAL 1=SUSPECT 2=CONFIRMED
    "collision_prob",    # Monte-Carlo collision probability
    "uncertainty",       # positional uncertainty / GPS noise
]

# ---------------------------------------------------------------------------
# Dataset generation
# ---------------------------------------------------------------------------

def _sigmoid(x: np.ndarray, smoothing: float = 0.0) -> np.ndarray:
    """Numerically stable element-wise sigmoid with optional label smoothing."""
    pos = x >= 0
    result = np.empty_like(x, dtype=float)
    result[pos]  = 1.0 / (1.0 + np.exp(-x[pos]))
    e = np.exp(x[~pos])
    result[~pos] = e / (1.0 + e)

    if smoothing > 0:
        # Pull values slightly away from 0 and 1
        result = result * (1.0 - smoothing) + 0.5 * smoothing
    return result


def _bce_loss(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Binary cross-entropy loss with epsilon clip to avoid log(0)."""
    eps = 1e-9
    p = np.clip(y_pred, eps, 1.0 - eps)
    return float(-np.mean(y_true * np.log(p) + (1.0 - y_true) * np.log(1.0 - p)))


def generate_dataset(
    total_samples: int = 2000,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """Generate a stratified synthetic dataset with 5 scenario classes.

    Scenarios
    ---------
    1. correct_driving    -- low speed, aligned, low TTC risk
    2. slight_angle_dev   -- small angle drift, GPS jitter noise
    3. slow_drift         -- gradual heading deviation, low speed
    4. gps_noise          -- random spikes in angle_dev and uncertainty
    5. real_wrong_way     -- large angle, sustained temporal state, high col prob

    Label
    -----
    Ground truth label is derived from the same latent risk formula used
    by the original train_synthetic() so the model objective is identical.
    """
    rng = np.random.default_rng(seed)
    n_per = total_samples // 5
    remainder = total_samples - n_per * 5
    counts = [n_per] * 5
    counts[-1] += remainder   # add any leftover to last scenario

    rows: list[np.ndarray] = []

    # --- 1. Correct driving -----------------------------------------------
    n = counts[0]
    ttc = rng.uniform(5.0, 15.0, n)
    rows.append(np.column_stack([
        1.0 / np.maximum(ttc, 0.1),           # inv_ttc (low)
        rng.uniform(3.0, 10.0, n),             # speed (moderate)
        rng.uniform(0.0, 3.0, n),              # relative_speed (low)
        rng.uniform(0.0, 15.0, n),             # angle_dev (small)
        np.zeros(n),                            # temporal_state = NORMAL
        rng.uniform(0.0, 0.15, n),             # collision_prob (low)
        rng.uniform(0.0, 0.3, n),              # uncertainty (low)
    ]))

    # --- 2. Slight angle deviation (GPS jitter / intersection turns) -------
    n = counts[1]
    ttc = rng.uniform(4.0, 12.0, n)
    rows.append(np.column_stack([
        1.0 / np.maximum(ttc, 0.1),
        rng.uniform(4.0, 14.0, n),
        rng.uniform(0.0, 8.0, n),
        rng.uniform(15.0, 60.0, n),            # moderate angle
        rng.integers(0, 2, n).astype(float),   # NORMAL / SUSPECT
        rng.uniform(0.0, 0.35, n),
        rng.uniform(0.3, 0.9, n),              # high uncertainty = GPS noise
    ]))

    # --- 3. Slow drift (gradual wrong-way approach) ----------------------
    n = counts[2]
    ttc = rng.uniform(2.5, 8.0, n)
    rows.append(np.column_stack([
        1.0 / np.maximum(ttc, 0.1),
        rng.uniform(2.0, 7.0, n),              # slow speed
        rng.uniform(2.0, 10.0, n),
        rng.uniform(60.0, 130.0, n),           # growing deviation
        rng.choice([0.0, 1.0], n),             # mostly NORMAL / SUSPECT
        rng.uniform(0.0, 0.5, n),
        rng.uniform(0.2, 0.7, n),
    ]))

    # --- 4. GPS noise (false-positive candidate) -------------------------
    n = counts[3]
    ttc = rng.uniform(6.0, 20.0, n)
    rows.append(np.column_stack([
        1.0 / np.maximum(ttc, 0.1),
        rng.uniform(3.0, 12.0, n),
        rng.uniform(0.0, 5.0, n),
        rng.uniform(0.0, 180.0, n),            # random angle spike
        np.zeros(n),                            # stays NORMAL despite angle
        rng.uniform(0.0, 0.2, n),
        rng.uniform(0.8, 1.5, n),              # very high uncertainty
    ]))

    # --- 5. Real wrong-way -----------------------------------------------
    n = counts[4]
    ttc = rng.uniform(0.3, 4.0, n)            # critical TTC
    rows.append(np.column_stack([
        1.0 / np.maximum(ttc, 0.1),           # inv_ttc (high)
        rng.uniform(8.0, 25.0, n),             # fast
        rng.uniform(10.0, 30.0, n),            # high closing speed
        rng.uniform(140.0, 180.0, n),          # near-180 angle
        rng.choice([1.0, 2.0], n),             # SUSPECT or CONFIRMED
        rng.uniform(0.55, 1.0, n),             # high collision prob
        rng.uniform(0.0, 0.5, n),
    ]))

    X = np.vstack(rows).astype(float)

    # Labels via same latent formula as baseline (consistent objective)
    inv_ttc      = X[:, 0]
    speed        = X[:, 1]
    rel_speed    = X[:, 2]
    angle_dev    = X[:, 3]
    temp_state   = X[:, 4]
    col_prob     = X[:, 5]
    uncertainty  = X[:, 6]

    latent = (
        2.7  * inv_ttc
        + 0.055 * speed
        + 0.085 * rel_speed
        + 0.012 * angle_dev
        + 0.85  * temp_state
        + 2.4   * col_prob
        + 0.75  * uncertainty
        - 4.5
    )
    # Applying label smoothing at generation time to induce robustness
    probs = _sigmoid(latent, smoothing=0.08)
    y = (rng.uniform(size=len(X)) < probs).astype(float)

    # Shuffle
    idx = rng.permutation(len(X))
    return X[idx], y[idx]


# ---------------------------------------------------------------------------
# Train / validation split (stratified on label)
# ---------------------------------------------------------------------------

def stratified_split(
    X: np.ndarray,
    y: np.ndarray,
    val_frac: float = 0.20,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """80/20 stratified split preserving class balance."""
    rng = np.random.default_rng(seed)

    pos_idx = np.where(y == 1)[0]
    neg_idx = np.where(y == 0)[0]

    def _split(idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        idx = rng.permutation(idx)
        cut = max(1, int(len(idx) * val_frac))
        return idx[cut:], idx[:cut]

    tr_pos, va_pos = _split(pos_idx)
    tr_neg, va_neg = _split(neg_idx)

    tr_idx = rng.permutation(np.concatenate([tr_pos, tr_neg]))
    va_idx = rng.permutation(np.concatenate([va_pos, va_neg]))

    return X[tr_idx], y[tr_idx], X[va_idx], y[va_idx]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def fit_normalizer(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = X.mean(axis=0)
    std  = X.std(axis=0)
    std  = np.where(std < 1e-6, 1.0, std)
    return mean, std


def normalize(X: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    return (X - mean) / std


# ---------------------------------------------------------------------------
# Training loop with early stopping
# ---------------------------------------------------------------------------

def train(
    X_tr:    np.ndarray,
    y_tr:    np.ndarray,
    X_va:    np.ndarray,
    y_va:    np.ndarray,
    *,
    epochs:    int   = 800,
    lr:        float = 0.10,
    l2:        float = 1e-4,   # L2 regularisation coefficient
    patience:  int   = 5,
) -> dict:
    """Gradient-descent logistic regression with early stopping.

    Returns
    -------
    dict with keys: weights, bias, history, best_epoch, final_val_loss
    """
    n_tr = float(len(X_tr))

    weights = np.zeros(X_tr.shape[1], dtype=float)
    bias    = 0.0

    history: list[dict] = []

    best_val_loss  = math.inf
    best_weights   = weights.copy()
    best_bias      = bias
    patience_count = 0
    best_epoch     = 0

    for epoch in range(1, epochs + 1):
        # Forward pass
        logits_tr = X_tr @ weights + bias
        preds_tr  = _sigmoid(logits_tr)

        # Backward pass
        error    = preds_tr - y_tr
        grad_w   = (X_tr.T @ error) / n_tr + l2 * weights   # L2 penalty
        grad_b   = float(np.mean(error))
        weights -= lr * grad_w
        bias    -= lr * grad_b

        # Losses
        train_loss = _bce_loss(y_tr, preds_tr)

        logits_va = X_va @ weights + bias
        preds_va  = _sigmoid(logits_va)
        val_loss  = _bce_loss(y_va, preds_va)

        history.append({
            "epoch":      epoch,
            "train_loss": round(train_loss, 6),
            "val_loss":   round(val_loss,   6),
        })

        # Early stopping check
        if val_loss < best_val_loss - 1e-6:
            best_val_loss  = val_loss
            best_weights   = weights.copy()
            best_bias      = bias
            patience_count = 0
            best_epoch     = epoch
        else:
            patience_count += 1
            if patience_count >= patience:
                print(f"  Early stopping at epoch {epoch} "
                      f"(best epoch {best_epoch}, val_loss={best_val_loss:.6f})")
                break

    return {
        "weights":    best_weights,
        "bias":       best_bias,
        "history":    history,
        "best_epoch": best_epoch,
        "final_val_loss": best_val_loss,
    }


def cross_validate(
    X: np.ndarray,
    y: np.ndarray,
    k: int = 5,
    seed: int = 42,
    **train_kwargs
) -> dict:
    """Perform K-Fold Cross Validation and return aggregated results."""
    rng = np.random.default_rng(seed)
    indices = rng.permutation(len(X))
    fold_size = len(X) // k

    fold_results = []
    best_weights_list = []
    best_bias_list    = []

    for i in range(k):
        print(f"  --- Fold {i+1}/{k} ---")
        va_idx = indices[i * fold_size : (i + 1) * fold_size]
        tr_idx = np.concatenate([indices[:i * fold_size], indices[(i + 1) * fold_size:]])

        X_tr, y_tr = X[tr_idx], y[tr_idx]
        X_va, y_va = X[va_idx], y[va_idx]

        mean, std = fit_normalizer(X_tr)
        X_tr_n = normalize(X_tr, mean, std)
        X_va_n = normalize(X_va, mean, std)

        res = train(X_tr_n, y_tr, X_va_n, y_va, **train_kwargs)

        # Eval on val fold
        va_proba = _sigmoid(X_va_n @ res["weights"] + res["bias"])
        va_acc = accuracy(y_va, va_proba)
        va_metrics = precision_recall_f1(y_va, va_proba)

        fold_results.append({
            "val_accuracy": va_acc,
            "val_f1":       va_metrics["f1"],
            "val_loss":     res["final_val_loss"],
            "best_epoch":   res["best_epoch"],
        })
        best_weights_list.append(res["weights"])
        best_bias_list.append(res["bias"])

    # Average metrics
    avg_acc = float(np.mean([r["val_accuracy"] for r in fold_results]))
    avg_f1  = float(np.mean([r["val_f1"] for r in fold_results]))

    # Average weights for the final ensemble-like robust model
    final_weights = np.mean(best_weights_list, axis=0)
    final_bias    = float(np.mean(best_bias_list))
    avg_best_epoch = int(np.mean([r.get("best_epoch", 0) for r in fold_results]))

    return {
        "weights": final_weights,
        "bias":    final_bias,
        "avg_val_accuracy": avg_acc,
        "avg_val_f1":      avg_f1,
        "avg_best_epoch":  avg_best_epoch,
        "folds":           fold_results,
    }


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------

def accuracy(y_true: np.ndarray, y_pred_proba: np.ndarray, threshold: float = 0.5) -> float:
    pred_labels = (y_pred_proba >= threshold).astype(float)
    return float(np.mean(pred_labels == y_true))


def precision_recall_f1(
    y_true: np.ndarray,
    y_pred_proba: np.ndarray,
    threshold: float = 0.5,
) -> dict[str, float]:
    pred = (y_pred_proba >= threshold).astype(float)
    tp = float(np.sum((y_true == 1) & (pred == 1)))
    fp = float(np.sum((y_true == 0) & (pred == 1)))
    fn = float(np.sum((y_true == 1) & (pred == 0)))
    tn = float(np.sum((y_true == 0) & (pred == 0)))
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec  = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1   = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    return {
        "precision": round(prec, 4),
        "recall":    round(rec,  4),
        "f1":        round(f1,   4),
        "tp": int(tp), "fp": int(fp), "tn": int(tn), "fn": int(fn),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train LogisticRiskModel with early stopping")
    p.add_argument("--samples",  type=int,   default=2000,  help="Dataset size (default: 2000)")
    p.add_argument("--epochs",   type=int,   default=800,   help="Max training epochs (default: 800)")
    p.add_argument("--lr",       type=float, default=0.10,  help="Learning rate (default: 0.10)")
    p.add_argument("--l2",       type=float, default=1e-4,  help="L2 regularisation (default: 1e-4)")
    p.add_argument("--patience", type=int,   default=5,     help="Early-stop patience epochs (default: 5)")
    p.add_argument("--seed",     type=int,   default=42,    help="Random seed (default: 42)")
    p.add_argument("--out",      type=str,   default="instance/logistic_risk_model.npz",
                   help="Output path for saved weights (default: instance/logistic_risk_model.npz)")
    p.add_argument("--history",  type=str,   default=None,
                   help="Optional path to save loss history as JSON")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    sep  = "=" * 60
    sep2 = "-" * 60

    print(sep)
    print("  LogisticRiskModel -- Training Pipeline")
    print(sep)
    print(f"  Samples   : {args.samples}")
    print(f"  Max epochs: {args.epochs}")
    print(f"  LR        : {args.lr}")
    print(f"  L2        : {args.l2}")
    print(f"  Patience  : {args.patience}")
    print(f"  Seed      : {args.seed}")
    print(f"  Output    : {args.out}")
    print(sep2)

    # 1. Generate dataset -----------------------------------------------
    print("Generating dataset...")
    t0 = time.monotonic()
    X, y = generate_dataset(total_samples=args.samples, seed=args.seed)
    n_pos = int(y.sum());  n_neg = len(y) - n_pos
    print(f"  Total    : {len(X):,}  |  Positive (high-risk): {n_pos}  |  Negative: {n_neg}")
    print(f"  Scenarios: 5 (correct / slight-dev / slow-drift / gps-noise / wrong-way)")

    # 2. Train/val split ------------------------------------------------
    X_tr, y_tr, X_va, y_va = stratified_split(X, y, val_frac=0.20, seed=args.seed)
    print(f"  Train    : {len(X_tr):,}  (pos={int(y_tr.sum())}, neg={len(y_tr)-int(y_tr.sum())})")
    print(f"  Val      : {len(X_va):,}  (pos={int(y_va.sum())}, neg={len(y_va)-int(y_va.sum())})")

    # 3. Normalise (fit on train only, apply to both) -------------------
    mean, std = fit_normalizer(X_tr)
    X_tr_n = normalize(X_tr, mean, std)
    X_va_n = normalize(X_va, mean, std)

    # 4. Train (K-Fold CV) -----------------------------------------------
    print(sep2)
    print(f"Training with 5-Fold Cross-Validation...")
    cv_result = cross_validate(
        X, y, k=5, seed=args.seed,
        epochs=args.epochs,
        lr=args.lr,
        l2=args.l2,
        patience=args.patience
    )
    elapsed = time.monotonic() - t0

    weights = cv_result["weights"]
    bias    = cv_result["bias"]
    best_epoch = cv_result["avg_best_epoch"]

    # 5. Final metrics --------------------------------------------------
    # Use global mean/std for the final saved model
    mean, std = fit_normalizer(X)
    X_n = normalize(X, mean, std)
    all_proba = _sigmoid(X_n @ weights + bias)

    all_acc = accuracy(y, all_proba)
    all_metrics = precision_recall_f1(y, all_proba)

    print(sep2)
    print("  Results (K-Fold CV Aggregated)")
    print(sep2)
    print(f"  Avg Val Accuracy: {cv_result['avg_val_accuracy']:.4f}")
    print(f"  Avg Val F1-Score: {cv_result['avg_val_f1']:.4f}")
    print(f"  Global Accuracy : {all_acc:.4f}")
    print(f"  Global P/R/F1  : {all_metrics['precision']}/{all_metrics['recall']}/{all_metrics['f1']}")
    print(f"  Runtime         : {elapsed*1000:.0f} ms")

    # 6. Save model weights ----------------------------------------
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        out_path,
        weights=weights,
        bias=np.array([bias]),
        mean=mean,
        std=std,
        feature_names=np.array(FEATURE_NAMES),
        best_epoch=np.array([best_epoch]),
        val_accuracy=np.array([cv_result["avg_val_accuracy"]]),
        train_accuracy=np.array([all_acc]),
    )
    print(sep2)
    print(f"  Saved -> {out_path.resolve()}")

    # Optional: save loss history
    if args.history:
        hist_path = Path(args.history)
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        print(f"  History -> {hist_path.resolve()}")

    # 7. JSON summary ---------------------------------------------------
    print(sep)
    summary = {
        "accuracy":        round(all_acc, 4),
        "val_accuracy":    round(cv_result["avg_val_accuracy"], 4),
        "precision":       all_metrics["precision"],
        "recall":          all_metrics["recall"],
        "f1":              all_metrics["f1"],
        "fpr":             round(
            all_metrics["fp"] / max(all_metrics["fp"] + all_metrics["tn"], 1), 4
        ),
        "model_path":      str(out_path.resolve()),
        "n_features":      len(FEATURE_NAMES),
        "feature_names":   FEATURE_NAMES,
    }
    print(json.dumps(summary, indent=2))
    print(sep)


if __name__ == "__main__":
    main()
