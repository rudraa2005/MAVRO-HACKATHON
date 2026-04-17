from __future__ import annotations

import importlib.util
import unittest

HAS_SHAPELY = importlib.util.find_spec("shapely") is not None

if HAS_SHAPELY:
    from map_matching_core import GPSProbe, MapMatchingIndex, PreviousMatchState


@unittest.skipUnless(HAS_SHAPELY, "shapely is required for map-matching tests")
class MapMatchingIndexTests(unittest.TestCase):
    def test_parallel_roads_choose_nearest_segment(self) -> None:
        index = MapMatchingIndex.from_dicts(
            [
                {
                    "id": 1,
                    "oneway": False,
                    "length": 120.0,
                    "geometry": [
                        {"lat": 13.0000, "lon": 80.0000},
                        {"lat": 13.0000, "lon": 80.0010},
                    ],
                },
                {
                    "id": 2,
                    "oneway": False,
                    "length": 120.0,
                    "geometry": [
                        {"lat": 13.0002, "lon": 80.0000},
                        {"lat": 13.0002, "lon": 80.0010},
                    ],
                },
            ]
        )
        probe = GPSProbe(vehicle_id=99, lat=13.00018, lon=80.0005, timestamp=1.0, speed_mps=10.0, heading=90.0)

        result = index.match_probe(probe, candidate_limit=5, distance_threshold_m=30.0)

        self.assertEqual(result.matched_edge_id, 2)
        self.assertIsNotNone(result.snapped_point)

    def test_two_way_segment_can_match_reverse_heading(self) -> None:
        index = MapMatchingIndex.from_dicts(
            [
                {
                    "id": 10,
                    "oneway": False,
                    "length": 100.0,
                    "geometry": [
                        {"lat": 13.0000, "lon": 80.0000},
                        {"lat": 13.0000, "lon": 80.0010},
                    ],
                }
            ]
        )
        probe = GPSProbe(vehicle_id=1, lat=13.00001, lon=80.0005, timestamp=1.0, speed_mps=8.0, heading=270.0)

        result = index.match_probe(probe, distance_threshold_m=25.0)

        self.assertEqual(result.matched_edge_id, 10)
        self.assertLess(result.heading_diff or 999.0, 15.0)
        self.assertIsNotNone(result.road_vector)
        self.assertLess((result.road_vector or [0.0])[0], 0.0)

    def test_one_way_wrong_heading_reduces_confidence(self) -> None:
        index = MapMatchingIndex.from_dicts(
            [
                {
                    "id": 11,
                    "oneway": True,
                    "length": 100.0,
                    "geometry": [
                        {"lat": 13.0000, "lon": 80.0000},
                        {"lat": 13.0000, "lon": 80.0010},
                    ],
                }
            ]
        )
        probe = GPSProbe(vehicle_id=1, lat=13.00001, lon=80.0005, timestamp=1.0, speed_mps=8.0, heading=270.0)

        result = index.match_probe(probe, distance_threshold_m=25.0)

        self.assertEqual(result.matched_edge_id, 11)
        self.assertGreater(result.heading_diff or 0.0, 150.0)
        self.assertLess(result.confidence_score, 0.7)

    def test_sudden_jump_is_rejected(self) -> None:
        index = MapMatchingIndex.from_dicts(
            [
                {
                    "id": 21,
                    "oneway": False,
                    "length": 100.0,
                    "geometry": [
                        {"lat": 13.0000, "lon": 80.0000},
                        {"lat": 13.0000, "lon": 80.0010},
                    ],
                },
                {
                    "id": 22,
                    "oneway": False,
                    "length": 100.0,
                    "geometry": [
                        {"lat": 13.0020, "lon": 80.0000},
                        {"lat": 13.0020, "lon": 80.0010},
                    ],
                },
            ]
        )
        previous_state = PreviousMatchState(
            edge_id=21,
            lat=13.0000,
            lon=80.0002,
            timestamp=10.0,
            speed_mps=10.0,
        )
        probe = GPSProbe(vehicle_id=7, lat=13.0020, lon=80.0005, timestamp=11.0, speed_mps=9.0, heading=90.0)

        result = index.match_probe(
            probe,
            distance_threshold_m=25.0,
            previous_state=previous_state,
            max_jump_speed_mps=40.0,
        )

        self.assertIsNone(result.matched_edge_id)
        self.assertEqual(result.rejected_reason, "jump_threshold_exceeded")


if __name__ == "__main__":
    unittest.main()
