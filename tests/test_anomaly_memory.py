from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "backend" / "services" / "anomaly_memory.py"
SPEC = importlib.util.spec_from_file_location("anomaly_memory", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
anomaly_memory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(anomaly_memory)

reset_vehicle_memory = anomaly_memory.reset_vehicle_memory
update_memory = anomaly_memory.update_memory
vehicle_memory = anomaly_memory.vehicle_memory


class AnomalyMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_vehicle_memory()

    def test_confirmed_increments_violation_count_and_score(self) -> None:
        vehicles = [
            {
                "vehicle_id": 1,
                "timestamp": 10.0,
                "temporal_state": "CONFIRMED",
                "sustained_duration_s": 4.0,
            }
        ]

        result = update_memory(vehicles)

        self.assertEqual(result[0]["violation_count"], 1)
        self.assertEqual(result[0]["risk_score"], 1.7)
        self.assertTrue(result[0]["wrong_way_flag"])

    def test_suspect_applies_small_increment_without_violation_count_bump(self) -> None:
        first = update_memory(
            [
                {
                    "vehicle_id": 7,
                    "timestamp": 1.0,
                    "temporal_state": "SUSPECT",
                    "sustained_duration_s": 0.8,
                }
            ]
        )
        second = update_memory(
            [
                {
                    "vehicle_id": 7,
                    "timestamp": 2.0,
                    "temporal_state": "SUSPECT",
                    "sustained_duration_s": 0.5,
                }
            ]
        )

        self.assertEqual(first[0]["violation_count"], 0)
        self.assertEqual(second[0]["violation_count"], 0)
        self.assertGreater(second[0]["risk_score"], first[0]["risk_score"])

    def test_normal_state_decays_score_slowly(self) -> None:
        update_memory(
            [
                {
                    "vehicle_id": 2,
                    "timestamp": 1.0,
                    "temporal_state": "CONFIRMED",
                    "sustained_duration_s": 3.0,
                }
            ]
        )

        result = update_memory(
            [
                {
                    "vehicle_id": 2,
                    "timestamp": 2.0,
                    "temporal_state": "NORMAL",
                    "sustained_duration_s": 0.0,
                }
            ]
        )

        self.assertEqual(result[0]["violation_count"], 1)
        self.assertEqual(result[0]["risk_score"], 1.25)

    def test_score_is_clamped_between_zero_and_ten(self) -> None:
        result = update_memory(
            [
                {
                    "vehicle_id": 3,
                    "timestamp": 5.0,
                    "temporal_state": "CONFIRMED",
                    "sustained_duration_s": 50.0,
                }
            ]
        )

        self.assertEqual(result[0]["risk_score"], 10.0)

    def test_memory_persists_history_across_frames(self) -> None:
        update_memory(
            [
                {
                    "vehicle_id": 9,
                    "timestamp": 1.0,
                    "temporal_state": "SUSPECT",
                    "sustained_duration_s": 1.0,
                }
            ]
        )
        update_memory(
            [
                {
                    "vehicle_id": 9,
                    "timestamp": 2.0,
                    "temporal_state": "CONFIRMED",
                    "sustained_duration_s": 2.0,
                }
            ]
        )

        memory = vehicle_memory[9]
        self.assertEqual(memory["violation_count"], 1)
        self.assertEqual(memory["last_seen"], 2.0)
        self.assertEqual(len(memory["history"]), 2)


if __name__ == "__main__":
    unittest.main()
