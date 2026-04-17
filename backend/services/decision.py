from typing import List

def decision_layer(vehicles: List[dict]) -> List[dict]:
    """
    Final decision layer. Formulates unified alerts based on the
    synthesized outputs from detection, semantic reasoning, and spatial modules.
    """
    for v in vehicles:
        # Default safety state
        v["alert"] = "SAFE"
        
        # Pull required logic fields safely with defaults
        v_class = v.get("class", "normal")
        v_ttc = v.get("ttc", float("inf"))
        v_risk = v.get("risk", "safe")
        
        # Hierarchical Alert Logic
        if v_class == "wrong_way" and v_ttc < 3:
            v["alert"] = "COLLISION_ALERT"
            
        elif v_class == "wrong_way":
            v["alert"] = "HIGH_ALERT"
            
        elif v_risk == "danger":
            v["alert"] = "COLLISION_WARNING"
            
        elif v_risk == "risky":
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
            f"class:{v.get('class', 'missing')} | "
            f"risk:{v.get('risk', 'missing')} | "
            f"ttc:{round(v.get('ttc', 999), 2)} | "
            f"alert:{v.get('alert', 'missing')}"
        )
    print("="*60)
