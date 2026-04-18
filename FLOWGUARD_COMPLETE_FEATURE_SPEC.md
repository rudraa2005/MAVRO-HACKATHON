# FlowGuard Complete Feature Specification (v0 -> Current)

This document is the full, detailed implementation reference for the current FlowGuard codebase state.  
It captures the system from foundational setup to the latest production-oriented upgrades.

---

## 1) System Goal

FlowGuard is a live traffic intelligence platform that:

- ingests real road networks and POIs,
- simulates moving vehicles and controlled wrong-way scenarios,
- map-matches noisy GPS points to road segments,
- detects direction anomalies with temporal consistency,
- enriches signals with memory, anomaly, and prediction layers,
- computes conflict and route risk,
- serves analytics-ready APIs,
- visualizes live intelligence in a browser dashboard.

---

## 2) Core Runtime Architecture

### 2.1 Backend Stack

- Flask app lifecycle and blueprint routing
- SQLAlchemy ORM models + persistent DB
- service-oriented pipeline modules
- deterministic simulation engine

### 2.2 Frontend Stack

- Leaflet map rendering
- periodic polling of backend APIs
- layered overlays:
  - roads
  - vehicles
  - humans
  - heatmap cells
  - trajectory lines
  - collision points/links

### 2.3 Data Persistence

- relational tables for:
  - roads
  - POIs
  - vehicles
  - vehicle history
- persistent historical samples per vehicle for temporal analytics

---

## 3) Data Models and Stored Signals

### 3.1 Vehicle

- identity: `id`
- location: `lat`, `lon`
- kinematics: `speed_mps`, `bearing`, `timestamp`
- routing context: `road_segment_id`, `direction`, `progress_m`
- behavior metadata: `behavior`
- violation flags: `wrong_way`, `wrong_way_until`

### 3.2 VehicleHistory

- immutable trajectory trail per vehicle over time
- includes segment, position, speed, bearing, timestamp
- powers:
  - temporal trend analysis
  - seeded direction buffers
  - context-aware behavior summaries

### 3.3 RoadSegment

- network geometry + bearing + class
- one-way direction semantics
- length and density metadata

### 3.4 POI

- type-classified points (signal/intersection/shop/parking/etc.)
- nearest road linkage for human-agent and risk context generation

---

## 4) End-to-End Intelligence Pipeline

Live vehicle processing flow:

1. Load current vehicle set from DB.
2. Map-match each point against candidate road segments.
3. Infer legal direction constraints (one-way/two-way).
4. Run direction intelligence engine with per-vehicle rolling memory.
5. Update persistent anomaly memory scores.
6. Compute spatial conflict/TTC-based pairwise risk.
7. Add lightweight ML-derived anomaly/risk cluster signals.
8. Predict short-horizon trajectories.
9. Fuse multi-signal risk into final risk level.
10. Produce decision-layer alert states.
11. Serve enriched payloads to APIs + frontend.

---

## 5) Road Ingestion and Bootstrapping Features

- location search and area loading workflow
- ingest + normalize OSM geometry
- derive road directionality and attributes
- extract/store nearby POIs
- reset/reseed runtime state on area reload
- automatic simulation startup after successful bootstrap
- cache invalidation hooks after topology changes

Admin endpoints provide:

- bootstrap by place query,
- candidate search for location disambiguation,
- simulation start/stop controls.

---

## 6) Simulation Engine Features

- periodic tick-based fleet movement
- behavior profiles (normal/aggressive/calm)
- configurable vehicle count and wrong-way count
- GPS noise support
- deterministic random seed support
- explicit wrong-way scenario trigger with duration control
- fleet clearing and network refresh controls

Scenario support:

- targeted wrong-way injection on one-way segments
- automatic wrong-way timeout handling
- visualization-ready scenario response payloads

---

## 7) Map Matching Features

- candidate search with configurable limits
- distance threshold gating
- heading consistency support
- one-way compatibility scoring
- sudden-jump rejection with max jump speed guard
- state-cache support for continuity
- benchmark endpoint for throughput testing
- diagnostic SQL generation for PostGIS candidate retrieval

Live and batch modes:

- payload-based `/api/map-match`
- live DB-driven `/api/map-match/live`

---

## 8) Direction Intelligence Engine Features

Implemented in `direction_intelligence_core.py`, wrapped by service integration.

### 8.1 Motion Understanding

- computes motion vectors from rolling trajectories
- projects geo coordinates locally for stable vector math
- computes cosine similarity against road direction vectors
- converts similarity to wrong-way probability (`raw_wwp`)

### 8.2 Temporal Smoothing

- rolling wrong-way probability window buffer
- mean and variance-based stability interpretation
- sustained-duration checks above suspect threshold
- transient spike suppression

### 8.3 Rule Parameters

- suspect and violation thresholds
- oneway/twoway weighting
- temporal stability beta
- min speed gate
- stable variance threshold
- sustained time requirements

### 8.4 State Machine

Outputs:

- `NORMAL`
- `SUSPECT`
- `CONFIRMED`

With additional output fields:

- direction similarity
- wrong-way probability
- confidence
- window size
- temporal average/variance
- sustained duration
- stability flag

---

## 9) Anomaly Memory and Behavioral Memory Features

### 9.1 Persistent Vehicle Memory

- violation count tracking
- risk score accumulation and decay
- rolling bounded history store
- risk clamping to safe numeric bounds

State-based scoring behavior:

- `CONFIRMED`: increments violation count
- `SUSPECT`: incremental risk increase
- `NORMAL`: controlled risk decay

### 9.2 ML Memory Add-ons

- acceleration memory per vehicle
- repeat behavior similarity scoring
- bounded anomaly history window for unsupervised scoring

---

## 10) Spatial Risk and Collision Features

- relative motion-based TTC estimation
- pairwise vehicle risk analysis
- distance and divergence gating
- symmetric updates to involved entities
- probability-style conflict scoring
- scenario text classification for conflicts

Risk interpretation supports:

- watch/elevated/high/critical categories
- conflict ranking and selected-vehicle prioritization

---

## 11) Prediction Features

- short-horizon trajectory generation per vehicle
- timestamped future path points
- prediction memory state carry-over across ticks
- compatibility with map overlays and collision prediction

---

## 12) Unified Risk Engine Features

- fuses temporal, memory, spatial, anomaly, and semantic cues
- computes `risk_level` from combined evidence
- supports escalation for wrong-way and high-collision contexts
- includes continuous refined risk components

---

## 13) Decision Layer Features

- converts risk context into actionable alert state
- alert categories:
  - `SAFE`
  - `WARNING`
  - `HIGH_ALERT`
  - `COLLISION_ALERT`
- ensures final payload is UI- and API-ready

---

## 14) Live Traffic Intelligence Layer Features

Implemented in `backend/services/ml_layer.py`.

### 14.1 Snapshot Composition

- selected vehicle payload
- human-agent synthesis
- collision predictions
- heatmap cells
- route overlap risk for selected vehicle

### 14.2 Human-Agent Modeling

- POI-anchored synthetic pedestrian movement
- context-specific intent labels
- crossing and curbside behavior styles
- fallback generation when POI anchors are sparse

### 14.3 Heatmap Modeling

Risk blends:

- vehicle density
- human density
- POI intensity
- average speed pressure
- wrong-way presence
- collision signal

Outputs include:

- risk score/level
- scenario narrative
- influence radius
- counts and aggregate speed context

### 14.4 Selected Vehicle Insights

- temporal analytics from history
- behavior awareness profile and narrative
- selected collision subset
- route heat overlap subset

---

## 15) Surrounding Context and Selection Stability (Latest Upgrade)

### 15.1 Stable Selection

- sticky selected vehicle memory to avoid frequent oscillation
- explicit selection source tagging:
  - `client_selected`
  - `sticky_previous`
  - `auto_wrong_way`
  - `auto_first_vehicle`
  - `empty_fleet`

### 15.2 Rich Surrounding Context

For selected vehicle:

- nearest vehicle distance
- nearest human distance
- nearby vehicles list with:
  - distance
  - relative speed
  - wrong-way status
  - behavior profile
- nearby humans list with:
  - distance
  - intent
  - risk zone
- active collision count involving selected vehicle
- aggregated `context_risk_score`

---

## 16) Model Evaluation and ROC Analytics (Latest Upgrade)

### 16.1 New Evaluation Service

`backend/services/evaluation.py` now provides:

- confusion matrix computation (`tp/fp/tn/fn`)
- precision, recall, FPR, TPR
- accuracy, specificity, F1
- robust ROC point generation
- trapezoidal AUC computation
- safe handling of missing/single-class edge cases

### 16.2 New Analytics Endpoint

`GET /api/analytics/model-metrics`

Features:

- threshold override support via query param
- evaluates live direction payloads using:
  - label field: `wrong_way`
  - score field: `wrong_way_probability`
- returns:
  - sample counts and class counts
  - confusion matrix
  - scalar metrics
  - ROC curve points
  - AUC (or null with warning when statistically invalid)

### 16.3 Configuration Support

New config key:

- `EVAL_WRONG_WAY_THRESHOLD` (default `0.65`)

---

## 17) API Surface

### 17.1 Core Read APIs

- `GET /api/health`
- `GET /api/roads`
- `GET /api/vehicles`
- `GET /api/vehicles/history`
- `GET /api/live-analysis`
- `GET /api/pois`
- `GET /api/summary`

### 17.2 Direction/Matching APIs

- `GET /api/direction/live`
- `POST /api/map-match`
- `GET /api/map-match/live`
- `POST /api/admin/map-match/benchmark`

### 17.3 Admin Control APIs

- `POST /api/admin/bootstrap`
- `POST /api/admin/location-search`
- `POST /api/admin/simulation/start`
- `POST /api/admin/simulation/stop`
- `POST /api/admin/scenarios/wrong-way`

### 17.4 Analytics APIs

- `GET /api/analytics/model-metrics`

---

## 18) Frontend Dashboard Features

### 18.1 Map and Layers

- road network rendering with one-way visual styling
- POI overlays
- vehicle markers with selection + wrong-way highlighting
- human overlays
- risk heatmap overlays
- trajectory overlays
- collision markers/links

### 18.2 Control Panel

- load street area by query
- start/stop simulation
- trigger wrong-way scenario
- preset query shortcuts
- live status and health messaging

### 18.3 Insight Panels

- selected vehicle summary
- temporal analysis block
- behavioral awareness block
- collision predictions
- route heatmap overlap
- wrong-way monitor list
- endpoint quick reference

### 18.4 New UI Enhancements

- selection source display in selected-vehicle narrative
- context risk and nearest-entity awareness indicators
- speed variability visibility in behavior panel

---

## 19) Caching, State, and Consistency Features

- snapshot signature-based caching in direction intelligence service
- duplicate recomputation suppression for close-timing API calls
- invalidation routines on simulation stop/bootstrap
- reset hooks for:
  - direction engine buffers
  - anomaly memory
  - prediction memory
  - ML state memory

---

## 20) Configurability and Operational Controls

Environment/config controls include:

- DB URI normalization and SQLite defaults
- simulation toggles and timing
- fleet sizing and wrong-way duration
- map matching candidate/search thresholds
- poll interval controls
- deterministic seeding
- analytics threshold configuration

---

## 21) Test Coverage and Quality Gates

Current automated suite covers:

- direction intelligence core behavior
- temporal state transitions
- anomaly memory behavior
- map matching correctness + edge cases
- spatial risk computation
- prediction behavior
- risk engine decisioning
- decision layer outputs
- ML intelligence helpers
- new evaluation/ROC metric computation

Validation commands used in this project include:

- Python unittest discovery across `tests/`
- JS syntax checks for frontend bundle

---

## 22) Production-Grade Improvements Already Landed

Recent hardening upgrades:

- robust model-evaluation endpoint with edge-case handling
- explicit warning semantics for invalid ROC/AUC conditions
- selection stability to reduce context flicker
- richer, structured surrounding context payload
- threshold configurability for evaluation behavior
- full regression test pass after changes

---

## 23) Known Boundaries (Current State)

Implemented and reliable:

- end-to-end demo-grade real-time intelligence loop
- deterministic and test-backed backend behavior
- actionable API and visualization outputs

Not yet fully production enterprise-grade:

- no authn/authz or multitenancy layer
- no queue-based async worker topology
- no SLO/SLA observability stack (metrics/tracing/log aggregation)
- no schema migration history described in this doc
- no full blue/green deployment playbook in repo docs

---

## 24) Recommended Next Production Steps

1. Add API authentication and role-based access controls.
2. Add persistent audit logs and alert history tables.
3. Add observability:
   - structured logs
   - metrics (latency/error/rate)
   - tracing
4. Add load/perf benchmarks and target budgets per endpoint.
5. Add strict API contracts (OpenAPI schema + versioning).
6. Add CI quality gates:
   - unit + integration + lint + format checks
7. Add fault-tolerance strategy:
   - retries/backoff
   - degraded mode for external ingestion outages
8. Add dashboard chart widgets for live ROC/confusion tracking.

---

## 25) Quick Executive Summary

FlowGuard now has a complete integrated traffic-intelligence stack that moves from ingestion and simulation through map matching, direction anomaly detection, memory and risk enrichment, trajectory and collision prediction, live visualization, and evaluation analytics.  
The latest upgrades directly address context stability and metric explainability by adding sticky selection, rich surrounding context, and a formal precision/recall/FPR/ROC/AUC evaluation API.

