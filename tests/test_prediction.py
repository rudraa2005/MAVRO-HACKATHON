from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "backend" / "services" / "prediction.py"
SPEC = importlib.util.spec_from_file_location("prediction", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
prediction_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prediction_module)

predict_trajectory = prediction_module.predict_trajectory
reset_prediction_memory = prediction_module.reset_prediction_memory
prediction_memory = prediction_module.prediction_memory


class PredictionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_prediction_memory()

    def test_predict_trajectory_adds_default_future_positions(self) -> None:
        vehicle = {
            "vehicle_id": 1,
            "lat": 13.0,
            "lon": 80.0,
            "speed": 10.0,
            "bearing": 90.0,
            "timestamp": 1.0,
        }

        result = predict_trajectory(vehicle)

        self.assertEqual(len(result["future_positions"]), 5)
        self.assertEqual(len(result["prediction_state"]), 4)

    def test_eastbound_vehicle_predictions_move_longitude_forward(self) -> None:
        vehicle = {
            "vehicle_id": 2,
            "lat": 13.0,
            "lon": 80.0,
            "speed": 10.0,
            "bearing": 90.0,
            "timestamp": 1.0,
        }

        result = predict_trajectory(vehicle, steps=3, step_dt_s=1.0)
        future_positions = result["future_positions"]

        self.assertAlmostEqual(future_positions[0][0], 13.0, places=4)
        self.assertGreater(future_positions[0][1], 80.0)
        self.assertGreater(future_positions[1][1], future_positions[0][1])
        self.assertGreater(future_positions[2][1], future_positions[1][1])

    def test_prediction_memory_persists_across_frames(self) -> None:
        first = {
            "vehicle_id": 3,
            "lat": 13.0,
            "lon": 80.0,
            "speed": 10.0,
            "bearing": 90.0,
            "timestamp": 1.0,
        }
        second = {
            "vehicle_id": 3,
            "lat": 13.0,
            "lon": 80.0001,
            "speed": 8.0,
            "bearing": 90.0,
            "timestamp": 2.0,
        }

        predict_trajectory(first)
        result = predict_trajectory(second)

        self.assertIn(3, prediction_memory)
        self.assertEqual(prediction_memory[3]["timestamp"], 2.0)
        self.assertGreater(result["prediction_state"][2], 0.0)


if __name__ == "__main__":
    unittest.main()
