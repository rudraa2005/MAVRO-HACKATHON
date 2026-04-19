from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import math
from typing import Any

import numpy as np
import os
import joblib
import torch
from backend.models.logistic_gpu import LogisticRiskModel
from backend.eval.clean_features import extract_clean_features, CLEAN_FEATURE_NAMES

# Paths to production artifacts
_MODEL_PATH = "backend/models/final_model.pt"
_SCALER_PATH = "backend/models/final_model_scaler.pkl"


TEMPORAL_ENCODING = {
    "NORMAL": 0.0,
    "SUSPECT": 1.0,
    "CONFIRMED": 2.0,
}
BEHAVIOR_LABELS = ("normal", "aggressive", "wrong_way")
_ACCELERATION_MEMORY: dict[int, tuple[float, float]] = {}
_ANOMALY_HISTORY: deque[np.ndarray] = deque(maxlen=256)
_BEHAVIOR_HISTORY: defaultdict[int, deque[np.ndarray]] = defaultdict(lambda: deque(maxlen=12))


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _vehicle_id(vehicle: dict[str, Any]) -> int:
    return int(vehicle.get("vehicle_id", vehicle.get("id")))


def _temporal_state_encoded(vehicle: dict[str, Any]) -> float:
    temporal_state = str(vehicle.get("temporal_state", "NORMAL") or "NORMAL").upper()
    return TEMPORAL_ENCODING.get(temporal_state, 0.0)


def _direction_similarity(vehicle: dict[str, Any]) -> float:
    if "direction_similarity" in vehicle:
        return _float_value(vehicle.get("direction_similarity"))
    if "alignment" in vehicle:
        return _float_value(vehicle.get("alignment"))
    return 1.0


def _angle_dev(vehicle: dict[str, Any]) -> float:
    if "angle_dev" in vehicle:
        return _float_value(vehicle.get("angle_dev"))
    if "angle_diff" in vehicle:
        return _float_value(vehicle.get("angle_diff"))
    similarity = max(-1.0, min(1.0, _direction_similarity(vehicle)))
    return math.degrees(math.acos(similarity))


def _safe_ttc(vehicle: dict[str, Any]) -> float:
    ttc = vehicle.get("ttc")
    if ttc is None:
        return 10.0
    return max(_float_value(ttc, 10.0), 0.1)


def _compute_acceleration(vehicle: dict[str, Any]) -> float:
    vehicle_id = _vehicle_id(vehicle)
    speed = _float_value(vehicle.get("speed", vehicle.get("speed_mps", 0.0)))
    timestamp = _float_value(vehicle.get("timestamp", 0.0))
    previous = _ACCELERATION_MEMORY.get(vehicle_id)

    acceleration = 0.0
    if previous is not None:
        prev_speed, prev_timestamp = previous
        dt = max(timestamp - prev_timestamp, 0.0)
        if dt > 1e-6:
            acceleration = (speed - prev_speed) / dt

    _ACCELERATION_MEMORY[vehicle_id] = (speed, timestamp)
    return acceleration


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-9:
        return 0.0
    return float(np.dot(a, b) / denom)


@dataclass
class LogisticRiskModel:
    weights: np.ndarray
    bias: float
    mean: np.ndarray
    std: np.ndarray

    @classmethod
    def train_synthetic(
        cls,
        *,
        samples: int = 1600,
        learning_rate: float = 0.18,
        epochs: int = 650,
        seed: int = 42,
    ) -> "LogisticRiskModel":
        rng = np.random.default_rng(seed)

        ttc = rng.uniform(0.25, 10.0, size=samples)
        inv_ttc = 1.0 / np.maximum(ttc, 0.1)
        speed = rng.uniform(0.0, 35.0, size=samples)
        relative_speed = rng.uniform(0.0, 30.0, size=samples)
        angle_dev = rng.uniform(0.0, 180.0, size=samples)
        temporal_state = rng.integers(0, 3, size=samples).astype(float)
        collision_probability = rng.uniform(0.0, 1.0, size=samples)
        uncertainty = rng.uniform(0.0, 1.5, size=samples)

        X = np.column_stack(
            [
                inv_ttc,
                speed,
                relative_speed,
                angle_dev,
                temporal_state,
                collision_probability,
                uncertainty,
            ]
        )

        latent = (
            2.7 * inv_ttc
            + 0.055 * speed
            + 0.085 * relative_speed
            + 0.012 * angle_dev
            + 0.85 * temporal_state
            + 2.4 * collision_probability
            + 0.75 * uncertainty
            - 4.5
        )
        probabilities = 1.0 / (1.0 + np.exp(-latent))
        y = (rng.uniform(0.0, 1.0, size=samples) < probabilities).astype(float)

        mean = X.mean(axis=0)
        std = X.std(axis=0)
        std = np.where(std < 1e-6, 1.0, std)
        Xn = (X - mean) / std

        weights = np.zeros(Xn.shape[1], dtype=float)
        bias = 0.0
        n = float(samples)
        lambda_reg = 0.01  # L2 regularization coefficient

        for _ in range(epochs):
            logits = Xn @ weights + bias
            preds = 1.0 / (1.0 + np.exp(-logits))
            error = preds - y
            # Gradient of MSE/BCE + L2 penalty
            grad_w = (Xn.T @ error) / n + lambda_reg * weights
            grad_b = float(np.mean(error))
            weights -= learning_rate * grad_w
            bias -= learning_rate * grad_b

        return cls(weights=weights, bias=bias, mean=mean, std=std)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        Xn = (X - self.mean) / self.std
        logits = Xn @ self.weights + self.bias
        return 1.0 / (1.0 + np.exp(-logits))


@dataclass
class IsolationNode:
    feature_index: int | None = None
    split_value: float | None = None
    left: "IsolationNode | None" = None
    right: "IsolationNode | None" = None
    size: int = 0


def _c_factor(size: int) -> float:
    if size <= 1:
        return 0.0
    if size == 2:
        return 1.0
    return 2.0 * (math.log(size - 1.0) + 0.5772156649) - (2.0 * (size - 1.0) / size)


class SimpleIsolationForest:
    def __init__(self, trees: int = 18, sample_size: int = 64, seed: int = 7) -> None:
        self.trees = trees
        self.sample_size = sample_size
        self.seed = seed
        self._forest: list[IsolationNode] = []
        self._effective_sample_size = 0

    def fit(self, X: np.ndarray) -> "SimpleIsolationForest":
        if len(X) == 0:
            self._forest = []
            self._effective_sample_size = 0
            return self

        rng = np.random.default_rng(self.seed)
        self._effective_sample_size = int(min(self.sample_size, len(X)))
        max_depth = max(1, int(math.ceil(math.log2(max(self._effective_sample_size, 2)))))
        self._forest = []

        for _ in range(self.trees):
            if len(X) <= self._effective_sample_size:
                sample = X
            else:
                indices = rng.choice(len(X), size=self._effective_sample_size, replace=False)
                sample = X[indices]
            self._forest.append(self._build_tree(sample, depth=0, max_depth=max_depth, rng=rng))

        return self

    def _build_tree(
        self,
        X: np.ndarray,
        *,
        depth: int,
        max_depth: int,
        rng: np.random.Generator,
    ) -> IsolationNode:
        node = IsolationNode(size=len(X))
        if len(X) <= 1 or depth >= max_depth:
            return node

        mins = X.min(axis=0)
        maxs = X.max(axis=0)
        valid_features = np.where(maxs - mins > 1e-9)[0]
        if len(valid_features) == 0:
            return node

        feature_index = int(rng.choice(valid_features))
        split_value = float(rng.uniform(mins[feature_index], maxs[feature_index]))
        left_mask = X[:, feature_index] < split_value
        right_mask = ~left_mask
        if not np.any(left_mask) or not np.any(right_mask):
            return node

        node.feature_index = feature_index
        node.split_value = split_value
        node.left = self._build_tree(X[left_mask], depth=depth + 1, max_depth=max_depth, rng=rng)
        node.right = self._build_tree(X[right_mask], depth=depth + 1, max_depth=max_depth, rng=rng)
        return node

    def _path_length(self, node: IsolationNode, row: np.ndarray, depth: int) -> float:
        if node.feature_index is None or node.left is None or node.right is None:
            return depth + _c_factor(node.size)
        if row[node.feature_index] < float(node.split_value):
            return self._path_length(node.left, row, depth + 1)
        return self._path_length(node.right, row, depth + 1)

    def score_samples(self, X: np.ndarray) -> np.ndarray:
        if not self._forest or len(X) == 0:
            return np.zeros(len(X), dtype=float)

        avg_lengths = np.array(
            [
                np.mean([self._path_length(tree, row, 0) for tree in self._forest])
                for row in X
            ],
            dtype=float,
        )
        c_n = max(_c_factor(max(self._effective_sample_size, 2)), 1e-6)
        return np.power(2.0, -avg_lengths / c_n)


class SimpleKMeans:
    def __init__(self, k: int = 3, iterations: int = 18, seed: int = 21) -> None:
        self.k = k
        self.iterations = iterations
        self.seed = seed
        self.centroids: np.ndarray | None = None

    def fit_predict(self, X: np.ndarray) -> np.ndarray:
        if len(X) == 0:
            self.centroids = np.zeros((self.k, 0), dtype=float)
            return np.zeros(0, dtype=int)
        if len(X) <= self.k:
            self.centroids = X.copy()
            return np.arange(len(X), dtype=int)

        rng = np.random.default_rng(self.seed)
        centroids = np.empty((self.k, X.shape[1]), dtype=float)
        first_idx = int(rng.integers(0, len(X)))
        centroids[0] = X[first_idx]

        closest_dist_sq = np.sum((X - centroids[0]) ** 2, axis=1)
        for index in range(1, self.k):
            probs = closest_dist_sq / max(np.sum(closest_dist_sq), 1e-9)
            chosen_idx = int(rng.choice(len(X), p=probs))
            centroids[index] = X[chosen_idx]
            new_dist_sq = np.sum((X - centroids[index]) ** 2, axis=1)
            closest_dist_sq = np.minimum(closest_dist_sq, new_dist_sq)

        labels = np.zeros(len(X), dtype=int)
        for _ in range(self.iterations):
            distances = np.linalg.norm(X[:, np.newaxis, :] - centroids[np.newaxis, :, :], axis=2)
            labels = np.argmin(distances, axis=1)
            for index in range(self.k):
                members = X[labels == index]
                if len(members) > 0:
                    centroids[index] = members.mean(axis=0)

        self.centroids = centroids
        return labels


_LOGISTIC_MODEL = None
_SCALER = None

def _get_production_model():
    global _LOGISTIC_MODEL, _SCALER
    if _LOGISTIC_MODEL is None:
        if os.path.exists(_MODEL_PATH) and os.path.exists(_SCALER_PATH):
            try:
                _LOGISTIC_MODEL = LogisticRiskModel.load(_MODEL_PATH)
                _SCALER = joblib.load(_SCALER_PATH)
                print(f"[ML INFO] Loaded production model from {_MODEL_PATH}")
            except Exception as e:
                print(f"[ML WARNING] Failed to load production model: {e}")
                _LOGISTIC_MODEL = LogisticRiskModel.train_synthetic()
        else:
            print("[ML INFO] Production model not found, using synthetic fallback.")
            _LOGISTIC_MODEL = LogisticRiskModel.train_synthetic()
            _SCALER = None  # Synthetic model has its own normalization
    return _LOGISTIC_MODEL, _SCALER


_ANOMALY_FOREST = SimpleIsolationForest()
_KMEANS_MODEL = SimpleKMeans()


def _logistic_features(vehicles: list[dict[str, Any]]) -> np.ndarray:
    rows = []
    for vehicle in vehicles:
        rows.append(
            [
                1.0 / _safe_ttc(vehicle),
                _float_value(vehicle.get("speed", vehicle.get("speed_mps", 0.0))),
                _float_value(vehicle.get("relative_speed", 0.0)),
                _angle_dev(vehicle),
                _temporal_state_encoded(vehicle),
                _float_value(vehicle.get("collision_probability", 0.0)),
                _float_value(vehicle.get("uncertainty", 0.0)),
            ]
        )
    return np.array(rows, dtype=float)


def _anomaly_features(vehicles: list[dict[str, Any]]) -> np.ndarray:
    rows = []
    for vehicle in vehicles:
        acceleration = _compute_acceleration(vehicle)
        vehicle["acceleration"] = round(acceleration, 4)
        rows.append(
            [
                _float_value(vehicle.get("speed", vehicle.get("speed_mps", 0.0))),
                _angle_dev(vehicle),
                _safe_ttc(vehicle),
                acceleration,
                _float_value(vehicle.get("sustained_duration_s", 0.0)),
            ]
        )
    return np.array(rows, dtype=float)


def _clustering_features(vehicles: list[dict[str, Any]]) -> np.ndarray:
    rows = []
    for vehicle in vehicles:
        rows.append(
            [
                _float_value(vehicle.get("speed", vehicle.get("speed_mps", 0.0))),
                _angle_dev(vehicle),
                _float_value(vehicle.get("collision_probability", 0.0)),
                _float_value(vehicle.get("anomaly_score", 0.0)),
                _float_value(vehicle.get("wrong_way_probability", 0.0)),
            ]
        )
    return np.array(rows, dtype=float)


def _normalize_matrix(X: np.ndarray) -> np.ndarray:
    if len(X) == 0:
        return X
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std = np.where(std < 1e-6, 1.0, std)
    return (X - mean) / std


def _behavior_name_mapping(centroids: np.ndarray) -> dict[int, str]:
    if len(centroids) == 0:
        return {}

    wrong_way_idx = int(np.argmax(centroids[:, 1] + centroids[:, 2] * 180.0 + centroids[:, 4] * 120.0))
    aggressive_candidates = [idx for idx in range(len(centroids)) if idx != wrong_way_idx]
    if aggressive_candidates:
        aggressive_idx = max(aggressive_candidates, key=lambda idx: centroids[idx, 0] + centroids[idx, 3] * 20.0)
    else:
        aggressive_idx = wrong_way_idx

    mapping: dict[int, str] = {}
    for idx in range(len(centroids)):
        if idx == wrong_way_idx:
            mapping[idx] = "wrong_way"
        elif idx == aggressive_idx:
            mapping[idx] = "aggressive"
        else:
            mapping[idx] = "normal"
    return mapping


def _repeat_behavior_score(vehicle: dict[str, Any]) -> float:
    vehicle_id = _vehicle_id(vehicle)
    vector = np.array(
        [
            _float_value(vehicle.get("speed", vehicle.get("speed_mps", 0.0))),
            _angle_dev(vehicle) / 180.0,
            _float_value(vehicle.get("collision_probability", 0.0)),
            _float_value(vehicle.get("uncertainty", 0.0)),
            _float_value(vehicle.get("anomaly_score", 0.0)),
        ],
        dtype=float,
    )

    history = _BEHAVIOR_HISTORY[vehicle_id]
    if history:
        similarity = max(_cosine_similarity(vector, past) for past in history)
    else:
        similarity = 0.0
    history.append(vector)
    return float(similarity)


def apply_learned_risk_model(vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not vehicles:
        return vehicles

    model, scaler = _get_production_model()
    if model is None:
        return vehicles

    # Convert vehicle list to DataFrame for the clean feature extractor
    # We must ensure all required columns are present or mapped
    df_raw = pd.DataFrame(vehicles)
    
    # Map internal keys to expected eval keys if necessary
    # engine.py snap_vehicles uses: id, speed, bearing, angle_diff, gps_quality, etc.
    # clean_features expects: speed, dev_angle, bearing, timestamp, vehicle_id, label
    mapping = {
        "id": "vehicle_id",
        "speed": "speed_mps",
        "angle_diff": "dev_angle",
    }
    for k_orig, k_new in mapping.items():
        if k_orig in df_raw.columns and k_new not in df_raw.columns:
            df_raw[k_new] = df_raw[k_orig]
    
    # Label is not needed for inference, but extract_clean_features expects it
    if "label" not in df_raw.columns:
        df_raw["label"] = 0

    try:
        X_clean, _ = extract_clean_features(df_raw)
        X_vals = X_clean[CLEAN_FEATURE_NAMES].values
        
        # Apply production scaler if available
        if scaler is not None:
            X_vals = scaler.transform(X_vals)

        probabilities = model.predict_proba(X_vals)
        
        for vehicle, probability in zip(vehicles, probabilities, strict=False):
            vehicle["ml_collision_probability"] = round(float(probability), 4)
            # Threshold gating for the dashboard (Realistic behavior)
            vehicle["is_wrong_way_ml"] = bool(probability > 0.75)
    except Exception as e:
        print(f"[ML ERROR] Live inference failed: {e}")

    return vehicles


def apply_anomaly_detection(vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not vehicles:
        return vehicles

    X = _anomaly_features(vehicles)
    for row in X:
        _ANOMALY_HISTORY.append(row)

    if len(_ANOMALY_HISTORY) >= 8:
        history_matrix = np.array(list(_ANOMALY_HISTORY), dtype=float)
        history_matrix = _normalize_matrix(history_matrix)
        _ANOMALY_FOREST.fit(history_matrix)
        scores = _ANOMALY_FOREST.score_samples(_normalize_matrix(X))
    else:
        scores = np.zeros(len(vehicles), dtype=float)

    for vehicle, score in zip(vehicles, scores, strict=False):
        heuristic_score = min(
            1.0,
            (
                min(_float_value(vehicle.get("speed", vehicle.get("speed_mps", 0.0))) / 35.0, 1.0) * 0.15
                + min(_angle_dev(vehicle) / 180.0, 1.0) * 0.40  # Increased weight for bearing-based safety
                + min(max(0.0, 3.5 - _safe_ttc(vehicle)) / 3.5, 1.0) * 0.20
                + min(abs(_float_value(vehicle.get("acceleration", 0.0))) / 8.0, 1.0) * 0.10
                + min(_float_value(vehicle.get("sustained_duration_s", 0.0)) / 2.5, 1.0) * 0.15 # Faster duration penalty
            ),
        )
        final_score = max(float(score), heuristic_score)
        vehicle["anomaly_score"] = round(final_score, 4)
        vehicle["is_anomalous"] = bool(final_score > 0.58)
    return vehicles


def apply_behavior_clustering(vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not vehicles:
        return vehicles

    X = _normalize_matrix(_clustering_features(vehicles))
    labels = _KMEANS_MODEL.fit_predict(X)
    centroids = _KMEANS_MODEL.centroids if _KMEANS_MODEL.centroids is not None else np.zeros((0, 0))
    behavior_names = _behavior_name_mapping(centroids)

    for vehicle, label in zip(vehicles, labels, strict=False):
        cluster_id = int(label)
        cluster_name = behavior_names.get(cluster_id, BEHAVIOR_LABELS[min(cluster_id, len(BEHAVIOR_LABELS) - 1)])
        vehicle["behavior_cluster"] = cluster_id
        vehicle["behavior_cluster_name"] = cluster_name
    return vehicles


def apply_repeat_behavior_memory(vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for vehicle in vehicles:
        vehicle["repeat_behavior_score"] = round(_repeat_behavior_score(vehicle), 4)
    return vehicles


def enrich_ml_signals(vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add lightweight ML signals for edge-deployable inference."""
    vehicles = apply_anomaly_detection(vehicles)
    vehicles = apply_behavior_clustering(vehicles)
    vehicles = apply_learned_risk_model(vehicles)
    vehicles = apply_repeat_behavior_memory(vehicles)
    return vehicles


def reset_ml_state() -> None:
    _ACCELERATION_MEMORY.clear()
    _ANOMALY_HISTORY.clear()
    _BEHAVIOR_HISTORY.clear()
