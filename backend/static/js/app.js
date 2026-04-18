const body = document.body

const state = {
    map: null,
    layers: {},
    currentPage: "dashboard",
    roads: [],
    pois: [],
    vehicles: [],
    matches: [],
    summary: null,
    selectedCandidateId: null,
    searchCandidates: [],
    selectedVehicleId: null,
    selectedRoadId: null,
    currentPlace: body.dataset.place || "FlowGuard Demo",
    pollIntervalMs: Number.parseInt(body.dataset.pollIntervalMs || "500", 10),
    pollHandle: null,
    logs: [],
    vehicleHistory: new Map(),
    riskTimeline: [],
    lastAlertState: new Map(),
    isRefreshing: false,
    vehicleCount: 30,
    speedVariation: false,
    nearbyRadiusM: Number.parseFloat(body.dataset.nearbyRadiusM || "120"),
}

const dom = {}

const ALERT_COLORS = {
    SAFE: "#3a7d44",
    WARNING: "#c9a227",
    HIGH_ALERT: "#b94a48",
    COLLISION_ALERT: "#b94a48",
}

const RISK_COLORS = {
    low: "#3a7d44",
    medium: "#c9a227",
    high: "#b94a48",
    critical: "#b94a48",
    safe: "#3a7d44",
    risky: "#c9a227",
    danger: "#b94a48",
}

function $(id) {
    return document.getElementById(id)
}

function cacheDom() {
    dom.pageTitle = $("page-title")
    dom.pageSubtitle = $("page-subtitle")
    dom.statusBadge = $("system-status")
    dom.lastSync = $("last-sync")
    dom.activeVehicles = $("metric-active-vehicles")
    dom.wrongWayCount = $("metric-wrong-way")
    dom.highRiskCount = $("metric-high-risk")
    dom.avgTtc = $("metric-avg-ttc")
    dom.mapEmpty = $("map-empty-state")
    dom.eventFeed = $("event-feed")
    dom.runtimeSummary = $("runtime-summary")
    dom.vehicleIntelligence = $("vehicle-intelligence")
    dom.vehicleTable = $("vehicle-table-body")
    dom.vehicleDetail = $("vehicle-detail")
    dom.riskTable = $("risk-table-body")
    dom.riskTimeline = $("risk-timeline")
    dom.collisionPreview = $("collision-preview")
    dom.roadTable = $("road-table-body")
    dom.roadExplanation = $("road-explanation")
    dom.logsList = $("logs-list")
    dom.logSeverity = $("log-severity-filter")
    dom.logVehicle = $("log-vehicle-filter")
    dom.roadHeatmap = $("road-heatmap")
    dom.searchResults = $("search-results")
    dom.candidateMeta = $("candidate-meta")
    dom.scenarioQuery = $("scenario-query")
    dom.scenarioRadius = $("scenario-radius")
    dom.scenarioPreset = $("scenario-preset")
    dom.locationSelector = $("location-selector")
    dom.vehicleControlSelect = $("vehicle-control-select")
    dom.vehicleWrongWayToggle = $("vehicle-wrong-way-toggle")
    dom.vehicleCount = $("vehicle-count")
    dom.vehicleCountValue = $("vehicle-count-value")
    dom.speedVariationToggle = $("speed-variation-toggle")
    dom.responseConsole = $("response-console")
    dom.simulationState = $("simulation-state")
    dom.networkState = $("network-state")
    dom.analyticsCount = $("analytics-count")
}

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;")
}

function requestJSON(url, options = {}) {
    return fetch(url, {
        headers: {
            "Content-Type": "application/json",
            ...(options.headers || {}),
        },
        ...options,
    }).then(async (response) => {
        const text = await response.text()
        const payload = text ? JSON.parse(text) : {}
        if (!response.ok) {
            const message = payload.error || payload.message || `Request failed: ${response.status}`
            throw new Error(message)
        }
        return payload
    })
}

function formatNumber(value, digits = 0, fallback = "--") {
    const numeric = Number(value)
    if (!Number.isFinite(numeric)) {
        return fallback
    }
    return numeric.toFixed(digits)
}

function setStatus(message, tone = "neutral") {
    dom.statusBadge.textContent = message
    dom.statusBadge.dataset.tone = tone
}

function addLog(severity, message, meta = {}) {
    const entry = {
        id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        severity,
        message,
        vehicleId: meta.vehicleId ?? null,
        timestamp: Date.now(),
    }
    state.logs.unshift(entry)
    state.logs = state.logs.slice(0, 200)
    renderEventFeed()
    renderLogsPage()
}

function alertRank(alert) {
    if (alert === "COLLISION_ALERT") return 4
    if (alert === "HIGH_ALERT") return 3
    if (alert === "WARNING") return 2
    return 1
}

function riskRank(level) {
    if (level === "critical") return 4
    if (level === "high") return 3
    if (level === "medium") return 2
    return 1
}

function alertColor(alert) {
    return ALERT_COLORS[alert] || "#9aa3b2"
}

function riskColor(level) {
    return RISK_COLORS[level] || "#9aa3b2"
}

function mapDangerRadius(vehicle) {
    const ttc = Number(vehicle.ttc)
    if (!Number.isFinite(ttc)) {
        return 0
    }
    if (ttc < 2) return 22
    if (ttc < 5) return 14
    return 8
}

function distanceMeters(a, b) {
    const toRad = (value) => (Number(value) * Math.PI) / 180
    const lat1 = toRad(a.lat)
    const lat2 = toRad(b.lat)
    const dLat = toRad(b.lat - a.lat)
    const dLon = toRad(b.lon - a.lon)
    const x = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2
    return 2 * 6371000 * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x))
}

function lineWeightForRoad(road) {
    const roadClass = String(road.road_class || "").toLowerCase()
    if (["motorway", "trunk", "primary"].includes(roadClass)) return 3
    if (["secondary", "tertiary"].includes(roadClass)) return 2.4
    return 1.8
}

function resolveScenarioPreset(preset) {
    if (preset === "sparse-highway") {
        return { query: `${state.currentPlace}`, radius: "1000" }
    }
    if (preset === "dense-urban") {
        return { query: `${state.currentPlace}`, radius: "700" }
    }
    return { query: `${state.currentPlace}`, radius: "700" }
}

function roadIntelligenceScore(road) {
    const poiDensity = Number(road.poi_density || 0)
    const length = Number(road.length || 0)
    const oneWayWeight = road.oneway ? 2.2 : 0.8
    const classWeight = ["motorway", "trunk", "primary"].includes(String(road.road_class || "").toLowerCase()) ? 2.2 : 1.0
    return Math.min(10, oneWayWeight + classWeight + Math.min(3.5, poiDensity * 2.4) + Math.min(2.1, length / 220))
}

function roadComplexityLabel(score) {
    if (score >= 7.5) return "High"
    if (score >= 4.5) return "Moderate"
    return "Low"
}

function selectedVehicle() {
    return state.vehicles.find((vehicle) => vehicle.id === state.selectedVehicleId) || null
}

function selectedRoad() {
    return state.roads.find((road) => road.id === state.selectedRoadId) || null
}

function buildSparkline(series, color) {
    if (!series.length) {
        return '<div class="empty-inline">No samples yet</div>'
    }
    const values = series.map((item) => Number(item.value || 0))
    const max = Math.max(...values, 1)
    const min = Math.min(...values, 0)
    const width = 220
    const height = 56
    const range = Math.max(max - min, 1)
    const points = values
        .map((value, index) => {
            const x = (index / Math.max(values.length - 1, 1)) * width
            const y = height - ((value - min) / range) * height
            return `${x},${y}`
        })
        .join(" ")
    return `
        <svg viewBox="0 0 ${width} ${height}" class="sparkline" aria-hidden="true">
            <polyline fill="none" stroke="${color}" stroke-width="2.2" points="${points}"></polyline>
        </svg>
    `
}

function buildPredictionMiniMap(vehicle) {
    const points = [[vehicle.lat, vehicle.lon], ...(vehicle.future_positions || [])]
    if (points.length < 2) {
        return '<div class="empty-inline">Prediction unavailable</div>'
    }
    const lats = points.map((point) => Number(point[0]))
    const lons = points.map((point) => Number(point[1]))
    const minLat = Math.min(...lats)
    const maxLat = Math.max(...lats)
    const minLon = Math.min(...lons)
    const maxLon = Math.max(...lons)
    const width = 220
    const height = 140
    const padding = 12
    const spanLat = Math.max(maxLat - minLat, 1e-6)
    const spanLon = Math.max(maxLon - minLon, 1e-6)

    const projected = points.map(([lat, lon]) => {
        const x = padding + ((Number(lon) - minLon) / spanLon) * (width - padding * 2)
        const y = height - padding - ((Number(lat) - minLat) / spanLat) * (height - padding * 2)
        return [x, y]
    })

    const polyline = projected.map(([x, y]) => `${x},${y}`).join(" ")
    const [firstX, firstY] = projected[0]

    return `
        <svg viewBox="0 0 ${width} ${height}" class="prediction-mini-map" aria-hidden="true">
            <rect x="0" y="0" width="${width}" height="${height}" rx="4" ry="4"></rect>
            <polyline fill="none" stroke="#c9a227" stroke-width="2" stroke-dasharray="6 5" points="${polyline}"></polyline>
            <circle cx="${firstX}" cy="${firstY}" r="5" fill="#e6e8ec"></circle>
        </svg>
    `
}

function buildVehiclePopup(vehicle) {
    const ttc = vehicle.ttc == null ? "--" : `${formatNumber(vehicle.ttc, 2)} s`
    return `
        <div class="popup-card">
            <div class="popup-head">
                <strong>Vehicle ${escapeHtml(vehicle.id)}</strong>
                <span class="inline-badge" style="--badge-color:${alertColor(vehicle.alert)}">${escapeHtml(vehicle.alert || "SAFE")}</span>
            </div>
            <div class="popup-grid">
                <span>Speed</span><strong>${formatNumber(vehicle.speed, 1)} m/s</strong>
                <span>WWP</span><strong>${formatNumber(vehicle.wrong_way_probability, 2)}</strong>
                <span>TTC</span><strong>${ttc}</strong>
                <span>Temporal</span><strong>${escapeHtml(vehicle.temporal_state || "NORMAL")}</strong>
                <span>Risk</span><strong>${escapeHtml(vehicle.risk_level || "low")}</strong>
                <span>Memory</span><strong>${formatNumber(vehicle.risk_score, 2)}</strong>
                <span>Violations</span><strong>${formatNumber(vehicle.violation_count, 0)}</strong>
                <span>Collision With</span><strong>${vehicle.collision_with ?? "--"}</strong>
            </div>
        </div>
    `
}

function initMap() {
    state.map = L.map("map", {
        zoomControl: false,
        preferCanvas: true,
    }).setView([13.0827, 80.2707], 13)

    L.control.zoom({ position: "bottomright" }).addTo(state.map)

    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
        maxZoom: 20,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; CARTO',
    }).addTo(state.map)

    state.layers.roadLayer = L.layerGroup().addTo(state.map)
    state.layers.poiLayer = L.layerGroup().addTo(state.map)
    state.layers.vehicleLayer = L.layerGroup().addTo(state.map)
    state.layers.dangerLayer = L.layerGroup().addTo(state.map)
    state.layers.predictionLayer = L.layerGroup().addTo(state.map)
    state.layers.collisionLayer = L.layerGroup().addTo(state.map)
    state.layers.snapLayer = L.layerGroup().addTo(state.map)
}

function setPage(page) {
    state.currentPage = page
    document.querySelectorAll("[data-page]").forEach((section) => {
        section.hidden = section.dataset.page !== page
    })
    document.querySelectorAll(".nav-item").forEach((button) => {
        button.classList.toggle("active", button.dataset.pageTarget === page)
    })

    const meta = {
        dashboard: {
            title: "Operational Dashboard",
            subtitle: "Live vehicle intelligence, alert status, and network activity.",
        },
        simulation: {
            title: "Simulation Control",
            subtitle: "Load an area, manage runtime, and trigger controlled wrong-way scenarios.",
        },
        analytics: {
            title: "Vehicle Analytics",
            subtitle: "Per-vehicle direction intelligence, temporal state, and trajectory preview.",
        },
        risk: {
            title: "Risk Monitoring",
            subtitle: "System-wide TTC exposure, collision preview, and timeline monitoring.",
        },
        roads: {
            title: "Road Intelligence",
            subtitle: "Segment-level semantic context from topology and POI density.",
        },
        logs: {
            title: "System Logs",
            subtitle: "Detection, prediction, and alert events with filterable audit traces.",
        },
    }[page]

    dom.pageTitle.textContent = meta.title
    dom.pageSubtitle.textContent = meta.subtitle

    if (page === "dashboard" && state.map) {
        window.setTimeout(() => state.map.invalidateSize(), 0)
    }
}

function renderMapEmptyState() {
    dom.mapEmpty.classList.toggle("visible", !state.roads.length)
}

function renderRoads() {
    state.layers.roadLayer.clearLayers()
    if (!state.roads.length) {
        renderMapEmptyState()
        return
    }

    for (const road of state.roads) {
        const latlngs = (road.geometry || []).map((point) => [point.lat, point.lon])
        if (!latlngs.length) continue
        const selected = road.id === state.selectedRoadId
        const polyline = L.polyline(latlngs, {
            color: selected ? "#e6e8ec" : road.oneway ? "#c9a227" : "#4d5666",
            weight: selected ? 4 : lineWeightForRoad(road),
            opacity: selected ? 0.95 : 0.7,
            dashArray: road.oneway ? "8 6" : null,
        })
        polyline.on("click", () => {
            state.selectedRoadId = road.id
            renderRoads()
            renderRoadIntelligence()
        })
        polyline.bindPopup(`
            <div class="popup-card">
                <div class="popup-head">
                    <strong>Road ${escapeHtml(road.id)}</strong>
                    <span class="inline-badge" style="--badge-color:${road.oneway ? "#c9a227" : "#3a7d44"}">
                        ${road.oneway ? "ONE WAY" : "TWO WAY"}
                    </span>
                </div>
                <div class="popup-grid">
                    <span>Class</span><strong>${escapeHtml(road.road_class || "unknown")}</strong>
                    <span>Length</span><strong>${formatNumber(road.length, 1)} m</strong>
                    <span>POI Density</span><strong>${formatNumber(road.poi_density, 2)}</strong>
                    <span>Score</span><strong>${formatNumber(roadIntelligenceScore(road), 2)}</strong>
                </div>
            </div>
        `)
        polyline.addTo(state.layers.roadLayer)
    }
    renderMapEmptyState()
}

function renderPois() {
    state.layers.poiLayer.clearLayers()
    for (const poi of state.pois) {
        L.circleMarker([poi.lat, poi.lon], {
            radius: poi.type === "intersection" ? 4.5 : 3,
            color: poi.type === "intersection" ? "#c9a227" : "#9aa3b2",
            weight: 1,
            fillColor: poi.type === "intersection" ? "#c9a227" : "#9aa3b2",
            fillOpacity: 0.8,
        })
            .bindPopup(`
                <div class="popup-card">
                    <div class="popup-head">
                        <strong>POI ${escapeHtml(poi.id)}</strong>
                    </div>
                    <div class="popup-grid">
                        <span>Type</span><strong>${escapeHtml(poi.type)}</strong>
                        <span>Nearest Road</span><strong>${poi.nearest_road_segment_id ?? "--"}</strong>
                    </div>
                </div>
            `)
            .addTo(state.layers.poiLayer)
    }
}

function renderVehicles() {
    state.layers.vehicleLayer.clearLayers()
    state.layers.dangerLayer.clearLayers()
    state.layers.predictionLayer.clearLayers()
    state.layers.collisionLayer.clearLayers()
    state.layers.snapLayer.clearLayers()

    const byId = new Map(state.vehicles.map((vehicle) => [vehicle.id, vehicle]))
    const selected = selectedVehicle()
    const nearbyIds = new Set()
    if (selected) {
        for (const vehicle of state.vehicles) {
            if (vehicle.id === selected.id) continue
            if (distanceMeters(selected, vehicle) <= state.nearbyRadiusM) nearbyIds.add(vehicle.id)
        }
    }

    for (const vehicle of state.vehicles) {
        const selected = vehicle.id === state.selectedVehicleId
        const isWrongWay = vehicle.semantic_class === "wrong_way" || vehicle.wrong_way_flag || Number(vehicle.wrong_way_probability) >= 0.7
        const isNearby = nearbyIds.has(vehicle.id)
        const markerColor = selected ? "#e6e8ec" : isWrongWay ? "#8f4342" : isNearby ? "#727987" : "#4d5666"
        const marker = L.circleMarker([vehicle.lat, vehicle.lon], {
            radius: selected ? 8 : 6,
            color: selected ? "#ffffff" : markerColor,
            weight: selected ? 2.4 : 1.4,
            fillColor: markerColor,
            fillOpacity: 0.9,
        })
        marker.bindPopup(buildVehiclePopup(vehicle))
        marker.on("click", () => {
            state.selectedVehicleId = vehicle.id
            renderVehicleAnalytics()
            renderVehicles()
            renderVehicleIntelligencePanel()
        })
        marker.addTo(state.layers.vehicleLayer)

        const zoneRadius = mapDangerRadius(vehicle)
        if (zoneRadius > 0) {
            L.circle([vehicle.lat, vehicle.lon], {
                radius: zoneRadius,
                color: alertColor(vehicle.alert),
                weight: 1,
                fillColor: alertColor(vehicle.alert),
                fillOpacity: 0.12,
            }).addTo(state.layers.dangerLayer)
        }

        const futurePositions = vehicle.future_positions || []
        if (futurePositions.length && selected) {
            L.polyline([[vehicle.lat, vehicle.lon], ...futurePositions], {
                color: "#c9a227",
                weight: 1.2,
                opacity: 0.8,
                dashArray: "8 6",
            }).addTo(state.layers.predictionLayer)
        }

        if (vehicle.collision_with != null) {
            const other = byId.get(vehicle.collision_with)
            if (other && vehicle.id < other.id) {
                L.polyline([[vehicle.lat, vehicle.lon], [other.lat, other.lon]], {
                    color: "#b94a48",
                    weight: 1.5,
                    opacity: 0.75,
                    dashArray: "4 5",
                }).addTo(state.layers.collisionLayer)
            }
        }
    }

    for (const match of state.matches) {
        if (!Array.isArray(match.snapped_point)) continue
        const confidence = Number(match.confidence_score || 0)
        const color = confidence >= 0.8 ? "#3a7d44" : confidence >= 0.5 ? "#c9a227" : "#b94a48"
        L.circleMarker(match.snapped_point, {
            radius: 3,
            color,
            weight: 1,
            fillColor: color,
            fillOpacity: 0.9,
        })
            .bindPopup(`
                <div class="popup-card">
                    <div class="popup-head">
                        <strong>Map Match</strong>
                    </div>
                    <div class="popup-grid">
                        <span>Vehicle</span><strong>${match.vehicle_id ?? "--"}</strong>
                        <span>Road</span><strong>${match.matched_edge_id ?? "--"}</strong>
                        <span>Confidence</span><strong>${formatNumber(match.confidence_score, 2)}</strong>
                    </div>
                </div>
            `)
            .addTo(state.layers.snapLayer)
    }
}

function renderRuntimeSummary() {
    const summary = state.summary || {}
    const highRisk = state.vehicles.filter((vehicle) => ["high", "critical"].includes(String(vehicle.risk_level || "").toLowerCase())).length
    const matchRate = state.matches.length
        ? `${formatNumber((state.matches.filter((item) => item.matched_edge_id != null).length / state.matches.length) * 100, 0)}%`
        : "--"

    if (dom.runtimeSummary) {
        dom.runtimeSummary.innerHTML = `
            <div class="runtime-row"><span>Network</span><strong>${summary.has_data ? "Loaded" : "Awaiting bootstrap"}</strong></div>
            <div class="runtime-row"><span>Place</span><strong>${escapeHtml(state.currentPlace)}</strong></div>
            <div class="runtime-row"><span>Road Segments</span><strong>${formatNumber(summary.roads, 0)}</strong></div>
            <div class="runtime-row"><span>POIs</span><strong>${formatNumber(summary.pois, 0)}</strong></div>
            <div class="runtime-row"><span>One-Way Roads</span><strong>${formatNumber(summary.oneway_segments, 0)}</strong></div>
            <div class="runtime-row"><span>Map Match Rate</span><strong>${matchRate}</strong></div>
            <div class="runtime-row"><span>Simulation</span><strong>${summary.simulation_running ? "Running" : "Stopped"}</strong></div>
            <div class="runtime-row"><span>High Risk Vehicles</span><strong>${highRisk}</strong></div>
        `
    }

    dom.networkState.textContent = summary.has_data ? "Network loaded" : "No network loaded"
    dom.simulationState.textContent = summary.simulation_running ? "Simulation running" : "Simulation stopped"
    dom.lastSync.textContent = new Date().toLocaleTimeString()
}

function renderMetrics() {
    const wrongWay = state.vehicles.filter((vehicle) => vehicle.semantic_class === "wrong_way" || vehicle.wrong_way_flag).length
    const highRisk = state.vehicles.filter((vehicle) => ["high", "critical"].includes(String(vehicle.risk_level || "").toLowerCase())).length
    const ttcValues = state.vehicles.map((vehicle) => Number(vehicle.ttc)).filter((value) => Number.isFinite(value))
    const avgTtcValue = ttcValues.length ? ttcValues.reduce((sum, value) => sum + value, 0) / ttcValues.length : null

    dom.activeVehicles.textContent = String(state.vehicles.length)
    dom.wrongWayCount.textContent = String(wrongWay)
    dom.highRiskCount.textContent = String(highRisk)
    dom.avgTtc.textContent = avgTtcValue == null ? "--" : `${formatNumber(avgTtcValue, 2)} s`
    dom.analyticsCount.textContent = String(state.vehicles.length)
    if (dom.vehicleCount && String(dom.vehicleCount.value) !== String(state.vehicleCount)) {
        dom.vehicleCount.value = String(state.vehicleCount)
    }
    if (dom.vehicleCountValue) {
        dom.vehicleCountValue.textContent = String(state.vehicleCount)
    }
    if (dom.speedVariationToggle) {
        dom.speedVariationToggle.checked = state.speedVariation
    }
    if (dom.vehicleControlSelect) {
        dom.vehicleControlSelect.innerHTML = `<option value="">Select vehicle ID</option>${state.vehicles
            .map((vehicle) => `<option value="${vehicle.id}" ${vehicle.id === state.selectedVehicleId ? "selected" : ""}>Vehicle ${vehicle.id}</option>`)
            .join("")}`
    }
}

function renderEventFeed() {
    if (!dom.eventFeed) return
    const items = state.logs.slice(0, 10)
    if (!items.length) {
        dom.eventFeed.innerHTML = '<li class="feed-empty">No events recorded yet.</li>'
        return
    }

    dom.eventFeed.innerHTML = items
        .map((item) => `
            <li class="feed-item">
                <span class="feed-time">${new Date(item.timestamp).toLocaleTimeString()}</span>
                <div>
                    <strong class="severity severity-${escapeHtml(item.severity)}">${escapeHtml(item.severity.toUpperCase())}</strong>
                    <p>${escapeHtml(item.message)}</p>
                </div>
            </li>
        `)
        .join("")
}

function recordVehicleHistory() {
    for (const vehicle of state.vehicles) {
        const history = state.vehicleHistory.get(vehicle.id) || []
        history.push({
            ts: Date.now(),
            value: Number(vehicle.wrong_way_probability || 0),
            risk: Number(vehicle.risk_score || 0),
            directionScore: Number(vehicle.direction_score || 0),
            ttc: Number.isFinite(Number(vehicle.ttc)) ? Number(vehicle.ttc) : null,
        })
        if (history.length > 24) history.shift()
        state.vehicleHistory.set(vehicle.id, history)
    }
}

function recordRiskTimeline() {
    const snapshot = {
        ts: Date.now(),
        critical: state.vehicles.filter((vehicle) => String(vehicle.risk_level || "").toLowerCase() === "critical").length,
        high: state.vehicles.filter((vehicle) => String(vehicle.risk_level || "").toLowerCase() === "high").length,
        medium: state.vehicles.filter((vehicle) => String(vehicle.risk_level || "").toLowerCase() === "medium").length,
    }
    state.riskTimeline.push(snapshot)
    if (state.riskTimeline.length > 24) state.riskTimeline.shift()
}

function emitPipelineLogs() {
    for (const vehicle of state.vehicles) {
        const current = {
            alert: vehicle.alert || "SAFE",
            temporalState: vehicle.temporal_state || "NORMAL",
            collisionWith: vehicle.collision_with ?? null,
        }
        const previous = state.lastAlertState.get(vehicle.id)

        if (!previous || previous.alert !== current.alert) {
            const ttcNote = vehicle.ttc == null ? "" : ` (TTC ${formatNumber(vehicle.ttc, 2)} s)`
            addLog(
                current.alert === "COLLISION_ALERT" ? "critical" : current.alert === "HIGH_ALERT" ? "warning" : "info",
                `Vehicle ${vehicle.id} changed alert state to ${current.alert}${ttcNote}.`,
                { vehicleId: vehicle.id },
            )
        } else if (!previous || previous.temporalState !== current.temporalState) {
            addLog(
                current.temporalState === "CONFIRMED" ? "warning" : "info",
                `Vehicle ${vehicle.id} temporal state is ${current.temporalState}.`,
                { vehicleId: vehicle.id },
            )
        }

        if (current.collisionWith != null && (!previous || previous.collisionWith !== current.collisionWith)) {
            addLog(
                "critical",
                `Vehicle ${vehicle.id} has a collision candidate with vehicle ${current.collisionWith}.`,
                { vehicleId: vehicle.id },
            )
        }

        state.lastAlertState.set(vehicle.id, current)
    }
}

function renderVehicleIntelligencePanel() {
    if (!dom.vehicleIntelligence) return
    const vehicle = selectedVehicle()
    if (!vehicle) {
        dom.vehicleIntelligence.innerHTML = '<div class="detail-empty">Select a vehicle from the map to view intelligence context.</div>'
        return
    }

    const nearby = state.vehicles
        .filter((item) => item.id !== vehicle.id && distanceMeters(vehicle, item) <= state.nearbyRadiusM)
        .sort((a, b) => distanceMeters(vehicle, a) - distanceMeters(vehicle, b))
    const closestDistance = nearby.length ? distanceMeters(vehicle, nearby[0]) : null
    const relativeVelocity = nearby.length
        ? Math.max(...nearby.map((item) => Math.abs(Number(vehicle.speed || 0) - Number(item.speed || 0))))
        : 0
    const riskAmp = Number(vehicle.poi_density || 0) > 0.45 ? 0.2 : 0.1

    dom.vehicleIntelligence.innerHTML = `
        <div class="detail-section">
            <h4>Vehicle Summary</h4>
            <div class="detail-grid">
                <span>Vehicle ID</span><strong>${vehicle.id}</strong>
                <span>Speed</span><strong>${formatNumber(vehicle.speed, 1)} m/s</strong>
                <span>State</span><strong>${escapeHtml(vehicle.temporal_state || "NORMAL")}</strong>
            </div>
        </div>
        <div class="detail-section">
            <h4>Direction Intelligence</h4>
            <div class="detail-grid">
                <span>Direction Score</span><strong>${formatNumber(vehicle.direction_score, 2)}</strong>
                <span>WWP</span><strong>${formatNumber(vehicle.wrong_way_probability, 2)} (${Number(vehicle.wrong_way_probability) >= 0.8 ? "High Confidence" : "Monitoring"})</strong>
            </div>
        </div>
        <div class="detail-section">
            <h4>Temporal Analysis</h4>
            <div class="detail-grid">
                <span>State</span><strong>${escapeHtml(vehicle.temporal_state || "NORMAL")}</strong>
                <span>Violation Duration</span><strong>${formatNumber(vehicle.sustained_duration_s, 1)} s</strong>
                <span>Stability</span><strong>${vehicle.is_stable ? "High" : "Dynamic"}</strong>
            </div>
        </div>
        <div class="detail-section">
            <h4>Risk Metrics</h4>
            <div class="detail-grid">
                <span>TTC</span><strong>${vehicle.ttc == null ? "--" : `${formatNumber(vehicle.ttc, 2)} s`}</strong>
                <span>Risk Level</span><strong>${escapeHtml(String(vehicle.risk_level || "low").toUpperCase())}</strong>
                <span>Maneuverability</span><strong>${formatNumber(vehicle.maneuverability, 2, "--")} ${Number(vehicle.maneuverability) < 0.4 ? "(Low Escape Space)" : ""}</strong>
            </div>
        </div>
        <div class="detail-section">
            <h4>Spatial Awareness</h4>
            <div class="detail-grid">
                <span>Nearby Vehicles</span><strong>${nearby.length}</strong>
                <span>Closest Distance</span><strong>${closestDistance == null ? "--" : `${formatNumber(closestDistance, 1)} m`}</strong>
                <span>Relative Velocity</span><strong>${relativeVelocity > 4 ? "High" : relativeVelocity > 2 ? "Medium" : "Low"}</strong>
            </div>
        </div>
        <div class="detail-section">
            <h4>Semantic Road Intelligence</h4>
            <div class="detail-grid">
                <span>Road Type</span><strong>${escapeHtml(vehicle.road_class || "Urban Dense")}</strong>
                <span>POI Density</span><strong>${Number(vehicle.poi_density || 0) > 0.45 ? "High" : "Moderate"}</strong>
                <span>Risk Amplifier</span><strong>+${formatNumber(riskAmp, 1)}</strong>
            </div>
        </div>
        <div class="detail-section">
            <h4>Prediction</h4>
            ${buildPredictionMiniMap(vehicle)}
            <div class="detail-grid">
                <span>Future Steps</span><strong>${formatNumber((vehicle.future_positions || []).length, 0)}</strong>
                <span>Estimated Collision Point</span><strong>${vehicle.collision_with == null ? "--" : `V${vehicle.collision_with}`}</strong>
            </div>
        </div>
    `
}

function renderVehicleAnalytics() {
    const vehicles = [...state.vehicles].sort((a, b) => {
        const alertDelta = alertRank(b.alert) - alertRank(a.alert)
        if (alertDelta !== 0) return alertDelta
        return Number(b.risk_score || 0) - Number(a.risk_score || 0)
    })

    if (!state.selectedVehicleId && vehicles.length) {
        state.selectedVehicleId = vehicles[0].id
    }

    dom.vehicleTable.innerHTML = vehicles.length
        ? vehicles.map((vehicle) => `
            <tr class="${vehicle.id === state.selectedVehicleId ? "selected-row" : ""}" data-vehicle-row="${vehicle.id}">
                <td>${vehicle.id}</td>
                <td>${formatNumber(vehicle.speed, 1)}</td>
                <td>${formatNumber(vehicle.wrong_way_probability, 2)}</td>
                <td>${escapeHtml(vehicle.risk_level || "low")}</td>
                <td>${escapeHtml(vehicle.temporal_state || "NORMAL")}</td>
            </tr>
        `).join("")
        : '<tr><td colspan="5" class="table-empty">No vehicle telemetry available.</td></tr>'

    document.querySelectorAll("[data-vehicle-row]").forEach((row) => {
        row.addEventListener("click", () => {
            state.selectedVehicleId = Number(row.dataset.vehicleRow)
            renderVehicleAnalytics()
            renderVehicles()
            renderVehicleIntelligencePanel()
        })
    })

    const vehicle = selectedVehicle()
    if (!vehicle) {
        dom.vehicleDetail.innerHTML = '<div class="detail-empty">Select a vehicle to inspect its pipeline state.</div>'
        return
    }

    const series = state.vehicleHistory.get(vehicle.id) || []
    const directionSeries = series.map((item) => ({ value: item.directionScore }))
    const ttcSeries = series.map((item) => ({ value: item.ttc == null ? 0 : item.ttc }))
    const riskSeries = series.map((item) => ({ value: item.risk }))

    dom.vehicleDetail.innerHTML = `
        <div class="detail-header">
            <div>
                <h3>Vehicle ${escapeHtml(vehicle.id)}</h3>
                <p>Selected for detailed analytics.</p>
            </div>
            <span class="inline-badge" style="--badge-color:${alertColor(vehicle.alert)}">${escapeHtml(vehicle.alert || "SAFE")}</span>
        </div>

        <div class="detail-section">
            <h4>Direction Score Over Time</h4>
            <div class="detail-grid">
                <span>Direction Score</span><strong>${formatNumber(vehicle.direction_score, 2)}</strong>
                <span>Wrong-Way Probability</span><strong>${formatNumber(vehicle.wrong_way_probability, 2)}</strong>
                <span>Alignment</span><strong>${formatNumber(vehicle.alignment, 2)}</strong>
                <span>Bearing</span><strong>${formatNumber(vehicle.bearing, 1)} deg</strong>
            </div>
            ${buildSparkline(directionSeries, "#c9a227")}
        </div>

        <div class="detail-section">
            <h4>Temporal Analysis</h4>
            <div class="detail-grid">
                <span>State</span><strong>${escapeHtml(vehicle.temporal_state || "NORMAL")}</strong>
                <span>Sustained Duration</span><strong>${formatNumber(vehicle.sustained_duration_s, 2)} s</strong>
                <span>Stability</span><strong>${vehicle.is_stable ? "Stable" : "Dynamic"}</strong>
                <span>Violation Count</span><strong>${formatNumber(vehicle.violation_count, 0)}</strong>
            </div>
        </div>

        <div class="detail-section">
            <h4>TTC Over Time</h4>
            ${buildSparkline(ttcSeries, "#8aa4ff")}
            <div class="detail-grid">
                <span>Current TTC</span><strong>${vehicle.ttc == null ? "--" : `${formatNumber(vehicle.ttc, 2)} s`}</strong>
                <span>Prediction State</span><strong>${escapeHtml((vehicle.prediction_state || []).join(", ") || "--")}</strong>
            </div>
        </div>

        <div class="detail-section">
            <h4>Risk Evolution</h4>
            <div class="detail-grid">
                <span>Spatial Risk</span><strong>${escapeHtml(vehicle.risk || "safe")}</strong>
                <span>Memory Score</span><strong>${formatNumber(vehicle.risk_score, 2)}</strong>
                <span>Final Risk</span><strong>${escapeHtml(vehicle.risk_level || "low")}</strong>
                <span>Future Steps</span><strong>${formatNumber((vehicle.future_positions || []).length, 0)}</strong>
            </div>
            ${buildSparkline(riskSeries, "#b94a48")}
        </div>
    `
}

function renderRiskMonitoring() {
    const vehicles = [...state.vehicles].sort((a, b) => {
        const riskDelta = riskRank(String(b.risk_level || "").toLowerCase()) - riskRank(String(a.risk_level || "").toLowerCase())
        if (riskDelta !== 0) return riskDelta
        const ttcA = Number.isFinite(Number(a.ttc)) ? Number(a.ttc) : 999
        const ttcB = Number.isFinite(Number(b.ttc)) ? Number(b.ttc) : 999
        return ttcA - ttcB
    })

    dom.riskTable.innerHTML = vehicles.length
        ? vehicles.map((vehicle) => `
            <tr>
                <td>${vehicle.id}</td>
                <td>${vehicle.ttc == null ? "--" : `${formatNumber(vehicle.ttc, 2)} s`}</td>
                <td>${formatNumber(vehicle.risk_score, 2)}</td>
                <td><span class="inline-badge" style="--badge-color:${riskColor(vehicle.risk_level)}">${escapeHtml(vehicle.risk_level || "low")}</span></td>
            </tr>
        `).join("")
        : '<tr><td colspan="4" class="table-empty">No risk telemetry available.</td></tr>'

    if (!state.riskTimeline.length) {
        dom.riskTimeline.innerHTML = '<div class="detail-empty">Risk timeline will appear once live data is streaming.</div>'
    } else {
        const maxCount = Math.max(...state.riskTimeline.flatMap((item) => [item.critical, item.high, item.medium]), 1)
        dom.riskTimeline.innerHTML = state.riskTimeline
            .map((item) => `
                <div class="timeline-row">
                    <span>${new Date(item.ts).toLocaleTimeString()}</span>
                    <div class="timeline-bars">
                        <i style="width:${(item.critical / maxCount) * 100}%; background:#b94a48"></i>
                        <i style="width:${(item.high / maxCount) * 100}%; background:#8a4f4d"></i>
                        <i style="width:${(item.medium / maxCount) * 100}%; background:#c9a227"></i>
                    </div>
                </div>
            `)
            .join("")
    }

    const collisionPairs = []
    const seen = new Set()
    for (const vehicle of state.vehicles) {
        if (vehicle.collision_with == null) continue
        const key = [vehicle.id, vehicle.collision_with].sort((a, b) => a - b).join("-")
        if (seen.has(key)) continue
        seen.add(key)
        collisionPairs.push(vehicle)
    }

    dom.collisionPreview.innerHTML = collisionPairs.length
        ? collisionPairs.map((vehicle) => `
            <li>
                <strong>Vehicle ${vehicle.id}</strong>
                <span>Projected interaction with vehicle ${vehicle.collision_with}.</span>
                <span>TTC ${vehicle.ttc == null ? "--" : `${formatNumber(vehicle.ttc, 2)} s`}</span>
            </li>
        `).join("")
        : '<li class="feed-empty">No collision links currently predicted.</li>'
}

function renderRoadIntelligence() {
    const rows = [...state.roads]
        .map((road) => ({ ...road, intelligence_score: roadIntelligenceScore(road) }))
        .sort((a, b) => b.intelligence_score - a.intelligence_score)
        .slice(0, 30)

    if (dom.roadHeatmap) {
        const heatRows = rows.slice(0, 8)
        dom.roadHeatmap.innerHTML = heatRows.length
            ? `
                <h4>Muted Heatmap View</h4>
                <div class="timeline-list">
                    ${heatRows.map((road) => `
                        <div class="timeline-row">
                            <span>Road ${road.id}</span>
                            <div class="timeline-bars">
                                <i style="width:${Math.min(100, road.intelligence_score * 10)}%; background:#556074"></i>
                            </div>
                        </div>
                    `).join("")}
                </div>
            `
            : '<div class="detail-empty">Heatmap becomes available after loading roads.</div>'
    }

    if (!state.selectedRoadId && rows.length) {
        state.selectedRoadId = rows[0].id
    }

    dom.roadTable.innerHTML = rows.length
        ? rows.map((road) => `
            <tr class="${road.id === state.selectedRoadId ? "selected-row" : ""}" data-road-row="${road.id}">
                <td>${road.id}</td>
                <td>${formatNumber(road.poi_density, 2)}</td>
                <td>${formatNumber(road.intelligence_score, 2)}</td>
                <td>${escapeHtml(roadComplexityLabel(road.intelligence_score))}</td>
            </tr>
        `).join("")
        : '<tr><td colspan="4" class="table-empty">No road network loaded.</td></tr>'

    document.querySelectorAll("[data-road-row]").forEach((row) => {
        row.addEventListener("click", () => {
            state.selectedRoadId = Number(row.dataset.roadRow)
            renderRoadIntelligence()
            renderRoads()
        })
    })

    const road = selectedRoad()
    if (!road) {
        dom.roadExplanation.innerHTML = '<div class="detail-empty">Load a road network to inspect semantic intelligence.</div>'
        return
    }

    const score = roadIntelligenceScore(road)
    dom.roadExplanation.innerHTML = `
        <div class="detail-header">
            <div>
                <h3>Road ${road.id}</h3>
                <p>Semantic explanation for current road segment selection.</p>
            </div>
            <span class="inline-badge" style="--badge-color:${score >= 7.5 ? "#b94a48" : score >= 4.5 ? "#c9a227" : "#3a7d44"}">
                ${roadComplexityLabel(score)}
            </span>
        </div>
        <div class="detail-section">
            <h4>Context</h4>
            <div class="detail-grid">
                <span>Road Class</span><strong>${escapeHtml(road.road_class || "unknown")}</strong>
                <span>Length</span><strong>${formatNumber(road.length, 1)} m</strong>
                <span>POI Density</span><strong>${formatNumber(road.poi_density, 2)}</strong>
                <span>Direction Rule</span><strong>${road.oneway ? "One-way enforced" : "Bi-directional"}</strong>
            </div>
        </div>
        <div class="detail-section">
            <h4>Explanation</h4>
            <p class="explanation-copy">
                Score ${formatNumber(score, 2)} reflects road hierarchy, one-way constraints, and nearby activity density.
                Segments with stronger directional constraints and higher POI concentration are treated as operationally more sensitive.
            </p>
        </div>
    `
}

function renderLogsPage() {
    if (!dom.logsList || !dom.logSeverity || !dom.logVehicle) return
    const severityFilter = dom.logSeverity.value || "all"
    const vehicleFilter = dom.logVehicle.value.trim()

    const items = state.logs.filter((item) => {
        if (severityFilter !== "all" && item.severity !== severityFilter) return false
        if (vehicleFilter && String(item.vehicleId || "") !== vehicleFilter) return false
        return true
    })

    dom.logsList.innerHTML = items.length
        ? items.map((item) => `
            <li class="log-item">
                <div class="log-meta">
                    <span>${new Date(item.timestamp).toLocaleTimeString()}</span>
                    <strong class="severity severity-${escapeHtml(item.severity)}">${escapeHtml(item.severity.toUpperCase())}</strong>
                    <span>${item.vehicleId == null ? "System" : `Vehicle ${item.vehicleId}`}</span>
                </div>
                <p>${escapeHtml(item.message)}</p>
            </li>
        `).join("")
        : '<li class="feed-empty">No logs match the selected filters.</li>'
}

function renderSearchResults() {
    if (!state.searchCandidates.length) {
        dom.searchResults.innerHTML = '<li class="feed-empty">Search results will appear here.</li>'
        dom.candidateMeta.textContent = "Search for a location and choose the exact match before loading."
        return
    }

    dom.searchResults.innerHTML = state.searchCandidates.map((candidate) => `
        <li class="candidate-item ${candidate.id === state.selectedCandidateId ? "selected" : ""}" data-candidate-id="${escapeHtml(candidate.id)}">
            <button type="button" class="candidate-button">
                <strong>${escapeHtml(candidate.display_name)}</strong>
                <span>${escapeHtml(candidate.geometry_type || "Unknown")} | ${escapeHtml(candidate.match_mode || "point")}</span>
            </button>
        </li>
    `).join("")

    document.querySelectorAll("[data-candidate-id]").forEach((item) => {
        item.addEventListener("click", () => {
            state.selectedCandidateId = item.dataset.candidateId
            renderSearchResults()
        })
    })

    dom.candidateMeta.textContent = `${state.searchCandidates.length} candidate location(s) resolved.`
}

async function loadStaticLayers(fit = false, center = null) {
    const [roads, pois] = await Promise.all([requestJSON("/api/roads"), requestJSON("/api/pois")])
    state.roads = Array.isArray(roads) ? roads : []
    state.pois = Array.isArray(pois) ? pois : []
    renderRoads()
    renderPois()

    if (fit && center && state.map) {
        state.map.setView([center.center_lat, center.center_lon], 15)
    } else if (fit && state.roads.length && state.map) {
        const coordinates = state.roads.flatMap((road) => (road.geometry || []).map((point) => [point.lat, point.lon]))
        if (coordinates.length) {
            const bounds = L.latLngBounds(coordinates)
            if (bounds.isValid()) {
                state.map.fitBounds(bounds.pad(0.08))
            }
        }
    }
}

async function searchLocations() {
    const query = dom.scenarioQuery.value.trim()
    if (!query) {
        setStatus("Enter a street, area, or coordinates before searching.", "warning")
        return
    }

    setStatus("Searching candidate locations...", "neutral")
    try {
        const result = await requestJSON("/api/admin/location-search", {
            method: "POST",
            body: JSON.stringify({ query, limit: 6 }),
        })
        state.searchCandidates = result.candidates || []
        state.selectedCandidateId = state.searchCandidates[0]?.id || null
        renderSearchResults()
        setStatus("Candidate search completed.", "success")
        addLog("info", `Resolved ${state.searchCandidates.length} candidate locations for "${query}".`)
    } catch (error) {
        setStatus(error.message, "danger")
        addLog("warning", `Location search failed: ${error.message}`)
    }
}

async function loadStreetArea() {
    const query = dom.scenarioQuery.value.trim()
    if (!query) {
        setStatus("Enter a street or area before loading the network.", "warning")
        return
    }

    const selection = state.searchCandidates.find((candidate) => candidate.id === state.selectedCandidateId) || null
    setStatus("Loading selected road network...", "neutral")
    dom.responseConsole.textContent = "Bootstrapping selected area and refreshing the live pipeline."

    try {
        const result = await requestJSON("/api/admin/bootstrap", {
            method: "POST",
            body: JSON.stringify({
                query,
                query_type: "auto",
                radius_m: Number(dom.scenarioRadius.value || 700),
                reset: true,
                selection,
            }),
        })

        state.currentPlace = result.resolved_query || selection?.display_name || query
        await loadStaticLayers(true, result)
        await refreshSnapshot()
        setStatus("Road network loaded and simulation started.", "success")
        dom.responseConsole.textContent = JSON.stringify(result, null, 2)
        addLog("info", `Loaded road network for ${state.currentPlace}.`)
    } catch (error) {
        setStatus(error.message, "danger")
        dom.responseConsole.textContent = error.message
        addLog("critical", `Bootstrap failed: ${error.message}`)
    }
}

async function startSimulation() {
    try {
        setStatus("Starting simulation...", "neutral")
        const result = await requestJSON("/api/admin/simulation/start", { method: "POST" })
        setStatus("Simulation running.", "success")
        dom.responseConsole.textContent = JSON.stringify(result, null, 2)
        addLog("info", "Simulation started.")
        await refreshSnapshot()
    } catch (error) {
        setStatus(error.message, "danger")
        dom.responseConsole.textContent = error.message
        addLog("warning", `Simulation start failed: ${error.message}`)
    }
}

async function stopSimulation() {
    try {
        setStatus("Stopping simulation...", "neutral")
        const result = await requestJSON("/api/admin/simulation/stop", { method: "POST" })
        setStatus("Simulation stopped.", "warning")
        dom.responseConsole.textContent = JSON.stringify(result, null, 2)
        addLog("warning", "Simulation stopped and fleet state cleared.")
        await refreshSnapshot()
    } catch (error) {
        setStatus(error.message, "danger")
        dom.responseConsole.textContent = error.message
        addLog("warning", `Simulation stop failed: ${error.message}`)
    }
}

async function resetSimulation() {
    await stopSimulation()
    await startSimulation()
}

async function triggerWrongWayScenario() {
    try {
        setStatus("Triggering wrong-way scenario...", "neutral")
        const result = await requestJSON("/api/admin/scenarios/wrong-way", {
            method: "POST",
            body: JSON.stringify({}),
        })
        setStatus("Wrong-way scenario injected.", "warning")
        dom.responseConsole.textContent = JSON.stringify(result, null, 2)
        addLog("warning", "Wrong-way scenario injected into the live simulation.")
        await refreshSnapshot()
    } catch (error) {
        setStatus(error.message, "danger")
        dom.responseConsole.textContent = error.message
        addLog("critical", `Wrong-way scenario failed: ${error.message}`)
    }
}

async function updateSimulationConfig() {
    try {
        const result = await requestJSON("/api/admin/simulation/config", {
            method: "POST",
            body: JSON.stringify({
                vehicle_count: state.vehicleCount,
                speed_variation_enabled: state.speedVariation,
            }),
        })
        if (result.vehicle_count != null) {
            state.vehicleCount = Number(result.vehicle_count)
            if (dom.vehicleCount) dom.vehicleCount.value = String(state.vehicleCount)
            if (dom.vehicleCountValue) dom.vehicleCountValue.textContent = String(state.vehicleCount)
        }
        if (result.speed_variation_enabled != null) {
            state.speedVariation = Boolean(result.speed_variation_enabled)
            if (dom.speedVariationToggle) dom.speedVariationToggle.checked = state.speedVariation
        }
    } catch (error) {
        addLog("warning", `Simulation config update failed: ${error.message}`)
    }
}

async function refreshSnapshot() {
    if (state.isRefreshing) return
    state.isRefreshing = true

    try {
        const summaryPromise = requestJSON("/api/summary")
        let livePayload

        try {
            livePayload = await requestJSON("/api/direction/live")
        } catch (error) {
            addLog("warning", `Direction endpoint fallback engaged: ${error.message}`)
            const fallbackVehicles = await requestJSON("/api/vehicles")
            livePayload = {
                direction: Array.isArray(fallbackVehicles) ? fallbackVehicles : [],
                matches: [],
            }
        }

        state.summary = await summaryPromise
        state.vehicles = Array.isArray(livePayload.direction) ? livePayload.direction : []
        state.matches = Array.isArray(livePayload.matches) ? livePayload.matches : []
        state.vehicleCount = Number(state.summary?.vehicle_count_target ?? state.vehicleCount)
        state.speedVariation = Boolean(state.summary?.speed_variation_enabled ?? state.speedVariation)
        state.nearbyRadiusM = Number(state.summary?.nearby_radius_m ?? state.nearbyRadiusM)

        recordVehicleHistory()
        recordRiskTimeline()
        emitPipelineLogs()
        renderMetrics()
        renderRuntimeSummary()
        renderVehicles()
        renderVehicleAnalytics()
        renderVehicleIntelligencePanel()
        renderRiskMonitoring()
        renderRoadIntelligence()
        renderMapEmptyState()

        if (!state.roads.length && state.summary?.has_data) {
            await loadStaticLayers(false)
        }

        setStatus(
            state.summary?.has_data ? "Live pipeline synchronized." : "No road network loaded yet.",
            state.summary?.has_data ? "success" : "warning",
        )
    } catch (error) {
        setStatus(error.message, "danger")
        addLog("critical", `Snapshot refresh failed: ${error.message}`)
    } finally {
        state.isRefreshing = false
    }
}

function bindControls() {
    document.querySelectorAll(".nav-item").forEach((button) => {
        button.addEventListener("click", () => setPage(button.dataset.pageTarget))
    })

    $("search-locations").addEventListener("click", searchLocations)
    $("load-street").addEventListener("click", loadStreetArea)
    $("start-simulation").addEventListener("click", startSimulation)
    $("stop-simulation").addEventListener("click", stopSimulation)
    $("reset-simulation").addEventListener("click", resetSimulation)
    $("run-wrong-way").addEventListener("click", triggerWrongWayScenario)

    if (dom.logSeverity && dom.logVehicle) {
        dom.logSeverity.addEventListener("change", renderLogsPage)
        dom.logVehicle.addEventListener("input", renderLogsPage)
    }

    dom.scenarioPreset.addEventListener("change", () => {
        if (dom.scenarioPreset.value === "wrong-way-event") {
            dom.vehicleWrongWayToggle.checked = true
            addLog("warning", "Scenario switched to wrong-way event mode.")
            return
        }
        if (dom.scenarioPreset.value === "collision-scenario") {
            addLog("warning", "Scenario switched to collision scenario mode.")
            return
        }
        dom.scenarioQuery.value = state.currentPlace
        addLog("info", "Scenario mode switched to normal.")
    })

    if (dom.locationSelector) {
        dom.locationSelector.addEventListener("change", () => {
            dom.scenarioQuery.value = dom.locationSelector.value === "chennai"
                ? state.currentPlace
                : "13.0827,80.2707"
        })
    }
    if (dom.vehicleCount && dom.vehicleCountValue) {
        dom.vehicleCount.addEventListener("input", () => {
            state.vehicleCount = Number(dom.vehicleCount.value)
            dom.vehicleCountValue.textContent = dom.vehicleCount.value
        })
        dom.vehicleCount.addEventListener("change", updateSimulationConfig)
    }
    if (dom.speedVariationToggle) {
        dom.speedVariationToggle.addEventListener("change", () => {
            state.speedVariation = dom.speedVariationToggle.checked
            updateSimulationConfig()
        })
    }
    if (dom.vehicleWrongWayToggle) {
        dom.vehicleWrongWayToggle.addEventListener("change", async () => {
            if (dom.vehicleWrongWayToggle.checked) {
                await triggerWrongWayScenario()
            }
        })
    }
    if (dom.vehicleControlSelect) {
        dom.vehicleControlSelect.addEventListener("change", () => {
            if (!dom.vehicleControlSelect.value) return
            state.selectedVehicleId = Number(dom.vehicleControlSelect.value)
            renderVehicles()
            renderVehicleAnalytics()
            renderVehicleIntelligencePanel()
        })
    }
}

async function bootstrapView() {
    cacheDom()
    initMap()
    bindControls()
    renderSearchResults()
    renderEventFeed()
    renderLogsPage()
    setPage("dashboard")
    if (dom.vehicleCountValue && dom.vehicleCount) {
        dom.vehicleCountValue.textContent = dom.vehicleCount.value
    }

    try {
        state.summary = await requestJSON("/api/summary")
        if (state.summary.has_data) {
            await loadStaticLayers(true)
        }
    } catch (error) {
        addLog("warning", `Initial summary load failed: ${error.message}`)
    }

    await refreshSnapshot()
    state.pollHandle = window.setInterval(refreshSnapshot, state.pollIntervalMs)
}

document.addEventListener("DOMContentLoaded", bootstrapView)
