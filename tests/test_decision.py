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
    def test_ttc_below_two_triggers_collision_alert(self) -> None:
        vehicles = [
            {
                "id": 1,
                "class": "wrong_way",
                "risk_level": "high",
                "ttc": 1.2,
            }
        ]

        result = decision_layer(vehicles)

        self.assertEqual(result[0]["alert"], "COLLISION_ALERT")

    def test_high_risk_level_triggers_high_alert(self) -> None:
        vehicles = [
            {
                "id": 2,
                "class": "normal",
                "risk_level": "high",
                "ttc": None,
            }
        ]

        result = decision_layer(vehicles)

        self.assertEqual(result[0]["alert"], "HIGH_ALERT")

    def test_medium_risk_level_triggers_warning(self) -> None:
        vehicles = [
            {
                "id": 3,
                "class": "risky",
                "risk_level": "medium",
                "ttc": 4.5,
            }
        ]

        result = run_decision(vehicles)

        self.assertEqual(result[0]["alert"], "WARNING")

    def test_low_risk_defaults_to_safe(self) -> None:
        vehicles = [
            {
                "id": 4,
                "class": "normal",
                "risk_level": "low",
                "ttc": None,
            }
        ]

        result = run_decision(vehicles)

        self.assertEqual(result[0]["alert"], "SAFE")


if __name__ == "__main__":
    unittest.main()
