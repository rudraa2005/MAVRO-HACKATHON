import sys
import os
sys.path.append(os.getcwd())
from backend import create_app
from backend.extensions import db
from backend.services.osm_ingestion import osm_ingestion_service

app = create_app()

def reset_db():
    with app.app_context():
        print("Dropping all tables...")
        db.drop_all()
        print("Creating all tables...")
        db.create_all()
        
        print("Ingesting initial map data (Parry's Corner)...")
        try:
            osm_ingestion_service.ingest_place("Parry's Corner, Chennai", reset=True)
            print("[SUCCESS] Database reset and map data ingested.")
        except Exception as e:
            print(f"[ERROR] Map ingestion failed: {e}")

if __name__ == "__main__":
    reset_db()
