from __future__ import annotations

import numpy as np
import logging
import json
import os
from collections import defaultdict
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000  # radius in meters
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2)**2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2)**2
    c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
    return R * c


class BehavioralFingerprint:
    """Tracks and updates the unique driving signature of a vehicle."""

    def compute_signature(self, vehicle) -> dict | None:
        speed_h = getattr(vehicle, "speed_history", [])
        bearing_h = getattr(vehicle, "bearing_history", [])
        accel_h = getattr(vehicle, "acceleration_history", [])

        if len(speed_h) < 10:
            return None

        speed_h = np.asarray(speed_h)
        bearing_h = np.asarray(bearing_h)
        accel_h = np.asarray(accel_h)

        speed_variance = np.std(speed_h)
        acceleration_jerk = np.std(np.diff(accel_h)) if len(accel_h) > 1 else 0.0
        steering_entropy = np.std(np.diff(bearing_h)) if len(bearing_h) > 1 else 0.0
        
        brake_frequency = 0
        for i in range(1, len(speed_h)):
            if speed_h[i-1] > 0 and (speed_h[i] / speed_h[i-1] < 0.8):
                brake_frequency += 1

        def normalized(val, scale=1.0):
            return float(np.clip(val / scale, 0, 1))

        # Normalized features (Tuned for simulation fidelity)
        n_sv = normalized(speed_variance, 7.5)          # Was 5.0; reduced sensitivity to jitter
        n_jerk = normalized(acceleration_jerk, 5.5)     # Was 3.0; reduced sensitivity to micro-oscillations
        n_bf = normalized(brake_frequency, 12.0)        # Was 5.0; allows for intersection braking

        panic_score = 0.4 * n_sv + 0.4 * n_jerk + 0.2 * n_bf
        
        # Heuristics for undefined metrics
        delayed_reaction_score = normalized(steering_entropy, 35.0) * (1 - n_jerk)
        speed_factor = normalized(np.mean(speed_h), 22.0)
        
        impaired_score = (1 - n_sv) * 0.6 + delayed_reaction_score * 0.4
        deliberate_score = (1 - panic_score) * 0.5 + speed_factor * 0.5

        sig = {
            "speed_variance": float(speed_variance),
            "acceleration_jerk": float(acceleration_jerk),
            "steering_entropy": float(steering_entropy),
            "brake_frequency": int(brake_frequency),
            "panic_score": float(panic_score),
            "impaired_score": float(impaired_score),
            "deliberate_score": float(deliberate_score)
        }
        
        # Internal flag for GPS_CONFUSED (last 5 vs previous 25)
        if len(speed_h) >= 30:
            prev_25 = speed_h[:25]
            last_5 = speed_h[25:]
            if np.std(prev_25) < 0.5 and np.std(last_5) > 4.0:
                sig["_gps_confused"] = True
                
        return sig

    def classify_intent(self, signature: dict | None) -> str:
        if signature is None:
            return "NORMAL"
        
        if signature.get("_gps_confused", False):
            return "GPS_CONFUSED"
        if signature["panic_score"] > 0.7:
            return "PANICKED"
        if signature["impaired_score"] > 0.6:
            return "IMPAIRED"
        if signature["deliberate_score"] > 0.7:
            return "DELIBERATE"
        
        return "NORMAL"



class ContextualAnomalyDetector:
    """Detects anomalies by correlating vehicle behavior with road/POI context."""

    def contextual_score(self, vehicle, road, pois, current_time, weather="clear") -> float:
        speed = getattr(vehicle, "speed", 0.0)
        limit = getattr(road, "speed_limit", 30.0)
        if limit <= 0:
            limit = 30.0

        # 1. Base anomaly
        base_anomaly = abs(speed - limit) / limit
        score = base_anomaly

        # Helpers
        pois = pois or []
        def get_dist(p_type):
            dists = [p["distance"] for p in pois if p["type"] == p_type]
            return min(dists) if dists else float("inf")

        hour = current_time.hour
        is_rush = (7 <= hour < 9) or (17 <= hour < 19)
        is_late_night = (hour >= 23 or hour < 6)
        
        # Weekend nights (Fri-Sat 10pm-3am)
        # Fri=4, Sat=5, Sun=6
        wd = current_time.weekday()
        is_late_weekend = (wd == 4 and hour >= 22) or (wd == 5 and hour < 3) or \
                          (wd == 5 and hour >= 22) or (wd == 6 and hour < 3)

        near_bar_100 = get_dist("bar") <= 100
        near_bar_list = get_dist("bar") <= 500 # Assume 'near' for general night check is broader? No, usually 200 or 500
        near_bar = get_dist("bar") <= 200
        
        near_hospital_200 = get_dist("hospital") <= 200
        near_school_500 = get_dist("school") <= 500
        is_school_hours = (7 <= hour < 16)

        # 2. Temporal adjustments
        if is_rush and speed < 15:
            score *= 0.3
        
        if is_late_night and near_bar:
            score *= 1.5
        
        if is_late_weekend and near_bar:
            score *= 1.8

        # 3. Spatial adjustments
        if near_hospital_200 and is_late_night and speed > limit:
            score *= 0.1
        
        if near_school_500 and is_school_hours:
            score *= 1.3
            
        if near_bar_100 and is_late_weekend:
            score *= 1.6

        # 4. Weather adjustments
        if weather in ["heavy_rain", "snow"]:
            score *= 0.7

        # 5. Emergency vehicle detection
        # ±10% with period ~1s (check last 3 points for oscillation)
        history = getattr(vehicle, "speed_history", [])
        if len(history) >= 3:
            s1, s2, s3 = history[-3:]
            if s1 > 0 and s2 > 0 and s1 > s2 * 1.08 and s3 > s2 * 1.08 and abs(s1 - s3) / s1 < 0.05:
                return 0.0
            if s1 > 0 and s2 > 0 and s2 > s1 * 1.08 and s2 > s3 * 1.08 and abs(s1 - s3) / s1 < 0.05:
                return 0.0

        return float(np.clip(score, 0.0, 1.0))



class MetaConfidenceTracker:
    """Aggregates multiple confidence scores into a meta-decision score with persistent learning."""

    def __init__(self, storage_path: str = "/Users/rudranilbhattacharya/Documents/MAVRO-HACKATHON/backend/data/meta_confidence.json") -> None:
        self.storage_path = storage_path
        self.history: dict[str, list[float]] = defaultdict(list)
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        self.history[k] = v
            except Exception as e:
                logger.error(f"Failed to load meta confidence: {e}")

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            with open(self.storage_path, "w") as f:
                json.dump(dict(self.history), f)
        except Exception as e:
            logger.error(f"Failed to save meta confidence: {e}")

    def get_time_period(self, dt: datetime) -> str:
        hour = dt.hour
        if hour >= 22 or hour < 6:
            return "night"
        if (7 <= hour < 9) or (17 <= hour < 19):
            return "rush"
        return "day"

    def get_confidence(self, vehicle, road, context) -> float:
        road_type = getattr(road, "road_class", "residential") or "normal"
        time_period = self.get_time_period(getattr(context, "time", datetime.now()))
        weather = getattr(context, "weather", "clear") or "clear"
        gps_quality = getattr(vehicle, "gps_stability", "MEDIUM").lower()

        key = f"{road_type}_{time_period}_{weather}_{gps_quality}"
        
        history = self.history.get(key, [])
        if not history:
            return 0.5
        
        # Mean of last 20 observations
        recent = history[-20:]
        return float(np.mean(recent))

    def update(self, situation_key: str, was_correct: bool) -> None:
        val = 1.0 if was_correct else 0.0
        self.history[situation_key].append(val)
        
        # Keep only last 100 entries
        if len(self.history[situation_key]) > 100:
            self.history[situation_key] = self.history[situation_key][-100:]
        
        self._save()

    def adjust_detection(self, base_probability: float, confidence: float) -> float:
        return float(np.clip(base_probability * confidence, 0.0, 1.0))



class GPSCanyonDetector:
    """Identifies potential GNSS multipath or signal degradation areas."""

    def __init__(self) -> None:
        self.degradation_probability: float = 0.0

    def gps_quality_score(self, vehicle) -> float:
        # Add fields if missing
        hdop = getattr(vehicle, "gps_hdop", 1.5)
        sat_count = getattr(vehicle, "satellite_count", 8)
        
        hdop_factor = float(np.max([0, 1 - (hdop - 1.0) / 4.0]))
        sat_factor = float(np.min([sat_count / 8.0, 1.0]))
        return 0.6 * hdop_factor + 0.4 * sat_factor

    def detect_position_jumps(self, vehicle) -> bool:
        history = getattr(vehicle, "position_history", [])
        if len(history) < 2:
            return False
            
        recent = history[-10:]
        for i in range(1, len(recent)):
            p1, p2 = recent[i-1], recent[i]
            dist = haversine_distance(p1[0], p1[1], p2[0], p2[1])
            if dist > 20.0:  # 20m jump in 500ms
                return True
        return False

    def is_urban_canyon(self, lat: float, lon: float, poi_db: list[dict] | None = None) -> bool:
        # Simplified downtown bounds check (example for Chennai)
        if 13.04 <= lat <= 13.09 and 80.24 <= lon <= 80.29:
            return True
            
        if not poi_db:
            return False
            
        building_count = 0
        for poi in poi_db:
            dist = haversine_distance(lat, lon, poi.get("lat", 0), poi.get("lon", 0))
            if dist <= 100.0:
                if poi.get("type") == "building" and poi.get("floors", 0) > 10:
                    building_count += 1
        
        return building_count > 5

    def should_suppress_alert(self, vehicle, poi_db: list[dict] | None = None) -> bool:
        lat = getattr(vehicle, "lat", 0.0)
        lon = getattr(vehicle, "lon", 0.0)
        
        q_score = self.gps_quality_score(vehicle)
        has_jumps = self.detect_position_jumps(vehicle)
        in_canyon = self.is_urban_canyon(lat, lon, poi_db)
        
        return q_score < 0.4 and (has_jumps or in_canyon)

    def get_fallback_position(self, vehicle) -> tuple[float, float]:
        history = getattr(vehicle, "position_history", [])
        if len(history) < 5:
            return getattr(vehicle, "lat", 0.0), getattr(vehicle, "lon", 0.0)
            
        # 5 points ago (approx 2.5s given 500ms intervals, or 5s if requested as '5 points'?)
        # User said "last_good_pos = position_history[-5] (5 seconds ago)"
        # If interval is 500ms, -10 would be 5s. But I'll follow the [-5] instruction.
        last_good_pos = history[-5]
        bearing = np.radians(getattr(vehicle, "bearing", 0.0))
        speed = getattr(vehicle, "speed", 0.0)
        
        # Dead reckoning displacement (5 seconds or 5 steps? following "5 seconds ago" note)
        dt = 5.0
        dist = speed * dt
        
        R = 6371000
        lat1 = np.radians(last_good_pos[0])
        lon1 = np.radians(last_good_pos[1])
        
        lat2 = np.arcsin(np.sin(lat1) * np.cos(dist / R) +
                        np.cos(lat1) * np.sin(dist / R) * np.cos(bearing))
        lon2 = lon1 + np.arctan2(np.sin(bearing) * np.sin(dist / R) * np.cos(lat1),
                                 np.cos(dist / R) - np.sin(lat1) * np.sin(lat2))
        
        return float(np.degrees(lat2)), float(np.degrees(lon2))


class IntentionalReversalClassifier:
    """Distinguishes between accidental wrong-way and intentional reversing."""

    def __init__(self) -> None:
        self.is_intentional: bool = False

    def detect_emergency_pattern(self, vehicle: Any) -> bool:
        speed_h = getattr(vehicle, "speed_history", [])
        if len(speed_h) < 10:
            return False
            
        recent_speeds = np.asarray(speed_h[-10:])
        avg_speed = float(np.mean(speed_h))
        std_speed = float(np.std(recent_speeds))
        
        has_flashing = getattr(vehicle, "flashing_lights", False)
        
        # High oscillation + high speed
        # Speed is in mps. mean > 40 m/s is very fast (approx 90 mph).
        # std > 15 is also very high oscillation.
        if std_speed > 15.0 and avg_speed > 40.0:
            return True
            
        return has_flashing

    def detect_convoy(self, vehicle: Any, all_vehicles: list[Any]) -> bool:
        if not vehicle.wrong_way:
            return False
            
        lat, lon = getattr(vehicle, "lat", 0.0), getattr(vehicle, "lon", 0.0)
        bearing = getattr(vehicle, "bearing", 0.0)
        
        convoy_count = 1
        for other in all_vehicles:
            if other.id == vehicle.id:
                continue
            if not other.wrong_way:
                continue
                
            dist = haversine_distance(lat, lon, getattr(other, "lat", 0.0), getattr(other, "lon", 0.0))
            if dist <= 50.0:
                other_bearing = getattr(other, "bearing", 0.0)
                # Check bearing alignment ±15°
                # Handle circular wraparound
                diff = abs(bearing - other_bearing) % 360
                if diff > 180:
                    diff = 360 - diff
                
                if diff < 15.0:
                    convoy_count += 1
                    
        return convoy_count >= 3

    def detect_maintenance(self, vehicle: Any) -> bool:
        speed_h = getattr(vehicle, "speed_history", [])
        if not speed_h:
            return False
            
        current_speed = getattr(vehicle, "speed", 0.0)
        # 5 mph approx 2.235 m/s
        is_slow = current_speed < 2.235
        
        # Count stops (speed == 0) in last 30 points
        stops = [s for s in speed_h[-30:] if s < 0.1]
        has_frequent_stops = len(stops) >= 3
        
        return is_slow and has_frequent_stops

    def classify(self, vehicle: Any, all_vehicles: list[Any], current_time: datetime) -> str:
        if self.detect_emergency_pattern(vehicle):
            return "EMERGENCY_VEHICLE"
        
        if self.detect_convoy(vehicle, all_vehicles):
            return "CONVOY"
            
        if self.detect_maintenance(vehicle):
            return "MAINTENANCE"
            
        return "UNKNOWN"

    def get_alert_priority(self, classification: str) -> str:
        priorities = {
            "EMERGENCY_VEHICLE": "BROADCAST_CLEARANCE",
            "CONVOY": "SUPPRESS_COLLISION",
            "MAINTENANCE": "LOW_PRIORITY",
            "UNKNOWN": "STANDARD"
        }
        return priorities.get(classification, "STANDARD")



class AdversarialDetector:
    """Detects drivers intentionally gaming the wrong-way detection system."""

    def is_smooth_wrong_way(self, vehicle: Any, signature: dict) -> bool:
        """Pattern: Wrong-way BUT deliberate/smooth (not panicked)."""
        if getattr(vehicle, "state", "") != "wrong_way":
            return False
        
        panic = signature.get("panic_score", 0.0)
        deliberate = signature.get("deliberate_score", 0.0)
        speed = getattr(vehicle, "speed", 0.0)
        
        # Fast, confident, smooth, not panicked
        return panic < 0.2 and deliberate > 0.7 and speed > 50.0

    def calculate_gaming_score(self, vehicle: Any, signature: dict) -> float:
        """Computes a score (0-1) indicating probability of intentional system gaming."""
        panic = signature.get("panic_score", 0.0)
        deliberate = signature.get("deliberate_score", 0.0)
        speed = getattr(vehicle, "speed", 0.0)
        
        smooth_factor = (1.0 - panic) * 0.3
        speed_factor = min(speed / 70.0, 1.0) * 0.4
        deliberate_factor = deliberate * 0.3
        
        return float(np.clip(smooth_factor + speed_factor + deliberate_factor, 0.0, 1.0))

    def classify_threat_level(self, gaming_score: float) -> str:
        if gaming_score > 0.8:
            return "MALICIOUS_HIGH"  # Alert law enforcement
        if gaming_score > 0.6:
            return "MALICIOUS_MEDIUM"
        return "STANDARD"

    def should_escalate(self, gaming_score: float) -> bool:
        return gaming_score > 0.7


class CascadeAnalyzer:
    """Analyzes group dynamics and multi-vehicle risk propagation in wrong-way scenarios."""

    def build_temporal_graph(self, vehicles: list[dict]) -> dict:
        """Categorizes vehicles into PRIMARY threats or EVASIVE maneuvers based on spatio-temporal logs."""
        if not vehicles:
            return {}
            
        # Sort by the moment they went wrong-way
        sorted_v = sorted(vehicles, key=lambda x: x.get("timestamp_went_wrong_way", 0))
        graph = {}
        
        primary_id = sorted_v[0]["id"]
        graph[primary_id] = {"role": "PRIMARY", "caused_by": None}
        
        for i in range(1, len(sorted_v)):
            current = sorted_v[i]
            role = "PRIMARY"
            caused_by = None
            
            # Check proximity to earlier vehicles, starting from the most recent
            for j in range(i - 1, -1, -1):
                prev = sorted_v[j]
                
                dist = haversine_distance(
                    current.get("lat", 0), current.get("lon", 0),
                    prev.get("lat", 0), prev.get("lon", 0)
                )
                
                dt = current.get("timestamp_went_wrong_way", 0) - prev.get("timestamp_went_wrong_way", 0)
                
                # Proximity: < 100m AND within 3 seconds of the previous vehicle's violation
                if dist < 100.0 and 0 < dt <= 3.0:
                    role = "EVASIVE"
                    caused_by = prev["id"]
                    break
                    
            graph[current["id"]] = {"role": role, "caused_by": caused_by}
            
        return graph

    def identify_primary_threat(self, vehicles: list[dict]) -> int | None:
        """Returns the ID of the vehicle with the earliest violation timestamp."""
        if not vehicles:
            return None
        sorted_v = sorted(vehicles, key=lambda x: x.get("timestamp_went_wrong_way", float("inf")))
        return sorted_v[0]["id"]

    def get_cascade_tree(self, vehicles: list[dict]) -> dict:
        """Builds a parent-child relationship map representing the chain reaction."""
        graph = self.build_temporal_graph(vehicles)
        tree = defaultdict(list)
        
        for v_id, info in graph.items():
            parent = info["caused_by"]
            if parent is not None:
                tree[parent].append(v_id)
                
        return dict(tree)

    def get_alert_strategy(self, vehicle_id: int, role: str) -> dict:
        if role == "PRIMARY":
            return {"priority": "CRITICAL", "message": "Primary wrong-way threat"}
        if role == "EVASIVE":
            return {"priority": "GUIDE", "message": "Evasive maneuver detected, guide to safety"}
        return {"priority": "STANDARD", "message": "Standard analysis"}

    def visualize_cascade(self, vehicles: list[dict]) -> list[tuple]:
        """Returns a list of (source_id, target_id, relationship) for frontend visualization."""
        graph = self.build_temporal_graph(vehicles)
        edges = []
        
        for v_id, info in graph.items():
            if info["caused_by"] is not None:
                edges.append((info["caused_by"], v_id, "caused"))
                
        return edges
