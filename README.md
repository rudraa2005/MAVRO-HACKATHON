# FlowGuard Input Layer

Production-style input layer for a real-time traffic intelligence demo built with Flask, PostgreSQL, OpenStreetMap, and a lightweight HTML/CSS/JavaScript frontend.

## What It Does

- Ingests drivable road segments from OpenStreetMap through `osmnx`
- Normalizes roads into lightweight `road_segments` rows with bearing, length, directionality, and geometry
- Extracts POIs for shops, traffic signals, parking, and graph-derived intersections
- Maps each POI to the nearest road segment and computes per-segment POI density
- Runs a background vehicle simulator that follows stored road geometry, adds GPS noise, and injects wrong-way vehicles
- Persists current vehicle state plus `vehicle_history`
- Exposes REST APIs and a live frontend dashboard

## Project Layout

```text
app.py
backend/
  models/
  routes/
  services/
  simulation/
  static/
  templates/
```

## Environment

Copy `.env.example` to `.env` and set at least:

```bash
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/flowguard
FLOWGUARD_PLACE=Chennai, India
AUTO_INGEST=false
ENABLE_SIMULATION=true
```

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run PostgreSQL

Create a database named `flowguard` in PostgreSQL. PostGIS is optional because the app stores lightweight WGS84 coordinate arrays and does nearest-road matching in Python.

## Bootstrap Data

Initialize tables:

```bash
flask --app app.py init-db
```

Ingest OSM data for Chennai:

```bash
flask --app app.py ingest-osm --place "Chennai, India" --reset
```

If you prefer automatic ingestion on startup, set:

```bash
AUTO_INGEST=true
```

## Run The App

```bash
flask --app app.py run --debug
```

Open `http://127.0.0.1:5000/`.

## API Surface

- `GET /api/health`
- `GET /api/roads`
- `GET /api/vehicles`
- `GET /api/vehicles/history?vehicle_id=1&limit=200`
- `GET /api/pois`
- `GET /api/summary`
- `POST /api/admin/bootstrap`

## Normalized Data Models

Roads returned by `GET /api/roads`:

```json
{
  "id": 1,
  "start": [13.08, 80.27],
  "end": [13.09, 80.28],
  "bearing": 91.4,
  "oneway": true,
  "length": 145.7
}
```

Vehicles returned by `GET /api/vehicles`:

```json
{
  "id": 7,
  "lat": 13.0851,
  "lon": 80.2780,
  "speed": 11.2,
  "bearing": 266.8,
  "timestamp": 1713436040.42,
  "road_segment_id": 189,
  "wrong_way": true
}
```

POIs returned by `GET /api/pois`:

```json
{
  "id": 12,
  "type": "signal",
  "lat": 13.0724,
  "lon": 80.2612,
  "nearest_road_segment_id": 41
}
```

## Notes

- Vehicles move along stored road geometry, not arbitrary free-space vectors.
- Speed is normalized to meters per second.
- Coordinates remain in WGS84 latitude/longitude.
- Timestamps are stored as UNIX seconds.
- The simulator is stateful; the HTTP APIs are read-only and stateless.
