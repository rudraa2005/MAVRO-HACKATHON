from semantic_reasoning import run_semantic
from compute_spatial import run_spatial
from decision import run_decision, debug_print_pipeline

# 🔹 Mock detect()
def detect(vehicles):
    for v in vehicles:
        # simple logic: negative vx = wrong way
        v["wrong_way_flag"] = v["vx"] < 0
        v["angle_dev"] = 180 if v["vx"] < 0 else 10
    return vehicles

def run_tests():
    # 🔹 TEST VEHICLES (VERY IMPORTANT)
    vehicles = [
        # 🚨 Wrong way fast vehicle
        {
            "id": 1,
            "x": 0, "y": 0,
            "vx": -10, "vy": 0
        },

        # 🚗 Normal vehicle
        {
            "id": 2,
            "x": 20, "y": 0,
            "vx": 5, "vy": 0
        },

        # ⚠️ Risky angled fast vehicle
        {
            "id": 3,
            "x": 10, "y": 5,
            "vx": 8, "vy": 6
        }
    ]

    # 🔁 Run multiple frames (simulate time)
    for step in range(1, 11):
        print(f"\n==================== STEP {step} ====================")
        
        # NOTE: Update positions based on velocity to simulate realistic movement
        for v in vehicles:
            v["x"] += v["vx"] * 0.5
            v["y"] += v["vy"] * 0.5

        vehicles = detect(vehicles)
        vehicles = run_semantic(vehicles, dt=0.5)
        vehicles = run_spatial(vehicles)
        vehicles = run_decision(vehicles)

        debug_print_pipeline(vehicles)
        
        # ✅ STEP 5: ADD ASSERTIONS (PRO LEVEL)
        for v in vehicles:
            assert "class" in v, f"Missing 'class' in vehicle {v['id']}"
            assert "risk" in v, f"Missing 'risk' in vehicle {v['id']}"
            assert "ttc" in v, f"Missing 'ttc' in vehicle {v['id']}"
            assert "alert" in v, f"Missing 'alert' in vehicle {v['id']}"

if __name__ == "__main__":
    run_tests()
