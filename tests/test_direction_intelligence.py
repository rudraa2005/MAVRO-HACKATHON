"""Tests for the Direction Intelligence Layer.

Covers: normal driving, wrong-way driving, U-turn recovery,
GPS noise filtering, low-speed gating, and one-way constraint weighting.
"""

from __future__ import annotations

import importlib.util
import math
import unittest

HAS_NUMPY = importlib.util.find_spec("numpy") is not None

if HAS_NUMPY:
    from direction_intelligence_core import (
        DirectionIntelligenceEngine,
        DirectionProbe,
        TrajectoryBuffer,
        WWPBuffer,
        compute_motion_vector,
        direction_similarity,
        raw_wwp,
    )
    import numpy as np


def _probe(vid, lat, lon, ts, speed, road_vec, oneway=True, edge_id=1):
    return DirectionProbe(
        vehicle_id=vid,
        lat=lat,
        lon=lon,
        timestamp=ts,
        speed_mps=speed,
        road_vector=road_vec,
        oneway=oneway,
        matched_edge_id=edge_id,
    )


@unittest.skipUnless(HAS_NUMPY, "numpy is required for direction intelligence tests")
class VectorMathTests(unittest.TestCase):
    """Unit tests for the pure vector helper functions."""

    def test_same_direction_similarity_positive(self) -> None:
        v1 = np.array([1.0, 0.0])
        v2 = np.array([1.0, 0.0])
        self.assertAlmostEqual(direction_similarity(v1, v2), 1.0, places=4)

    def test_opposite_direction_similarity_negative(self) -> None:
        v1 = np.array([1.0, 0.0])
        v2 = np.array([-1.0, 0.0])
        self.assertAlmostEqual(direction_similarity(v1, v2), -1.0, places=4)

    def test_perpendicular_similarity_zero(self) -> None:
        v1 = np.array([1.0, 0.0])
        v2 = np.array([0.0, 1.0])
        self.assertAlmostEqual(direction_similarity(v1, v2), 0.0, places=4)

    def test_raw_wwp_correct_direction(self) -> None:
        self.assertAlmostEqual(raw_wwp(1.0), 0.0, places=4)

    def test_raw_wwp_wrong_direction(self) -> None:
        self.assertAlmostEqual(raw_wwp(-1.0), 1.0, places=4)

    def test_raw_wwp_perpendicular(self) -> None:
        self.assertAlmostEqual(raw_wwp(0.0), 0.5, places=4)

    def test_motion_vector_east(self) -> None:
        # Points moving eastward (increasing longitude)
        points = [
            (13.0000, 80.0000, 1.0),
            (13.0000, 80.0005, 2.0),
            (13.0000, 80.0010, 3.0),
        ]
        v = compute_motion_vector(points)
        self.assertIsNotNone(v)
        # Should be roughly (1, 0) — pure eastward
        self.assertGreater(v[0], 0.99)
        self.assertAlmostEqual(v[1], 0.0, places=1)

    def test_motion_vector_north(self) -> None:
        # Points moving northward (increasing latitude)
        points = [
            (13.0000, 80.0000, 1.0),
            (13.0005, 80.0000, 2.0),
            (13.0010, 80.0000, 3.0),
        ]
        v = compute_motion_vector(points)
        self.assertIsNotNone(v)
        # Should be roughly (0, 1) — pure northward
        self.assertAlmostEqual(v[0], 0.0, places=1)
        self.assertGreater(v[1], 0.99)

    def test_motion_vector_stationary_returns_none(self) -> None:
        points = [
            (13.0000, 80.0000, 1.0),
            (13.0000, 80.0000, 2.0),
        ]
        v = compute_motion_vector(points)
        self.assertIsNone(v)

    def test_motion_vector_single_point_returns_none(self) -> None:
        v = compute_motion_vector([(13.0, 80.0, 1.0)])
        self.assertIsNone(v)


@unittest.skipUnless(HAS_NUMPY, "numpy is required for direction intelligence tests")
class TrajectoryBufferTests(unittest.TestCase):
    def test_max_points(self) -> None:
        buf = TrajectoryBuffer(max_points=3, max_age_seconds=100.0)
        for i in range(5):
            buf.add(13.0 + i * 0.0001, 80.0, float(i))
        self.assertEqual(buf.size(), 3)

    def test_age_pruning(self) -> None:
        buf = TrajectoryBuffer(max_points=100, max_age_seconds=3.0)
        buf.add(13.0, 80.0, 1.0)
        buf.add(13.0, 80.0, 2.0)
        buf.add(13.0, 80.0, 5.0)  # point at t=1.0 is >3s old
        self.assertEqual(buf.size(), 2)


@unittest.skipUnless(HAS_NUMPY, "numpy is required for direction intelligence tests")
class WWPBufferTests(unittest.TestCase):
    def test_mean_and_variance(self) -> None:
        buf = WWPBuffer(window_seconds=10.0)
        buf.add(0.9, 1.0)
        buf.add(0.8, 2.0)
        buf.add(0.85, 3.0)
        self.assertAlmostEqual(buf.mean(), 0.85, places=4)
        self.assertGreater(buf.variance(), 0.0)

    def test_window_pruning(self) -> None:
        buf = WWPBuffer(window_seconds=2.0)
        buf.add(0.9, 1.0)
        buf.add(0.8, 2.0)
        buf.add(0.7, 4.0)  # t=1.0 is now >2s old
        self.assertEqual(len(buf.scores), 2)


@unittest.skipUnless(HAS_NUMPY, "numpy is required for direction intelligence tests")
class EngineNormalDrivingTests(unittest.TestCase):
    """Vehicle driving in the correct direction → low WWP, no violation."""

    def test_correct_direction_low_wwp(self) -> None:
        engine = DirectionIntelligenceEngine(
            sustained_seconds=1.5,
            min_speed_mps=0.5,
        )
        road_vec = (1.0, 0.0)  # road points east

        # Feed 6 probes moving east (increasing longitude)
        for i in range(6):
            probe = _probe(
                vid=1,
                lat=13.0000,
                lon=80.0000 + i * 0.0003,
                ts=float(i),
                speed=10.0,
                road_vec=road_vec,
            )
            result = engine.process_probe(probe)

        self.assertLess(result.wrong_way_probability, 0.15)
        self.assertFalse(result.is_violation)
        self.assertGreater(result.confidence, 0.0)
        self.assertGreater(result.direction_similarity, 0.8)


@unittest.skipUnless(HAS_NUMPY, "numpy is required for direction intelligence tests")
class EngineWrongWayTests(unittest.TestCase):
    """Vehicle driving opposite to road direction → high WWP, violation."""

    def test_wrong_way_high_wwp(self) -> None:
        engine = DirectionIntelligenceEngine(
            sustained_seconds=1.5,
            min_speed_mps=0.5,
        )
        road_vec = (1.0, 0.0)  # road points east

        # Feed 8 probes moving WEST (decreasing longitude) for 4 seconds
        for i in range(8):
            probe = _probe(
                vid=2,
                lat=13.0000,
                lon=80.0020 - i * 0.0003,
                ts=float(i * 0.5),
                speed=10.0,
                road_vec=road_vec,
                oneway=True,
            )
            result = engine.process_probe(probe)

        self.assertGreater(result.wrong_way_probability, 0.55)
        self.assertTrue(result.is_violation)
        self.assertLess(result.direction_similarity, -0.8)


@unittest.skipUnless(HAS_NUMPY, "numpy is required for direction intelligence tests")
class EngineUTurnTests(unittest.TestCase):
    """A U-turn produces a transient negative dot product but should NOT
    trigger a sustained violation if the vehicle returns to correct direction."""

    def test_u_turn_no_sustained_violation(self) -> None:
        engine = DirectionIntelligenceEngine(
            sustained_seconds=2.0,
            min_speed_mps=0.5,
        )
        road_vec = (1.0, 0.0)  # road points east

        # Phase 1: 3 probes going east (correct)
        for i in range(3):
            probe = _probe(
                vid=3,
                lat=13.0000,
                lon=80.0000 + i * 0.0003,
                ts=float(i),
                speed=10.0,
                road_vec=road_vec,
            )
            engine.process_probe(probe)

        # Phase 2: 2 probes going west (U-turn, brief wrong way)
        for i in range(2):
            probe = _probe(
                vid=3,
                lat=13.0000,
                lon=80.0006 - i * 0.0003,
                ts=float(3 + i),
                speed=10.0,
                road_vec=road_vec,
            )
            result = engine.process_probe(probe)

        # Phase 3: 3 probes going east again (corrected)
        for i in range(3):
            probe = _probe(
                vid=3,
                lat=13.0000,
                lon=80.0002 + i * 0.0003,
                ts=float(5 + i),
                speed=10.0,
                road_vec=road_vec,
            )
            result = engine.process_probe(probe)

        # After recovery, should NOT be a violation
        self.assertFalse(result.is_violation)


@unittest.skipUnless(HAS_NUMPY, "numpy is required for direction intelligence tests")
class EngineLowSpeedTests(unittest.TestCase):
    """Low-speed / stationary vehicles should be ignored (empty result)."""

    def test_stationary_vehicle_ignored(self) -> None:
        engine = DirectionIntelligenceEngine(min_speed_mps=1.5)
        road_vec = (1.0, 0.0)

        for i in range(5):
            probe = _probe(
                vid=4,
                lat=13.0000,
                lon=80.0000,  # stationary
                ts=float(i),
                speed=0.0,
                road_vec=road_vec,
            )
            result = engine.process_probe(probe)

        self.assertAlmostEqual(result.wrong_way_probability, 0.0)
        self.assertFalse(result.is_violation)
        self.assertAlmostEqual(result.confidence, 0.0)

    def test_very_slow_vehicle_ignored(self) -> None:
        engine = DirectionIntelligenceEngine(min_speed_mps=1.5)
        road_vec = (1.0, 0.0)

        # Barely moving (< 1.5 m/s effective)
        for i in range(5):
            probe = _probe(
                vid=5,
                lat=13.0000,
                lon=80.0000 + i * 0.000001,
                ts=float(i),
                speed=0.3,
                road_vec=road_vec,
            )
            result = engine.process_probe(probe)

        self.assertAlmostEqual(result.confidence, 0.0)
        self.assertFalse(result.is_violation)


@unittest.skipUnless(HAS_NUMPY, "numpy is required for direction intelligence tests")
class EngineOnewayConstraintTests(unittest.TestCase):
    """One-way roads should produce higher WWP than two-way for the same
    wrong-way motion, due to the alpha weight difference."""

    def test_oneway_higher_penalty(self) -> None:
        road_vec = (1.0, 0.0)

        def run(oneway: bool) -> float:
            engine = DirectionIntelligenceEngine(
                sustained_seconds=1.0,
                min_speed_mps=0.5,
            )
            for i in range(6):
                probe = _probe(
                    vid=10,
                    lat=13.0000,
                    lon=80.0020 - i * 0.0003,
                    ts=float(i * 0.5),
                    speed=10.0,
                    road_vec=road_vec,
                    oneway=oneway,
                )
                result = engine.process_probe(probe)
            return result.wrong_way_probability

        wwp_oneway = run(True)
        wwp_twoway = run(False)
        self.assertGreater(wwp_oneway, wwp_twoway)


@unittest.skipUnless(HAS_NUMPY, "numpy is required for direction intelligence tests")
class EngineNoiseTests(unittest.TestCase):
    """GPS noise around a correct-direction trajectory should NOT trigger
    violations due to temporal consistency smoothing."""

    def test_noisy_correct_direction(self) -> None:
        import random
        rng = random.Random(42)
        engine = DirectionIntelligenceEngine(
            sustained_seconds=2.0,
            min_speed_mps=0.5,
        )
        road_vec = (1.0, 0.0)

        for i in range(10):
            noise_lat = rng.gauss(0, 0.00003)
            noise_lon = rng.gauss(0, 0.00003)
            probe = _probe(
                vid=20,
                lat=13.0000 + noise_lat,
                lon=80.0000 + i * 0.0003 + noise_lon,
                ts=float(i * 0.5),
                speed=10.0,
                road_vec=road_vec,
            )
            result = engine.process_probe(probe)

        self.assertFalse(result.is_violation)
        self.assertLess(result.wrong_way_probability, 0.4)


@unittest.skipUnless(HAS_NUMPY, "numpy is required for direction intelligence tests")
class EngineSeedTrajectoryTests(unittest.TestCase):
    """Seeding trajectory from history should immediately produce
    meaningful results on the first probe after seeding."""

    def test_seed_gives_early_result(self) -> None:
        engine = DirectionIntelligenceEngine(min_speed_mps=0.5)
        road_vec = (1.0, 0.0)

        # Seed with 5 historical eastward points
        history = [
            (13.0000, 80.0000 + i * 0.0003, float(i))
            for i in range(5)
        ]
        engine.seed_trajectory(30, history)

        # Now feed one more probe in the same direction
        probe = _probe(
            vid=30,
            lat=13.0000,
            lon=80.0015 + 0.0003,
            ts=5.0,
            speed=10.0,
            road_vec=road_vec,
        )
        result = engine.process_probe(probe)

        self.assertIsNotNone(result.motion_vector)
        self.assertGreater(result.direction_similarity, 0.5)
        self.assertLess(result.wrong_way_probability, 0.3)


if __name__ == "__main__":
    unittest.main()
