from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask

from backend.config import Config
from backend.extensions import db
from backend.routes.api import api_bp
from backend.routes.ui import ui_bp
from backend.services.bootstrap import ensure_input_data, register_cli_commands
from backend.simulation.engine import simulation_engine


def create_app() -> Flask:
    load_dotenv()
    project_root = Path(__file__).resolve().parent.parent
    instance_path = project_root / "instance"
    instance_path.mkdir(parents=True, exist_ok=True)
    cache_path = instance_path / ".cache"
    cache_path.mkdir(parents=True, exist_ok=True)
    matplotlib_cache = cache_path / "matplotlib"
    matplotlib_cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_path))

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        instance_path=str(instance_path),
    )
    app.config.from_object(Config)

    db.init_app(app)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(ui_bp)
    register_cli_commands(app)

    with app.app_context():
        if "postgresql" in app.config["SQLALCHEMY_DATABASE_URI"]:
            db.session.execute(db.text("CREATE EXTENSION IF NOT EXISTS postgis"))
            db.session.commit()
        db.create_all()
        ensure_input_data(app)

    if app.config["ENABLE_SIMULATION"] and _should_start_background_services(app):
        simulation_engine.start(app)

    return app


def _should_start_background_services(app: Flask) -> bool:
    if not app.debug:
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"
