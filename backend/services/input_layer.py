from __future__ import annotations

from collections.abc import Iterator

from backend.models import POI, RoadSegment, Vehicle
from backend.services.direction_intelligence import direction_intelligence_service


class FlowGuardInputLayer:
    def get_road_segments(self) -> list[dict]:
        segments = RoadSegment.query.order_by(RoadSegment.id).all()
        return [segment.to_dict() for segment in segments]

    def get_vehicle_updates(self) -> Iterator[dict]:
        if Vehicle.query.first() is None:
            return iter(())

        result = direction_intelligence_service.analyze_live_vehicles()
        return iter(result["direction"])

    def get_pois(self) -> list[dict]:
        pois = POI.query.order_by(POI.id).all()
        return [poi.to_dict() for poi in pois]
