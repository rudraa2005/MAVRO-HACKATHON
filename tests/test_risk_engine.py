from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "backend" / "services" / "risk_engine.py"
SPEC = importlib.util.spec_from_file_location("risk_engine", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
risk_engine_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(risk_engine_module)

evaluate_vehicle_risk = risk_engine_module.evaluate_vehicle_risk
run_risk_engine = risk_engine_module.run_risk_engine


class RiskEngineTests(unittest.TestCase):
    def test_ttc_below_two_is_critical(self) -> None:
        vehicle = {
            "temporal_state": "NORMAL",
            "risk_score": 1.0,
            "ttc": 1.5,
            "class": "normal",
        }

        result = evaluate_vehicle_risk(vehicle)

        self.assertEqual(result["risk_level"], "critical")

    def test_high_memory_score_is_high(self) -> None:
        vehicle = {
            "temporal_state": "NORMAL",
            "risk_score": 6.2,
            "ttc": None,
            "class": "normal",
        }

        result = evaluate_vehicle_risk(vehicle)

        self.assertEqual(result["risk_level"], "high")

    def test_suspect_is_medium(self) -> None:
        vehicle = {
            "temporal_state": "SUSPECT",
            "risk_score": 2.0,
            "ttc": None,
            "class": "normal",
        }

        result = evaluate_vehicle_risk(vehicle)

        self.assertEqual(result["risk_level"], "medium")

    def test_low_signal_vehicle_is_low(self) -> None:
        vehicle = {
            "temporal_state": "NORMAL",
            "risk_score": 1.0,
            "ttc": None,
            "class": "normal",
        }

        result = evaluate_vehicle_risk(vehicle)

        self.assertEqual(result["risk_level"], "low")

    def test_wrong_way_semantic_class_escalates_to_high(self) -> None:
        vehicles = [
            {
                "temporal_state": "NORMAL",
                "risk_score": 1.0,
                "ttc": None,
                "class": "wrong_way",
            }
        ]

        result = run_risk_engine(vehicles)

        self.assertEqual(result[0]["risk_level"], "high")


if __name__ == "__main__":
    unittest.main()
