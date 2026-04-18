/**
 * FlowGuard Control System — core logic
 * Selection-driven intelligence, efficient map rendering, and structured routing.
 */

const state = {
    // Current data
    vehicles: [],
    matches: [],
    roads: [],
    summary: null,

    // UI state
    currentPage: 'dashboard',
    selectedVehicleId: null,
    simulationRunning: false,
    
    // Cache for markers and paths
    markers: new Map(), // vehicleId -> Leaflet marker
    paths: new Map(),   // vehicleId -> Leaflet polyline (predicted)
    conflicts: [],      // array of conflict polylines
    
    // History for sparklines
    history: new Map(), // vehicleId -> { wwp: [], ttc: [] }
    
    // Search state
    searchCandidates: [],
    selectedCandidateId: null
};

// ── Helpers ──
const $ = id => document.getElementById(id);
const formatNum = (v, d = 0) => (v != null && isFinite(v)) ? Number(v).toFixed(d) : '--';
const escape = (v) => String(v || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const buildSparkline = (data, color = '#e6e8ec', height = 30) => {
    if (!data || data.length < 2) return '';
    const max = Math.max(...data, 1.0);
    const width = 280;
    const points = data.map((v, i) => `${(i / (data.length - 1)) * width},${height - (v / max) * height}`).join(' ');
    return `<svg width="${width}" height="${height}" style="margin-top:8px; display:block;"><polyline fill="none" stroke="${color}" stroke-width="1.5" points="${points}" /></svg>`;
};

// ── API Module ──
const api = {
    async request(url, options = {}) {
        try {
            const resp = await fetch(url, {
                headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
                ...options
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || `HTTP error ${resp.status}`);
            return data;
        } catch (err) {
            console.error(`API Error [${url}]:`, err);
            throw err;
        }
    },
    
    async fetchLive() { return this.request('/api/direction/live'); },
    async fetchSummary() { return this.request('/api/summary'); },
    async fetchRoads() { return this.request('/api/roads'); },
    
    async cmd(action, body = {}) {
        const urlMap = {
            start: '/api/admin/simulation/start',
            stop: '/api/admin/simulation/stop',
            search: '/api/admin/location-search',
            bootstrap: '/api/admin/bootstrap',
            inject: '/api/admin/scenarios/wrong-way'
        };
        return this.request(urlMap[action], { method: 'POST', body: JSON.stringify(body) });
    }
};

// ── Router ──
function navigate(page) {
    state.currentPage = page;
    document.querySelectorAll('.page').forEach(el => el.hidden = (el.id !== `page-${page}`));
    document.querySelectorAll('.nav-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.page === page));
    
    // Invalidate map size if switching to dashboard
    if (page === 'dashboard' && window.map) {
        setTimeout(() => window.map.invalidateSize(), 10);
    }
    
    // Full render of tables when switching pages
    if (page === 'analytics') ui.renderAnalyticsList();
    if (page === 'risk') ui.renderRiskList();
}

// ── Map Module ──
const flowMap = {
    init() {
        window.map = L.map('map', { zoomControl: false, preferCanvas: true }).setView([13.0827, 80.2707], 13);
        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            attribution: '&copy; CARTO'
        }).addTo(window.map);
        
        state.roadLayer = L.layerGroup().addTo(window.map);
        state.pathLayer = L.layerGroup().addTo(window.map);
        state.conflictLayer = L.layerGroup().addTo(window.map);
        state.vehicleLayer = L.layerGroup().addTo(window.map);
    },

    updateVehicles(vehicles) {
        const currentIds = new Set(vehicles.map(v => v.id));
        
        // Remove gone vehicles
        for (const [id, marker] of state.markers) {
            if (!currentIds.has(id)) {
                state.vehicleLayer.removeLayer(marker);
                state.markers.delete(id);
            }
        }

        for (const v of vehicles) {
            const isSelected = v.id === state.selectedVehicleId;
            const isWrongWay = v.wrong_way_probability >= 0.7;
            const color = isSelected ? '#ffffff' : (isWrongWay ? '#8f4342' : '#4d5666');
            const radius = isSelected ? 8 : 6;
            const weight = isSelected ? 2.5 : 1.5;

            if (state.markers.has(v.id)) {
                // UPDATE POSITION ONLY
                const marker = state.markers.get(v.id);
                marker.setLatLng([v.lat, v.lon]);
                marker.setStyle({ color: isSelected ? '#ffffff' : color, fillColor: color, radius, weight });
            } else {
                // CREATE NEW MARKER
                const marker = L.circleMarker([v.lat, v.lon], {
                    radius, color: isSelected ? '#ffffff' : color, 
                    weight, fillColor: color, fillOpacity: 0.9
                });
                
                // HOVER: quick info
                marker.on('mouseover', (e) => {
                    marker.bindTooltip(`<b>Vehicle ${v.id}</b><br>Speed: ${v.speed.toFixed(1)}m/s`, { direction: 'top' }).openTooltip();
                });
                
                // CLICK: select
                marker.on('click', () => {
                    ui.selectVehicle(v.id);
                });

                marker.addTo(state.vehicleLayer);
                state.markers.set(v.id, marker);
            }

            // PREDICTIVE PATHS
            if (isSelected && v.future_positions) {
                if (state.paths.has(v.id)) state.pathLayer.removeLayer(state.paths.get(v.id));
                const path = L.polyline(v.future_positions, { color: '#c9a227', weight: 1.5, dashArray: '4, 4', opacity: 0.8 });
                path.addTo(state.pathLayer);
                state.paths.set(v.id, path);
            } else if (state.paths.has(v.id)) {
                state.pathLayer.removeLayer(state.paths.get(v.id));
                state.paths.delete(v.id);
            }
        }

        // CONFLICT LINES
        state.conflictLayer.clearLayers();
        const seenConflicts = new Set();
        for (const v of vehicles) {
            if (v.collision_with && v.ttc < 5.0) {
                const other = vehicles.find(o => o.id === v.collision_with);
                if (other) {
                    const key = [v.id, other.id].sort().join('-');
                    if (!seenConflicts.has(key)) {
                        L.polyline([[v.lat, v.lon], [other.lat, other.lon]], {
                            color: '#8f4342', weight: 2, dashArray: '2, 6', opacity: 0.6
                        }).addTo(state.conflictLayer);
                        seenConflicts.add(key);
                    }
                }
            }
        }
    },
    
    async renderRoads() {
        if (!state.roads.length) return;
        state.roadLayer.clearLayers();
        for (const r of state.roads) {
            const latlngs = r.geometry.map(p => [p.lat, p.lon]);
            L.polyline(latlngs, {
                color: r.oneway ? '#8a6d3b' : '#333',
                weight: r.oneway ? 2.5 : 1.5,
                opacity: 0.6,
                dashArray: r.oneway ? '8, 8' : null
            }).addTo(state.roadLayer);
        }
        
        // Fit bounds on initial load
        if (state.roads.length) {
            const allCoords = state.roads.flatMap(r => r.geometry.map(p => [p.lat, p.lon]));
            window.map.fitBounds(L.latLngBounds(allCoords).pad(0.1));
            $('map-empty').hidden = true;
        }
    }
};

// ── UI Module ──
const ui = {
    selectVehicle(id) {
        state.selectedVehicleId = id;
        this.updateIntelPanel();
        
        // Update sidebar select sync if on simulation page
        if ($('sim-vehicle-select')) {
             $('sim-vehicle-select').value = id;
        }
    },

    updateIntelPanel() {
        const body = $('intel-body');
        const v = state.vehicles.find(item => item.id === state.selectedVehicleId);
        
        if (!v) {
            body.innerHTML = '<p class="empty-hint">Click a vehicle on the map to view its intelligence context.</p>';
            return;
        }

        const wwpColor = v.wrong_way_probability >= 0.7 ? 'badge-red' : 'badge-green';
        const stateColor = v.temporal_state === 'CONFIRMED' ? 'badge-red' : (v.temporal_state === 'SUSPECT' ? 'badge-red' : 'badge-green');

        body.innerHTML = `
            <div class="intel-group">
                <h3>Vehicle Summary</h3>
                <div class="intel-row"><span>ID</span><strong>${v.id}</strong></div>
                <div class="intel-row"><span>Speed</span><strong>${v.speed.toFixed(1)} m/s</strong></div>
                <div class="intel-row"><span>State</span><span class="badge ${stateColor}">${v.temporal_state || 'NORMAL'}</span></div>
            </div>

            <div class="intel-group">
                <h3>Direction Intelligence</h3>
                <div class="intel-row"><span>WWP</span><strong>${formatNum(v.wrong_way_probability, 2)}</strong></div>
                 ${buildSparkline(state.history.get(v.id)?.wwp, '#8f4342')}
                <div class="intel-row"><span>Direction Score</span><strong>${formatNum(v.direction_score, 2)}</strong></div>
            </div>

            <div class="intel-group">
                <h3>Risk & Timing</h3>
                <div class="intel-row"><span>TTC</span><strong>${v.ttc == null ? '--' : formatNum(v.ttc, 2) + 's'}</strong></div>
                ${buildSparkline(state.history.get(v.id)?.ttc, '#4d5666')}
                <div class="intel-row"><span>Risk Score</span><strong>${formatNum(v.risk_score, 2)}</strong></div>
                <div class="intel-row"><span>Maneuverability</span><strong>${formatNum(v.maneuverability, 2)}</strong></div>
            </div>

            <div class="intel-group">
                <h3>Spatial Context</h3>
                <div class="intel-row"><span>Nearby Vehicles</span><strong>${v.nearby_count || 0}</strong></div>
                <div class="intel-row"><span>Closest Interaction</span><strong>${v.closest_dist ? formatNum(v.closest_dist, 1) + 'm' : '--'}</strong></div>
            </div>

            <div class="intel-group">
                <h3>Semantic Road Data</h3>
                <div class="intel-row"><span>Road Type</span><strong>${v.road_class || 'Urban'}</strong></div>
                <div class="intel-row"><span>POI Density</span><strong>${formatNum(v.poi_density, 2)}</strong></div>
            </div>

            <div class="intel-group">
                <h3>AI Pipe State</h3>
                <div class="intel-row"><span>Anomaly Score</span><strong>${formatNum(v.anomaly_score, 2)}</strong></div>
                <div class="intel-row"><span>Memory Match</span><strong>${v.memory_triggered ? 'MATCH' : 'NONE'}</strong></div>
            </div>
        `;
    },

    renderAnalyticsList() {
        const tbody = $('analytics-tbody');
        if (!tbody) return;
        
        tbody.innerHTML = state.vehicles.map(v => `
            <tr class="${v.id === state.selectedVehicleId ? 'active' : ''}" onclick="ui.selectVehicle(${v.id}); navigate('analytics');">
                <td>${v.id}</td>
                <td>${v.speed.toFixed(1)}</td>
                <td>${formatNum(v.wrong_way_probability, 2)}</td>
                <td>${v.risk_level || 'low'}</td>
                <td>${v.temporal_state || 'NORMAL'}</td>
            </tr>
        `).join('') || '<tr><td colspan="5" class="empty-hint">No data available</td></tr>';
        
        // Basic detail view for analytics
        const detail = $('analytics-detail');
        const selected = state.vehicles.find(v => v.id === state.selectedVehicleId);
        detail.innerHTML = selected ? `
            <div class="intel-group">
                <h3>History Insight (ID: ${selected.id})</h3>
                <p>Showing trend data for direction and risk coefficients.</p>
                <div class="intel-row"><span>Max WWP seen</span><strong>0.92</strong></div>
                <div class="intel-row"><span>Avg TTC</span><strong>4.2s</strong></div>
            </div>
            <div class="sub-card" style="height: auto; padding: 10px;">
                <p class="muted-text">Trend charts will be implemented here using Canvas/SVG.</p>
            </div>
        ` : '<p class="empty-hint">Select a vehicle from the table.</p>';
    },

    renderRiskList() {
        const tbody = $('risk-tbody');
        if (!tbody) return;
        
        tbody.innerHTML = state.vehicles.filter(v => (v.risk_score > 0.3 || v.wrong_way_probability > 0.5)).map(v => `
            <tr>
                <td>${v.id}</td>
                <td>${formatNum(v.ttc, 2)}s</td>
                <td>${formatNum(v.risk_score, 2)}</td>
                <td><span class="badge ${v.risk_score > 0.7 ? 'badge-red' : 'badge-green'}">${v.risk_level}</span></td>
            </tr>
        `).join('') || '<tr><td colspan="4" class="empty-hint">No active risks detected</td></tr>';

        const collisions = $('collision-list');
        const pairs = state.vehicles.filter(v => v.collision_with);
        collisions.innerHTML = pairs.length ? pairs.map(v => `
            <li class="badge-red" style="margin-bottom:8px">
                <b>V${v.id}</b> projected interaction with <b>V${v.collision_with}</b>
                <div style="font-size:10px">TTC: ${formatNum(v.ttc, 2)}s</div>
            </li>
        `).join('') : '<li class="empty-hint">No predictions</li>';
    },

    renderSearchResults() {
        const list = $('search-results');
        list.innerHTML = state.searchCandidates.map(c => `
            <li class="${c.id === state.selectedCandidateId ? 'selected' : ''}" onclick="ui.selectCandidate('${c.id}')">
                <b>${escape(c.display_name)}</b><br>
                <small>${c.geometry_type} | ${c.match_mode}</small>
            </li>
        `).join('');
    },

    selectCandidate(id) {
        state.selectedCandidateId = id;
        this.renderSearchResults();
    },

    updateSidebar() {
        $('sys-status').textContent = 'Live';
        $('sim-state').textContent = state.simulationRunning ? 'Running' : 'Stopped';
        $('vehicle-count').textContent = state.vehicles.length;
        $('last-sync').textContent = new Date().toLocaleTimeString();
        
        // Simulation Page selects
        if (state.currentPage === 'simulation' && $('sim-vehicle-select')) {
            const sel = $('sim-vehicle-select');
            const newIds = state.vehicles.map(v => v.id).join(',');
            if (sel._lastIds !== newIds) {
                const currentVal = sel.value;
                sel.innerHTML = '<option value="">Select vehicle to target</option>' + 
                                state.vehicles.map(v => `<option value="${v.id}" ${v.id == currentVal ? 'selected' : ''}>Vehicle ${v.id}</option>`).join('');
                sel._lastIds = newIds;
            }
        }
    }
};

// ── Polling & Lifecycle ──
async function sync() {
    try {
        const live = await api.fetchLive();
        state.vehicles = live.direction || [];
        state.matches = live.matches || [];
        
        // Record history
        for (const v of state.vehicles) {
            if (!state.history.has(v.id)) state.history.set(v.id, { wwp: [], ttc: [] });
            const h = state.history.get(v.id);
            h.wwp.push(v.wrong_way_probability || 0);
            h.ttc.push(v.ttc || 10.0);
            if (h.wwp.length > 30) { h.wwp.shift(); h.ttc.shift(); }
        }

        const summary = await api.fetchSummary();
        state.summary = summary;
        state.simulationRunning = summary.simulation_running;

        // Efficient Map Update
        flowMap.updateVehicles(state.vehicles);
        
        // Selected vehicle intel update
        if (state.selectedVehicleId) ui.updateIntelPanel();
        
        // Stats
        ui.updateSidebar();
        
        // Sync static layers if not loaded
        if (!state.roads.length && summary.has_data) {
            state.roads = await api.fetchRoads();
            flowMap.renderRoads();
        }
    } catch (err) {
        $('sys-status').textContent = 'Error';
        $('sys-status').style.color = 'var(--warn)';
    }
}

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
    flowMap.init();
    
    // Bind navigation
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => navigate(btn.dataset.page));
    });

    // Bind Sim Controls
    $('btn-search').onclick = async () => {
        const query = $('sim-query').value;
        const res = await api.cmd('search', { query, limit: 6 });
        state.searchCandidates = res.candidates || [];
        state.selectedCandidateId = state.searchCandidates[0]?.id;
        ui.renderSearchResults();
    };

    $('btn-load').onclick = async () => {
        const selection = state.searchCandidates.find(c => c.id === state.selectedCandidateId);
        if (!selection) return alert('Select a location search result first.');
        
        $('response-log').textContent = 'Loading area...';
        const res = await api.cmd('bootstrap', {
            query: $('sim-query').value,
            radius_m: Number($('sim-radius').value),
            reset: true,
            selection
        });
        $('response-log').textContent = JSON.stringify(res, null, 2);
        state.roads = []; // Trigger road refresh
        await sync();
    };

    $('btn-start').onclick = async () => {
        $('response-log').textContent = 'Starting simulation...';
        const res = await api.cmd('start');
        $('response-log').textContent = 'Simulation started: ' + JSON.stringify(res, null, 2);
        await sync();
    };

    $('btn-stop').onclick = async () => {
        $('response-log').textContent = 'Stopping simulation...';
        const res = await api.cmd('stop');
        $('response-log').textContent = 'Simulation stopped: ' + JSON.stringify(res, null, 2);
        await sync();
    };

    $('btn-reset').onclick = async () => {
        $('response-log').textContent = 'Resetting environment...';
        await api.cmd('stop');
        const res = await api.cmd('start');
        $('response-log').textContent = 'Reset complete. Simulation restarted.';
        state.roads = []; // Refresh network
        await sync();
    };
    
    $('btn-wrong-way').onclick = async () => {
        const vid = $('sim-vehicle-select').value;
        if (!vid) return alert('Select a vehicle in the dropdown first.');
        const res = await api.cmd('inject', { vehicle_id: Number(vid) });
        $('response-log').textContent = 'Wrong-way injected: ' + JSON.stringify(res, null, 2);
        
        // Auto-zoom/select
        ui.selectVehicle(Number(vid));
        navigate('dashboard');
        const v = state.vehicles.find(item => item.id === Number(vid));
        if (v && window.map) window.map.setView([v.lat, v.lon], 16);
    };

    $('sim-vehicle-select').onchange = (e) => {
        if (e.target.value) ui.selectVehicle(Number(e.target.value));
    };

    // Global Poll
    setInterval(sync, 500);
    sync(); // First run
});
