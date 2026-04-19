import sys
import os
sys.path.append(os.getcwd())
from backend.services.ml_intelligence import apply_anomaly_detection
import pandas as pd

def test_rule_fallback():
    print("Testing Rule-Based Fallback Detection...")
    
    # Simulate a wrong-way vehicle (High bearing deviation, sustained duration)
    # But we won't run the ML model.
    mock_vehicles = [{
        "vehicle_id": 999,
        "speed": 12.0,
        "bearing": 180.0,
        "angle_diff": 175.0, # High deviation
        "sustained_duration_s": 5.0, # > 4s cutoff
        "ttc": 1.5,
        "acceleration": 0.0,
        "gps_stability": "HIGH"
    }]
    
    # Run the anomaly detection (Heuristic-based)
    results = apply_anomaly_detection(mock_vehicles)
    v = results[0]
    
    print(f"Anomaly Score: {v['anomaly_score']}")
    print(f"Is Anomalous: {v['is_anomalous']}")
    
    assert v['is_anomalous'] == True, "Fallback failed! Vehicle should be anomalous."
    assert v['anomaly_score'] > 0.58, "Fallback failed! Score should be above 0.58."
    
    print("[SUCCESS] Rule-based fallback is active and correctly flags high-risk behavioral patterns.")

if __name__ == "__main__":
    test_rule_fallback()
