from __future__ import annotations

import math
import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import os
import joblib
import pandas as pd

from backend.models import POI, RoadSegment, Vehicle, VehicleHistory
from backend.services.geo import (
    cumulative_path_lengths,
    haversine_distance_m,
    interpolate_path_position,
    move_coordinate,
)
from backend.services.behavioral_analysis import (
    BehavioralFingerprint,
    ContextualAnomalyDetector,
    MetaConfidenceTracker,
    GPSCanyonDetector,
    IntentionalReversalClassifier,
    AdversarialDetector,
    CascadeAnalyzer,
)
from backend.simulation.engine import simulation_engine


@dataclass
class HumanAgent:
    id: str
    lat: float
    lon: float
    speed_mps: float
    bearing: float
    intent: str
    nearest_road_segment_id: int | None
    risk_zone_m: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lat": self.lat,
            "lon": self.lon,
            "speed": self.speed_mps,
            "bearing": self.bearing,
            "intent": self.intent,
            "nearest_road_segment_id": self.nearest_road_segment_id,
            "risk_zone_m": self.risk_zone_m,
        }


class LiveTrafficIntelligence:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_selected_vehicle_id: int | None = None
        self._last_eval_at: float = 0.0
        self._last_eval_payload: dict = {
            "evaluation": {"tp": 0, "fp": 0, "tn": 0, "fn": 0, "precision": 0.0, "recall": 0.0, "fpr": 0.0},
            "roc": [],
            "confidence_distribution": [],
        }
        
        # New model integration & calibration metrics
        self._model_path = "backend/models/final_model.pkl"
        self._model_data = self._load_model()
        self._violation_starts = {}  # vehicle_id -> timestamp
        self._detections = {}        # vehicle_id -> timestamp
        self._latency_history = []   # list of detection delays
        self._fp_total = 0
        self._tn_total = 0

        # Behavioral Intelligence Singletons
        self.fingerprint = BehavioralFingerprint()
        self.contextual_anomaly = ContextualAnomalyDetector()
        self.meta_confidence = MetaConfidenceTracker()
        self.gps_detector = GPSCanyonDetector()
        self.reversal_classifier = IntentionalReversalClassifier()
        self.adversarial_detector = AdversarialDetector()
        self.cascade_analyzer = CascadeAnalyzer()

    def _load_model(self) -> dict | None:
        if os.path.exists(self._model_path):
            try:
                return joblib.load(self._model_path)
            except Exception as e:
                print(f"Failed to load calibrated model: {e}")
        return None

    def build_snapshot(self, selected_vehicle_id: int | None = None) -> dict:
        vehicles = Vehicle.query.order_by(Vehicle.id).all()
        roads = RoadSegment.query.order_by(RoadSegment.id).all()
        pois = POI.query.order_by(POI.id).all()
        now = max((v.timestamp for v in vehicles), default=0.0)

        humans = self._build_humans(roads, pois, now)
        selected_vehicle, selection_source = self._select_vehicle(vehicles, selected_vehicle_id)
        with self._lock:
            self._last_selected_vehicle_id = selected_vehicle.id if selected_vehicle else None
        vehicle_predictions = {
            v.id: simulation_engine.predict_vehicle_path(v.id)
            for v in vehicles
        }
        human_predictions = {
            h.id: self._predict_human_path(h) for h in humans
        }
        collisions = self._predict_collisions(
            vehicles, humans, vehicle_predictions, human_predictions,
            selected_vehicle.id if selected_vehicle else None,
        )
        heatmap = self._build_heatmap(roads, vehicles, humans, collisions)

        # Populate history buffers for all active vehicles to enable behavioral profiling
        # We fetch the last 40 seconds of data for all vehicles in a single batch query
        window_start = now - 40.0
        active_ids = [v.id for v in vehicles]
        all_hist = VehicleHistory.query.filter(
            VehicleHistory.vehicle_id.in_(active_ids),
            VehicleHistory.timestamp >= window_start
        ).order_by(VehicleHistory.vehicle_id, VehicleHistory.timestamp.asc()).all()

        # Group history by vehicle_id
        hist_map = defaultdict(list)
        for h in all_hist:
            hist_map[h.vehicle_id].append(h)

        # Feed the in-memory deques used by the BehavioralFingerprint
        for v in vehicles:
            v_hist = hist_map.get(v.id, [])
            v.speed_history = [h.speed_mps for h in v_hist]
            v.bearing_history = [h.bearing for h in v_hist]
            v.history_timestamps = [h.timestamp for h in v_hist]
            v.position_history = [(h.lat, h.lon) for h in v_hist]
            
            # Reconstruct acceleration history for the jerk calculation
            if len(v.speed_history) > 1:
                v.acceleration_history = [0.0]
                for i in range(1, len(v.speed_history)):
                    dt = v.history_timestamps[i] - v.history_timestamps[i-1]
                    dv = v.speed_history[i] - v.speed_history[i-1]
                    v.acceleration_history.append(dv / dt if dt > 0 else 0.0)
            else:
                v.acceleration_history = []

        # Behavioral Intelligence Pass
        all_enhanced_scores = {}
        for v in vehicles:
            road = next((r for r in roads if r.id == v.road_segment_id), None)
            # Find nearby POIs for the contextual detector
            nearby_pois = [
                {"type": p.poi_type, "distance": haversine_distance_m(v.lat, v.lon, p.lat, p.lon)}
                for p in pois
                if haversine_distance_m(v.lat, v.lon, p.lat, p.lon) < 500.0
            ]
            scores = self.compute_scores(v, vehicles, road, nearby_pois, now)
            all_enhanced_scores[v.id] = scores

        # Cascade Analysis for Multi-Vehicle Wrong-Way Scenarios
        wrong_way_list = []
        for v in vehicles:
            if v.state == "wrong_way" or v.wrong_way:
                # Mock a dictionary format compatible with CascadeAnalyzer
                wrong_way_list.append({
                    "id": v.id,
                    "lat": v.lat,
                    "lon": v.lon,
                    "bearing": v.bearing,
                    "timestamp_went_wrong_way": getattr(v, "wrong_way_since", v.timestamp - 2.0) # Heuristic for now
                })
        
        cascade_tree = {}
        if len(wrong_way_list) > 1:
            cascade_tree = self.cascade_analyzer.build_temporal_graph(wrong_way_list)
            for v_id, info in cascade_tree.items():
                if v_id in all_enhanced_scores:
                    all_enhanced_scores[v_id]["cascade_role"] = info["role"]
                    # Update model field
                    for v in vehicles:
                        if v.id == v_id:
                            v.cascade_role = info["role"]

        return {
            "selected_vehicle_id": selected_vehicle.id if selected_vehicle else None,
            "selection_source": selection_source,
            "humans": [human.to_dict() for human in humans],
            "heatmap": heatmap,
            "collision_predictions": collisions,
            "enhanced_telemetry": all_enhanced_scores,
            "cascade_tree": cascade_tree,
            "selected_vehicle": (
                self._selected_vehicle_insights(
                    selected_vehicle,
                    vehicle_predictions.get(selected_vehicle.id, []),
                    collisions,
                    heatmap,
                )
                if selected_vehicle
                else None
            ),
        }

    def build_analytics_timeseries(self) -> dict:
        """Return timeseries data for analytics charts."""
        raw = simulation_engine.get_analytics_timeseries()
        if not raw:
            vehicles = simulation_engine.get_vehicles_snapshot()
            max_wwp = max((v["wwp"] for v in vehicles), default=0.0)
            ttc_values = [v["ttc"] for v in vehicles if v["ttc"] is not None]
            min_ttc = min(ttc_values) if ttc_values else None
            max_risk = max((v["risk_score"] for v in vehicles), default=0.0)
            return {
                "ticks": [time.time()] if vehicles else [],
                "wwp": [round(max_wwp, 3)] if vehicles else [],
                "ttc": [round(min_ttc, 1)] if min_ttc is not None else ([] if not vehicles else [None]),
                "risk": [round(max_risk, 3)] if vehicles else [],
                "labels": [0.0] if vehicles else [],
                "evaluation": self._evaluation_metrics(),
                "roc": self._roc_points(),
                "confidence_distribution": self._confidence_distribution(),
            }

        ticks = []
        wwp_series = []
        ttc_series = []
        risk_series = []

        for snap in raw:
            t = snap["t"]
            vehicles = snap["vehicles"]
            if not vehicles:
                continue

            ticks.append(t)

            # Aggregate: max WWP, min TTC, max risk across all vehicles
            max_wwp = max((v["wwp"] for v in vehicles), default=0.0)
            ttc_values = [v["ttc"] for v in vehicles if v["ttc"] is not None]
            min_ttc = min(ttc_values) if ttc_values else None
            max_risk = max((v["risk"] for v in vehicles), default=0.0)

            wwp_series.append(round(max_wwp, 3))
            ttc_series.append(round(min_ttc, 1) if min_ttc is not None else None)
            risk_series.append(round(max_risk, 3))

        # Normalize timestamps to relative seconds from start
        if ticks:
            t0 = ticks[0]
            labels = [round(t - t0, 1) for t in ticks]
        else:
            labels = []

        now = time.time()
        if (now - self._last_eval_at) >= 2.0:
            self._last_eval_payload = {
                "evaluation": self._evaluation_metrics(),
                "roc": self._roc_points(),
                "confidence_distribution": self._confidence_distribution(),
            }
            self._last_eval_at = now

        return {
            "ticks": ticks,
            "labels": labels,
            "wwp": wwp_series,
            "ttc": ttc_series,
            "risk": risk_series,
            "evaluation": self._last_eval_payload["evaluation"],
            "roc": self._last_eval_payload["roc"],
            "confidence_distribution": self._last_eval_payload["confidence_distribution"],
        }

    def build_risk_monitor(self) -> dict:
        """Return data for the risk monitor page."""
        risk_vehicles = simulation_engine.get_risk_vehicles()

        # Get collision predictions from live analysis
        vehicles = Vehicle.query.order_by(Vehicle.id).all()
        vehicle_predictions = {
            v.id: simulation_engine.predict_vehicle_path(v.id)
            for v in vehicles
        }

        # Find imminent collisions (TTC < 15s)
        active_alerts = []
        for v in risk_vehicles:
            alert = {
                "vehicle_id": v["id"],
                "risk_score": v["risk_score"],
                "risk_level": self._risk_level(v["risk_score"]),
                "state": v["state"],
                "speed": v["speed"],
                "wwp": v["wwp"],
                "ttc": v["ttc"],
                "anomaly_score": v["anomaly_score"],
                "lat": v["lat"],
                "lon": v["lon"],
            }
            active_alerts.append(alert)

        # Predicted collisions: vehicles with low TTC
        predicted_collisions = []
        for v in risk_vehicles:
            if v["ttc"] is not None and v["ttc"] < 15.0:
                predicted_collisions.append({
                    "vehicle_id": v["id"],
                    "ttc": v["ttc"],
                    "risk_score": v["risk_score"],
                    "risk_level": self._risk_level(v["risk_score"]),
                    "speed": v["speed"],
                    "state": v["state"],
                })

        predicted_collisions.sort(key=lambda x: x["ttc"] if x["ttc"] is not None else 999)

        return {
            "high_risk_vehicles": active_alerts,
            "predicted_collisions": predicted_collisions[:10],
            "total_vehicles": Vehicle.query.count(),
            "wrong_way_count": sum(1 for v in risk_vehicles if v.get("state") == "wrong_way" or v.get("wrong_way")),
            "critical_count": sum(1 for a in active_alerts if a["risk_level"] == "critical"),
        }

    def compute_scores(self, vehicle, all_vehicles, road, pois, current_time) -> dict:
        """Centralized scoring pipeline for behavioral intelligence."""
        # A. Core Behavioral Analysis
        signature = self.fingerprint.compute_signature(vehicle)
        intent = self.fingerprint.classify_intent(signature)
        gps_quality = self.gps_detector.gps_quality_score(vehicle)
        
        # B. GPS Canyon & Dead Reckoning
        gps_degraded = False
        if self.gps_detector.should_suppress_alert(vehicle, pois):
            gps_degraded = True
            lat_fallback, lon_fallback = self.gps_detector.get_fallback_position(vehicle)
            # We don't modify the vehicle object's core lat/lon here to keep DB integrity,
            # but we flag it for the telemetry.
            
        # C. Contextual Filtering & Meta-Learning
        ctx_score = self.contextual_anomaly.contextual_score(
            vehicle, road, pois, datetime.fromtimestamp(current_time)
        )
        # Wrap context for MetaConfidenceTracker
        class ContextWrapper:
            def __init__(self, t, w):
                self.time = t
                self.weather = w
        context = ContextWrapper(datetime.fromtimestamp(current_time), "clear")
        
        confidence = self.meta_confidence.get_confidence(vehicle, road, context)
        
        # D. Calibrated Prediction & Thresholding
        if self._model_data:
            model = self._model_data["model"]
            scaler = self._model_data.get("scaler")
            
            # Feature calculation: dev_angle is the deviation from road bearing
            road_bearing = road.bearing if road else 0.0
            vehicle_bearing = vehicle.bearing
            angle_diff = abs(((vehicle_bearing - road_bearing + 180.0) % 360.0) - 180.0)
            
            # Build feature dataframe [speed, dev_angle, anomaly_score, gps_quality]
            features_dict = {
                "speed": vehicle.speed_mps,
                "dev_angle": angle_diff,
                "anomaly_score": vehicle.anomaly_score,
                "gps_quality": gps_quality
            }
            features_df = pd.DataFrame([features_dict])
            
            try:
                # Apply scaler transform if available
                if scaler:
                    X_scaled = scaler.transform(features_df)
                else:
                    X_scaled = features_df # Fallback if scaler missing (unlikely)

                calibrated_prob = model.predict_proba(X_scaled)[0][1]
                confidence = float(calibrated_prob)
                
                # Apply optimized threshold
                threshold = self._model_data.get("threshold", 0.5)
                
                # Update vehicle state with hysteresis for stability
                # PRESERVE: Do not downgrade if simulation engine explicitly flags as wrong_way
                engine_wrong_way = getattr(vehicle, "wrong_way", False)
                
                if confidence > threshold or engine_wrong_way:
                    vehicle.state = "wrong_way"
                elif vehicle.state == "wrong_way" and confidence < (threshold * 0.7):
                    # Higher hysteresis gap (0.7) for real-world simulation stability
                    vehicle.state = "normal"
            except Exception as e:
                print(f"Prediction failed: {e}")

        # E. Active Feedback Loop (Learning)
        # Update the meta-confidence tracker based on prediction correctness
        if self._model_data and hasattr(vehicle, "ground_truth_wrong_way"):
            actual = vehicle.ground_truth_wrong_way
            predicted = vehicle.state == "wrong_way"
            was_correct = (actual == predicted)
            
            # Key = road_type_time_weather_gps
            road_type = getattr(road, "road_class", "primary") or "primary"
            time_period = self.meta_confidence.get_time_period(datetime.fromtimestamp(current_time))
            # We'll use a string key directly
            situation_key = f"{road_type}_{time_period}_clear_medium" 
            self.meta_confidence.update(situation_key, was_correct)

        # F. Performance Tracking (Temporal & FP)
        # Ground Truth comparison (assuming simulation_engine.get_vehicle_live)
        # Here we use vehicle.wrong_way attribute as ground truth (from sim)
        actual = bool(getattr(vehicle, "wrong_way", False))
        predicted = (confidence > self._model_data.get("threshold", 0.5)) if self._model_data else (vehicle.state == "wrong_way")
        
        if actual:
            if vehicle.id not in self._violation_starts:
                self._violation_starts[vehicle.id] = current_time
            
            if predicted and vehicle.id not in self._detections:
                self._detections[vehicle.id] = current_time
                delay = current_time - self._violation_starts[vehicle.id]
                self._latency_history.append(delay)
                print(f"VEHICLE {vehicle.id} DETECTED with delay {delay:.2f}s (Avg: {sum(self._latency_history)/len(self._latency_history):.2f}s)")
        else:
            # Not a violation ground truth
            self._violation_starts.pop(vehicle.id, None)
            self._detections.pop(vehicle.id, None)
            
            if predicted:
                self._fp_total += 1
            else:
                self._tn_total += 1
            
            if (self._fp_total + self._tn_total) % 100 == 0:
                fpr = self._fp_total / max(1, (self._fp_total + self._tn_total))
                print(f"LIVE FPR: {fpr:.4f}")

        adjusted_anomaly = ctx_score * confidence
        
        # F. Wrong-Way Depth Analysis
        gaming_score = 0.0
        intentional_class = "UNKNOWN"
        alert_suppressed = False
        escalate = False
        
        if vehicle.wrong_way or vehicle.state == "wrong_way":
            if signature:
                gaming_score = self.adversarial_detector.calculate_gaming_score(vehicle, signature)
                escalate = self.adversarial_detector.should_escalate(gaming_score)
                
            intentional_class = self.reversal_classifier.classify(
                vehicle, all_vehicles, datetime.fromtimestamp(current_time)
            )
            
            # Policy-based alert suppression
            if intentional_class in ["EMERGENCY_VEHICLE", "CONVOY"]:
                alert_suppressed = True

        # E. Update Model Fields
        vehicle.behavioral_signature = signature
        vehicle.intent_classification = intent
        vehicle.confidence_adjustment = float(confidence)
        vehicle.gps_quality_score = float(gps_quality)
        # Note: cascade_role is handled in build_snapshot for multi-vehicle context
        
        # Prepare output
        return {
            "vehicle_id": vehicle.id,
            "behavioral_signature": signature,
            "intent_classification": intent,
            "gps_quality_score": round(gps_quality, 3),
            "gps_degraded": gps_degraded,
            "confidence_adjustment": round(confidence, 3),
            "adjusted_anomaly": round(adjusted_anomaly, 3),
            "gaming_score": round(gaming_score, 3),
            "intentional_class": intentional_class,
            "alert_suppressed": alert_suppressed,
            "escalate_law_enforcement": escalate,
            "cascade_role": getattr(vehicle, "cascade_role", "NONE")
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _select_vehicle(
        self,
        vehicles: list[Vehicle],
        selected_vehicle_id: int | None,
    ) -> tuple[Vehicle | None, str]:
        if not vehicles:
            return None, "empty_fleet"

        if selected_vehicle_id is not None:
            for vehicle in vehicles:
                if vehicle.id == selected_vehicle_id:
                    return vehicle, "client_selected"
        with self._lock:
            sticky_id = self._last_selected_vehicle_id
        if sticky_id is not None:
            for vehicle in vehicles:
                if vehicle.id == sticky_id:
                    return vehicle, "sticky_previous"
        for vehicle in vehicles:
            if vehicle.wrong_way:
                return vehicle, "auto_wrong_way"
        return vehicles[0], "auto_first_vehicle"

    def _build_humans(
        self, roads: list[RoadSegment], pois: list[POI], now: float,
    ) -> list[HumanAgent]:
        roads_by_id = {r.id: r for r in roads}
        anchors = [
            p for p in pois
            if p.nearest_road_segment_id is not None
            and p.poi_type in {"intersection", "signal", "shop", "parking"}
        ]
        anchors.sort(
            key=lambda p: (
                {"signal": 0, "intersection": 1, "shop": 2, "parking": 3}.get(p.poi_type, 9),
                p.id,
            )
        )

        humans: list[HumanAgent] = []
        for poi in anchors[:18]:
            road = roads_by_id.get(poi.nearest_road_segment_id)
            if road is None:
                continue

            crossing = poi.poi_type in {"signal", "intersection"}
            phase = (now / (5.0 if crossing else 8.0)) + (poi.id % 7)
            offset_m = math.sin(phase) * (8.0 if crossing else 15.0)
            bearing = (road.bearing + (90.0 if crossing else 0.0)) % 360.0
            lat, lon = move_coordinate(poi.lat, poi.lon, offset_m, bearing)
            intent = (
                "crossing at signal" if poi.poi_type == "signal"
                else "crossing at intersection" if poi.poi_type == "intersection"
                else "walking near storefront" if poi.poi_type == "shop"
                else "moving from parked vehicle"
            )
            speed = 1.35 if crossing else 1.05
            humans.append(HumanAgent(
                id=f"h-{poi.id}", lat=lat, lon=lon, speed_mps=speed,
                bearing=bearing, intent=intent,
                nearest_road_segment_id=road.id,
                risk_zone_m=10.0 if crossing else 7.0,
            ))

        if humans or not roads:
            return humans

        for road in roads[:min(10, len(roads))]:
            midpoint = self._road_point(road, road.length_m * 0.5)
            offset_m = math.sin((now / 6.0) + road.id) * 7.0
            bearing = (road.bearing + 90.0) % 360.0
            lat, lon = move_coordinate(midpoint["lat"], midpoint["lon"], offset_m, bearing)
            humans.append(HumanAgent(
                id=f"h-road-{road.id}", lat=lat, lon=lon, speed_mps=1.2,
                bearing=bearing, intent="crossing mid-block",
                nearest_road_segment_id=road.id, risk_zone_m=9.0,
            ))
        return humans

    def _predict_human_path(self, human: HumanAgent) -> list[dict]:
        points = []
        for i in range(15):
            t = float(i)
            lat, lon = move_coordinate(human.lat, human.lon, human.speed_mps * t, human.bearing)
            points.append({"t": t, "lat": lat, "lon": lon, "speed": human.speed_mps})
        return points

    def _predict_collisions(
        self, vehicles, humans, vehicle_predictions, human_predictions, selected_vehicle_id,
    ) -> list[dict]:
        risks: list[dict] = []

        for i, v in enumerate(vehicles):
            for other in vehicles[i + 1:]:
                risk = self._closest_approach(
                    vehicle_predictions.get(v.id, []),
                    vehicle_predictions.get(other.id, []),
                    v.speed_mps, other.speed_mps, threshold_m=11.0,
                )
                if risk is None:
                    continue
                risks.append(self._collision_payload(
                    risk, v.id, "vehicle", other.id, "vehicle",
                    selected_vehicle_id, self._vehicle_collision_scenario(v, other),
                ))

        for v in vehicles:
            for h in humans:
                risk = self._closest_approach(
                    vehicle_predictions.get(v.id, []),
                    human_predictions.get(h.id, []),
                    v.speed_mps, h.speed_mps, threshold_m=14.0,
                )
                if risk is None:
                    continue
                risks.append(self._collision_payload(
                    risk, v.id, "vehicle", h.id, "human",
                    selected_vehicle_id, h.intent,
                ))

        risks.sort(key=lambda x: (0 if x["involves_selected"] else 1, -x["risk_score"], x["seconds_to_conflict"]))
        return risks[:12]

    def _closest_approach(self, first_path, second_path, first_speed, second_speed, threshold_m):
        if not first_path or not second_path:
            return None

        by_t = {round(p["t"], 2): p for p in second_path}
        best = None
        for p in first_path:
            other = by_t.get(round(p["t"], 2))
            if other is None:
                continue
            distance = haversine_distance_m(p["lat"], p["lon"], other["lat"], other["lon"])
            if best is None or distance < best["distance_m"]:
                best = {
                    "seconds_to_conflict": float(p["t"]),
                    "distance_m": distance,
                    "lat": (p["lat"] + other["lat"]) / 2.0,
                    "lon": (p["lon"] + other["lon"]) / 2.0,
                }

        if best is None or best["distance_m"] > threshold_m:
            return None

        closing_weight = min((first_speed + second_speed) / 24.0, 1.0)
        proximity_weight = max(0.0, 1.0 - (best["distance_m"] / threshold_m))
        urgency_weight = max(0.0, 1.0 - (best["seconds_to_conflict"] / 14.0))
        best["risk_score"] = round(
            min(1.0, 0.50 * proximity_weight + 0.30 * urgency_weight + 0.20 * closing_weight), 3,
        )
        return best

    def _collision_payload(self, risk, primary_id, primary_type, target_id, target_type, selected_vehicle_id, scenario):
        involves_selected = selected_vehicle_id in {primary_id, target_id}
        return {
            "primary_id": primary_id,
            "primary_type": primary_type,
            "target_id": target_id,
            "target_type": target_type,
            "seconds_to_conflict": round(risk["seconds_to_conflict"], 1),
            "distance_m": round(risk["distance_m"], 1),
            "lat": risk["lat"],
            "lon": risk["lon"],
            "risk_score": risk["risk_score"],
            "risk_level": self._risk_level(risk["risk_score"]),
            "scenario": scenario,
            "involves_selected": involves_selected,
        }

    def _build_heatmap(self, roads, vehicles, humans, collisions) -> list[dict]:
        vehicles_by_segment: dict[int, list] = defaultdict(list)
        humans_by_segment: dict[int, list] = defaultdict(list)
        collision_by_segment: dict[int, float] = defaultdict(float)
        vehicle_by_id = {v.id: v for v in vehicles}

        for v in vehicles:
            vehicles_by_segment[v.road_segment_id].append(v)
        for h in humans:
            if h.nearest_road_segment_id is not None:
                humans_by_segment[h.nearest_road_segment_id].append(h)
        for c in collisions:
            primary = vehicle_by_id.get(c["primary_id"])
            if primary is not None:
                collision_by_segment[primary.road_segment_id] = max(
                    collision_by_segment[primary.road_segment_id], c["risk_score"],
                )

        cells = []
        for road in roads:
            seg_v = vehicles_by_segment.get(road.id, [])
            seg_h = humans_by_segment.get(road.id, [])
            if not seg_v and not seg_h and road.poi_density < 0.1:
                continue

            wrong_way_count = sum(1 for v in seg_v if v.wrong_way)
            avg_speed = statistics.fmean(v.speed_mps for v in seg_v) if seg_v else 0.0
            max_anomaly = max((v.anomaly_score for v in seg_v), default=0.0)
            avg_confidence = statistics.fmean(getattr(v, "wwp", 0.0) for v in seg_v) if seg_v else 0.0

            density_score = min(len(seg_v) / 4.0, 1.0)
            human_score = min(len(seg_h) / 2.0, 1.0)
            poi_score = min(float(road.poi_density or 0.0) / 12.0, 1.0)
            speed_score = min(avg_speed / 18.0, 1.0)
            wrong_score = 1.0 if wrong_way_count else 0.0
            collision_score = collision_by_segment.get(road.id, 0.0)
            anomaly_factor = max_anomaly

            risk_score = min(
                1.0,
                0.18 * density_score
                + 0.15 * human_score
                + 0.10 * poi_score
                + 0.10 * speed_score
                + 0.20 * wrong_score
                + 0.15 * collision_score
                + 0.12 * anomaly_factor
                + 0.10 * avg_confidence,
            )
            if risk_score < 0.12:
                continue

            point = self._road_point(road, road.length_m * 0.5)
            # Heatmap radius tied to anomaly score per spec
            radius = max(28.0, min(anomaly_factor * 40 + 20, 90.0))

            cells.append({
                "road_segment_id": road.id,
                "lat": point["lat"],
                "lon": point["lon"],
                "radius_m": round(radius, 1),
                "risk_score": round(risk_score, 3),
                "risk_level": self._risk_level(risk_score),
                "confidence": round(avg_confidence, 3),
                "scenario": self._road_scenario(road, wrong_way_count, len(seg_h), collision_score),
                "vehicle_count": len(seg_v),
                "human_count": len(seg_h),
                "avg_speed_mps": round(avg_speed, 1),
            })

        cells.sort(key=lambda c: c["risk_score"], reverse=True)
        return cells[:45]

    def _selected_vehicle_insights(self, vehicle, trajectory, collisions, heatmap) -> dict:
        history = (
            VehicleHistory.query.filter_by(vehicle_id=vehicle.id)
            .order_by(VehicleHistory.timestamp.desc())
            .limit(80)
            .all()
        )
        history = list(reversed(history))
        behavior = self._behavior_awareness(vehicle, history, collisions)
        heatmap_by_segment = {c["road_segment_id"]: c for c in heatmap}
        path_risks = [
            heatmap_by_segment[p["road_segment_id"]]
            for p in trajectory
            if p["road_segment_id"] in heatmap_by_segment
        ]
        selected_collisions = [c for c in collisions if c["involves_selected"]]

        road = RoadSegment.query.get(vehicle.road_segment_id)
        live_lookup = {v["id"]: v for v in simulation_engine.get_vehicles_snapshot()}
        live_state = live_lookup.get(vehicle.id, {})
        detection_logic = self._detection_logic(vehicle, road, history, live_state)
        false_positive = self._false_positive_status(
            vehicle, history, detection_logic["road_bearing"], detection_logic["angle_difference_deg"]
        )
        detection_logic = self._apply_false_positive_filter(detection_logic, false_positive)
        selected_heatmap = self._selected_vehicle_heatmap(vehicle, history)
        alert_triggered = self._alert_triggered(vehicle, false_positive, detection_logic)

        return {
            "id": vehicle.id,
            "speed": vehicle.speed_mps,
            "state": vehicle.state,
            "trajectory": trajectory,
            "temporal_analysis": self._temporal_analysis(history),
            "behavioral_awareness": behavior,
            "route_risk": path_risks[:8],
            "collision_predictions": selected_collisions[:6],
            # Direction metrics
            "wwp": round(vehicle.wwp, 3),
            "direction_score": round(1.0 - vehicle.wwp, 3),
            # Risk metrics
            "ttc": round(vehicle.ttc, 1) if vehicle.ttc is not None else None,
            "risk_score": round(vehicle.risk_score, 3),
            "maneuverability": round(vehicle.maneuverability, 3),
            # Spatial metrics
            "nearby_count": vehicle.nearby_count,
            "closest_distance_m": (
                round(vehicle.closest_distance_m, 1)
                if vehicle.closest_distance_m is not None else None
            ),
            # Semantic
            "road_class": road.road_class if road else None,
            "poi_density": round(road.poi_density, 2) if road else 0.0,
            # ML
            "anomaly_score": round(vehicle.anomaly_score, 3),
            "memory_match": round(self._memory_match_score(vehicle, history), 3),
            # Advanced direction/confidence
            "heading": round(float(live_state.get("heading", vehicle.bearing or 0.0)), 1),
            "road_bearing": round(getattr(vehicle, "bearing", 0.0), 1) if road is None else round(road.bearing, 1),
            "angle_diff": round(float(live_state.get("angle_diff", 0.0)), 1),
            "confidence": round(float(live_state.get("confidence", vehicle.wwp)), 3),
            "status": (
                "CONFIRMED" if float(live_state.get("confidence", vehicle.wwp)) >= 0.75 else
                "SUSPICIOUS" if float(live_state.get("confidence", vehicle.wwp)) >= 0.55 else
                "NORMAL"
            ),
            "gps_stability": live_state.get("gps_stability", "MEDIUM"),
            "edge_case": live_state.get("edge_case", "NONE"),
            # Detection logic + false positive handling
            "detection_logic": detection_logic,
            "false_positive": false_positive,
            "selected_vehicle_heatmap": selected_heatmap,
            "alert_triggered": alert_triggered,
            "surrounding_context": self._surrounding_context(vehicle, road, heatmap),
            "kinematics": live_state.get("kinematics", {
                "braking_distance": 0.0,
                "heading_drift": 0.0,
                "lateral_offset": 0.0
            }),
        }

    def _detection_logic(self, vehicle, road, history, live_state: dict | None = None) -> dict:
        live_state = live_state or {}
        road_bearing = float(road.bearing) if road and road.bearing is not None else 0.0
        vehicle_bearing = float(live_state.get("heading", vehicle.bearing or 0.0))
        angle_diff = abs(((vehicle_bearing - road_bearing + 180.0) % 360.0) - 180.0)
        temporal = self._temporal_stability(history, angle_diff)
        confidence = float(live_state.get("confidence", vehicle.wwp))
        if confidence >= 0.88 and angle_diff >= 160.0:
            decision = "WRONG-WAY"
        elif confidence >= 0.85 and angle_diff > 130.0:
            decision = "SUSPICIOUS"
        else:
            decision = "NORMAL"
        return {
            "pipeline": [
                "Road Direction",
                "Vehicle Heading",
                "Angle Difference",
                "Temporal Filter",
                "Final Decision",
            ],
            "road_bearing": round(road_bearing, 1),
            "vehicle_bearing": round(vehicle_bearing, 1),
            "angle_difference_deg": round(angle_diff, 1),
            "temporal_stability": temporal,
            "decision": decision,
            "confidence": round(confidence, 3),
        }

    def _temporal_stability(self, history, current_angle_diff: float) -> str:
        if len(history) < 6:
            return "LOW"
        recent = history[-8:]
        headings = [float(row.bearing or 0.0) for row in recent]
        if len(headings) < 2:
            return "LOW"
        diffs = []
        for i in range(1, len(headings)):
            diffs.append(abs(((headings[i] - headings[i - 1] + 180.0) % 360.0) - 180.0))
        avg_change = statistics.fmean(diffs) if diffs else 180.0
        if current_angle_diff >= 150.0 and avg_change <= 12.0:
            return "HIGH"
        if avg_change <= 25.0:
            return "MEDIUM"
        return "LOW"

    def _false_positive_status(self, vehicle, history, road_bearing: float, angle_diff: float) -> dict:
        if len(history) < 4:
            return {"risk": "MEDIUM", "reason": "Noise", "confidence_modifier": 0.85}

        now_ts = history[-1].timestamp
        recent_wrong = [
            h for h in history
            if abs(((float(h.bearing or 0.0) - road_bearing + 180.0) % 360.0) - 180.0) >= 150.0
        ]
        # Case 1: short wrong-way spike (U-turn)
        violation_duration = (
            recent_wrong[-1].timestamp - recent_wrong[0].timestamp
            if len(recent_wrong) >= 2 else 0.0
        )
        if violation_duration < 2.0 and vehicle.wrong_way:
            return {"risk": "HIGH", "reason": "U-turn", "confidence_modifier": 0.55}

        # Case 2: intersection crossing profile (~90-degree)
        if 60.0 < angle_diff < 120.0:
            return {"risk": "MEDIUM", "reason": "Intersection", "confidence_modifier": 0.7}

        # Case 3: noisy heading variance
        heading_window = [float(h.bearing or 0.0) for h in history[-12:]]
        heading_var = statistics.pvariance(heading_window) if len(heading_window) > 1 else 0.0
        if heading_var > 800.0:
            return {"risk": "HIGH", "reason": "Noise", "confidence_modifier": 0.6}

        # Stable violation has low FP risk
        if vehicle.wrong_way and (now_ts - history[max(0, len(history) - 8)].timestamp) >= 2.0:
            return {"risk": "LOW", "reason": "Stable", "confidence_modifier": 1.0}

        return {"risk": "MEDIUM", "reason": "Uncertain", "confidence_modifier": 0.85}

    def _selected_vehicle_heatmap(self, vehicle, history) -> list[dict]:
        points = []
        if not history:
            return points
        tail = history[-40:]
        for row in tail:
            # Weight by recency + anomaly contribution for dynamic intensity.
            recency = max(0.2, min(1.0, (row.timestamp - tail[0].timestamp) / max(tail[-1].timestamp - tail[0].timestamp, 0.1)))
            intensity = min(
                1.0,
                0.35 * recency + 0.35 * min(vehicle.anomaly_score + vehicle.wwp, 1.0) + 0.3 * min(vehicle.risk_score + 0.1, 1.0),
            )
            points.append({
                "lat": row.lat,
                "lon": row.lon,
                "intensity": round(intensity, 3),
                "timestamp": row.timestamp,
            })
        return points

    def _memory_match_score(self, vehicle, history) -> float:
        if len(history) < 5:
            return max(0.15, min(vehicle.anomaly_score * 0.6 + vehicle.wwp * 0.4, 0.85))
        speeds = [row.speed_mps for row in history[-20:]]
        bearings = [row.bearing for row in history[-20:]]
        speed_var = statistics.pvariance(speeds) if len(speeds) > 1 else 0.0
        bearing_var = statistics.pvariance(bearings) if len(bearings) > 1 else 0.0
        stability = max(0.0, 1.0 - min((speed_var / 9.0) + (bearing_var / 2200.0), 1.0))
        novelty = min(1.0, vehicle.anomaly_score * 0.6 + vehicle.wwp * 0.4)
        # "Memory match" means similarity to known patterns; stable normal motion gives higher match.
        return max(0.05, min(1.0, 0.65 * stability + 0.35 * (1.0 - novelty)))

    def _surrounding_context(self, vehicle, road, heatmap) -> list[dict]:
        contexts: list[dict] = []
        if road is not None:
            if road.road_class in {"motorway", "trunk", "primary"}:
                contexts.append({"label": "High-speed corridor", "risk": "HIGH"})
            if road.length_m > 220 and road.oneway:
                contexts.append({"label": "Long one-way stretch", "risk": "ELEVATED"})
            if road.poi_density >= 8:
                contexts.append({"label": "Construction/curbside-like activity", "risk": "MEDIUM"})
            if road.length_m < 35:
                contexts.append({"label": "Tight turn or short connector", "risk": "MEDIUM"})
        # Nearby heatmap top risks
        for cell in heatmap[:3]:
            if cell.get("risk_score", 0) >= 0.48:
                contexts.append({
                    "label": cell.get("scenario", "Localized risk hotspot"),
                    "risk": str(cell.get("risk_level", "high")).upper(),
                })
        if not contexts:
            contexts.append({"label": "Stable surrounding flow", "risk": "LOW"})
        return contexts[:5]

    def _apply_false_positive_filter(self, detection_logic: dict, false_positive: dict) -> dict:
        decision = detection_logic.get("decision", "NORMAL")
        reason = false_positive.get("reason", "")
        if reason in {"U-turn", "Intersection"}:
            detection_logic["decision"] = "NORMAL (FILTERED)"
        elif reason == "Noise" and false_positive.get("risk") == "HIGH":
            detection_logic["decision"] = "SUSPECT (NOISY)"
        else:
            detection_logic["decision"] = decision
        return detection_logic

    def _alert_triggered(self, vehicle, false_positive: dict, detection_logic: dict) -> bool:
        # Alert should not fire for likely false positives.
        if detection_logic.get("decision") == "NORMAL (FILTERED)":
            return False
        if false_positive.get("reason") == "Noise" and false_positive.get("risk") == "HIGH":
            return False
        ttc_alert = vehicle.ttc is not None and vehicle.ttc < 10.0
        wrong_way_alert = detection_logic.get("decision", "").startswith("WRONG-WAY")
        risk_alert = vehicle.risk_score >= 0.48
        return bool(ttc_alert or wrong_way_alert or risk_alert)

    def _evaluation_metrics(self) -> dict:
        vehicles = simulation_engine.get_vehicles_snapshot()
        tp = fp = tn = fn = 0
        for v in vehicles:
            truth = bool(v.get("wrong_way"))
            # Prediction is True if the system has flagged it in ANY way
            pred = (v.get("state") == "wrong_way") or (v.get("state") == "suspicious") or (v.get("wwp", 0) > 0.7)
            
            if truth and pred:
                tp += 1
            elif not truth and pred:
                fp += 1
            elif not truth and not pred:
                tn += 1
            elif truth and not pred:
                fn += 1
        
        # Accuracy calculation (standard formula)
        total = tp + fp + tn + fn
        accuracy = (tp + tn) / total if total > 0 else 1.0
        
        fpr = fp / (fp + tn) if (fp + tn) else 0.0
        return {
            "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "accuracy": round(accuracy, 3),
            "fpr": round(fpr, 3)
        }

    def _roc_points(self) -> list[dict]:
        vehicles = simulation_engine.get_vehicles_snapshot()
        points: list[dict] = []
        for threshold in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
            tp = fp = tn = fn = 0
            for v in vehicles:
                truth = bool(v.get("wrong_way"))
                # Use wwp (= engine confidence) for threshold sweeps
                wwp_score = float(v.get("wwp", 0.0))
                pred = wwp_score >= threshold
                if truth and pred:
                    tp += 1
                elif not truth and pred:
                    fp += 1
                elif not truth and not pred:
                    tn += 1
                else:
                    fn += 1
            tpr = tp / (tp + fn) if (tp + fn) else 0.0
            fpr = fp / (fp + tn) if (fp + tn) else 0.0
            points.append({"threshold": threshold, "tpr": round(tpr, 3), "fpr": round(fpr, 3)})
        return points

    def _confidence_distribution(self) -> dict:
        vehicles = simulation_engine.get_vehicles_snapshot()
        bins = {"0.0-0.3": 0, "0.3-0.5": 0, "0.5-0.75": 0, "0.75-1.0": 0}
        for v in vehicles:
            c = float(v.get("wwp", 0.0))
            if c < 0.3:
                bins["0.0-0.3"] += 1
            elif c < 0.5:
                bins["0.3-0.5"] += 1
            elif c < 0.75:
                bins["0.5-0.75"] += 1
            else:
                bins["0.75-1.0"] += 1
        return bins

    def _behavior_awareness(self, vehicle, history, collisions) -> dict:
        recent_speeds = [row.speed_mps for row in history[-12:]]
        speed_std = statistics.pstdev(recent_speeds) if len(recent_speeds) > 1 else 0.0
        selected_collision = any(c["involves_selected"] for c in collisions)
        gap_seconds = simulation_engine.recommended_gap_seconds(vehicle.behavior)
        flags = []
        if vehicle.wrong_way:
            flags.append("wrong-way movement")
        if vehicle.behavior == "aggressive":
            flags.append("short headway tendency")
        if speed_std > 2.0:
            flags.append("unstable speed profile")
        if selected_collision:
            flags.append("projected conflict")
        if not flags:
            flags.append("stable route following")

        return {
            "profile": vehicle.behavior,
            "recommended_gap_seconds": gap_seconds,
            "awareness_flags": flags,
            "speed_variability_mps": round(speed_std, 3),
            "recent_speed_avg_mps": round(statistics.fmean(recent_speeds), 2) if recent_speeds else 0.0,
            "narrative": self._behavior_narrative(vehicle, flags),
        }

    def _temporal_analysis(self, history: list[VehicleHistory]) -> dict:
        if len(history) < 2:
            return {
                "samples": len(history),
                "average_speed_mps": 0.0,
                "speed_trend": "insufficient history",
                "acceleration_mps2": 0.0,
                "heading_change_deg": 0.0,
            }

        speeds = [row.speed_mps for row in history]
        first = history[0]
        last = history[-1]
        dt = max(last.timestamp - first.timestamp, 0.1)
        heading_change = abs(((last.bearing - first.bearing + 180.0) % 360.0) - 180.0)
        acceleration = (last.speed_mps - first.speed_mps) / dt
        trend = (
            "accelerating" if acceleration > 0.35
            else "braking" if acceleration < -0.35
            else "steady"
        )
        return {
            "samples": len(history),
            "average_speed_mps": round(statistics.fmean(speeds), 2),
            "speed_trend": trend,
            "acceleration_mps2": round(acceleration, 3),
            "heading_change_deg": round(heading_change, 1),
            "window_seconds": round(dt, 1),
        }

    def _road_point(self, road, distance_m) -> dict:
        lengths = cumulative_path_lengths(road.geometry)
        lat, lon = interpolate_path_position(road.geometry, lengths, distance_m)
        return {"lat": lat, "lon": lon}

    def _road_scenario(self, road, wrong_way_count, human_count, collision_score) -> str:
        if wrong_way_count:
            return "wrong-way on one-way corridor"
        if collision_score >= 0.5:
            return "near-term collision conflict"
        if human_count:
            return "pedestrian crossing pressure"
        if road.poi_density >= 8:
            return "market or curbside activity"
        if road.road_class in {"primary", "trunk", "motorway"}:
            return "high-speed arterial flow"
        return "dense mixed traffic"

    def _vehicle_collision_scenario(self, vehicle, other) -> str:
        if vehicle.road_segment_id == other.road_segment_id:
            return "same-lane rear-end or head-on conflict"
        if vehicle.wrong_way or other.wrong_way:
            return "wrong-way vehicle conflict"
        return "junction merge conflict"

    def _behavior_narrative(self, vehicle, flags) -> str:
        if vehicle.wrong_way:
            return "Vehicle is moving against permitted flow; prioritize interception and upstream warnings."
        if "projected conflict" in flags:
            return "Vehicle path intersects another road user inside the prediction horizon."
        if vehicle.behavior == "aggressive":
            return "Vehicle is modeled with lower gap acceptance and faster segment choices."
        if vehicle.behavior == "calm":
            return "Vehicle is maintaining conservative speed and wider headway."
        return "Vehicle is following the network with normal speed and headway."

    def _risk_level(self, score: float) -> str:
        if score >= 0.72:
            return "critical"
        if score >= 0.48:
            return "high"
        if score >= 0.25:
            return "elevated"
        return "watch"


live_traffic_intelligence = LiveTrafficIntelligence()
