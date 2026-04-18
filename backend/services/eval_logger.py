"""
eval_logger.py
==============
Real-time evaluation pipeline for FlowGuard wrong-way detection.

Responsibilities
----------------
- Receives per-frame vehicle data from the simulation tick.
- Applies ground-truth labels:   wrong_way == True  -> label = 1
                                  otherwise          -> label = 0
- Applies predicted label:       wwp >= threshold   -> pred = 1
- Accumulates logs in a fixed-size ring buffer (zero heap allocation on push).
- Computes full confusion matrix + derived metrics on demand.
- Optionally dumps the buffer to CSV.

No external libraries — only stdlib + numpy.
"""

from __future__ import annotations

import csv
import io
import time
from collections import deque
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BUFFER_MAXLEN: int = 10_000          # max log entries kept in memory
DEFAULT_THRESHOLD: float = 0.65      # matches EVAL_WRONG_WAY_THRESHOLD in config
ROC_POINTS: int = 50                 # number of threshold steps for ROC curve


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass

class EvalRecord:
    """One logged observation: one vehicle at one timestamp."""
    timestamp: float
    vehicle_id: int
    wrong_way_probability: float     # raw score from engine (wwp)
    predicted_label: int             # 1 if wwp >= threshold else 0
    ground_truth_label: int          # 1 if vehicle.wrong_way else 0


# ---------------------------------------------------------------------------
# Ring buffer + evaluation engine
# ---------------------------------------------------------------------------

class EvalLogger:
    """Thread-safe evaluation logger.

    All writes come from the simulation tick thread.
    Reads come from the Flask request thread.
    A threading.Lock is NOT used here — Python's GIL protects deque.append()
    and deque iteration, which is sufficient for this use-case.
    """

    def __init__(
        self,
        maxlen: int = BUFFER_MAXLEN,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self._buf: deque[EvalRecord] = deque(maxlen=maxlen)
        self._threshold = threshold

    # ------------------------------------------------------------------
    # Write path (called from simulation tick — must be < 1ms overhead)
    # ------------------------------------------------------------------

    def log_frame(self, vehicles: list[dict[str, Any]]) -> None:
        """Log one observation per vehicle in the current tick.

        Expected vehicle dict fields:
            id / vehicle_id   -- int
            wwp               -- float in [0, 1]   (wrong-way probability)
            wrong_way         -- bool               (ground truth)
            timestamp         -- float              (unix epoch; optional)
        """
        ts = time.time()
        threshold = self._threshold

        for v in vehicles:
            vid = int(v.get("db_id", v.get("id", v.get("vehicle_id", 0))))
            wwp = float(v.get("wwp", 0.0))
            gt = 1 if bool(v.get("wrong_way", False)) else 0
            pred = 1 if wwp >= threshold else 0
            vts = float(v.get("timestamp", ts))

            self._buf.append(
                EvalRecord(
                    timestamp=vts,
                    vehicle_id=vid,
                    wrong_way_probability=round(wwp, 4),
                    predicted_label=pred,
                    ground_truth_label=gt,
                )
            )

    # ------------------------------------------------------------------
    # Read path (called from Flask route thread)
    # ------------------------------------------------------------------

    def get_logs(self) -> list[EvalRecord]:
        """Return a snapshot of the current buffer as a plain list."""
        return list(self._buf)

    def get_threshold(self) -> float:
        return self._threshold

    def set_threshold(self, threshold: float) -> None:
        self._threshold = float(max(0.0, min(1.0, threshold)))

    def clear(self) -> None:
        self._buf.clear()

    # ------------------------------------------------------------------
    # Confusion matrix + metrics (numpy, no pandas)
    # ------------------------------------------------------------------

    def compute_confusion_matrix(
        self,
        logs: list[EvalRecord] | None = None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """Compute TP/FP/TN/FN and all derived metrics.

        Parameters
        ----------
        logs      : optional snapshot. If None, uses current buffer contents.
        threshold : if provided, recalculates predicted labels using this
                    decision boundary instead of the pre-computed labels.

        Returns
        -------
        dict with keys:
            samples, positives, negatives,
            threshold,
            confusion: {tp, fp, tn, fn},
            metrics:   {accuracy, precision, recall, f1, fpr, fnr}
        """
        records = logs if logs is not None else self.get_logs()

        # Effective threshold for this result dict
        eff_threshold = threshold if threshold is not None else self._threshold

        if not records:
            return _empty_result(eff_threshold)

        y_true = np.array([r.ground_truth_label for r in records], dtype=np.int8)

        if threshold is not None:
            # Recalculate based on raw scores
            y_score = np.array([r.wrong_way_probability for r in records], dtype=np.float32)
            y_pred = (y_score >= threshold).astype(np.int8)
        else:
            # Use pre-computed labels
            y_pred = np.array([r.predicted_label for r in records], dtype=np.int8)

        tp = int(np.sum((y_true == 1) & (y_pred == 1)))
        fp = int(np.sum((y_true == 0) & (y_pred == 1)))
        tn = int(np.sum((y_true == 0) & (y_pred == 0)))
        fn = int(np.sum((y_true == 1) & (y_pred == 0)))

        total = tp + fp + tn + fn
        positives = int(np.sum(y_true == 1))
        negatives = total - positives

        accuracy  = _div(tp + tn, total)
        precision = _div(tp, tp + fp)
        recall    = _div(tp, tp + fn)      # TPR / sensitivity
        f1        = _div(2.0 * precision * recall, precision + recall)
        fpr       = _div(fp, fp + tn)      # false positive rate
        fnr       = _div(fn, fn + tp)      # false negative rate (miss rate)

        return {
            "samples":   total,
            "positives": positives,
            "negatives": negatives,
            "threshold": round(self._threshold, 4),
            "confusion": {
                "tp": tp,
                "fp": fp,
                "tn": tn,
                "fn": fn,
            },
            "metrics": {
                "accuracy":  round(accuracy,  4),
                "precision": round(precision, 4),
                "recall":    round(recall,    4),
                "f1":        round(f1,        4),
                "fpr":       round(fpr,       4),
                "fnr":       round(fnr,       4),
            },
        }

    # ------------------------------------------------------------------
    # ROC curve + AUC
    # ------------------------------------------------------------------

    def compute_roc_auc(
        self,
        logs: list[EvalRecord] | None = None,
        n_thresholds: int = ROC_POINTS,
    ) -> dict[str, Any]:
        """Compute a ROC curve and AUC from the logged score/label data.

        Algorithm
        ---------
        1. Extract y_true  (ground_truth_label) and
                   y_score (wrong_way_probability) from the buffer.
        2. Sweep n_thresholds values linearly from 1.0 → 0.0 (inclusive).
           For each threshold t:  y_pred[i] = 1 if y_score[i] >= t else 0
        3. Compute TPR = TP / (TP + FN)  and  FPR = FP / (FP + TN)
           per threshold with full numerical stability (_div guards /0).
        4. AUC via np.trapz (trapezoidal rule) over the FPR axis.
           Result clamped to [0, 1].

        Returns
        -------
        dict with keys:
            samples, positives, negatives,
            thresholds  : list[float]   length = n_thresholds
            fpr         : list[float]
            tpr         : list[float]
            auc         : float  in [0, 1]  (None if only one class present)
            warnings    : list[str]
        """
        records = logs if logs is not None else self.get_logs()

        if not records:
            return _empty_roc_result(n_thresholds)

        y_true  = np.array([r.ground_truth_label    for r in records], dtype=np.float32)
        y_score = np.array([r.wrong_way_probability for r in records], dtype=np.float32)

        # Clamp scores to [0, 1] for numerical safety
        y_score = np.clip(y_score, 0.0, 1.0)

        positives = int(np.sum(y_true == 1))
        negatives = int(np.sum(y_true == 0))
        total     = positives + negatives

        warnings: list[str] = []
        if positives == 0 or negatives == 0:
            warnings.append(
                "ROC/AUC is not meaningful: only one class present in the buffer."
            )

        # Threshold grid: 50 evenly-spaced values from 1.0 down to 0.0
        # Shape: (n_thresholds,)
        thresholds = np.linspace(1.0, 0.0, max(n_thresholds, 2))

        # Broadcast comparison: shape (n_records, n_thresholds)
        # y_score[:, None] >= thresholds[None, :]  →  predicted positive matrix
        pred_matrix = (y_score[:, np.newaxis] >= thresholds[np.newaxis, :]).astype(np.int8)

        # y_true tiled for broadcasting: shape (n_records, n_thresholds)
        gt_matrix = y_true[:, np.newaxis].astype(np.int8)

        # Per-threshold confusion counts  (sum over records axis=0)
        tp_vec = np.sum((gt_matrix == 1) & (pred_matrix == 1), axis=0).astype(float)
        fp_vec = np.sum((gt_matrix == 0) & (pred_matrix == 1), axis=0).astype(float)
        fn_vec = np.sum((gt_matrix == 1) & (pred_matrix == 0), axis=0).astype(float)
        tn_vec = np.sum((gt_matrix == 0) & (pred_matrix == 0), axis=0).astype(float)

        # TPR = TP / (TP + FN),  FPR = FP / (FP + TN)
        # Vectorised safe division: where denominator == 0 -> output 0
        # np.errstate suppresses the RuntimeWarning that np.where raises when
        # it evaluates the division branch on zero-denominator elements
        # (the result for those positions is discarded by the mask).
        with np.errstate(invalid="ignore", divide="ignore"):
            denom_tpr = tp_vec + fn_vec
            denom_fpr = fp_vec + tn_vec
            tpr_vec = np.where(denom_tpr > 0.0, tp_vec / denom_tpr, 0.0)
            fpr_vec = np.where(denom_fpr > 0.0, fp_vec / denom_fpr, 0.0)

        # AUC: trapezoidal rule integrated over FPR axis
        # np.trapz expects x to be monotonically increasing,
        # so we sort by fpr before integrating.
        sort_idx   = np.argsort(fpr_vec)
        fpr_sorted = fpr_vec[sort_idx]
        tpr_sorted = tpr_vec[sort_idx]
        auc_raw    = float(np.trapz(tpr_sorted, fpr_sorted))
        auc        = round(float(np.clip(auc_raw, 0.0, 1.0)), 4)

        return {
            "samples":    total,
            "positives":  positives,
            "negatives":  negatives,
            "thresholds": [round(float(t), 4) for t in thresholds.tolist()],
            "fpr":        [round(float(v), 4) for v in fpr_vec.tolist()],
            "tpr":        [round(float(v), 4) for v in tpr_vec.tolist()],
            "auc":        auc if (positives > 0 and negatives > 0) else None,
            "warnings":   warnings,
        }

    # ------------------------------------------------------------------
    # Youden's J-statistic threshold optimization
    # ------------------------------------------------------------------

    def optimal_threshold(
        self,
        logs: list[EvalRecord] | None = None,
        n_thresholds: int = ROC_POINTS,
        *,
        apply: bool = False,
    ) -> dict:
        """Find the threshold that maximises Youden's J = TPR - FPR.

        Parameters
        ----------
        logs          : evaluation records (defaults to current buffer).
        n_thresholds  : granularity of the ROC sweep.
        apply         : if True, update ``self.threshold`` to the optimal value.

        Returns
        -------
        dict with keys:
            best_threshold, best_tpr, best_fpr, best_j,
            previous_threshold (if apply=True), applied
        """
        roc = self.compute_roc_auc(logs=logs, n_thresholds=n_thresholds)

        thresholds = roc["thresholds"]
        tpr_list   = roc["tpr"]
        fpr_list   = roc["fpr"]

        if not thresholds or roc["auc"] is None:
            return {
                "best_threshold": None,
                "best_tpr":       None,
                "best_fpr":       None,
                "best_j":         None,
                "applied":        False,
                "warnings":       roc.get("warnings", [])
                                + ["Cannot optimise: insufficient data or single class."],
            }

        # Youden's J = TPR - FPR  (maximise)
        j_scores = [tpr - fpr for tpr, fpr in zip(tpr_list, fpr_list)]
        best_idx = int(max(range(len(j_scores)), key=lambda i: j_scores[i]))

        best_threshold = round(thresholds[best_idx], 4)
        best_tpr       = round(tpr_list[best_idx], 4)
        best_fpr       = round(fpr_list[best_idx], 4)
        best_j         = round(j_scores[best_idx], 4)

        result: dict = {
            "best_threshold": best_threshold,
            "best_tpr":       best_tpr,
            "best_fpr":       best_fpr,
            "best_j":         best_j,
            "applied":        False,
            "warnings":       roc.get("warnings", []),
            "auc":            roc["auc"],
        }

        if apply:
            result["previous_threshold"] = self._threshold
            self._threshold = best_threshold
            result["applied"] = True

        return result

    def update_threshold(self, new_threshold: float) -> float:
        """Manually set the decision threshold.  Returns the old value."""
        old = self._threshold
        self._threshold = float(max(0.0, min(1.0, new_threshold)))
        return old

    # ------------------------------------------------------------------
    # CSV dump
    # ------------------------------------------------------------------

    def dump_csv(self, logs: list[EvalRecord] | None = None) -> str:
        """Serialize log records to a CSV string.

        Columns: timestamp, vehicle_id, wrong_way_probability,
                 predicted_label, ground_truth_label
        """
        records = logs if logs is not None else self.get_logs()
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[
                "timestamp",
                "vehicle_id",
                "wrong_way_probability",
                "predicted_label",
                "ground_truth_label",
            ],
        )
        writer.writeheader()
        for r in records:
            writer.writerow(asdict(r))
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0.0 else 0.0


def _empty_result(threshold: float) -> dict[str, Any]:
    return {
        "samples":   0,
        "positives": 0,
        "negatives": 0,
        "threshold": round(threshold, 4),
        "confusion": {"tp": 0, "fp": 0, "tn": 0, "fn": 0},
        "metrics": {
            "accuracy":  0.0,
            "precision": 0.0,
            "recall":    0.0,
            "f1":        0.0,
            "fpr":       0.0,
            "fnr":       0.0,
        },
    }


def _empty_roc_result(n_thresholds: int) -> dict[str, Any]:
    return {
        "samples":    0,
        "positives":  0,
        "negatives":  0,
        "thresholds": [round(float(t), 4) for t in np.linspace(1.0, 0.0, n_thresholds).tolist()],
        "fpr":        [0.0] * n_thresholds,
        "tpr":        [0.0] * n_thresholds,
        "auc":        None,
        "warnings":   ["No log data available yet."],
    }


# ---------------------------------------------------------------------------
# Module-level singleton (imported by engine + route)
# ---------------------------------------------------------------------------

eval_logger = EvalLogger()
