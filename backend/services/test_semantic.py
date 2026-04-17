import copy
from semantic_reasoning import run_semantic

def run_tests():
    print("=== TEST 1 & 2: TEMPORAL BEHAVIOR & MULTIPLE FRAMES ===")
    vehicles = [
        # Vehicle 1: Will eventually become wrong_way
        {"id": 1, "x": 0, "y": 0, "vx": -5, "vy": 0, "wrong_way_flag": True, "angle_dev": 180},
        # Vehicle 2: Should always be normal
        {"id": 2, "x": 0, "y": 0, "vx": 5, "vy": 0, "wrong_way_flag": False, "angle_dev": 10},
        # Vehicle 3: Should trigger risky
        {"id": 3, "x": 0, "y": 0, "vx": 12, "vy": 5, "wrong_way_flag": False, "angle_dev": 60}
    ]

    for step in range(1, 11):
        print(f"\nStep {step}")
        vehicles = run_semantic(vehicles, dt=0.5)
        for v in vehicles:
            print(f"ID:{v['id']} | dev:{v['deviation_time']:.2f} | angle:{v['angle_dev']} | class:{v['class']}")

    print("\n=== TEST 3: EDGE CASES ===")
    edge_vehicles = [
        # Stationary
        {"id": "Edge1-Stationary", "x": 0, "y": 0, "vx": 0, "vy": 0, "wrong_way_flag": True, "angle_dev": 180},
        # Low-speed wrong-way
        {"id": "Edge2-LowSpeed", "x": 0, "y": 0, "vx": -1, "vy": 0, "wrong_way_flag": True, "angle_dev": 180, "deviation_time": 4.0},
        # Noise spike (one frame)
        {"id": "Edge3-Noise", "x": 0, "y": 0, "vx": -10, "vy": 0, "wrong_way_flag": True, "angle_dev": 180, "deviation_time": 0.0}
    ]
    
    res = run_semantic(copy.deepcopy(edge_vehicles), dt=0.5)
    for v in res:
        print(f"ID:{v['id']} -> {v['class']} ({v.get('reason','')})")

if __name__ == "__main__":
    run_tests()
