/* ===================================================================
   FlowGuard Control Page — control.js
   =================================================================== */

const pollMs = Number(document.body.dataset.pollIntervalMs || 500);

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
    const el = document.getElementById("ctrl-status");
    el.textContent = msg;
    el.style.color = type === "error" ? "#e53e3e" : type === "success" ? "#38a169" : "#888";
}

/* ------------------------------------------------------------------ */
/* Stats polling                                                      */
/* ------------------------------------------------------------------ */

async function refreshStats() {
    try {
        const s = await api("/api/summary");
        const simEl = document.getElementById("ctrl-sim-state");
        simEl.textContent = s.simulation_running ? "RUNNING" : "STOPPED";
        simEl.style.color = s.simulation_running ? "#38a169" : "#e53e3e";
        document.getElementById("ctrl-vehicle-count").textContent = s.vehicles ?? 0;
        document.getElementById("ctrl-road-count").textContent = s.roads ?? 0;
        document.getElementById("ctrl-ww-count").textContent = s.wrong_way_vehicles ?? 0;

        document.getElementById("btn-start").disabled = s.simulation_running;
        document.getElementById("btn-stop").disabled = !s.simulation_running;
    } catch (err) {
        console.error(err);
    }
}

/* ------------------------------------------------------------------ */
/* Simulation lifecycle                                               */
/* ------------------------------------------------------------------ */

async function startSim() {
    try {
        setStatus("Starting...", "neutral");
        await api("/api/admin/simulation/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
        });
        setStatus("Simulation started.", "success");
        await refreshStats();
    } catch (err) {
        setStatus("Start failed: " + err.message, "error");
    }
}

async function stopSim() {
    try {
        setStatus("Stopping...", "neutral");
        await api("/api/admin/simulation/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
        });
        setStatus("Simulation stopped.", "neutral");
        await refreshStats();
    } catch (err) {
        setStatus("Stop failed: " + err.message, "error");
    }
}

async function resetSim() {
    try {
        setStatus("Resetting...", "neutral");
        await api("/api/admin/simulation/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
        });
        setStatus("Simulation reset and restarted.", "success");
        await refreshStats();
    } catch (err) {
        setStatus("Reset failed: " + err.message, "error");
    }
}

/* ------------------------------------------------------------------ */
/* Density slider                                                     */
/* ------------------------------------------------------------------ */

let densityTimer = null;

function onDensityChange(e) {
    const val = e.target.value;
    document.getElementById("density-value").textContent = val;
    clearTimeout(densityTimer);
    densityTimer = setTimeout(async () => {
        try {
            await api("/api/admin/density", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ count: Number(val) }),
            });
            setStatus(`Density set to ${val} vehicles.`, "success");
        } catch (err) {
            setStatus("Density update failed: " + err.message, "error");
        }
    }, 300);
}

/* ------------------------------------------------------------------ */
/* Wrong-way injection                                                */
/* ------------------------------------------------------------------ */

async function injectWrongWay() {
    const vehicleId = document.getElementById("ww-vehicle-id").value || null;
    const duration = Number(document.getElementById("ww-duration").value || 45);

    try {
        setStatus("Injecting wrong-way...", "neutral");
        const result = await api("/api/admin/scenarios/wrong-way", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                vehicle_id: vehicleId ? Number(vehicleId) : null,
                duration_seconds: duration,
            }),
        });
        setStatus(
            `Vehicle #${result.vehicle_id} is now wrong-way on segment ${result.road_segment_id}.`,
            "error"
        );
        await refreshStats();
    } catch (err) {
        setStatus("Wrong-way failed: " + err.message, "error");
    }
}

/* ------------------------------------------------------------------ */
/* Scenario presets                                                   */
/* ------------------------------------------------------------------ */

async function runScenario(scenario) {
    try {
        setStatus(`Running scenario: ${scenario}...`, "neutral");

        if (scenario === "normal") {
            await api("/api/admin/density", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ count: 30 }),
            });
            document.getElementById("density-slider").value = 30;
            document.getElementById("density-value").textContent = "30";
            setStatus("Normal flow scenario active.", "success");
        } else if (scenario === "dense") {
            await api("/api/admin/density", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ count: 40 }),
            });
            document.getElementById("density-slider").value = 40;
            document.getElementById("density-value").textContent = "40";
            setStatus("High density scenario active.", "success");
        } else if (scenario === "wrong-way") {
            await api("/api/admin/density", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ count: 40 }),
            });
            document.getElementById("density-slider").value = 40;
            document.getElementById("density-value").textContent = "40";
            await api("/api/admin/scenarios/wrong-way", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ duration_seconds: 45 }),
            });
            setStatus("Wrong-way + collision scenario active.", "error");
        }

        await refreshStats();
    } catch (err) {
        setStatus("Scenario failed: " + err.message, "error");
    }
}

/* ------------------------------------------------------------------ */
/* Load area                                                          */
/* ------------------------------------------------------------------ */

async function loadArea() {
    const query = document.getElementById("area-query").value.trim();
    const radius = Number(document.getElementById("area-radius").value || 700);

    if (!query) {
        setStatus("Enter a street or area name.", "error");
        return;
    }

    try {
        setStatus(`Loading ${query}...`, "neutral");
        const result = await api("/api/admin/bootstrap", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query, query_type: "auto", radius_m: radius, reset: true }),
        });
        document.getElementById("area-query").value = result.resolved_query || query;
        setStatus(
            `Loaded ${result.road_segments} roads around ${result.resolved_query || query}.`,
            "success"
        );
        await refreshStats();
    } catch (err) {
        setStatus("Load failed: " + err.message, "error");
    }
}

/* ------------------------------------------------------------------ */
/* Wire up                                                            */
/* ------------------------------------------------------------------ */

function init() {
    document.getElementById("btn-start").addEventListener("click", startSim);
    document.getElementById("btn-stop").addEventListener("click", stopSim);
    document.getElementById("btn-reset").addEventListener("click", resetSim);
    document.getElementById("btn-wrong-way").addEventListener("click", injectWrongWay);
    document.getElementById("btn-load-area").addEventListener("click", loadArea);
    document.getElementById("density-slider").addEventListener("input", onDensityChange);

    document.querySelectorAll(".scenario-btn").forEach((btn) => {
        btn.addEventListener("click", () => runScenario(btn.dataset.scenario));
    });

    document.querySelectorAll(".preset-area").forEach((btn) => {
        btn.addEventListener("click", () => {
            document.getElementById("area-query").value = btn.dataset.query || "";
        });
    });

    document.getElementById("area-query").addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            loadArea();
        }
    });

    refreshStats();
    setInterval(refreshStats, 2000);
}

window.addEventListener("DOMContentLoaded", init);
