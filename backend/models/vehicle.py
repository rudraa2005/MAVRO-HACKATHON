from __future__ import annotations

from backend.extensions import db
from sqlalchemy import orm


class Vehicle(db.Model):
    __tablename__ = "vehicles"

    id = db.Column(db.Integer, primary_key=True)
    road_segment_id = db.Column(
        db.Integer, db.ForeignKey("road_segments.id"), nullable=False, index=True
    )
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)
    speed_mps = db.Column(db.Float, nullable=False)
    bearing = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.Float, nullable=False)
    direction = db.Column(db.Integer, nullable=False, default=1)
    progress_m = db.Column(db.Float, nullable=False, default=0.0)
    wrong_way = db.Column(db.Boolean, nullable=False, default=False)
    wrong_way_until = db.Column(db.Float, nullable=True)
    behavior = db.Column(db.String(32), nullable=False, default="normal")

    # ML metric columns
    state = db.Column(db.String(16), nullable=False, default="normal")
    anomaly_score = db.Column(db.Float, nullable=False, default=0.0)
    risk_score = db.Column(db.Float, nullable=False, default=0.0)
    wwp = db.Column(db.Float, nullable=False, default=0.0)
    ttc = db.Column(db.Float, nullable=True)
    maneuverability = db.Column(db.Float, nullable=False, default=1.0)
    nearby_count = db.Column(db.Integer, nullable=False, default=0)
    closest_distance_m = db.Column(db.Float, nullable=True)

    # Polymorphic inheritance
    type = db.Column(db.String(50))

    # Advanced Analysis Fields
    behavioral_signature = db.Column(db.JSON, nullable=True)
    intent_classification = db.Column(db.String(64), nullable=True)
    confidence_adjustment = db.Column(db.Float, nullable=False, default=0.0)
    gps_quality_score = db.Column(db.Float, nullable=False, default=1.0)
    cascade_role = db.Column(db.String(32), nullable=False, default="NONE")

    __mapper_args__ = {
        "polymorphic_identity": "base_vehicle",
        "polymorphic_on": type,
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._init_history()

    @orm.reconstructor
    def _init_history(self):
        """Initialize in-memory history buffers."""
        self.history_timestamps = []
        self.speed_history = []
        self.bearing_history = []
        self.position_history = []
        self.acceleration_history = []

    road_segment = db.relationship("RoadSegment", back_populates="vehicles")
    history = db.relationship(
        "VehicleHistory",
        back_populates="vehicle",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lat": self.lat,
            "lon": self.lon,
            "speed": self.speed_mps,
            "bearing": self.bearing,
            "timestamp": self.timestamp,
            "road_segment_id": self.road_segment_id,
            "wrong_way": self.wrong_way,
            "behavior": self.behavior,
            "state": self.state,
            "anomaly_score": round(self.anomaly_score, 3),
            "risk_score": round(self.risk_score, 3),
            "wwp": round(self.wwp, 3),
            "ttc": round(self.ttc, 1) if self.ttc is not None else None,
            "maneuverability": round(self.maneuverability, 3),
            "nearby_count": self.nearby_count,
            "closest_distance_m": (
                round(self.closest_distance_m, 1)
                if self.closest_distance_m is not None
                else None
            ),
        }


class VehicleHistory(db.Model):
    __tablename__ = "vehicle_history"

    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicles.id"), nullable=False)
    road_segment_id = db.Column(
        db.Integer, db.ForeignKey("road_segments.id"), nullable=False, index=True
    )
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)
    speed_mps = db.Column(db.Float, nullable=False)
    bearing = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.Float, nullable=False, index=True)

    vehicle = db.relationship("Vehicle", back_populates="history")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "vehicle_id": self.vehicle_id,
            "road_segment_id": self.road_segment_id,
            "lat": self.lat,
            "lon": self.lon,
            "speed": self.speed_mps,
            "bearing": self.bearing,
            "timestamp": self.timestamp,
        }
