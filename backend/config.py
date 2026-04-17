import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_DIR = PROJECT_ROOT / "instance"


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_database_url(raw_url: str) -> str:
    if raw_url.startswith("sqlite:///") and not raw_url.startswith("sqlite:////"):
        sqlite_target = raw_url.removeprefix("sqlite:///")
        sqlite_path = Path(sqlite_target)
        if not sqlite_path.is_absolute():
            sqlite_path = PROJECT_ROOT / sqlite_path
        return f"sqlite:///{sqlite_path.resolve().as_posix()}"
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql+psycopg://", 1)
    if raw_url.startswith("postgresql://"):
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw_url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "flowguard-dev-secret")
    SQLALCHEMY_DATABASE_URI = _normalize_database_url(
        os.getenv("DATABASE_URL", f"sqlite:///{(INSTANCE_DIR / 'flowguard.db').as_posix()}")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
    }

    FLOWGUARD_PLACE = os.getenv("FLOWGUARD_PLACE", "Chennai, India")
    AUTO_INGEST = _as_bool(os.getenv("AUTO_INGEST"), False)
    ENABLE_SIMULATION = _as_bool(os.getenv("ENABLE_SIMULATION"), True)

    VEHICLE_COUNT = int(os.getenv("VEHICLE_COUNT", "30"))
    WRONG_WAY_COUNT = int(os.getenv("WRONG_WAY_COUNT", "2"))
    WRONG_WAY_DURATION_SECONDS = float(
        os.getenv("WRONG_WAY_DURATION_SECONDS", "30")
    )
    SIMULATION_INTERVAL_SECONDS = float(
        os.getenv("SIMULATION_INTERVAL_SECONDS", "0.5")
    )
    GPS_NOISE_METERS = float(os.getenv("GPS_NOISE_METERS", "6.0"))
    SIMULATION_RANDOM_SEED = int(os.getenv("SIMULATION_RANDOM_SEED", "42"))
    FRONTEND_POLL_INTERVAL_MS = int(os.getenv("FRONTEND_POLL_INTERVAL_MS", "1000"))
    MAP_MATCH_CANDIDATE_LIMIT = int(os.getenv("MAP_MATCH_CANDIDATE_LIMIT", "5"))
    MAP_MATCH_DISTANCE_THRESHOLD_M = float(
        os.getenv("MAP_MATCH_DISTANCE_THRESHOLD_M", "30.0")
    )
    MAP_MATCH_MAX_JUMP_SPEED_MPS = float(
        os.getenv("MAP_MATCH_MAX_JUMP_SPEED_MPS", "60.0")
    )
