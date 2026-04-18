from __future__ import annotations

from flask import Blueprint, current_app, render_template


ui_bp = Blueprint("ui", __name__)


@ui_bp.get("/")
def index():
    return render_template(
        "index.html",
        place_name=current_app.config["FLOWGUARD_PLACE"],
        poll_interval_ms=current_app.config["FRONTEND_POLL_INTERVAL_MS"],
        nearby_radius_m=current_app.config["FRONTEND_NEARBY_RADIUS_M"],
    )
