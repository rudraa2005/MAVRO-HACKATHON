from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request, Response

from backend.models import POI, RoadSegment, Vehicle, VehicleHistory
from backend.services.bootstrap import bootstrap_input_layer
from backend.services.direction_intelligence import direction_intelligence_service
from backend.services.eval_logger import eval_logger
from backend.services.evaluation import evaluate_binary_classifier
from backend.services.input_layer import FlowGuardInputLayer
from backend.services.ml_layer import live_traffic_intelligence
from backend.services.map_matching import map_matching_service
from backend.services.osm_ingestion import IngestionError, osm_ingestion_service
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
        "1", "true", "yes", "on",
    }
    segments = input_layer.get_road_segments()
    if only_oneway:
        segments = [s for s in segments if s["oneway"]]
    return jsonify(segments)


@api_bp.get("/vehicles")
def vehicles():
    """Return vehicle data. Uses in-memory engine state for speed."""
    snapshot = simulation_engine.get_vehicles_snapshot()
    if snapshot:
        return jsonify(snapshot)
    # Fallback to DB if engine has no vehicles
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


@api_bp.get("/analytics/model-metrics")
def analytics_model_metrics():
    threshold = request.args.get(
        "threshold",
        default=current_app.config["EVAL_WRONG_WAY_THRESHOLD"],
        type=float,
    )
    threshold = min(1.0, max(0.0, float(threshold)))

    analysis = direction_intelligence_service.analyze_live_vehicles()
    direction_rows = analysis.get("direction", [])
    if not direction_rows:
        return jsonify(
            {
                "samples": 0,
                "metrics": {"precision": 0.0, "recall": 0.0, "fpr": 0.0, "tpr": 0.0},
                "confusion": {"tp": 0, "fp": 0, "tn": 0, "fn": 0},
                "roc_curve": [],
                "auc": None,
                "warnings": ["No active vehicles were available for evaluation."],
            }
        )

    records = []
    for row in direction_rows:
        records.append(
            {
                "ground_truth": bool(row.get("wrong_way", False)),
                "score": row.get(
                    "wrong_way_probability",
                    row.get("direction_score", row.get("ml_collision_probability", 0.0)),
                ),
            }
        )

    payload = evaluate_binary_classifier(records, threshold=threshold)
    payload["score_field"] = "wrong_way_probability"
    payload["label_field"] = "wrong_way"
    return jsonify(payload)


@api_bp.get("/analytics")
def analytics():
    """Return timeseries data for analytics charts."""
    return jsonify(live_traffic_intelligence.build_analytics_timeseries())


@api_bp.get("/analytics/metrics")
def analytics_metrics():
    """Real-time evaluation metrics derived from the ground-truth log buffer.

    Query params
    ------------
    threshold : float  (default: EVAL_WRONG_WAY_THRESHOLD from config)
        Decision threshold applied to wrong_way_probability.
    format    : str    ('json' | 'csv')  default: 'json'
        Return raw log data as CSV instead of metrics JSON.
    clear     : bool   (0|1)  default: 0
        Flush the log buffer after responding.
    """
    threshold = request.args.get(
        "threshold",
        default=current_app.config.get("EVAL_WRONG_WAY_THRESHOLD", 0.65),
        type=float,
    )
    threshold = float(max(0.0, min(1.0, threshold)))
    fmt = request.args.get("format", default="json").lower()
    do_clear = request.args.get("clear", default="0").lower() in {"1", "true", "yes"}

    # Apply threshold for this request (does not permanently change the logger)
    from backend.services.eval_logger import EvalLogger, BUFFER_MAXLEN
    temp_logger = EvalLogger(maxlen=BUFFER_MAXLEN, threshold=threshold)
    logs = eval_logger.get_logs()   # snapshot — list copy, thread-safe
    # Re-apply threshold to snapshot so metrics match the requested threshold
    from backend.services.eval_logger import EvalRecord
    re_thresholded = [
        EvalRecord(
            timestamp=r.timestamp,
            vehicle_id=r.vehicle_id,
            wrong_way_probability=r.wrong_way_probability,
            predicted_label=1 if r.wrong_way_probability >= threshold else 0,
            ground_truth_label=r.ground_truth_label,
        )
        for r in logs
    ]

    if fmt == "csv":
        csv_data = eval_logger.dump_csv(re_thresholded)
        if do_clear:
            eval_logger.clear()
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=eval_log.csv"},
        )

    result = temp_logger.compute_confusion_matrix(re_thresholded)
    result["buffer_size"] = len(logs)
    result["buffer_maxlen"] = BUFFER_MAXLEN
    result["log_window"] = {
        "oldest_ts": round(logs[0].timestamp, 3) if logs else None,
        "newest_ts": round(logs[-1].timestamp, 3) if logs else None,
    }

    if do_clear:
        eval_logger.clear()

    return jsonify(result)


@api_bp.get("/analytics/roc")
def analytics_roc():
    """ROC curve and AUC from the ground-truth evaluation log buffer.

    Query params
    ------------
    n         : int   (default: 50)  Number of threshold points.
    clear     : bool  (0|1)          Flush the buffer after responding.
    """
    from backend.services.eval_logger import BUFFER_MAXLEN, ROC_POINTS

    n = request.args.get("n", default=ROC_POINTS, type=int)
    n = max(2, min(n, 500))
    do_clear = request.args.get("clear", default="0").lower() in {"1", "true", "yes"}

    logs = eval_logger.get_logs()
    result = eval_logger.compute_roc_auc(logs=logs, n_thresholds=n)

    result["buffer_size"] = len(logs)
    result["buffer_maxlen"] = BUFFER_MAXLEN
    result["log_window"] = {
        "oldest_ts": round(logs[0].timestamp, 3) if logs else None,
        "newest_ts": round(logs[-1].timestamp, 3) if logs else None,
    }

    if do_clear:
        eval_logger.clear()

    return jsonify(result)


@api_bp.get("/analytics/optimal-threshold")
def analytics_optimal_threshold():
    """Compute the optimal decision threshold via Youden's J statistic.

    Query params
    ------------
    n     : int   (default: 50)  Number of ROC threshold points.
    apply : bool  (0|1)          Apply the optimal threshold to the live system.
    """
    from backend.services.eval_logger import ROC_POINTS

    n = request.args.get("n", default=ROC_POINTS, type=int)
    n = max(2, min(n, 500))
    do_apply = request.args.get("apply", default="0").lower() in {"1", "true", "yes"}

    result = eval_logger.optimal_threshold(n_thresholds=n, apply=do_apply)
    return jsonify(result)


@api_bp.get("/risk-monitor")
def risk_monitor():
    """Return data for the risk monitor page."""
    return jsonify(live_traffic_intelligence.build_risk_monitor())


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
        "map_matching_ready": RoadSegment.query.count() > 0,
        "vehicle_count_target": current_app.config["VEHICLE_COUNT"],
    }


@api_bp.post("/map-match")
def map_match_points():
    payload = request.get_json(silent=True) or {}
    points = payload.get("points")
    if isinstance(points, dict):
        points = [points]
    if not isinstance(points, list) or not points:
        return jsonify({"error": 'Provide "points" as a non-empty array.'}), 400

    candidate_limit = int(
        payload.get("candidate_limit", current_app.config["MAP_MATCH_CANDIDATE_LIMIT"])
    )
    distance_threshold_m = float(
        payload.get("distance_threshold_m", current_app.config["MAP_MATCH_DISTANCE_THRESHOLD_M"])
    )
    max_jump_speed_mps = float(
        payload.get("max_jump_speed_mps", current_app.config["MAP_MATCH_MAX_JUMP_SPEED_MPS"])
    )

    try:
        matches = map_matching_service.match_payloads(
            payloads=points,
            candidate_limit=candidate_limit,
            distance_threshold_m=distance_threshold_m,
            max_jump_speed_mps=max_jump_speed_mps,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": f"Invalid map-match payload: {exc}"}), 400

    return jsonify({
        "matches": matches,
        "stats": {
            "points": len(points),
            "matched": sum(1 for m in matches if m["matched_edge_id"] is not None),
            "postgis_candidate_sql": map_matching_service.postgis_candidate_sql(
                candidate_limit=candidate_limit,
                distance_threshold_m=distance_threshold_m,
            ),
        },
    })


@api_bp.get("/map-match/live")
def map_match_live():
    limit = request.args.get("limit", type=int)
    candidate_limit = request.args.get(
        "candidate_limit", default=current_app.config["MAP_MATCH_CANDIDATE_LIMIT"], type=int,
    )
    distance_threshold_m = request.args.get(
        "distance_threshold_m", default=current_app.config["MAP_MATCH_DISTANCE_THRESHOLD_M"], type=float,
    )
    max_jump_speed_mps = request.args.get(
        "max_jump_speed_mps", default=current_app.config["MAP_MATCH_MAX_JUMP_SPEED_MPS"], type=float,
    )
    return jsonify(map_matching_service.match_live_vehicles(
        candidate_limit=candidate_limit,
        distance_threshold_m=distance_threshold_m,
        max_jump_speed_mps=max_jump_speed_mps,
        limit=limit,
    ))


@api_bp.post("/admin/bootstrap")
def admin_bootstrap():
    payload = request.get_json(silent=True) or {}
    query = payload.get("query") or payload.get("place")
    query_type = payload.get("query_type", "auto")
    radius_m = int(payload.get("radius_m", 700))
    reset = bool(payload.get("reset", False))
    selection = payload.get("selection")
    try:
        summary = bootstrap_input_layer(
            query=query, query_type=query_type,
            radius_m=radius_m, reset=reset, selection=selection,
        )
    except IngestionError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(summary), 202


@api_bp.post("/admin/location-search")
def admin_location_search():
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query") or "").strip()
    limit = max(1, min(int(payload.get("limit", 5)), 8))
    try:
        result = osm_ingestion_service.search_candidates(query=query, limit=limit)
    except IngestionError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result), 200


@api_bp.post("/admin/simulation/start")
def admin_start_simulation():
    if RoadSegment.query.first() is None:
        return jsonify({"error": "Load a street area before starting the simulation."}), 400

    simulation_engine.start(current_app._get_current_object(), force=True)
    return jsonify({
        "simulation_running": simulation_engine.is_running(),
        "vehicles": Vehicle.query.count(),
        "roads": RoadSegment.query.count(),
    }), 202


@api_bp.post("/admin/simulation/stop")
def admin_stop_simulation():
    simulation_engine.stop()
    simulation_engine.clear_fleet(current_app._get_current_object())
    map_matching_service.invalidate_cache()
    direction_intelligence_service.invalidate_cache()
    return jsonify({
        "simulation_running": simulation_engine.is_running(),
        "vehicles": Vehicle.query.count(),
        "roads": RoadSegment.query.count(),
    }), 202


@api_bp.post("/admin/simulation/reset")
def admin_reset_simulation():
    """Full reset: stop, clear fleet, reseed, restart."""
    app = current_app._get_current_object()
    simulation_engine.stop()
    simulation_engine.reseed_demo_fleet(app)
    simulation_engine.start(app, force=True)
    map_matching_service.invalidate_cache()
    direction_intelligence_service.invalidate_cache()
    return jsonify({
        "simulation_running": simulation_engine.is_running(),
        "vehicles": Vehicle.query.count(),
        "roads": RoadSegment.query.count(),
    }), 202


@api_bp.post("/admin/density")
def admin_set_density():
    """Adjust vehicle density (count) from the UI slider."""
    payload = request.get_json(silent=True) or {}
    count = int(payload.get("count", 30))
    app = current_app._get_current_object()
    simulation_engine.set_vehicle_count(app, count)
    return jsonify({
        "vehicle_count_target": app.config["VEHICLE_COUNT"],
    }), 200


@api_bp.post("/admin/map-match/benchmark")
def admin_map_match_benchmark():
    payload = request.get_json(silent=True) or {}
    points = int(payload.get("points", 1000))
    candidate_limit = int(
        payload.get("candidate_limit", current_app.config["MAP_MATCH_CANDIDATE_LIMIT"])
    )
    distance_threshold_m = float(
        payload.get("distance_threshold_m", current_app.config["MAP_MATCH_DISTANCE_THRESHOLD_M"])
    )
    max_jump_speed_mps = float(
        payload.get("max_jump_speed_mps", current_app.config["MAP_MATCH_MAX_JUMP_SPEED_MPS"])
    )
    try:
        result = map_matching_service.benchmark(
            points_count=max(1, min(points, 10000)),
            candidate_limit=candidate_limit,
            distance_threshold_m=distance_threshold_m,
            max_jump_speed_mps=max_jump_speed_mps,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result), 200


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


@api_bp.get("/direction/live")
def direction_live():
    candidate_limit = request.args.get(
        "candidate_limit", default=current_app.config["MAP_MATCH_CANDIDATE_LIMIT"], type=int,
    )
    distance_threshold_m = request.args.get(
        "distance_threshold_m", default=current_app.config["MAP_MATCH_DISTANCE_THRESHOLD_M"], type=float,
    )
    max_jump_speed_mps = request.args.get(
        "max_jump_speed_mps", default=current_app.config["MAP_MATCH_MAX_JUMP_SPEED_MPS"], type=float,
    )
    limit = request.args.get("limit", type=int)
    return jsonify(direction_intelligence_service.analyze_live_vehicles(
        candidate_limit=candidate_limit,
        distance_threshold_m=distance_threshold_m,
        max_jump_speed_mps=max_jump_speed_mps,
        limit=limit,
    ))
