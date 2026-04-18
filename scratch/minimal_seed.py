import sys
import os
sys.path.append(os.getcwd())
import time
from backend import create_app
from backend.extensions import db
from backend.models import RoadSegment, POI

app = create_app()

def run_minimal_seed():
    with app.app_context():
        print("Creating minimal road network for demo...")
        
        # Parry's Corner approx
        lat, lon = 13.088, 80.291
        
        # Segment 1: Main road (Oneway)
        s1 = RoadSegment(
            id=1,
            osm_way_id=101,
            start_node_id=1,
            end_node_id=2,
            start_lat=lat,
            start_lon=lon,
            end_lat=lat + 0.005,
            end_lon=lon,
            bearing=0.0,
            oneway=True,
            length_m=500.0,
            road_class="trunk",
            speed_limit_mps=16.0,
            geometry=[
                {"lat": lat, "lon": lon},
                {"lat": lat + 0.005, "lon": lon}
            ]
        )
        
        # Segment 2: Cross road
        s2 = RoadSegment(
            id=2,
            osm_way_id=102,
            start_node_id=2,
            end_node_id=3,
            start_lat=lat + 0.005,
            start_lon=lon,
            end_lat=lat + 0.005,
            end_lon=lon + 0.005,
            bearing=90.0,
            oneway=False,
            length_m=500.0,
            road_class="primary",
            speed_limit_mps=12.0,
            geometry=[
                {"lat": lat + 0.005, "lon": lon},
                {"lat": lat + 0.005, "lon": lon + 0.005}
            ]
        )
        
        db.session.add(s1)
        db.session.add(s2)
        
        p1 = POI(
            id=1,
            osm_feature_id="p1",
            poi_type="intersection",
            lat=lat + 0.005,
            lon=lon,
            nearest_road_segment_id=1
        )
        db.session.add(p1)
        
        db.session.commit()
        print("[SUCCESS] Minimal seed complete. 2 Roads, 1 POI.")

if __name__ == "__main__":
    run_minimal_seed()
