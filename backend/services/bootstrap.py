from __future__ import annotations

import click
from flask import Flask, current_app

from backend.extensions import db
from backend.models import RoadSegment
from backend.services.direction_intelligence import direction_intelligence_service
from backend.services.map_matching import map_matching_service
from backend.services.osm_ingestion import osm_ingestion_service
from backend.simulation.engine import simulation_engine


def register_cli_commands(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command() -> None:
        db.create_all()
        click.echo("Database tables are ready.")

    @app.cli.command("ingest-osm")
    @click.option("--query", default=None, help="Place or street query to ingest.")
    @click.option(
        "--query-type",
        default="auto",
        type=click.Choice(["auto", "place"], case_sensitive=False),
        help="Use place-wide ingestion or a geocoded street-area ingest.",
    )
    @click.option("--radius-m", default=700, type=int, help="Radius for street-area ingest.")
    @click.option("--reset/--no-reset", default=False, help="Replace existing data.")
    def ingest_osm_command(
        query: str | None,
        query_type: str,
        radius_m: int,
        reset: bool,
    ) -> None:
        summary = bootstrap_input_layer(
            query=query,
            query_type=query_type,
            radius_m=radius_m,
            reset=reset,
        )
        click.echo(f"Ingestion complete: {summary}")

    @app.cli.command("seed-simulation")
    def seed_simulation_command() -> None:
        simulation_engine.start(current_app._get_current_object(), force=True)
        click.echo("Simulation engine is running.")

    @app.cli.command("benchmark-map-matching")
    @click.option("--points", default=1000, type=int, help="Number of points to score.")
    @click.option("--candidate-limit", default=5, type=int, help="Nearby road candidates per point.")
    @click.option("--distance-threshold-m", default=30.0, type=float, help="Max snap distance in meters.")
    def benchmark_map_matching_command(
        points: int,
        candidate_limit: int,
        distance_threshold_m: float,
    ) -> None:
        result = map_matching_service.benchmark(
            points_count=max(1, min(points, 10000)),
            candidate_limit=candidate_limit,
            distance_threshold_m=distance_threshold_m,
            max_jump_speed_mps=current_app.config["MAP_MATCH_MAX_JUMP_SPEED_MPS"],
        )
        click.echo(f"Map matching benchmark: {result}")


def bootstrap_input_layer(
    query: str | None = None,
    query_type: str = "auto",
    radius_m: int = 700,
    reset: bool = False,
    selection: dict | None = None,
) -> dict:
    db.create_all()
    app = current_app._get_current_object()
    was_running = simulation_engine.is_running()
    if was_running:
        simulation_engine.stop()

    target_query = query or current_app.config["FLOWGUARD_PLACE"]
    try:
        summary = osm_ingestion_service.ingest_query(
            query=target_query,
            query_type=query_type,
            radius_m=radius_m,
            reset=reset,
            selection=selection,
        )
    except Exception:
        simulation_engine.refresh_network(app)
        if was_running:
            simulation_engine.start(app, force=True)
        raise

    simulation_engine.refresh_network(app)
    map_matching_service.invalidate_cache()
    direction_intelligence_service.invalidate_cache()
    simulation_engine.clear_fleet(app)
    simulation_engine.start(app, force=True)

    summary["simulation_running"] = simulation_engine.is_running()
    return summary


def ensure_input_data(app: Flask) -> None:
    with app.app_context():
        if RoadSegment.query.first() is None and app.config["AUTO_INGEST"]:
            bootstrap_input_layer(
                query=app.config["FLOWGUARD_PLACE"],
                query_type="place",
                radius_m=700,
                reset=False,
            )
