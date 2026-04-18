from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "backend" / "services" / "ml_intelligence.py"
SPEC = importlib.util.spec_from_file_location("ml_intelligence", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
ml_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ml_module
SPEC.loader.exec_module(ml_module)

apply_learned_risk_model = ml_module.apply_learned_risk_model
apply_anomaly_detection = ml_module.apply_anomaly_detection
apply_behavior_clustering = ml_module.apply_behavior_clustering
apply_repeat_behavior_memory = ml_module.apply_repeat_behavior_memory
reset_ml_state = ml_module.reset_ml_state


class MLIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_ml_state()

    def test_learned_risk_model_outputs_probability_and_orders_risk(self) -> None:
        vehicles = [
            {
                "id": 1,
                "ttc": 8.0,
                "speed": 4.0,
                "relative_speed": 1.0,
                "angle_dev": 5.0,
                "temporal_state": "NORMAL",
                "collision_probability": 0.05,
                "uncertainty": 0.02,
            },
            {
                "id": 2,
                "ttc": 0.8,
                "speed": 20.0,
                "relative_speed": 18.0,
                "angle_dev": 170.0,
                "temporal_state": "CONFIRMED",
                "collision_probability": 0.82,
                "uncertainty": 0.7,
            },
        ]

        result = apply_learned_risk_model(vehicles)

        self.assertGreaterEqual(result[0]["ml_collision_probability"], 0.0)
        self.assertLessEqual(result[0]["ml_collision_probability"], 1.0)
        self.assertGreater(result[1]["ml_collision_probability"], result[0]["ml_collision_probability"])

    def test_anomaly_detection_flags_extreme_behavior(self) -> None:
        normal_batch = [
            {
                "id": index,
                "speed": 8.0 + (index % 3),
                "angle_dev": 8.0 + (index % 4),
                "ttc": 8.0,
                "timestamp": float(index),
                "sustained_duration_s": 0.2,
            }
            for index in range(1, 13)
        ]
        apply_anomaly_detection(normal_batch)

        risky_batch = [
            {
                "id": 99,
                "speed": 30.0,
                "angle_dev": 175.0,
                "ttc": 0.6,
                "timestamp": 20.0,
                "sustained_duration_s": 4.0,
            }
        ]
        result = apply_anomaly_detection(risky_batch)

        self.assertIn("anomaly_score", result[0])
        self.assertTrue(result[0]["anomaly_score"] >= 0.0)
        self.assertTrue(result[0]["is_anomalous"])

    def test_behavior_clustering_adds_cluster_name(self) -> None:
        vehicles = [
            {
                "id": 1,
                "speed": 6.0,
                "angle_dev": 5.0,
                "collision_probability": 0.02,
                "anomaly_score": 0.1,
                "wrong_way_probability": 0.05,
            },
            {
                "id": 2,
                "speed": 18.0,
                "angle_dev": 45.0,
                "collision_probability": 0.25,
                "anomaly_score": 0.45,
                "wrong_way_probability": 0.25,
            },
            {
                "id": 3,
                "speed": 10.0,
                "angle_dev": 175.0,
                "collision_probability": 0.82,
                "anomaly_score": 0.7,
                "wrong_way_probability": 0.9,
            },
        ]

        result = apply_behavior_clustering(vehicles)

        for vehicle in result:
            self.assertIn("behavior_cluster", vehicle)
            self.assertIn(vehicle["behavior_cluster_name"], {"normal", "aggressive", "wrong_way"})

    def test_repeat_behavior_score_increases_for_repeated_pattern(self) -> None:
        vehicle = {
            "id": 7,
            "speed": 12.0,
            "angle_dev": 20.0,
            "collision_probability": 0.3,
            "uncertainty": 0.1,
            "anomaly_score": 0.2,
        }

        first = apply_repeat_behavior_memory([dict(vehicle)])[0]
        second = apply_repeat_behavior_memory([dict(vehicle)])[0]

        self.assertEqual(first["repeat_behavior_score"], 0.0)
        self.assertGreater(second["repeat_behavior_score"], 0.99)


if __name__ == "__main__":
    unittest.main()
