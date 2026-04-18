from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ConfusionCounts:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.tn + self.fn


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator <= 0.0:
        return 0.0
    return numerator / denominator


def _to_label(value: Any) -> int:
    return 1 if bool(value) else 0


def _to_score(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def confusion_from_scores(
    y_true: list[int],
    y_score: list[float],
    *,
    threshold: float,
) -> ConfusionCounts:
    counts = ConfusionCounts()
    for actual, score in zip(y_true, y_score, strict=False):
        predicted = 1 if score >= threshold else 0
        if actual == 1 and predicted == 1:
            counts.tp += 1
        elif actual == 0 and predicted == 1:
            counts.fp += 1
        elif actual == 0 and predicted == 0:
            counts.tn += 1
        else:
            counts.fn += 1
    return counts


def metrics_from_confusion(counts: ConfusionCounts) -> dict[str, float]:
    precision = _safe_div(counts.tp, counts.tp + counts.fp)
    recall = _safe_div(counts.tp, counts.tp + counts.fn)
    fpr = _safe_div(counts.fp, counts.fp + counts.tn)
    tpr = recall
    accuracy = _safe_div(counts.tp + counts.tn, counts.total)
    specificity = _safe_div(counts.tn, counts.tn + counts.fp)
    f1 = _safe_div(2.0 * precision * recall, precision + recall)

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "fpr": round(fpr, 4),
        "tpr": round(tpr, 4),
        "accuracy": round(accuracy, 4),
        "specificity": round(specificity, 4),
        "f1": round(f1, 4),
    }


def roc_curve_points(y_true: list[int], y_score: list[float]) -> list[dict[str, float]]:
    if not y_true or not y_score:
        return []

    thresholds = sorted(set(y_score), reverse=True)
    if thresholds[0] < 1.0:
        thresholds = [1.0] + thresholds
    if thresholds[-1] > 0.0:
        thresholds = thresholds + [0.0]

    points: list[dict[str, float]] = []
    for threshold in thresholds:
        counts = confusion_from_scores(y_true, y_score, threshold=threshold)
        metrics = metrics_from_confusion(counts)
        points.append(
            {
                "threshold": round(float(threshold), 4),
                "fpr": metrics["fpr"],
                "tpr": metrics["tpr"],
            }
        )

    points.sort(key=lambda p: (p["fpr"], p["tpr"]))
    deduped: list[dict[str, float]] = []
    seen: set[tuple[float, float]] = set()
    for point in points:
        key = (point["fpr"], point["tpr"])
        if key in seen:
            continue
        deduped.append(point)
        seen.add(key)
    return deduped


def auc_from_roc(points: list[dict[str, float]]) -> float:
    if len(points) < 2:
        return 0.0
    auc = 0.0
    for left, right in zip(points, points[1:], strict=False):
        width = max(0.0, right["fpr"] - left["fpr"])
        height = (left["tpr"] + right["tpr"]) / 2.0
        auc += width * height
    return round(min(max(auc, 0.0), 1.0), 4)


def evaluate_binary_classifier(
    records: list[dict[str, Any]],
    *,
    threshold: float,
) -> dict[str, Any]:
    y_true = [_to_label(record.get("ground_truth")) for record in records]
    y_score = [_to_score(record.get("score")) for record in records]

    counts = confusion_from_scores(y_true, y_score, threshold=threshold)
    metrics = metrics_from_confusion(counts)
    roc_points = roc_curve_points(y_true, y_score)

    positives = sum(y_true)
    negatives = len(y_true) - positives
    has_both_classes = positives > 0 and negatives > 0

    return {
        "samples": len(records),
        "positives": positives,
        "negatives": negatives,
        "threshold": round(float(threshold), 4),
        "confusion": {
            "tp": counts.tp,
            "fp": counts.fp,
            "tn": counts.tn,
            "fn": counts.fn,
        },
        "metrics": metrics,
        "roc_curve": roc_points,
        "auc": auc_from_roc(roc_points) if has_both_classes else None,
        "warnings": (
            []
            if has_both_classes
            else ["ROC/AUC is not statistically meaningful without both classes present."]
        ),
    }
