from __future__ import annotations

import json
import math
import time
from pathlib import Path

import requests


API_BASE_URL = "http://127.0.0.1:5000"
VEHICLES_ENDPOINT = f"{API_BASE_URL}/api/vehicles"
ROADS_ENDPOINT = f"{API_BASE_URL}/api/roads"
OUTPUT_PATH = "world_state.json"
POLL_INTERVAL_SECONDS = 1.0


def load_input(api_base_url=API_BASE_URL):
    """Fetch a live vehicle and road snapshot from the Flask API."""
    vehicles_url = f"{api_base_url.rstrip('/')}/api/vehicles"
    roads_url = f"{api_base_url.rstrip('/')}/api/roads"

    vehicles_response = requests.get(vehicles_url, timeout=5)
    roads_response = requests.get(roads_url, timeout=5)
    vehicles_response.raise_for_status()
    roads_response.raise_for_status()

    return {
        "timestamp": time.time(),
        "vehicles": vehicles_response.json(),
        "roads": roads_response.json(),
    }


def build_world_model(data):
    """Build a compact world state from vehicle JSON and optional road JSON."""
    if isinstance(data, list):
        raw_vehicles = data
        raw_roads = []
        timestamp = None
    else:
        raw_vehicles = data.get("vehicles", data.get("vehicle_updates", []))
        raw_roads = data.get("roads", data.get("road_segments", []))
        timestamp = data.get("timestamp")

    if timestamp is None:
        timestamps = [
            float(vehicle["timestamp"])
            for vehicle in raw_vehicles
            if vehicle.get("timestamp") is not None
        ]
        timestamp = max(timestamps) if timestamps else time.time()

    origin_lat = None
    origin_lon = None
    for vehicle in raw_vehicles:
        if vehicle.get("lat") is not None and vehicle.get("lon") is not None:
            origin_lat = float(vehicle["lat"])
            origin_lon = float(vehicle["lon"])
            break

    def lat_lon_to_xy(lat, lon):
        if origin_lat is None or origin_lon is None:
            return None, None
        meters_per_lat = 111_320.0
        meters_per_lon = meters_per_lat * math.cos(math.radians(origin_lat))
        x = (float(lon) - origin_lon) * meters_per_lon
        y = (float(lat) - origin_lat) * meters_per_lat
        return round(x, 2), round(y, 2)

    roads_by_id = {
        str(road.get("id")): road for road in raw_roads if road.get("id") is not None
    }
    vehicles = {}

    for index, raw_vehicle in enumerate(raw_vehicles):
        vehicle_id = str(raw_vehicle.get("id", index))
        lat = raw_vehicle.get("lat")
        lon = raw_vehicle.get("lon")

        if raw_vehicle.get("x") is not None and raw_vehicle.get("y") is not None:
            x = float(raw_vehicle["x"])
            y = float(raw_vehicle["y"])
        else:
            x, y = (
                lat_lon_to_xy(lat, lon)
                if lat is not None and lon is not None
                else (None, None)
            )

        road_id = raw_vehicle.get("road_segment_id", raw_vehicle.get("road_id"))
        lane = raw_vehicle.get("lane", raw_vehicle.get("lane_id", road_id))
        longitudinal = raw_vehicle.get("progress_m", raw_vehicle.get("s", x))
        road = roads_by_id.get(str(road_id))

        vehicles[vehicle_id] = {
            "id": vehicle_id,
            "x": x,
            "y": y,
            "lat": lat,
            "lon": lon,
            "speed": raw_vehicle.get("speed", raw_vehicle.get("speed_mps", 0.0)),
            "bearing": raw_vehicle.get("bearing"),
            "lane": str(lane) if lane is not None else "unknown",
            "road_segment_id": road_id,
            "road_bearing": road.get("bearing") if road else None,
            "oneway": road.get("oneway") if road else None,
            "s": float(longitudinal) if longitudinal is not None else None,
            "timestamp": raw_vehicle.get("timestamp", timestamp),
        }

    neighbor_state = compute_neighbors(vehicles)
    lane_occupancy = compute_lane_occupancy(vehicles)

    for vehicle_id, neighbor_info in neighbor_state.items():
        vehicles[vehicle_id].update(neighbor_info)

    total_road_length_m = 0.0
    for road in raw_roads:
        length = road.get("length", road.get("length_m", 0.0))
        total_road_length_m += float(length or 0.0)

    lane_count = max(len(lane_occupancy), 1)
    if total_road_length_m > 0:
        density = len(vehicles) / (total_road_length_m / 1000.0)
    else:
        density = len(vehicles) / lane_count

    return {
        "timestamp": timestamp,
        "vehicles": vehicles,
        "lanes": lane_occupancy,
        "scene": {
            "vehicle_count": len(vehicles),
            "lane_count": len(lane_occupancy),
            "road_count": len(raw_roads),
            "density": round(density, 3),
            "density_units": (
                "vehicles_per_km" if total_road_length_m > 0 else "vehicles_per_lane"
            ),
        },
    }


def compute_neighbors(vehicles):
    """Compute nearest, front, and rear neighbors for each vehicle."""
    vehicle_list = (
        list(vehicles.values()) if isinstance(vehicles, dict) else list(vehicles)
    )
    result = {}

    for vehicle in vehicle_list:
        vehicle_id = str(vehicle.get("id"))
        x = vehicle.get("x")
        y = vehicle.get("y")
        nearest_id = None
        nearest_distance = None

        if x is not None and y is not None:
            for other in vehicle_list:
                other_id = str(other.get("id"))
                if (
                    other_id == vehicle_id
                    or other.get("x") is None
                    or other.get("y") is None
                ):
                    continue
                distance = math.hypot(
                    float(other["x"]) - float(x),
                    float(other["y"]) - float(y),
                )
                if nearest_distance is None or distance < nearest_distance:
                    nearest_id = other_id
                    nearest_distance = distance

        same_lane = [
            other
            for other in vehicle_list
            if str(other.get("id")) != vehicle_id
            and other.get("lane") == vehicle.get("lane")
            and other.get("s") is not None
            and vehicle.get("s") is not None
        ]
        front_vehicle = None
        rear_vehicle = None
        front_gap = None
        rear_gap = None
        current_s = vehicle.get("s")

        if current_s is not None:
            for other in same_lane:
                gap = float(other["s"]) - float(current_s)
                if gap >= 0 and (front_gap is None or gap < front_gap):
                    front_vehicle = str(other.get("id"))
                    front_gap = gap
                if gap < 0 and (rear_gap is None or abs(gap) < rear_gap):
                    rear_vehicle = str(other.get("id"))
                    rear_gap = abs(gap)

        result[vehicle_id] = {
            "nearest_vehicle": nearest_id,
            "nearest_distance": (
                round(nearest_distance, 2) if nearest_distance is not None else None
            ),
            "front_vehicle": front_vehicle,
            "front_gap": round(front_gap, 2) if front_gap is not None else None,
            "rear_vehicle": rear_vehicle,
            "rear_gap": round(rear_gap, 2) if rear_gap is not None else None,
        }

    return result


def compute_lane_occupancy(vehicles):
    """Count vehicles per lane or road segment."""
    vehicle_list = (
        list(vehicles.values()) if isinstance(vehicles, dict) else list(vehicles)
    )
    occupancy = {}

    for vehicle in vehicle_list:
        lane = str(vehicle.get("lane", "unknown"))
        if lane not in occupancy:
            occupancy[lane] = {
                "vehicle_count": 0,
                "vehicle_ids": [],
                "average_speed": 0.0,
            }
        occupancy[lane]["vehicle_count"] += 1
        occupancy[lane]["vehicle_ids"].append(str(vehicle.get("id")))
        occupancy[lane]["average_speed"] += float(vehicle.get("speed") or 0.0)

    for lane_state in occupancy.values():
        count = max(lane_state["vehicle_count"], 1)
        lane_state["average_speed"] = round(lane_state["average_speed"] / count, 2)

    return occupancy


def export_world_state(world_state, output_path):
    """Write world state JSON to disk."""
    with Path(output_path).open("w", encoding="utf-8") as file:
        json.dump(world_state, file, indent=2)
    return output_path


def main():
    """Continuously fetch live data, build the world model, and export it."""
    print("World model runner started. Press Ctrl+C to stop.")

    while True:
        try:
            data = load_input()
            world_state = build_world_model(data)
            export_world_state(world_state, OUTPUT_PATH)

            scene = world_state["scene"]
            print(
                "timestamp={timestamp:.3f} vehicle_count={vehicle_count} density={density}".format(
                    timestamp=float(world_state["timestamp"]),
                    vehicle_count=scene["vehicle_count"],
                    density=scene["density"],
                )
            )
        except requests.RequestException as exc:
            print(f"API fetch failed: {exc}")
        except Exception as exc:
            print(f"World model update failed: {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
