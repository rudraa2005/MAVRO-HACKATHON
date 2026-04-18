from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

from backend.models import POI, RoadSegment, Vehicle, VehicleHistory
from backend.services.geo import (
    cumulative_path_lengths,
    haversine_distance_m,
    interpolate_path_position,
    move_coordinate,
)
from backend.simulation.engine import simulation_engine


@dataclass(slots=True)
class HumanAgent:
    id: str
    lat: float
    lon: float
    speed_mps: float
    bearing: float
    intent: str
    nearest_road_segment_id: int | None
    risk_zone_m: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lat": self.lat,
            "lon": self.lon,
            "speed": self.speed_mps,
            "bearing": self.bearing,
            "intent": self.intent,
            "nearest_road_segment_id": self.nearest_road_segment_id,
            "risk_zone_m": self.risk_zone_m,
        }


class LiveTrafficIntelligence:
    def build_snapshot(self, selected_vehicle_id: int | None = None) -> dict:
        vehicles = Vehicle.query.order_by(Vehicle.id).all()
        roads = RoadSegment.query.order_by(RoadSegment.id).all()
        pois = POI.query.order_by(POI.id).all()
        now = max((vehicle.timestamp for vehicle in vehicles), default=0.0)

        humans = self._build_humans(roads, pois, now)
        selected_vehicle = self._select_vehicle(vehicles, selected_vehicle_id)
        vehicle_predictions = {
            vehicle.id: simulation_engine.predict_vehicle_path(vehicle)
            for vehicle in vehicles
        }
        human_predictions = {
            human.id: self._predict_human_path(human) for human in humans
        }
        collisions = self._predict_collisions(
            vehicles,
            humans,
            vehicle_predictions,
            human_predictions,
            selected_vehicle.id if selected_vehicle else None,
        )
        heatmap = self._build_heatmap(roads, vehicles, humans, collisions)

        return {
            "selected_vehicle_id": selected_vehicle.id if selected_vehicle else None,
            "humans": [human.to_dict() for human in humans],
            "heatmap": heatmap,
            "collision_predictions": collisions,
            "selected_vehicle": (
                self._selected_vehicle_insights(
                    selected_vehicle,
                    vehicle_predictions.get(selected_vehicle.id, []),
                    collisions,
                    heatmap,
                )
                if selected_vehicle
                else None
            ),
        }

    def _select_vehicle(
        self,
        vehicles: list[Vehicle],
        selected_vehicle_id: int | None,
    ) -> Vehicle | None:
        if selected_vehicle_id is not None:
            for vehicle in vehicles:
                if vehicle.id == selected_vehicle_id:
                    return vehicle
        for vehicle in vehicles:
            if vehicle.wrong_way:
                return vehicle
        return vehicles[0] if vehicles else None

    def _build_humans(
        self,
        roads: list[RoadSegment],
        pois: list[POI],
        now: float,
    ) -> list[HumanAgent]:
        roads_by_id = {road.id: road for road in roads}
        anchors = [
            poi
            for poi in pois
            if poi.nearest_road_segment_id is not None
            and poi.poi_type in {"intersection", "signal", "shop", "parking"}
        ]
        anchors.sort(
            key=lambda poi: (
                {"signal": 0, "intersection": 1, "shop": 2, "parking": 3}.get(
                    poi.poi_type,
                    9,
                ),
                poi.id,
            )
        )

        humans: list[HumanAgent] = []
        for poi in anchors[:18]:
            road = roads_by_id.get(poi.nearest_road_segment_id)
            if road is None:
                continue

            crossing = poi.poi_type in {"signal", "intersection"}
            phase = (now / (5.0 if crossing else 8.0)) + (poi.id % 7)
            offset_m = math.sin(phase) * (8.0 if crossing else 15.0)
            bearing = (road.bearing + (90.0 if crossing else 0.0)) % 360.0
            lat, lon = move_coordinate(poi.lat, poi.lon, offset_m, bearing)
            intent = (
                "crossing at signal"
                if poi.poi_type == "signal"
                else "crossing at intersection"
                if poi.poi_type == "intersection"
                else "walking near storefront"
                if poi.poi_type == "shop"
                else "moving from parked vehicle"
            )
            speed = 1.35 if crossing else 1.05
            humans.append(
                HumanAgent(
                    id=f"h-{poi.id}",
                    lat=lat,
                    lon=lon,
                    speed_mps=speed,
                    bearing=bearing,
                    intent=intent,
                    nearest_road_segment_id=road.id,
                    risk_zone_m=10.0 if crossing else 7.0,
                )
            )

        if humans or not roads:
            return humans

        for road in roads[: min(10, len(roads))]:
            midpoint = self._road_point(road, road.length_m * 0.5)
            offset_m = math.sin((now / 6.0) + road.id) * 7.0
            bearing = (road.bearing + 90.0) % 360.0
            lat, lon = move_coordinate(midpoint["lat"], midpoint["lon"], offset_m, bearing)
            humans.append(
                HumanAgent(
                    id=f"h-road-{road.id}",
                    lat=lat,
                    lon=lon,
                    speed_mps=1.2,
                    bearing=bearing,
                    intent="crossing mid-block",
                    nearest_road_segment_id=road.id,
                    risk_zone_m=9.0,
                )
            )
        return humans

    def _predict_human_path(self, human: HumanAgent) -> list[dict]:
        points = []
        for index in range(15):
            t = float(index)
            lat, lon = move_coordinate(
                human.lat,
                human.lon,
                human.speed_mps * t,
                human.bearing,
            )
            points.append({"t": t, "lat": lat, "lon": lon, "speed": human.speed_mps})
        return points

    def _predict_collisions(
        self,
        vehicles: list[Vehicle],
        humans: list[HumanAgent],
        vehicle_predictions: dict[int, list[dict]],
        human_predictions: dict[str, list[dict]],
        selected_vehicle_id: int | None,
    ) -> list[dict]:
        vehicle_by_id = {vehicle.id: vehicle for vehicle in vehicles}
        risks: list[dict] = []

        for index, vehicle in enumerate(vehicles):
            for other in vehicles[index + 1 :]:
                risk = self._closest_approach(
                    vehicle_predictions.get(vehicle.id, []),
                    vehicle_predictions.get(other.id, []),
                    vehicle.speed_mps,
                    other.speed_mps,
                    threshold_m=11.0,
                )
                if risk is None:
                    continue
                risks.append(
                    self._collision_payload(
                        risk,
                        vehicle.id,
                        "vehicle",
                        other.id,
                        "vehicle",
                        selected_vehicle_id,
                        self._vehicle_collision_scenario(vehicle, other),
                    )
                )

        for vehicle in vehicles:
            for human in humans:
                risk = self._closest_approach(
                    vehicle_predictions.get(vehicle.id, []),
                    human_predictions.get(human.id, []),
                    vehicle.speed_mps,
                    human.speed_mps,
                    threshold_m=14.0,
                )
                if risk is None:
                    continue
                risks.append(
                    self._collision_payload(
                        risk,
                        vehicle.id,
                        "vehicle",
                        human.id,
                        "human",
                        selected_vehicle_id,
                        human.intent,
                    )
                )

        risks.sort(
            key=lambda item: (
                0 if item["involves_selected"] else 1,
                -item["risk_score"],
                item["seconds_to_conflict"],
            )
        )
        return risks[:12]

    def _closest_approach(
        self,
        first_path: list[dict],
        second_path: list[dict],
        first_speed: float,
        second_speed: float,
        threshold_m: float,
    ) -> dict | None:
        if not first_path or not second_path:
            return None

        by_t = {round(point["t"], 2): point for point in second_path}
        best: dict | None = None
        for point in first_path:
            other = by_t.get(round(point["t"], 2))
            if other is None:
                continue
            distance = haversine_distance_m(
                point["lat"],
                point["lon"],
                other["lat"],
                other["lon"],
            )
            if best is None or distance < best["distance_m"]:
                best = {
                    "seconds_to_conflict": float(point["t"]),
                    "distance_m": distance,
                    "lat": (point["lat"] + other["lat"]) / 2.0,
                    "lon": (point["lon"] + other["lon"]) / 2.0,
                }

        if best is None or best["distance_m"] > threshold_m:
            return None

        closing_weight = min((first_speed + second_speed) / 24.0, 1.0)
        proximity_weight = max(0.0, 1.0 - (best["distance_m"] / threshold_m))
        urgency_weight = max(0.0, 1.0 - (best["seconds_to_conflict"] / 14.0))
        best["risk_score"] = round(
            min(1.0, 0.50 * proximity_weight + 0.30 * urgency_weight + 0.20 * closing_weight),
            3,
        )
        return best

    def _collision_payload(
        self,
        risk: dict,
        primary_id: int,
        primary_type: str,
        target_id: int | str,
        target_type: str,
        selected_vehicle_id: int | None,
        scenario: str,
    ) -> dict:
        involves_selected = selected_vehicle_id in {primary_id, target_id}
        return {
            "primary_id": primary_id,
            "primary_type": primary_type,
            "target_id": target_id,
            "target_type": target_type,
            "seconds_to_conflict": round(risk["seconds_to_conflict"], 1),
            "distance_m": round(risk["distance_m"], 1),
            "lat": risk["lat"],
            "lon": risk["lon"],
            "risk_score": risk["risk_score"],
            "risk_level": self._risk_level(risk["risk_score"]),
            "scenario": scenario,
            "involves_selected": involves_selected,
        }

    def _build_heatmap(
        self,
        roads: list[RoadSegment],
        vehicles: list[Vehicle],
        humans: list[HumanAgent],
        collisions: list[dict],
    ) -> list[dict]:
        vehicles_by_segment: dict[int, list[Vehicle]] = defaultdict(list)
        humans_by_segment: dict[int, list[HumanAgent]] = defaultdict(list)
        collision_by_segment: dict[int, float] = defaultdict(float)
        vehicle_by_id = {vehicle.id: vehicle for vehicle in vehicles}

        for vehicle in vehicles:
            vehicles_by_segment[vehicle.road_segment_id].append(vehicle)
        for human in humans:
            if human.nearest_road_segment_id is not None:
                humans_by_segment[human.nearest_road_segment_id].append(human)
        for collision in collisions:
            primary = vehicle_by_id.get(collision["primary_id"])
            if primary is not None:
                collision_by_segment[primary.road_segment_id] = max(
                    collision_by_segment[primary.road_segment_id],
                    collision["risk_score"],
                )

        cells = []
        for road in roads:
            segment_vehicles = vehicles_by_segment.get(road.id, [])
            segment_humans = humans_by_segment.get(road.id, [])
            if not segment_vehicles and not segment_humans and road.poi_density < 0.1:
                continue

            wrong_way_count = sum(1 for vehicle in segment_vehicles if vehicle.wrong_way)
            avg_speed = (
                statistics.fmean(vehicle.speed_mps for vehicle in segment_vehicles)
                if segment_vehicles
                else 0.0
            )
            density_score = min(len(segment_vehicles) / 4.0, 1.0)
            human_score = min(len(segment_humans) / 2.0, 1.0)
            poi_score = min(float(road.poi_density or 0.0) / 12.0, 1.0)
            speed_score = min(avg_speed / 18.0, 1.0)
            wrong_score = 1.0 if wrong_way_count else 0.0
            collision_score = collision_by_segment.get(road.id, 0.0)

            risk_score = min(
                1.0,
                0.22 * density_score
                + 0.22 * human_score
                + 0.14 * poi_score
                + 0.14 * speed_score
                + 0.18 * wrong_score
                + 0.30 * collision_score,
            )
            if risk_score < 0.12:
                continue

            point = self._road_point(road, road.length_m * 0.5)
            cells.append(
                {
                    "road_segment_id": road.id,
                    "lat": point["lat"],
                    "lon": point["lon"],
                    "radius_m": round(min(max(road.length_m * 0.32, 28.0), 90.0), 1),
                    "risk_score": round(risk_score, 3),
                    "risk_level": self._risk_level(risk_score),
                    "scenario": self._road_scenario(
                        road,
                        wrong_way_count,
                        len(segment_humans),
                        collision_score,
                    ),
                    "vehicle_count": len(segment_vehicles),
                    "human_count": len(segment_humans),
                    "avg_speed_mps": round(avg_speed, 1),
                }
            )

        cells.sort(key=lambda cell: cell["risk_score"], reverse=True)
        return cells[:45]

    def _selected_vehicle_insights(
        self,
        vehicle: Vehicle,
        trajectory: list[dict],
        collisions: list[dict],
        heatmap: list[dict],
    ) -> dict:
        history = (
            VehicleHistory.query.filter_by(vehicle_id=vehicle.id)
            .order_by(VehicleHistory.timestamp.desc())
            .limit(80)
            .all()
        )
        history = list(reversed(history))
        behavior = self._behavior_awareness(vehicle, history, collisions)
        heatmap_by_segment = {cell["road_segment_id"]: cell for cell in heatmap}
        path_risks = [
            heatmap_by_segment[point["road_segment_id"]]
            for point in trajectory
            if point["road_segment_id"] in heatmap_by_segment
        ]
        selected_collisions = [
            collision for collision in collisions if collision["involves_selected"]
        ]

        return {
            "id": vehicle.id,
            "trajectory": trajectory,
            "temporal_analysis": self._temporal_analysis(history),
            "behavioral_awareness": behavior,
            "route_risk": path_risks[:8],
            "collision_predictions": selected_collisions[:6],
        }

    def _behavior_awareness(
        self,
        vehicle: Vehicle,
        history: list[VehicleHistory],
        collisions: list[dict],
    ) -> dict:
        recent_speeds = [row.speed_mps for row in history[-12:]]
        speed_std = statistics.pstdev(recent_speeds) if len(recent_speeds) > 1 else 0.0
        selected_collision = any(collision["involves_selected"] for collision in collisions)
        gap_seconds = simulation_engine.recommended_gap_seconds(vehicle.behavior)
        flags = []
        if vehicle.wrong_way:
            flags.append("wrong-way movement")
        if vehicle.behavior == "aggressive":
            flags.append("short headway tendency")
        if speed_std > 2.0:
            flags.append("unstable speed profile")
        if selected_collision:
            flags.append("projected conflict")
        if not flags:
            flags.append("stable route following")

        return {
            "profile": vehicle.behavior,
            "recommended_gap_seconds": gap_seconds,
            "awareness_flags": flags,
            "narrative": self._behavior_narrative(vehicle, flags),
        }

    def _temporal_analysis(self, history: list[VehicleHistory]) -> dict:
        if len(history) < 2:
            return {
                "samples": len(history),
                "average_speed_mps": 0.0,
                "speed_trend": "insufficient history",
                "acceleration_mps2": 0.0,
                "heading_change_deg": 0.0,
            }

        speeds = [row.speed_mps for row in history]
        first = history[0]
        last = history[-1]
        dt = max(last.timestamp - first.timestamp, 0.1)
        heading_change = abs(((last.bearing - first.bearing + 180.0) % 360.0) - 180.0)
        acceleration = (last.speed_mps - first.speed_mps) / dt
        trend = (
            "accelerating"
            if acceleration > 0.35
            else "braking"
            if acceleration < -0.35
            else "steady"
        )
        return {
            "samples": len(history),
            "average_speed_mps": round(statistics.fmean(speeds), 2),
            "speed_trend": trend,
            "acceleration_mps2": round(acceleration, 3),
            "heading_change_deg": round(heading_change, 1),
            "window_seconds": round(dt, 1),
        }

    def _road_point(self, road: RoadSegment, distance_m: float) -> dict[str, float]:
        lengths = cumulative_path_lengths(road.geometry)
        lat, lon = interpolate_path_position(road.geometry, lengths, distance_m)
        return {"lat": lat, "lon": lon}

    def _road_scenario(
        self,
        road: RoadSegment,
        wrong_way_count: int,
        human_count: int,
        collision_score: float,
    ) -> str:
        if wrong_way_count:
            return "wrong-way on one-way corridor"
        if collision_score >= 0.5:
            return "near-term collision conflict"
        if human_count:
            return "pedestrian crossing pressure"
        if road.poi_density >= 8:
            return "market or curbside activity"
        if road.road_class in {"primary", "trunk", "motorway"}:
            return "high-speed arterial flow"
        return "dense mixed traffic"

    def _vehicle_collision_scenario(self, vehicle: Vehicle, other: Vehicle) -> str:
        if vehicle.road_segment_id == other.road_segment_id:
            return "same-lane rear-end or head-on conflict"
        if vehicle.wrong_way or other.wrong_way:
            return "wrong-way vehicle conflict"
        return "junction merge conflict"

    def _behavior_narrative(self, vehicle: Vehicle, flags: list[str]) -> str:
        if vehicle.wrong_way:
            return "Vehicle is moving against permitted flow; prioritize interception and upstream warnings."
        if "projected conflict" in flags:
            return "Vehicle path intersects another road user inside the prediction horizon."
        if vehicle.behavior == "aggressive":
            return "Vehicle is modeled with lower gap acceptance and faster segment choices."
        if vehicle.behavior == "calm":
            return "Vehicle is maintaining conservative speed and wider headway."
        return "Vehicle is following the network with normal speed and headway."

    def _risk_level(self, score: float) -> str:
        if score >= 0.72:
            return "critical"
        if score >= 0.48:
            return "high"
        if score >= 0.25:
            return "elevated"
        return "watch"


live_traffic_intelligence = LiveTrafficIntelligence()
