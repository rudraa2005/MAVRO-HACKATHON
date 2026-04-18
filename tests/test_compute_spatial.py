from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "backend" / "services" / "compute_spatial.py"
SPEC = importlib.util.spec_from_file_location("compute_spatial", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
compute_spatial_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compute_spatial_module)

compute_spatial = compute_spatial_module.compute_spatial
monte_carlo_collision = compute_spatial_module.monte_carlo_collision


EARTH_RADIUS_M = 6_371_000.0


def meters_to_lon(distance_m: float, lat_deg: float = 0.0) -> float:
    return math.degrees(distance_m / (EARTH_RADIUS_M * max(math.cos(math.radians(lat_deg)), 1e-9)))


class ComputeSpatialTests(unittest.TestCase):
    def test_head_on_pair_updates_symmetrically(self) -> None:
        vehicles = [
            {
                "vehicle_id": 1,
                "lat": 0.0,
                "lon": 0.0,
                "speed": 5.0,
                "bearing": 90.0,
            },
            {
                "vehicle_id": 2,
                "lat": 0.0,
                "lon": meters_to_lon(10.0),
                "speed": 5.0,
                "bearing": 270.0,
            },
        ]

        result = compute_spatial(vehicles)

        self.assertEqual(result[0]["collision_with"], 2)
        self.assertEqual(result[1]["collision_with"], 1)
        self.assertEqual(result[0]["risk"], "danger")
        self.assertEqual(result[1]["risk"], "danger")
        self.assertAlmostEqual(result[0]["ttc"], 1.0, places=1)
        self.assertAlmostEqual(result[1]["ttc"], 1.0, places=1)
        self.assertGreater(result[0]["collision_probability"], 0.7)
        self.assertGreater(result[1]["collision_probability"], 0.7)
        self.assertEqual(result[0]["cluster_size"], 2)
        self.assertEqual(result[1]["cluster_size"], 2)
        self.assertIn(2, result[0]["collision_neighbors"])
        self.assertIn(1, result[1]["collision_neighbors"])
        self.assertIsInstance(result[0]["safe_actions"], list)
        self.assertIsInstance(result[0]["unsafe_actions"], list)
        self.assertAlmostEqual(result[0]["time_to_action"], 0.0, places=1)

    def test_far_vehicles_are_skipped(self) -> None:
        vehicles = [
            {
                "vehicle_id": 1,
                "lat": 0.0,
                "lon": 0.0,
                "speed": 8.0,
                "bearing": 90.0,
            },
            {
                "vehicle_id": 2,
                "lat": 0.0,
                "lon": meters_to_lon(80.0),
                "speed": 8.0,
                "bearing": 270.0,
            },
        ]

        result = compute_spatial(vehicles)

        self.assertIsNone(result[0]["ttc"])
        self.assertIsNone(result[1]["ttc"])
        self.assertEqual(result[0]["risk"], "safe")
        self.assertEqual(result[1]["risk"], "safe")
        self.assertEqual(result[0]["collision_probability"], 0.0)
        self.assertEqual(result[1]["collision_probability"], 0.0)
        self.assertEqual(result[0]["cluster_size"], 1)
        self.assertEqual(result[1]["cluster_size"], 1)

    def test_moving_apart_is_skipped(self) -> None:
        vehicles = [
            {
                "vehicle_id": 1,
                "lat": 0.0,
                "lon": 0.0,
                "speed": 5.0,
                "bearing": 270.0,
            },
            {
                "vehicle_id": 2,
                "lat": 0.0,
                "lon": meters_to_lon(10.0),
                "speed": 5.0,
                "bearing": 90.0,
            },
        ]

        result = compute_spatial(vehicles)

        self.assertIsNone(result[0]["ttc"])
        self.assertIsNone(result[1]["ttc"])
        self.assertEqual(result[0]["collision_with"], None)
        self.assertEqual(result[1]["collision_with"], None)
        self.assertEqual(result[0]["collision_neighbors"], [])
        self.assertEqual(result[1]["collision_neighbors"], [])

    def test_monte_carlo_collision_returns_probability(self) -> None:
        vehicle_a = {
            "id": 1,
            "x": 0.0,
            "y": 0.0,
            "vx": 5.0,
            "vy": 0.0,
        }
        vehicle_b = {
            "id": 2,
            "x": 10.0,
            "y": 0.0,
            "vx": -5.0,
            "vy": 0.0,
        }

        probability = monte_carlo_collision(vehicle_a, vehicle_b, n=50)

        self.assertGreaterEqual(probability, 0.0)
        self.assertLessEqual(probability, 1.0)
        self.assertGreater(probability, 0.7)


if __name__ == "__main__":
    unittest.main()
