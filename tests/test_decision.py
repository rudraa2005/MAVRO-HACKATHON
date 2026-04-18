from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "backend" / "services" / "decision.py"
SPEC = importlib.util.spec_from_file_location("decision", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
decision_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(decision_module)

decision_layer = decision_module.decision_layer
run_decision = decision_module.run_decision


class DecisionLayerTests(unittest.TestCase):
    def test_high_collision_probability_triggers_collision_alert(self) -> None:
        vehicles = [
            {
                "id": 1,
                "collision_probability": 0.82,
                "risk_score_refined": 8.2,
                "temporal_state": "CONFIRMED",
            }
        ]

        result = decision_layer(vehicles)

        self.assertEqual(result[0]["alert"], "COLLISION_ALERT")

    def test_high_refined_risk_triggers_high_alert(self) -> None:
        vehicles = [
            {
                "id": 2,
                "collision_probability": 0.25,
                "risk_score_refined": 6.5,
                "temporal_state": "CONFIRMED",
            }
        ]

        result = decision_layer(vehicles)

        self.assertEqual(result[0]["alert"], "HIGH_ALERT")

    def test_suspect_triggers_warning(self) -> None:
        vehicles = [
            {
                "id": 3,
                "collision_probability": 0.1,
                "risk_score_refined": 4.0,
                "temporal_state": "SUSPECT",
            }
        ]

        result = run_decision(vehicles)

        self.assertEqual(result[0]["alert"], "WARNING")

    def test_low_risk_defaults_to_safe(self) -> None:
        vehicles = [
            {
                "id": 4,
                "collision_probability": 0.0,
                "risk_score_refined": 2.1,
                "temporal_state": "NORMAL",
            }
        ]

        result = run_decision(vehicles)

        self.assertEqual(result[0]["alert"], "SAFE")


if __name__ == "__main__":
    unittest.main()
