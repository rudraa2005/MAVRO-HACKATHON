from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "backend" / "services" / "risk_engine.py"
SPEC = importlib.util.spec_from_file_location("risk_engine", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
risk_engine_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(risk_engine_module)

compute_risk_score = risk_engine_module.compute_risk_score
evaluate_vehicle_risk = risk_engine_module.evaluate_vehicle_risk
run_risk_engine = risk_engine_module.run_risk_engine


class RiskEngineTests(unittest.TestCase):
    def test_refined_risk_score_uses_continuous_formula(self) -> None:
        vehicle = {
            "speed": 10.0,
            "ttc": 2.0,
            "temporal_state": "CONFIRMED",
            "semantic_class": "wrong_way",
            "distance": 5.0,
        }

        result = compute_risk_score(vehicle)

        self.assertAlmostEqual(result, 9.0, places=2)

    def test_high_collision_probability_is_critical(self) -> None:
        vehicle = {
            "temporal_state": "NORMAL",
            "risk_score": 1.0,
            "ttc": 1.5,
            "collision_probability": 0.82,
            "class": "normal",
        }

        result = evaluate_vehicle_risk(vehicle)

        self.assertEqual(result["risk_level"], "critical")
        self.assertGreater(result["risk_score_refined"], 0.0)

    def test_suspect_is_medium_when_refined_score_is_moderate(self) -> None:
        vehicle = {
            "speed": 4.0,
            "temporal_state": "SUSPECT",
            "risk_score": 2.0,
            "ttc": None,
            "collision_probability": 0.1,
            "class": "normal",
        }

        result = evaluate_vehicle_risk(vehicle)

        self.assertEqual(result["risk_level"], "medium")

    def test_low_signal_vehicle_is_low(self) -> None:
        vehicle = {
            "speed": 2.0,
            "temporal_state": "NORMAL",
            "risk_score": 1.0,
            "ttc": None,
            "collision_probability": 0.0,
            "class": "normal",
        }

        result = evaluate_vehicle_risk(vehicle)

        self.assertEqual(result["risk_level"], "low")

    def test_wrong_way_semantic_class_escalates_to_high(self) -> None:
        vehicles = [
            {
                "speed": 8.0,
                "temporal_state": "NORMAL",
                "risk_score": 1.0,
                "ttc": None,
                "collision_probability": 0.0,
                "class": "wrong_way",
            }
        ]

        result = run_risk_engine(vehicles)

        self.assertEqual(result[0]["risk_level"], "high")
        self.assertGreater(result[0]["risk_score_refined"], 3.0)


if __name__ == "__main__":
    unittest.main()
