from __future__ import annotations

import os

from backend.extensions import db

_DB_URL = os.getenv("DATABASE_URL", "")
_USE_POSTGIS = "postgresql" in _DB_URL or "postgres" in _DB_URL

if _USE_POSTGIS:
    from geoalchemy2 import Geometry


class RoadSegment(db.Model):
    __tablename__ = "road_segments"

    id = db.Column(db.Integer, primary_key=True)
    osm_way_id = db.Column(db.BigInteger, nullable=True, index=True)
    start_node_id = db.Column(db.BigInteger, nullable=False, index=True)
    end_node_id = db.Column(db.BigInteger, nullable=False, index=True)
    start_lat = db.Column(db.Float, nullable=False)
    start_lon = db.Column(db.Float, nullable=False)
    end_lat = db.Column(db.Float, nullable=False)
    end_lon = db.Column(db.Float, nullable=False)
    bearing = db.Column(db.Float, nullable=False)
    oneway = db.Column(db.Boolean, nullable=False, default=False)
    length_m = db.Column(db.Float, nullable=False)
    geometry = db.Column(db.JSON, nullable=False)
    if _USE_POSTGIS:
        geom = db.Column(
            Geometry("LINESTRING", srid=4326, spatial_index=True),
            nullable=True,
        )
    road_class = db.Column(db.String(64), nullable=True)
    speed_limit_mps = db.Column(db.Float, nullable=True)
    poi_density = db.Column(db.Float, nullable=False, default=0.0)

    vehicles = db.relationship("Vehicle", back_populates="road_segment", lazy="dynamic")
    pois = db.relationship("POI", back_populates="nearest_road_segment", lazy="dynamic")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start": [self.start_lat, self.start_lon],
            "end": [self.end_lat, self.end_lon],
            "bearing": self.bearing,
            "oneway": self.oneway,
            "length": self.length_m,
            "road_class": self.road_class,
            "speed_limit_mps": self.speed_limit_mps,
            "poi_density": self.poi_density,
            "geometry": self.geometry,
        }
