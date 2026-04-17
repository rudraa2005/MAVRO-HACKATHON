from __future__ import annotations

from collections.abc import Iterator

from backend.models import POI, RoadSegment, Vehicle


class FlowGuardInputLayer:
    def get_road_segments(self) -> list[dict]:
        segments = RoadSegment.query.order_by(RoadSegment.id).all()
        return [segment.to_dict() for segment in segments]

    def get_vehicle_updates(self) -> Iterator[dict]:
        for vehicle in Vehicle.query.order_by(Vehicle.id).all():
            yield vehicle.to_dict()

    def get_pois(self) -> list[dict]:
        pois = POI.query.order_by(POI.id).all()
        return [poi.to_dict() for poi in pois]
