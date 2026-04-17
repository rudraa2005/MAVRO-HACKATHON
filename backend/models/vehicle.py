from __future__ import annotations

from backend.extensions import db


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
