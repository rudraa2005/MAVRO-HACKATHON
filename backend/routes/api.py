from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from backend.models import POI, RoadSegment, Vehicle, VehicleHistory
from backend.services.bootstrap import bootstrap_input_layer
from backend.services.input_layer import FlowGuardInputLayer
from backend.services.ml_layer import live_traffic_intelligence
from backend.services.osm_ingestion import IngestionError
from backend.simulation.engine import simulation_engine


api_bp = Blueprint("api", __name__)
input_layer = FlowGuardInputLayer()


@api_bp.get("/health")
def health() -> tuple[dict, int]:
    return {
        "status": "ok",
        "place": current_app.config["FLOWGUARD_PLACE"],
        "roads": RoadSegment.query.count(),
        "vehicles": Vehicle.query.count(),
        "pois": POI.query.count(),
    }, 200


@api_bp.get("/roads")
def roads():
    only_oneway = request.args.get("only_oneway", default="false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    segments = input_layer.get_road_segments()
    if only_oneway:
        segments = [segment for segment in segments if segment["oneway"]]
    return jsonify(segments)


@api_bp.get("/vehicles")
def vehicles():
    return jsonify(list(input_layer.get_vehicle_updates()))


@api_bp.get("/vehicles/history")
def vehicle_history():
    vehicle_id = request.args.get("vehicle_id", type=int)
    limit = request.args.get("limit", default=200, type=int)

    query = VehicleHistory.query.order_by(VehicleHistory.timestamp.desc())
    if vehicle_id is not None:
        query = query.filter_by(vehicle_id=vehicle_id)

    rows = query.limit(min(limit, 1000)).all()
    return jsonify([row.to_dict() for row in reversed(rows)])


@api_bp.get("/live-analysis")
def live_analysis():
    selected_vehicle_id = request.args.get("vehicle_id", type=int)
    simulation_engine.refresh_network(current_app._get_current_object())
    return jsonify(live_traffic_intelligence.build_snapshot(selected_vehicle_id))


@api_bp.get("/pois")
def pois():
    return jsonify(input_layer.get_pois())


@api_bp.get("/summary")
def summary():
    wrong_way_count = Vehicle.query.filter_by(wrong_way=True).count()
    oneway_segments = RoadSegment.query.filter_by(oneway=True).count()
    return {
        "roads": RoadSegment.query.count(),
        "vehicles": Vehicle.query.count(),
        "pois": POI.query.count(),
        "oneway_segments": oneway_segments,
        "wrong_way_vehicles": wrong_way_count,
        "ready_for_demo": oneway_segments > 0,
        "has_data": RoadSegment.query.count() > 0,
        "simulation_running": simulation_engine.is_running(),
        "simulation_interval_seconds": current_app.config["SIMULATION_INTERVAL_SECONDS"],
        "poll_interval_ms": current_app.config["FRONTEND_POLL_INTERVAL_MS"],
    }


@api_bp.post("/admin/bootstrap")
def admin_bootstrap():
    payload = request.get_json(silent=True) or {}
    query = payload.get("query") or payload.get("place")
    query_type = payload.get("query_type", "auto")
    radius_m = int(payload.get("radius_m", 700))
    reset = bool(payload.get("reset", False))
    try:
        summary = bootstrap_input_layer(
            query=query,
            query_type=query_type,
            radius_m=radius_m,
            reset=reset,
        )
    except IngestionError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(summary), 202


@api_bp.post("/admin/simulation/start")
def admin_start_simulation():
    if RoadSegment.query.first() is None:
        return jsonify({"error": "Load a street area before starting the simulation."}), 400

    simulation_engine.start(current_app._get_current_object(), force=True)
    return jsonify(
        {
            "simulation_running": simulation_engine.is_running(),
            "vehicles": Vehicle.query.count(),
            "roads": RoadSegment.query.count(),
        }
    ), 202


@api_bp.post("/admin/simulation/stop")
def admin_stop_simulation():
    simulation_engine.stop()
    simulation_engine.clear_fleet(current_app._get_current_object())
    return jsonify(
        {
            "simulation_running": simulation_engine.is_running(),
            "vehicles": Vehicle.query.count(),
            "roads": RoadSegment.query.count(),
        }
    ), 202


@api_bp.post("/admin/scenarios/wrong-way")
def admin_wrong_way_scenario():
    payload = request.get_json(silent=True) or {}
    segment_id = payload.get("segment_id")
    vehicle_id = payload.get("vehicle_id")
    duration_seconds = payload.get("duration_seconds")

    try:
        result = simulation_engine.trigger_wrong_way_demo(
            current_app._get_current_object(),
            segment_id=int(segment_id) if segment_id is not None else None,
            vehicle_id=int(vehicle_id) if vehicle_id is not None else None,
            duration_seconds=float(duration_seconds) if duration_seconds is not None else None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(result), 202
