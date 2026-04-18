
import csv
import os
import time
from datetime import datetime

class DatasetLogger:
    def __init__(self, path="backend/data/eval_dataset.csv"):
        self.path = path
        self.header = [
            "vehicle_id", "timestamp", "speed", "bearing", "dev_angle",
            "anomaly_score", "wrong_way_prob", "risk_score",
            "gps_quality", "intent", "label"
        ]
        self._ensure_file()

    def _ensure_file(self):
        # Ensure directory exists
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        
        # Write header if new file or empty
        if not os.path.exists(self.path) or os.stat(self.path).st_size == 0:
            with open(self.path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.header)

    def log(self, vehicle_id, timestamp, speed, bearing, dev_angle, 
            anomaly_score, wrong_way_prob, risk_score, 
            gps_quality, intent, label):
        """Append a single row to the evaluation dataset."""
        row = [
            vehicle_id,
            timestamp,
            round(speed, 2),
            round(bearing, 1),
            round(dev_angle, 1),
            round(anomaly_score, 3),
            round(wrong_way_prob, 3),
            round(risk_score, 3),
            round(gps_quality, 3),
            intent,
            1 if label else 0
        ]
        
        with open(self.path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(row)

    def log_frame(self, vehicles_data):
        """Log multiple vehicles from a simulation frame."""
        for v in vehicles_data:
            # v is a dict or object with the necessary fields
            # For the simulation engine integration, we expect a dict-like or object
            # that we mapped in engine.py
            self.log(
                vehicle_id=v.get("id"),
                timestamp=v.get("timestamp"),
                speed=v.get("speed"),
                bearing=v.get("bearing", 0.0),
                dev_angle=v.get("angle_diff", 0.0),
                anomaly_score=v.get("anomaly", 0.0),
                wrong_way_prob=v.get("wwp", 0.0),
                risk_score=v.get("risk", 0.0),
                gps_quality=v.get("gps_quality", 1.0),
                intent=v.get("intent", "UNKNOWN"),
                label=v.get("ground_truth_wrong_way", False)
            )

# Singleton instance
eval_dataset_logger = DatasetLogger()
