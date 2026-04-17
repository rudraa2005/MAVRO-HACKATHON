import math
from typing import List

def escalate_risk(current_risk: str) -> str:
    """Helper to escalate risk by one level for semantic violations"""
    if current_risk == "safe":
        return "risky"
    elif current_risk == "risky":
        return "danger"
    return "danger" # maxes out at danger

def compute_spatial(vehicles: List[dict]) -> List[dict]:
    """
    Computes pairwise TTC between vehicles and determines collision risk.
    O(N^2) complexity with unique pairs (i, j where j > i).
    Updates spatial awareness attributes in-place.
    """
    # 1. Initialize fields
    for vehicle in vehicles:
        vehicle["ttc"] = float("inf")
        vehicle["risk"] = "safe"
        vehicle["collision_with"] = None

    n = len(vehicles)
    
    # 2. Pairwise vehicle comparison
    for i in range(n):
        A = vehicles[i]
        
        for j in range(i + 1, n):
            B = vehicles[j]

            # 3. Relative motion computation
            dx = B.get("x", 0.0) - A.get("x", 0.0)
            dy = B.get("y", 0.0) - A.get("y", 0.0)

            # 6. Distance filtering (Run prior to velocity calculation for performance)
            distance = math.sqrt(dx**2 + dy**2)
            if distance > 50:
                continue

            dvx = B.get("vx", 0.0) - A.get("vx", 0.0)
            dvy = B.get("vy", 0.0) - A.get("vy", 0.0)

            # 4. Check if vehicles are approaching
            dot = dx * dvx + dy * dvy
            if dot >= 0:
                continue # moving apart

            # 5. Compute TTC
            rel_speed_sq = dvx**2 + dvy**2
            if rel_speed_sq == 0:
                continue

            ttc = -dot / rel_speed_sq

            # 7. Risk classification
            if ttc < 2:
                risk = "danger"
            elif ttc < 5:
                risk = "risky"
            else:
                risk = "safe"

            # 8. Update both vehicles
            if ttc < A["ttc"]:
                A["ttc"] = round(ttc, 2)
                A["risk"] = risk
                A["collision_with"] = B.get("id")

            if ttc < B["ttc"]:
                B["ttc"] = round(ttc, 2)
                B["risk"] = risk
                B["collision_with"] = A.get("id")

    # Step 9 & Bonus applied to each vehicle independently
    for vehicle in vehicles:
        # 9. Semantic-aware adjustment
        if vehicle.get("class") == "wrong_way":
            vehicle["risk"] = escalate_risk(vehicle["risk"])

        # 🔥 Bonus: Danger Zone Radius
        current_ttc = vehicle["ttc"]
        if current_ttc != float('inf'):
            radius = max(5.0, 20.0 - current_ttc)
            vehicle["danger_zone_radius"] = round(radius, 1)
        else:
            vehicle["danger_zone_radius"] = 5.0 # Base minimum idle radius

    return vehicles

def run_spatial(vehicles: List[dict]) -> List[dict]:
    """Safe pipeline wrapper for TTC spatial tracking"""
    try:
        return compute_spatial(vehicles)
    except Exception as e:
        print("[Spatial ERROR]", e)
        return vehicles
