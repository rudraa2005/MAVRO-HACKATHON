from __future__ import annotations

from backend.extensions import db


class POI(db.Model):
    __tablename__ = "pois"

    id = db.Column(db.Integer, primary_key=True)
    osm_feature_id = db.Column(db.String(128), nullable=True, index=True)
    poi_type = db.Column(db.String(32), nullable=False, index=True)
    lat = db.Column(db.Float, nullable=False)
    lon = db.Column(db.Float, nullable=False)
    nearest_road_segment_id = db.Column(
        db.Integer, db.ForeignKey("road_segments.id"), nullable=True, index=True
    )

    nearest_road_segment = db.relationship("RoadSegment", back_populates="pois")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.poi_type,
            "lat": self.lat,
            "lon": self.lon,
            "nearest_road_segment_id": self.nearest_road_segment_id,
        }
