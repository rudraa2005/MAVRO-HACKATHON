const state = {
  map: null,
  roadsLayer: null,
  poisLayer: null,
  vehicleLayer: null,
  scenarioLayer: null,
  vehicleMarkers: new Map(),
  roadsById: new Map(),
  hasSuccessfulSync: false,
  syncFailures: 0,
  pollHandle: null,
};

const pollIntervalMs = Number(document.body.dataset.pollIntervalMs || 1000);
const initialPlaceName = document.body.dataset.place || "Chennai, India";

async function requestJSON(path, options = {}) {
  const requestOptions = { cache: "no-store", ...options };
  const response = await fetch(path, requestOptions);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const detail =
      typeof payload === "string" ? payload : payload.error || JSON.stringify(payload);
    throw new Error(`${response.status} ${response.statusText}: ${detail}`);
  }
  return payload;
}

function initMap() {
  state.map = L.map("map", {
    zoomControl: false,
    preferCanvas: true,
  }).setView([13.0827, 80.2707], 12);

  L.control.zoom({ position: "bottomright" }).addTo(state.map);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(state.map);

  state.roadsLayer = L.layerGroup().addTo(state.map);
  state.poisLayer = L.layerGroup().addTo(state.map);
  state.vehicleLayer = L.layerGroup().addTo(state.map);
  state.scenarioLayer = L.layerGroup().addTo(state.map);
}

function updateStatus(message, type = "neutral") {
  const pill = document.getElementById("status-pill");
  pill.textContent = message;
  pill.style.color =
    type === "error" ? "#ff9cab" : type === "success" ? "#8dffd8" : "#93a4be";
}

function setMapEmptyState(visible, message) {
  const emptyState = document.getElementById("map-empty-state");
  if (!visible) {
    emptyState.classList.remove("visible");
    return;
  }

  if (message) {
    emptyState.innerHTML = `<strong>No road network loaded yet.</strong><p>${message}</p>`;
  }
  emptyState.classList.add("visible");
}

function updateSimulationState(running) {
  const node = document.getElementById("simulation-state");
  node.textContent = running ? "running" : "stopped";
  node.style.color = running ? "#8dffd8" : "#93a4be";
}

function updateSimulationButtons(running) {
  document.getElementById("start-simulation").disabled = running;
  document.getElementById("stop-simulation").disabled = !running;
}

function updateCounts(summary) {
  document.getElementById("roads-count").textContent = summary.roads ?? 0;
  document.getElementById("vehicles-count").textContent = summary.vehicles ?? 0;
  document.getElementById("wrong-way-count").textContent =
    summary.wrong_way_vehicles ?? 0;
  document.getElementById("pois-count").textContent = summary.pois ?? 0;
  document.getElementById("oneway-count").textContent =
    summary.oneway_segments ?? 0;
  document.getElementById("poll-interval").textContent = `${pollIntervalMs} ms`;
  document.getElementById("place-name").textContent =
    document.getElementById("scenario-query").value.trim() || initialPlaceName;
  updateSimulationState(Boolean(summary.simulation_running));
  updateSimulationButtons(Boolean(summary.simulation_running));
}

function updateWrongWayList(vehicles) {
  const list = document.getElementById("wrong-way-list");
  const wrongWayVehicles = vehicles.filter((vehicle) => vehicle.wrong_way);

  if (!wrongWayVehicles.length) {
    list.innerHTML = "<li>No active wrong-way vehicles.</li>";
    return;
  }

  list.innerHTML = wrongWayVehicles
    .map((vehicle) => {
      const speed = Number(vehicle.speed || 0).toFixed(1);
      return `<li>Vehicle #${vehicle.id} moving at ${speed} m/s on segment ${vehicle.road_segment_id}</li>`;
    })
    .join("");
}

function clearVehicleMarkers() {
  [...state.vehicleMarkers.values()].forEach((marker) => {
    state.vehicleLayer.removeLayer(marker);
  });
  state.vehicleMarkers.clear();
  updateWrongWayList([]);
}

function clearScenarioOverlay() {
  state.scenarioLayer.clearLayers();
}

function clearMapForReload() {
  clearScenarioOverlay();
  clearVehicleMarkers();
  state.roadsLayer.clearLayers();
  state.poisLayer.clearLayers();
  state.roadsById = new Map();
}

function renderRoads(roads) {
  state.roadsLayer.clearLayers();
  state.roadsById = new Map();

  if (!roads.length) {
    setMapEmptyState(
      true,
      'Use "Load Street Area" with a place like "Anna Salai, Chennai, Tamil Nadu, India" to ingest roads and start the simulation.'
    );
    return;
  }

  setMapEmptyState(false);
  const bounds = [];
  roads.forEach((road) => {
    state.roadsById.set(road.id, road);
    const latLngs = (road.geometry || []).map((point) => [point.lat, point.lon]);
    if (!latLngs.length) {
      return;
    }
    latLngs.forEach((point) => bounds.push(point));
    L.polyline(latLngs, {
      color: road.oneway ? "#7f8ea2" : "#5a6f88",
      weight: road.oneway ? 4 : 2.4,
      opacity: road.oneway ? 0.82 : 0.68,
    })
      .bindPopup(
        `<strong>Road ${road.id}</strong><br>Bearing: ${road.bearing.toFixed(1)}°<br>Length: ${road.length.toFixed(
          1
        )} m<br>One-way: ${road.oneway ? "yes" : "no"}<br>POI density: ${road.poi_density.toFixed(
          2
        )} / km`
      )
      .addTo(state.roadsLayer);
  });

  if (bounds.length) {
    state.map.fitBounds(bounds, { padding: [24, 24] });
  }
}

function renderPois(pois) {
  state.poisLayer.clearLayers();
  pois.forEach((poi) => {
    const color =
      poi.type === "signal"
        ? "#ffb703"
        : poi.type === "parking"
        ? "#8dffd8"
        : poi.type === "intersection"
        ? "#f97316"
        : "#ffd166";

    L.circleMarker([poi.lat, poi.lon], {
      radius: 4,
      color,
      fillColor: color,
      fillOpacity: 0.95,
      weight: 1,
    })
      .bindPopup(
        `<strong>${poi.type}</strong><br>Nearest road: ${poi.nearest_road_segment_id ?? "n/a"}`
      )
      .addTo(state.poisLayer);
  });
}

function renderVehicles(vehicles) {
  const seen = new Set();
  vehicles.forEach((vehicle) => {
    seen.add(vehicle.id);
    const latLng = [vehicle.lat, vehicle.lon];
    const color = vehicle.wrong_way ? "#ff5d73" : "#60a5fa";
    let marker = state.vehicleMarkers.get(vehicle.id);

    if (!marker) {
      marker = L.circleMarker(latLng, {
        radius: vehicle.wrong_way ? 6 : 5,
        color,
        fillColor: color,
        fillOpacity: 0.95,
        weight: 2,
      }).addTo(state.vehicleLayer);
      state.vehicleMarkers.set(vehicle.id, marker);
    }

    marker.setLatLng(latLng);
    marker.setStyle({
      color,
      fillColor: color,
      radius: vehicle.wrong_way ? 6 : 5,
    });
    marker.bindPopup(
      `<strong>Vehicle #${vehicle.id}</strong><br>Speed: ${Number(
        vehicle.speed || 0
      ).toFixed(1)} m/s<br>Bearing: ${Number(vehicle.bearing || 0).toFixed(
        1
      )}°<br>Segment: ${vehicle.road_segment_id}<br>Wrong-way: ${
        vehicle.wrong_way ? "yes" : "no"
      }`
    );
  });

  [...state.vehicleMarkers.entries()].forEach(([vehicleId, marker]) => {
    if (seen.has(vehicleId)) {
      return;
    }
    state.vehicleLayer.removeLayer(marker);
    state.vehicleMarkers.delete(vehicleId);
  });

  updateWrongWayList(vehicles);
}

function highlightScenario(result) {
  clearScenarioOverlay();
  const geometry = result.geometry || [];
  const latLngs = geometry.map((point) => [point.lat, point.lon]);
  if (!latLngs.length) {
    return;
  }

  const line = L.polyline(latLngs, {
    color: "#ff5d73",
    weight: 7,
    opacity: 0.9,
    dashArray: "10 10",
  })
    .bindPopup(
      `<strong>Wrong-way demo</strong><br>Vehicle: ${result.vehicle_id}<br>Segment: ${result.road_segment_id}`
    )
    .addTo(state.scenarioLayer);

  state.map.fitBounds(line.getBounds(), { padding: [48, 48], maxZoom: 17 });
}

async function refreshSnapshot() {
  try {
    if (!state.hasSuccessfulSync && state.syncFailures === 0) {
      updateStatus("Syncing backend state...");
    }

    const [summary, vehicles] = await Promise.all([
      requestJSON("/api/summary"),
      requestJSON("/api/vehicles"),
    ]);

    state.hasSuccessfulSync = true;
    state.syncFailures = 0;
    updateCounts(summary);
    renderVehicles(vehicles);
    document.getElementById("last-update").textContent =
      new Date().toLocaleTimeString();

    if (!summary.has_data) {
      setMapEmptyState(
        true,
        'Use "Load Street Area" with a place like "Anna Salai, Chennai, Tamil Nadu, India" to ingest roads and start the simulation.'
      );
      updateStatus("No road network loaded yet.", "neutral");
      return;
    }

    if (!summary.simulation_running) {
      updateStatus(
        "Simulation stopped. Load another area or start the simulation again.",
        "neutral"
      );
      return;
    }

    if (!summary.ready_for_demo) {
      updateStatus(
        "Area loaded, but no one-way roads were found. Try a denser street area.",
        "error"
      );
      return;
    }

    updateStatus("Live sync healthy. Ready for wrong-way demo.", "success");
  } catch (error) {
    console.error(error);
    state.syncFailures += 1;
    updateStatus(
      "Backend sync failed. Run the page through Flask to enable live data.",
      "error"
    );
  }
}

async function loadStaticLayers() {
  try {
    const [roads, pois] = await Promise.all([
      requestJSON("/api/roads"),
      requestJSON("/api/pois"),
    ]);
    renderRoads(roads);
    renderPois(pois);
  } catch (error) {
    console.error(error);
    updateStatus("Static layer load failed", "error");
  }
}

async function loadStreetArea() {
  const query = document.getElementById("scenario-query").value.trim();
  const radiusM = Number(document.getElementById("scenario-radius").value || 700);
  if (!query) {
    updateStatus("Enter a street or area name first.", "error");
    return;
  }

  try {
    updateStatus(`Loading ${query}...`);
    const result = await requestJSON("/api/admin/bootstrap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        query_type: "auto",
        radius_m: radiusM,
        reset: true,
      }),
    });

    clearMapForReload();
    document.getElementById("scenario-query").value =
      result.resolved_query || query;

    await loadStaticLayers();
    await refreshSnapshot();

    if (result.simulation_running) {
      updateStatus(
        `Loaded ${result.road_segments} roads around ${result.resolved_query || query}.`,
        "success"
      );
      return;
    }

    updateStatus(
      `Loaded ${result.road_segments} roads around ${result.resolved_query || query}. Simulation is stopped; press Start Simulation when ready.`,
      "neutral"
    );
  } catch (error) {
    console.error(error);
    updateStatus(`Ingestion failed: ${error.message}`, "error");
  }
}

async function startSimulation() {
  try {
    updateStatus("Starting simulation...");
    await requestJSON("/api/admin/simulation/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    await refreshSnapshot();
    updateStatus("Simulation running.", "success");
  } catch (error) {
    console.error(error);
    updateStatus(`Could not start simulation: ${error.message}`, "error");
  }
}

async function stopSimulation() {
  try {
    updateStatus("Stopping simulation...");
    await requestJSON("/api/admin/simulation/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    clearScenarioOverlay();
    await refreshSnapshot();
    updateStatus("Simulation stopped.", "neutral");
  } catch (error) {
    console.error(error);
    updateStatus(`Could not stop simulation: ${error.message}`, "error");
  }
}

async function triggerWrongWayScenario() {
  try {
    updateStatus("Triggering wrong-way vehicle...");
    const result = await requestJSON("/api/admin/scenarios/wrong-way", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ duration_seconds: 45 }),
    });
    highlightScenario(result);
    await refreshSnapshot();
    updateStatus(
      `Vehicle #${result.vehicle_id} is now travelling wrong-way on segment ${result.road_segment_id}.`,
      "error"
    );
  } catch (error) {
    console.error(error);
    updateStatus(`Wrong-way demo failed: ${error.message}`, "error");
  }
}

function bindControls() {
  document.getElementById("load-street").addEventListener("click", loadStreetArea);
  document
    .getElementById("start-simulation")
    .addEventListener("click", startSimulation);
  document
    .getElementById("stop-simulation")
    .addEventListener("click", stopSimulation);
  document
    .getElementById("run-wrong-way")
    .addEventListener("click", triggerWrongWayScenario);

  document
    .getElementById("scenario-query")
    .addEventListener("keydown", async (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        await loadStreetArea();
      }
    });

  document.querySelectorAll(".preset-button").forEach((button) => {
    button.addEventListener("click", () => {
      document.getElementById("scenario-query").value = button.dataset.query || "";
    });
  });
}

async function bootstrapView() {
  initMap();
  bindControls();

  if (window.location.protocol === "file:") {
    document.getElementById("last-update").textContent = "Preview mode";
    document.getElementById("poll-interval").textContent = "disabled";
    updateSimulationButtons(false);
    setMapEmptyState(
      true,
      'Static preview only. Open "http://127.0.0.1:5000/" through Flask to ingest roads and run the live demo.'
    );
    updateStatus(
      "Static preview only. Open http://127.0.0.1:5000/ through Flask for backend sync.",
      "error"
    );
    return;
  }

  await loadStaticLayers();
  await refreshSnapshot();

  state.pollHandle = window.setInterval(refreshSnapshot, pollIntervalMs);
}

window.addEventListener("DOMContentLoaded", bootstrapView);
