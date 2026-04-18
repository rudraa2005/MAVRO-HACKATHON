/* ===================================================================
   FlowGuard Dashboard — app.js
   Smooth vehicle movement, heatmap, vehicle panel, 500ms updates
   =================================================================== */

const state = {
    map: null,
    roadsLayer: null,
    poisLayer: null,
    vehicleLayer: null,
    heatmapLayer: null,
    predictionLayer: null,
    collisionLayer: null,
    laneHighlightLayer: null,
    arrowLayer: null,
    vehicleMarkers: new Map(),
    vehicleArrows: new Map(),
    roadsById: new Map(),
    selectedVehicleId: null,
    latestVehicles: [],
    latestAnalysis: null,
    hasData: false,
    pollHandle: null,
};

const pollIntervalMs = Number(document.body.dataset.pollIntervalMs || 500);

/* ------------------------------------------------------------------ */
/* Utility                                                            */
/* ------------------------------------------------------------------ */

async function api(path, options = {}) {
    const res = await fetch(path, { cache: "no-store", ...options });
    const ct = res.headers.get("content-type") || "";
    const body = ct.includes("json") ? await res.json() : await res.text();
    if (!res.ok) {
        const detail = typeof body === "string" ? body : body.error || JSON.stringify(body);
        throw new Error(`${res.status}: ${detail}`);
    }
    return body;
}

function setStatus(msg, type) {
    const el = document.getElementById("status-pill");
    el.textContent = msg;
    el.style.color = type === "error" ? "#e53e3e" : type === "success" ? "#38a169" : "#888";
}

function riskColor(score) {
    if (score >= 0.72) return "#e53e3e";
    if (score >= 0.48) return "#d69e2e";
    if (score >= 0.25) return "#5a9bd5";
    return "#38a169";
}

function fmtPct(v) { return `${Math.round((v || 0) * 100)}%`; }

/* ------------------------------------------------------------------ */
/* Map                                                                */
/* ------------------------------------------------------------------ */

function initMap() {
    state.map = L.map("map", {
        zoomControl: false,
        preferCanvas: true,
    }).setView([13.0827, 80.2707], 12);

    L.control.zoom({ position: "bottomright" }).addTo(state.map);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://carto.com/">CARTO</a>',
    }).addTo(state.map);

    state.roadsLayer = L.layerGroup().addTo(state.map);
    state.poisLayer = L.layerGroup().addTo(state.map);
    state.heatmapLayer = L.layerGroup().addTo(state.map);
    state.vehicleLayer = L.layerGroup().addTo(state.map);
    state.predictionLayer = L.layerGroup().addTo(state.map);
    state.collisionLayer = L.layerGroup().addTo(state.map);
    state.laneHighlightLayer = L.layerGroup().addTo(state.map);
    state.arrowLayer = L.layerGroup().addTo(state.map);
}

function showEmptyState(visible) {
    const el = document.getElementById("map-empty-state");
    el.classList.toggle("visible", visible);
}

/* ------------------------------------------------------------------ */
/* Static layers (roads, POIs)                                        */
/* ------------------------------------------------------------------ */

function renderRoads(roads) {
    state.roadsLayer.clearLayers();
    state.roadsById.clear();

    if (!roads.length) {
        showEmptyState(true);
        return;
    }

    showEmptyState(false);
    const bounds = [];
    roads.forEach((r) => {
        state.roadsById.set(r.id, r);
        const coords = (r.geometry || []).map((p) => [p.lat, p.lon]);
        if (!coords.length) return;
        coords.forEach((c) => bounds.push(c));
        L.polyline(coords, {
            color: r.oneway ? "#666" : "#444",
            weight: r.oneway ? 3 : 2,
            opacity: r.oneway ? 0.7 : 0.5,
        }).addTo(state.roadsLayer);
    });

    if (bounds.length) {
        state.map.fitBounds(bounds, { padding: [20, 20] });
    }
}

function renderPois(pois) {
    state.poisLayer.clearLayers();
    pois.forEach((p) => {
        L.circleMarker([p.lat, p.lon], {
            radius: 3,
            color: "#555",
            fillColor: "#555",
            fillOpacity: 0.6,
            weight: 1,
        }).addTo(state.poisLayer);
    });
}

/* ------------------------------------------------------------------ */
/* Vehicles (smooth movement — setLatLng, never re-render)            */
/* ------------------------------------------------------------------ */

function renderVehicles(vehicles) {
    state.latestVehicles = vehicles;
    const seen = new Set();

    vehicles.forEach((v) => {
        if (v.lat == null || v.lon == null) return;
        seen.add(v.id);
        const pos = [v.lat, v.lon];
        const isSelected = state.selectedVehicleId === v.id;
        const suspicious = v.state === "suspicious";
        const isWrongWay = v.wrong_way || v.state === "wrong_way";
        const isReference = Boolean(v.reference);
        const color = isWrongWay ? "#a01515" : suspicious ? "#d69e2e" : isSelected ? "#38a169" : "#6f86a3";
        const radius = isWrongWay || isSelected || isReference ? 7 : 5;
        const pulseWeight = isWrongWay ? (2.2 + (Math.sin(Date.now() / 300) + 1) * 0.8) : (isSelected || isReference ? 3 : 1.5);

        let marker = state.vehicleMarkers.get(v.id);
        if (!marker) {
            marker = L.circleMarker(pos, {
                radius,
                color,
                fillColor: color,
                fillOpacity: 0.9,
                weight: pulseWeight,
            }).addTo(state.vehicleLayer);

            marker.on("click", () => {
                state.selectedVehicleId = v.id;
                renderVehicles(state.latestVehicles);
                refreshAnalysis();
            });
            state.vehicleMarkers.set(v.id, marker);
        }

        // Smooth move — just update position
        marker.setLatLng(pos);
        marker.setStyle({ color, fillColor: color, radius, weight: pulseWeight, fillOpacity: isReference ? 1.0 : 0.9 });
        renderVehicleArrow(v, pos, color);

        // Tooltip on hover
        marker.unbindTooltip();
        marker.bindTooltip(
            `#${v.id}${v.reference ? " [REF]" : ""} | ${Number(v.speed || 0).toFixed(1)} m/s | ${v.state || "normal"} | conf ${Math.round((v.confidence || 0) * 100)}%`,
            { direction: "top", offset: [0, -8], className: "" }
        );
    });

    // Remove stale markers
    state.vehicleMarkers.forEach((marker, id) => {
        if (!seen.has(id)) {
            state.vehicleLayer.removeLayer(marker);
            state.vehicleMarkers.delete(id);
        }
    });
    state.vehicleArrows.forEach((arrow, id) => {
        if (!seen.has(id)) {
            state.arrowLayer.removeLayer(arrow);
            state.vehicleArrows.delete(id);
        }
    });

    updateWrongWayList(vehicles);
    renderWrongWayLaneHighlights(vehicles);
}

function projectPoint(lat, lon, distanceMeters, bearingDeg) {
    const r = 6378137;
    const brng = bearingDeg * Math.PI / 180;
    const lat1 = lat * Math.PI / 180;
    const lon1 = lon * Math.PI / 180;
    const lat2 = Math.asin(Math.sin(lat1) * Math.cos(distanceMeters / r) +
        Math.cos(lat1) * Math.sin(distanceMeters / r) * Math.cos(brng));
    const lon2 = lon1 + Math.atan2(
        Math.sin(brng) * Math.sin(distanceMeters / r) * Math.cos(lat1),
        Math.cos(distanceMeters / r) - Math.sin(lat1) * Math.sin(lat2)
    );
    return [lat2 * 180 / Math.PI, lon2 * 180 / Math.PI];
}

function renderVehicleArrow(vehicle, pos, color) {
    const tip = projectPoint(pos[0], pos[1], 10, Number(vehicle.bearing || 0));
    let arrow = state.vehicleArrows.get(vehicle.id);
    if (!arrow) {
        arrow = L.polyline([pos, tip], {
            color,
            weight: 2,
            opacity: 0.9,
        }).addTo(state.arrowLayer);
        state.vehicleArrows.set(vehicle.id, arrow);
        return;
    }
    arrow.setLatLngs([pos, tip]);
    arrow.setStyle({ color });
}

function renderWrongWayLaneHighlights(vehicles) {
    state.laneHighlightLayer.clearLayers();
    const wrongWaySegmentIds = new Set(
        vehicles.filter((v) => (v.wrong_way || v.state === "wrong_way") && v.road_segment_id != null).map((v) => v.road_segment_id)
    );
    wrongWaySegmentIds.forEach((segId) => {
        const road = state.roadsById.get(segId);
        const coords = (road?.geometry || []).map((p) => [p.lat, p.lon]);
        if (coords.length < 2) return;
        L.polyline(coords, {
            color: "#b71c1c",
            weight: 7,
            opacity: 0.45,
        }).addTo(state.laneHighlightLayer);
    });
}

function updateWrongWayList(vehicles) {
    const el = document.getElementById("wrong-way-list");
    const ww = vehicles.filter((v) => v.wrong_way || v.state === "wrong_way");
    const suspicious = vehicles.filter((v) => v.state === "suspicious");
    if (!ww.length && !suspicious.length) {
        el.innerHTML = "<li>No active wrong-way vehicles.</li>";
        return;
    }
    const wwItems = ww.map((v) =>
        `<li style="color:var(--danger)">Vehicle #${v.id}${v.reference ? " [REF]" : ""} — CONFIRMED (${Math.round((v.confidence || 0) * 100)}%)</li>`
    );
    const susItems = suspicious.slice(0, 6).map((v) =>
        `<li style="color:var(--warning)">Vehicle #${v.id} — SUSPICIOUS (${Math.round((v.confidence || 0) * 100)}%)</li>`
    );
    el.innerHTML = wwItems.concat(susItems).join("");
}

/* ------------------------------------------------------------------ */
/* Heatmap (smooth transitions — update existing circles)             */
/* ------------------------------------------------------------------ */

const _heatCircles = new Map();

function renderHeatmap(cells) {
    const seen = new Set();

    cells.forEach((c) => {
        const key = c.road_segment_id;
        seen.add(key);
        const intensity = Number(c.confidence != null ? c.confidence : c.risk_score);
        const color = riskColor(intensity);
        const radius = Math.max(intensity * 50 + 15, 20);

        let circle = _heatCircles.get(key);
        if (!circle) {
            circle = L.circle([c.lat, c.lon], {
                radius,
                color,
                fillColor: color,
                fillOpacity: 0.15,
                opacity: 0.35,
                weight: 1,
            }).addTo(state.heatmapLayer);
            _heatCircles.set(key, circle);
        }

        circle.setLatLng([c.lat, c.lon]);
        circle.setRadius(radius);
        circle.setStyle({ color, fillColor: color });
    });

    _heatCircles.forEach((circle, key) => {
        if (!seen.has(key)) {
            state.heatmapLayer.removeLayer(circle);
            _heatCircles.delete(key);
        }
    });
}

/* ------------------------------------------------------------------ */
/* Prediction trajectory & collisions                                 */
/* ------------------------------------------------------------------ */

function renderTrajectory(selected) {
    state.predictionLayer.clearLayers();
    const traj = selected?.trajectory || [];
    if (!traj.length) return;

    const coords = traj.map((p) => [p.lat, p.lon]);
    L.polyline(coords, {
        color: "#38a169",
        weight: 3,
        opacity: 0.7,
        dashArray: "6 6",
    }).addTo(state.predictionLayer);
}

function renderCollisions(collisions) {
    state.collisionLayer.clearLayers();
    collisions.forEach((c) => {
        const color = riskColor(c.risk_score);
        L.circleMarker([c.lat, c.lon], {
            radius: c.involves_selected ? 7 : 5,
            color,
            fillColor: color,
            fillOpacity: 0.7,
            weight: c.involves_selected ? 2 : 1,
        }).addTo(state.collisionLayer);
    });
}

/* ------------------------------------------------------------------ */
/* Vehicle panel (right side)                                         */
/* ------------------------------------------------------------------ */

function updateVehiclePanel(analysis) {
    const sv = analysis?.selected_vehicle;
    const emptyEl = document.getElementById("vehicle-panel-empty");
    const detailEl = document.getElementById("vehicle-detail");

    if (!sv) {
        emptyEl.style.display = "block";
        detailEl.style.display = "none";
        renderSelectedVehicleHeatmap([]);
        return;
    }

    emptyEl.style.display = "none";
    detailEl.style.display = "block";

    document.getElementById("vp-id").textContent = `#${sv.id}`;
    document.getElementById("vp-speed").textContent = `${Number(sv.speed || 0).toFixed(1)} m/s`;

    const stateEl = document.getElementById("vp-state");
    stateEl.textContent = sv.state || "normal";
    stateEl.className = "value" + (sv.state === "wrong_way" ? " danger" : "");

    // Direction
    const wwpEl = document.getElementById("vp-wwp");
    wwpEl.textContent = fmtPct(sv.wwp);
    wwpEl.className = "value" + (sv.wwp >= 0.5 ? " danger" : sv.wwp >= 0.2 ? " warning" : "");

    document.getElementById("vp-direction").textContent = fmtPct(sv.direction_score);

    // Risk
    const ttcEl = document.getElementById("vp-ttc");
    ttcEl.textContent = sv.ttc != null ? `${sv.ttc}s` : "—";
    ttcEl.className = "value" + (sv.ttc != null && sv.ttc < 5 ? " danger" : sv.ttc != null && sv.ttc < 10 ? " warning" : "");

    const riskEl = document.getElementById("vp-risk");
    riskEl.textContent = fmtPct(sv.risk_score);
    riskEl.className = "value" + (sv.risk_score >= 0.72 ? " danger" : sv.risk_score >= 0.48 ? " warning" : "");

    document.getElementById("vp-maneuver").textContent = fmtPct(sv.maneuverability);
    const alertEl = document.getElementById("vp-alert");
    const alertOn = Boolean(sv.alert_triggered);
    alertEl.textContent = alertOn ? "TRIGGERED" : "CLEAR";
    alertEl.className = "value" + (alertOn ? " danger" : " success");

    // Spatial
    document.getElementById("vp-nearby").textContent = sv.nearby_count ?? 0;
    document.getElementById("vp-closest").textContent = sv.closest_distance_m != null ? `${sv.closest_distance_m} m` : "—";

    // Semantic
    document.getElementById("vp-road-class").textContent = sv.road_class || "—";
    document.getElementById("vp-poi-density").textContent = sv.poi_density != null ? `${sv.poi_density}/km` : "—";

    // ML
    const anomalyEl = document.getElementById("vp-anomaly");
    anomalyEl.textContent = fmtPct(sv.anomaly_score);
    anomalyEl.className = "value" + (sv.anomaly_score >= 0.5 ? " danger" : "");

    document.getElementById("vp-memory").textContent = fmtPct(sv.memory_match);

    // Detection logic
    const d = sv.detection_logic || {};
    const roadBearing = sv.road_bearing ?? d.road_bearing;
    const vehBearing = sv.heading ?? d.vehicle_bearing;
    const angleDiff = sv.angle_diff ?? d.angle_difference_deg;
    document.getElementById("vp-road-bearing").textContent = roadBearing != null ? `${roadBearing}°` : "—";
    document.getElementById("vp-vehicle-bearing").textContent = vehBearing != null ? `${vehBearing}°` : "—";
    document.getElementById("vp-angle-diff").textContent = angleDiff != null ? `${angleDiff}°` : "—";
    document.getElementById("vp-temporal").textContent = d.temporal_stability || "—";
    const decisionEl = document.getElementById("vp-decision");
    decisionEl.textContent = d.decision || "—";
    decisionEl.className = "value" + (
        d.decision === "WRONG-WAY" ? " danger" :
        d.decision === "SUSPICIOUS" ? " warning" : " success"
    );

    const gaugeFill = document.getElementById("vp-angle-gauge");
    const gaugeLabel = document.getElementById("vp-angle-gauge-label");
    const angle = Number(angleDiff || 0);
    const clamped = Math.max(0, Math.min(180, angle));
    gaugeFill.style.width = `${(clamped / 180) * 100}%`;
    gaugeFill.style.background = clamped >= 150 ? "#e53e3e" : clamped >= 90 ? "#d69e2e" : "#38a169";
    gaugeLabel.textContent = `${clamped.toFixed(1)}° / 180°`;

    // False positive status
    const fp = sv.false_positive || {};
    const fpRiskEl = document.getElementById("vp-fp-risk");
    fpRiskEl.textContent = fp.risk || "—";
    fpRiskEl.className = "value" + (fp.risk === "HIGH" ? " danger" : fp.risk === "MEDIUM" ? " warning" : " success");
    document.getElementById("vp-fp-reason").textContent = fp.reason || "—";

    document.getElementById("vp-confidence").textContent = fmtPct(sv.confidence);
    const statusEl = document.getElementById("vp-status");
    statusEl.textContent = sv.status || "NORMAL";
    statusEl.className = "value" + (sv.status === "CONFIRMED" ? " danger" : sv.status === "SUSPICIOUS" ? " warning" : " success");
    document.getElementById("vp-gps-stability").textContent = sv.gps_stability || "—";
    document.getElementById("vp-edge-case").textContent = sv.edge_case || "NONE";

    const ctxEl = document.getElementById("vp-surrounding-context");
    const contexts = sv.surrounding_context || [];
    if (!contexts.length) {
        ctxEl.innerHTML = "<li>No contextual risk signals yet.</li>";
    } else {
        ctxEl.innerHTML = contexts.map((c) => `<li>${c.label} — ${c.risk}</li>`).join("");
    }

    renderSelectedVehicleHeatmap(sv.selected_vehicle_heatmap || []);

    // Collision list
    const collList = document.getElementById("collision-list");
    const collisions = sv.collision_predictions || [];
    if (!collisions.length) {
        collList.innerHTML = "<li>No near-term conflicts.</li>";
    } else {
        collList.innerHTML = collisions.map((c) =>
            `<li style="color:${riskColor(c.risk_score)}">${c.risk_level}: ${c.scenario} — TTC ${c.seconds_to_conflict}s, gap ${c.distance_m}m</li>`
        ).join("");
    }
}

function renderSelectedVehicleHeatmap(points) {
    const canvas = document.getElementById("selected-heatmap-canvas");
    const empty = document.getElementById("selected-heatmap-empty");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const width = canvas.clientWidth || canvas.width;
    canvas.width = width;

    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!points.length) {
        empty.style.display = "block";
        return;
    }
    empty.style.display = "none";

    const lats = points.map((p) => p.lat);
    const lons = points.map((p) => p.lon);
    const minLat = Math.min(...lats), maxLat = Math.max(...lats);
    const minLon = Math.min(...lons), maxLon = Math.max(...lons);
    const pad = 12;
    const drawW = canvas.width - pad * 2;
    const drawH = canvas.height - pad * 2;
    const latSpan = Math.max(maxLat - minLat, 1e-6);
    const lonSpan = Math.max(maxLon - minLon, 1e-6);

    points.forEach((p) => {
        const x = pad + ((p.lon - minLon) / lonSpan) * drawW;
        const y = pad + (1 - (p.lat - minLat) / latSpan) * drawH;
        const intensity = Number(p.intensity || 0.2);
        const radius = 5 + intensity * 16;
        const alpha = 0.12 + intensity * 0.45;
        ctx.beginPath();
        ctx.fillStyle = `rgba(229, 62, 62, ${alpha.toFixed(3)})`;
        ctx.arc(x, y, radius, 0, Math.PI * 2);
        ctx.fill();
    });
}

/* ------------------------------------------------------------------ */
/* Data fetching loop                                                 */
/* ------------------------------------------------------------------ */

function renderAnalysis(analysis) {
    state.latestAnalysis = analysis;
    renderHeatmap(analysis?.heatmap || []);
    renderTrajectory(analysis?.selected_vehicle);
    renderCollisions(analysis?.collision_predictions || []);
    updateVehiclePanel(analysis);
}

async function refreshAnalysis() {
    const q = state.selectedVehicleId ? `?vehicle_id=${state.selectedVehicleId}` : "";
    const analysis = await api(`/api/live-analysis${q}`);
    renderAnalysis(analysis);
}

async function refreshSnapshot() {
    try {
        const q = state.selectedVehicleId ? `?vehicle_id=${state.selectedVehicleId}` : "";
        const [summary, vehicles, analysis] = await Promise.all([
            api("/api/summary"),
            api("/api/vehicles"),
            api(`/api/live-analysis${q}`),
        ]);

        // Update stats
        document.getElementById("roads-count").textContent = summary.roads ?? 0;
        document.getElementById("vehicles-count").textContent = summary.vehicles ?? 0;
        document.getElementById("wrong-way-count").textContent = summary.wrong_way_vehicles ?? 0;
        document.getElementById("oneway-count").textContent = summary.oneway_segments ?? 0;
        document.getElementById("last-update").textContent = new Date().toLocaleTimeString();

        const simEl = document.getElementById("sim-state");
        simEl.textContent = summary.simulation_running ? "RUNNING" : "STOPPED";
        simEl.style.color = summary.simulation_running ? "#38a169" : "#e53e3e";

        // Render
        renderVehicles(vehicles);
        renderAnalysis(analysis);

        if (!summary.has_data) {
            showEmptyState(true);
            setStatus("No road network. Go to Control page to load an area.", "neutral");
            return;
        }

        state.hasData = true;
        showEmptyState(false);

        if (!summary.simulation_running) {
            setStatus("Simulation stopped. Go to Control to start.", "neutral");
            return;
        }

        setStatus(`Live — ${summary.vehicles} vehicles, ${summary.wrong_way_vehicles} wrong-way`, "success");
    } catch (err) {
        console.error(err);
        setStatus("Sync failed: " + err.message, "error");
    }
}

async function loadStaticLayers() {
    try {
        const [roads, pois] = await Promise.all([api("/api/roads"), api("/api/pois")]);
        renderRoads(roads);
        renderPois(pois);
    } catch (err) {
        console.error(err);
    }
}

/* ------------------------------------------------------------------ */
/* Bootstrap                                                          */
/* ------------------------------------------------------------------ */

async function boot() {
    initMap();
    await loadStaticLayers();
    await refreshSnapshot();
    state.pollHandle = setInterval(refreshSnapshot, pollIntervalMs);
}

window.addEventListener("DOMContentLoaded", boot);
