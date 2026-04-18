from __future__ import annotations

from flask import Blueprint, current_app, render_template


ui_bp = Blueprint("ui", __name__)


@ui_bp.get("/")
def index():
    return render_template(
        "index.html",
        place_name=current_app.config["FLOWGUARD_PLACE"],
        poll_interval_ms=current_app.config["FRONTEND_POLL_INTERVAL_MS"],
    )


@ui_bp.get("/control")
def control():
    return render_template(
        "control.html",
        place_name=current_app.config["FLOWGUARD_PLACE"],
        poll_interval_ms=current_app.config["FRONTEND_POLL_INTERVAL_MS"],
    )


@ui_bp.get("/analytics")
def analytics():
    return render_template(
        "analytics.html",
        place_name=current_app.config["FLOWGUARD_PLACE"],
        poll_interval_ms=current_app.config["FRONTEND_POLL_INTERVAL_MS"],
    )


@ui_bp.get("/risk")
def risk():
    return render_template(
        "risk.html",
        place_name=current_app.config["FLOWGUARD_PLACE"],
        poll_interval_ms=current_app.config["FRONTEND_POLL_INTERVAL_MS"],
    )
