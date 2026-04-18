import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app import create_app
from backend.extensions import db
from backend.services.ml_layer import LiveTrafficIntelligence
from backend.models import Vehicle, RoadSegment, POI

class MockVehicle:
    def __init__(self, vid, lat, lon, speed, bearing):
        self.id = vid
        self.lat = lat
        self.lon = lon
        self.speed_mps = speed
        self.bearing = bearing
        self.timestamp = datetime.now().timestamp()
        self.road_segment_id = 1
        self.wrong_way = False
        self.anomaly_score = 0.0
        self.state = "normal"
        self._init_history()

    def _init_history(self):
        self.speed_history = []
        self.bearing_history = []
        self.position_history = []
        self.acceleration_history = []
        self.history_timestamps = []

class ScenarioValidator:
    def __init__(self):
        self.app = create_app()
        self.intel = LiveTrafficIntelligence()
        self.road = RoadSegment(id=1, bearing=0.0, road_class="primary") # Northbound
        self.pois = []

    def generate_scenario(self, type: str, duration_sec: int = 40, tick_hz: int = 2) -> List[MockVehicle]:
        steps = duration_sec * tick_hz
        dt = 1.0 / tick_hz
        history = []
        
        # Base state
        lat, lon = 12.97, 77.59
        speed = 10.0
        bearing = 0.0 # Legal North
        
        for i in range(steps):
            now = i * dt
            current_wrong_way = False
            
            if type == "GPS_JUMP":
                if steps // 2 <= i <= steps // 2 + 2:
                    lat += 0.0005 # ~50m jump
            
            elif type == "U_TURN":
                if steps // 3 <= i <= steps // 3 + 10:
                    bearing = (bearing + 18.0) % 360 # Smooth 180 turn over 5 seconds
            
            elif type == "WRONG_WAY_SUDDEN":
                if i >= steps // 2:
                    bearing = 180.0
                    current_wrong_way = True
            
            elif type == "WRONG_WAY_GRADUAL":
                if i >= steps // 2:
                    # Slowly rotate to 180
                    target = 180.0
                    bearing = (bearing + 5.0) if bearing < 180 else 180
                    if i > steps // 2 + 10:
                        current_wrong_way = True

            elif type == "STOP_GO":
                speed = 10.0 + 8.0 * np.sin(i * 0.5) # Rapid oscillation
            
            # Update lat/lon based on speed/bearing
            rad = np.radians(bearing)
            lat += (speed * dt * np.cos(rad)) / 111000.0
            lon += (speed * dt * np.sin(rad)) / (111000.0 * np.cos(np.radians(lat)))
            
            v = MockVehicle(vid=101, lat=lat, lon=lon, speed=speed, bearing=bearing)
            v.wrong_way = current_wrong_way
            
            # Populate history (simulating real-time accumulation)
            for prev in history:
                v.speed_history.append(prev.speed_mps)
                v.bearing_history.append(prev.bearing)
                v.history_timestamps.append(prev.timestamp)
                v.position_history.append((prev.lat, prev.lon))
            
            # Add some dummy acceleration
            if len(v.speed_history) > 1:
                v.acceleration_history = [0.0] * len(v.speed_history)
            
            history.append(v)
            
        return history

    def run_tests(self):
        scenarios = ["GPS_JUMP", "U_TURN", "WRONG_WAY_SUDDEN", "WRONG_WAY_GRADUAL", "STOP_GO"]
        results = {}

        with self.app.app_context():
            print("\n" + "="*60)
            print("ML BEHAVIORAL VALIDATION HARNESS")
            print("="*60)
            
            for s_type in scenarios:
                print(f"\nRunning Scenario: {s_type}...")
                history = self.generate_scenario(s_type)
                
                tp, fp, tn, fn = 0, 0, 0, 0
                
                for i, v in enumerate(history):
                    # In our harness, 'v' already has history populated
                    scores = self.intel.compute_scores(v, [], self.road, self.pois, v.timestamp)
                    
                    conf = scores.get("confidence_adjustment", 0.0)
                    threshold = self.intel._model_data.get("threshold", 0.5)
                    predicted = conf > threshold or v.state == "wrong_way"
                    actual = v.wrong_way
                    
                    if actual and i % 10 == 0:
                        print(f"    [Step {i}] Actual=True, Predicted={predicted}, Conf={conf:.4f}, Threshold={threshold:.2f}")

                    if predicted and actual: tp += 1
                    elif predicted and not actual: fp += 1
                    elif not predicted and not actual: tn += 1
                    elif not predicted and actual: fn += 1
                
                results[s_type] = {"tp": tp, "fp": fp, "tn": tn, "fn": fn}
                
                prec = tp / (tp + fp) if (tp + fp) > 0 else 1.0
                rec = tp / (tp + fn) if (tp + fn) > 0 else 1.0
                
                print(f"  > Results: Precision={prec:.2f}, Recall={rec:.2f}")
                print(f"  > Confusion: TP={tp}, FP={fp}, TN={tn}, FN={fn}")

        print("\n" + "="*60)
        print("FINAL VALIDATION SUMMARY")
        print("="*60)
        for s, r in results.items():
            status = "PASS" if r["fp"] == 0 and (r["tp"] > 0 or s in ["GPS_JUMP", "U_TURN", "STOP_GO"]) else "REVIEW"
            print(f"{s:<20} | {status} | FP: {r['fp']} | TP: {r['tp']}")

if __name__ == "__main__":
    validator = ScenarioValidator()
    validator.run_tests()
