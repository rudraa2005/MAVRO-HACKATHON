import time
import requests
import json

BASE_URL = "http://localhost:5000"

def test_demo_loop(cycle=1):
    print(f"\n--- DEMO CYCLE {cycle} ---")
    
    # 1. Reset/Start simulation
    print("Resetting simulation...")
    try:
        requests.post(f"{BASE_URL}/api/admin/simulation/reset")
    except Exception as e:
        print(f"Reset failed: {e}")

    time.sleep(2.0)

    # 2. Trigger Wrong-Way Demo
    print("Triggering Wrong-Way Demo...")
    try:
        # Pass empty JSON to trigger default
        resp = requests.post(f"{BASE_URL}/api/admin/scenarios/wrong-way", json={})
        if resp.status_code != 200:
            print(f"Trigger failed with status {resp.status_code}: {resp.text}")
            return False
            
        data = resp.json()
        vid = data.get("vehicle_id")
        print(f"Demo triggered for Vehicle {vid}")
    except Exception as e:
        print(f"Trigger failed: {e}")
        return False

    # 3. Wait and check detection
    print("Monitoring for detection...")
    detections = 0
    start_time = time.time()
    while time.time() - start_time < 20:  # 20s timeout
        time.sleep(1.0)
        try:
            # Check analytics for detection
            resp = requests.get(f"{BASE_URL}/api/analytics")
            if resp.status_code != 200:
                continue
                
            snap = resp.json()
            if snap and len(snap) > 0:
                latest_tick = snap[-1]
                vehicles = latest_tick.get("vehicles", [])
                for v in vehicles:
                    if v.get("db_id") == vid:
                        wwp = v.get("wwp", 0.0)
                        state = v.get("wrong_way", False)
                        print(f"  T+{int(time.time()-start_time)}s: WWP={wwp:.3f} | State={state}")
                        if state or wwp > 0.5:
                            detections += 1
                
                if detections >= 3: # Need at least 3 detections to confirm stability
                    break
        except Exception as e:
            print(f"Poll error: {e}")
    
    if detections > 0:
        print(f"[SUCCESS] Vehicle {vid} detected in {detections} frames.")
        return True
    else:
        print(f"[FAIL] Vehicle {vid} NOT detected.")
        return False

if __name__ == "__main__":
    successes = 0
    for i in range(1, 6):
        if test_demo_loop(i):
            successes += 1
        else:
            print("Retry after delay...")
        time.sleep(3.0)
    
    print(f"\n============================================================")
    print(f"FINAL REHEARSAL RESULT: {successes}/5 SUCCESSFUL CYCLES")
    print(f"============================================================")
