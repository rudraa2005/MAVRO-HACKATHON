import math
from typing import List

# Default speed threshold (m/s). ~28 km/h
SPEED_THRESHOLD = 8.0 

def semantic_reasoning(vehicles: List[dict], dt: float) -> List[dict]:
    """
    Classifies vehicle behavior into logical states: 'normal', 'u_turn', 'wrong_way', 'risky'.
    Updates each vehicle dictionary in-place.
    """
    for vehicle in vehicles:
        # Check for required fields for safety
        if "wrong_way_flag" not in vehicle or "angle_dev" not in vehicle:
            # Fallback for silent failure prevention
            pass
            
        # 1. Maintain temporal state
        if "deviation_time" not in vehicle:
            vehicle["deviation_time"] = 0.0

        # Smooth deviation
        if vehicle.get("wrong_way_flag", False):
            vehicle["deviation_time"] += dt
        else:
            vehicle["deviation_time"] = max(0.0, vehicle["deviation_time"] - dt)

        dev_time = vehicle["deviation_time"]
        
        # 2. Compute speed
        vx = vehicle.get("vx", 0.0)
        vy = vehicle.get("vy", 0.0)
        speed = math.sqrt(vx**2 + vy**2)
        
        # Normalize angle (Handle Radians to Degrees)
        # Note: We check if it is within a small limit like 2*pi, but for safety math.pi is fine
        angle_dev = vehicle.get("angle_dev", 0.0)
        if angle_dev <= math.pi and angle_dev > 0:
            angle_dev = math.degrees(angle_dev)
            
        # 9. Edge Cases (Zero velocity)
        if speed == 0.0:
            vehicle["class"] = "normal"
            vehicle["confidence"] = 1.0
            vehicle["reason"] = "Vehicle is stationary"
            vehicle["is_high_risk"] = False
            vehicle["severity"] = "low"
            # Optional debug:
            # print(f"[Semantic] ID:{vehicle.get('id', 'Unknown')} → normal (Vehicle is stationary)")
            continue
        
        # 3. Classification Rules (CORE LOGIC)
        classification = "normal"
        reason = "Normal driving behavior"
        
        if angle_dev > 150 and dev_time > 3:
            classification = "wrong_way"
            reason = f"Sustained opposite direction > 3s (dev: {dev_time:.1f}s)"
        elif angle_dev > 150 and dev_time <= 2:
            classification = "u_turn"
            reason = "High angle deviation but short duration; possible U-turn"
        elif speed > SPEED_THRESHOLD and angle_dev > 30:
            classification = "risky"
            reason = "High speed combined with significant angular deviation"
        elif angle_dev > 150 and 2 < dev_time <= 3:
            # Handling the 2s ~ 3s gap
            classification = "risky"
            reason = "Transitioning to wrong way behavior"

        # Low-speed wrong-way filter
        if classification == "wrong_way" and speed < 2:
            classification = "risky"
            reason = "Low-speed reverse movement (not critical)"

        # 5. Confidence Calculation
        if classification == "wrong_way":
            confidence = min(1.0, (dev_time / 5.0) * (max(0.1, speed) / SPEED_THRESHOLD))
        elif classification == "u_turn":
            # high confidence initially, slowly drops off toward risky/wrong_way
            confidence = max(0.1, min(1.0, 1.0 - (dev_time / 3.0)))
        elif classification == "risky":
            confidence = 0.8
        else:
            # Normal
            confidence = 1.0
            
        # 7. Add Severity
        if classification == "wrong_way":
            severity = "high"
        elif classification == "risky":
            severity = "medium"
        else:
            severity = "low"
            
        # Write formatted output (Updates in-place)
        vehicle["class"] = classification
        vehicle["confidence"] = round(max(0.0, min(1.0, confidence)), 2) # Ensure within [0,1]
        vehicle["reason"] = reason
        vehicle["severity"] = severity
        
        # 🔥 Bonus: High Risk identifier
        vehicle["is_high_risk"] = True if classification == "wrong_way" else False

        # 8. Debug print
        print(f"[Semantic] ID:{vehicle.get('id', 'Unknown')} -> {classification} ({reason})")

    return vehicles

def run_semantic(vehicles: List[dict], dt: float) -> List[dict]:
    """
    Integration-safe wrapper for semantic reasoning.
    Usage: vehicles = run_semantic(vehicles, dt=0.5)
    """
    try:
        return semantic_reasoning(vehicles, dt)
    except Exception as e:
        print("[Semantic ERROR]", e)
        return vehicles
