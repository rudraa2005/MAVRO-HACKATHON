from typing import List

def decision_layer(vehicles: List[dict]) -> List[dict]:
    """
    Final decision layer for alert-ready vehicle objects.

    Inputs:
    - semantic class (optional metadata passthrough)
    - risk_level
    - ttc

    Output:
    - alert in {"SAFE", "WARNING", "HIGH_ALERT", "COLLISION_ALERT"}
    """
    for v in vehicles:
        v["alert"] = "SAFE"

        ttc = v.get("ttc")
        ttc_value = float(ttc) if ttc is not None else None
        risk_level = str(v.get("risk_level", "low") or "low").lower()

        if ttc_value is not None and ttc_value < 2.0:
            v["alert"] = "COLLISION_ALERT"
        elif risk_level == "high":
            v["alert"] = "HIGH_ALERT"
        elif risk_level == "medium":
            v["alert"] = "WARNING"

    return vehicles

def run_decision(vehicles: List[dict]) -> List[dict]:
    """Safe pipeline wrapper for the decision engine"""
    try:
        return decision_layer(vehicles)
    except Exception as e:
        print("[Decision ERROR]", e)
        return vehicles

# For Debugging 
def debug_print_pipeline(vehicles: List[dict]):
    """Utility function to instantly show the pipeline state"""
    print("="*60)
    for v in vehicles:
        print(
            f"ID:{v.get('id', 'N/A')} | "
            f"class:{v.get('class', v.get('semantic_class', 'missing'))} | "
            f"risk_level:{v.get('risk_level', 'missing')} | "
            f"ttc:{round(v.get('ttc', 999), 2) if v.get('ttc') is not None else 'None'} | "
            f"alert:{v.get('alert', 'missing')}"
        )
    print("="*60)
