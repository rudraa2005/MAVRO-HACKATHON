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
    FRONTEND_POLL_INTERVAL_MS = int(os.getenv("FRONTEND_POLL_INTERVAL_MS", "500"))
    FRONTEND_NEARBY_RADIUS_M = float(os.getenv("FRONTEND_NEARBY_RADIUS_M", "120.0"))
    SPEED_VARIATION_ENABLED = _as_bool(os.getenv("SPEED_VARIATION_ENABLED"), True)
    SPEED_VARIATION_MIN_FACTOR = float(os.getenv("SPEED_VARIATION_MIN_FACTOR", "0.92"))
    SPEED_VARIATION_MAX_FACTOR = float(os.getenv("SPEED_VARIATION_MAX_FACTOR", "1.08"))

    MAP_MATCH_CANDIDATE_LIMIT = int(os.getenv("MAP_MATCH_CANDIDATE_LIMIT", "5"))
    MAP_MATCH_DISTANCE_THRESHOLD_M = float(
        os.getenv("MAP_MATCH_DISTANCE_THRESHOLD_M", "30.0")
    )
    MAP_MATCH_MAX_JUMP_SPEED_MPS = float(
        os.getenv("MAP_MATCH_MAX_JUMP_SPEED_MPS", "60.0")
    )

    DIRECTION_TRAJECTORY_POINTS = int(os.getenv("DIRECTION_TRAJECTORY_POINTS", "10"))
    DIRECTION_TRAJECTORY_MAX_AGE_S = float(
        os.getenv("DIRECTION_TRAJECTORY_MAX_AGE_S", "10.0")
    )
    DIRECTION_WWP_WINDOW_S = float(os.getenv("DIRECTION_WWP_WINDOW_S", "5.0"))
    DIRECTION_SUSPECT_THRESHOLD = float(os.getenv("DIRECTION_SUSPECT_THRESHOLD", "0.4"))
    DIRECTION_VIOLATION_THRESHOLD = float(
        os.getenv("DIRECTION_VIOLATION_THRESHOLD", "0.65")
    )
    DIRECTION_SUSTAINED_SECONDS = float(os.getenv("DIRECTION_SUSTAINED_SECONDS", "2.0"))
    DIRECTION_MIN_SPEED_MPS = float(os.getenv("DIRECTION_MIN_SPEED_MPS", "1.5"))
    DIRECTION_ONEWAY_ALPHA = float(os.getenv("DIRECTION_ONEWAY_ALPHA", "0.75"))
    DIRECTION_TWOWAY_ALPHA = float(os.getenv("DIRECTION_TWOWAY_ALPHA", "0.55"))
    DIRECTION_TEMPORAL_BETA = float(os.getenv("DIRECTION_TEMPORAL_BETA", "0.25"))
    DIRECTION_STABLE_VARIANCE_THRESHOLD = float(
        os.getenv("DIRECTION_STABLE_VARIANCE_THRESHOLD", "0.05")
    )

    SEMANTIC_WRONG_WAY_ANGLE_DEG = float(os.getenv("SEMANTIC_WRONG_WAY_ANGLE_DEG", "150"))
    SEMANTIC_UTURN_MAX_SECONDS = float(os.getenv("SEMANTIC_UTURN_MAX_SECONDS", "2.0"))
    SEMANTIC_WRONG_WAY_MIN_SECONDS = float(
        os.getenv("SEMANTIC_WRONG_WAY_MIN_SECONDS", "3.0")
    )
    SEMANTIC_RISKY_SPEED_THRESHOLD_MPS = float(
        os.getenv("SEMANTIC_RISKY_SPEED_THRESHOLD_MPS", "8.0")
    )

    SPATIAL_MAX_INTERACTION_DISTANCE_M = float(
        os.getenv("SPATIAL_MAX_INTERACTION_DISTANCE_M", "50.0")
    )
    SPATIAL_TTC_DANGER_S = float(os.getenv("SPATIAL_TTC_DANGER_S", "2.0"))
    SPATIAL_TTC_RISKY_S = float(os.getenv("SPATIAL_TTC_RISKY_S", "5.0"))

    PREDICTION_STEPS = int(os.getenv("PREDICTION_STEPS", "5"))
    PREDICTION_STEP_DT_S = float(os.getenv("PREDICTION_STEP_DT_S", "1.0"))
    PREDICTION_VELOCITY_GAIN = float(os.getenv("PREDICTION_VELOCITY_GAIN", "0.45"))
    PREDICTION_POSITION_GAIN = float(os.getenv("PREDICTION_POSITION_GAIN", "0.35"))

    MEMORY_MAX_HISTORY = int(os.getenv("MEMORY_MAX_HISTORY", "50"))
    MEMORY_SUSPECT_INCREMENT = float(os.getenv("MEMORY_SUSPECT_INCREMENT", "0.25"))
    MEMORY_NORMAL_DECAY = float(os.getenv("MEMORY_NORMAL_DECAY", "0.15"))

    RISK_TTC_CRITICAL_S = float(os.getenv("RISK_TTC_CRITICAL_S", "2.0"))
    RISK_TTC_MEDIUM_S = float(os.getenv("RISK_TTC_MEDIUM_S", "5.0"))
    RISK_SCORE_HIGH_THRESHOLD = float(os.getenv("RISK_SCORE_HIGH_THRESHOLD", "5.0"))
