/* ===================================================================
   FlowGuard Risk Monitor — risk.js
   =================================================================== */

const pollMs = Number(document.body.dataset.pollIntervalMs || 500);

async function api(path) {
    const res = await fetch(path, { cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
}

function riskLevel(score) {
    if (score >= 0.72) return "critical";
    if (score >= 0.48) return "high";
    if (score >= 0.25) return "elevated";
    return "watch";
}

function riskColor(score) {
    if (score >= 0.72) return "#e53e3e";
    if (score >= 0.48) return "#d69e2e";
    if (score >= 0.25) return "#5a9bd5";
    return "#888";
}

/* ------------------------------------------------------------------ */
/* Render                                                             */
/* ------------------------------------------------------------------ */

function renderRiskTable(vehicles) {
    const tbody = document.getElementById("risk-table-body");
    if (!vehicles.length) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color: var(--muted);">No high-risk vehicles.</td></tr>';
        return;
    }

    tbody.innerHTML = vehicles.map((v) => {
        const color = riskColor(v.risk_score);
        return `<tr>
            <td>#${v.vehicle_id}</td>
            <td style="color:${v.state === "wrong_way" ? "#e53e3e" : "#888"}">${v.state}</td>
            <td>${Number(v.speed || 0).toFixed(1)} m/s</td>
            <td style="color:${v.wwp >= 0.5 ? "#e53e3e" : "#888"}">${(v.wwp * 100).toFixed(0)}%</td>
            <td style="color:${v.ttc != null && v.ttc < 5 ? "#e53e3e" : v.ttc != null && v.ttc < 10 ? "#d69e2e" : "#888"}">${v.ttc != null ? v.ttc + "s" : "—"}</td>
            <td style="color:${color}">${(v.risk_score * 100).toFixed(0)}%</td>
            <td>${(v.anomaly_score * 100).toFixed(0)}%</td>
        </tr>`;
    }).join("");
}

function renderCollisionCards(collisions) {
    const container = document.getElementById("collision-cards");
    if (!collisions.length) {
        container.innerHTML = '<div class="empty-state">No predicted collisions.</div>';
        return;
    }

    container.innerHTML = collisions.map((c) => {
        const level = c.risk_level || riskLevel(c.risk_score);
        const ttcClass = c.ttc < 5 ? "critical" : c.ttc < 10 ? "warning" : "";
        return `<div class="alert alert-${level}" style="margin-bottom: 8px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <div>
                    <strong>Vehicle #${c.vehicle_id}</strong> — ${c.state || "normal"}
                    <div style="font-size:0.8rem; margin-top:2px;">
                        Risk: ${(c.risk_score * 100).toFixed(0)}% | Speed: ${Number(c.speed || 0).toFixed(1)} m/s
                    </div>
                </div>
                <div class="countdown ${ttcClass}">
                    ${c.ttc != null ? c.ttc.toFixed(1) + "s" : "—"}
                </div>
            </div>
        </div>`;
    }).join("");
}

function renderAlerts(data) {
    const container = document.getElementById("alert-feed");
    const critical = (data.high_risk_vehicles || []).filter((v) => riskLevel(v.risk_score) === "critical");

    if (!critical.length) {
        // Check for wrong-way vehicles
        const wwVehicles = (data.high_risk_vehicles || []).filter((v) => v.state === "wrong_way");
        if (!wwVehicles.length) {
            container.innerHTML = '<div class="empty-state">No active alerts. System operating normally.</div>';
            return;
        }
        container.innerHTML = wwVehicles.map((v) =>
            `<div class="alert alert-high">
                <strong>⚠ Wrong-Way Detection</strong> — Vehicle #${v.vehicle_id} is travelling against permitted flow.
                WWP: ${(v.wwp * 100).toFixed(0)}% | Speed: ${Number(v.speed || 0).toFixed(1)} m/s
            </div>`
        ).join("");
        return;
    }

    container.innerHTML = critical.map((v) =>
        `<div class="alert alert-critical">
            <strong>CRITICAL</strong> — Vehicle #${v.vehicle_id}
            | Risk: ${(v.risk_score * 100).toFixed(0)}%
            | TTC: ${v.ttc != null ? v.ttc.toFixed(1) + "s" : "—"}
            | State: ${v.state}
            | Anomaly: ${(v.anomaly_score * 100).toFixed(0)}%
        </div>`
    ).join("");
}

/* ------------------------------------------------------------------ */
/* Polling                                                            */
/* ------------------------------------------------------------------ */

async function refresh() {
    try {
        const data = await api("/api/risk-monitor");

        document.getElementById("risk-total").textContent = data.total_vehicles ?? 0;
        document.getElementById("risk-high").textContent = (data.high_risk_vehicles || []).length;
        document.getElementById("risk-ww").textContent = data.wrong_way_count ?? 0;
        document.getElementById("risk-critical").textContent = data.critical_count ?? 0;

        renderRiskTable(data.high_risk_vehicles || []);
        renderCollisionCards(data.predicted_collisions || []);
        renderAlerts(data);

        const statusEl = document.getElementById("risk-status");
        if (data.critical_count > 0) {
            statusEl.textContent = `${data.critical_count} CRITICAL`;
            statusEl.style.color = "#e53e3e";
        } else if (data.wrong_way_count > 0) {
            statusEl.textContent = `${data.wrong_way_count} wrong-way detected`;
            statusEl.style.color = "#d69e2e";
        } else {
            statusEl.textContent = "All clear";
            statusEl.style.color = "#38a169";
        }
    } catch (err) {
        console.error(err);
        document.getElementById("risk-status").textContent = "Fetch failed";
        document.getElementById("risk-status").style.color = "#e53e3e";
    }
}

function init() {
    refresh();
    setInterval(refresh, pollMs);
}

window.addEventListener("DOMContentLoaded", init);
