/* ===================================================================
   FlowGuard Analytics Page — analytics.js
   Canvas-based line charts, 500ms polling
   =================================================================== */

const pollMs = Number(document.body.dataset.pollIntervalMs || 500);

async function api(path) {
    const res = await fetch(path, { cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
}

/* ------------------------------------------------------------------ */
/* Chart renderer (pure canvas, no library)                           */
/* ------------------------------------------------------------------ */

function drawLineChart(canvasId, labels, data, opts = {}) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;

    // Size canvas to container
    const rect = canvas.parentElement.getBoundingClientRect();
    const W = rect.width - 32; // account for padding
    const H = 220;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    ctx.scale(dpr, dpr);

    const pad = { top: 16, right: 16, bottom: 28, left: 48 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    // Clear
    ctx.clearRect(0, 0, W, H);

    // Filter valid data points
    const valid = data.map((v, i) => (v != null ? { x: i, y: v } : null)).filter(Boolean);
    if (valid.length < 1) {
        ctx.fillStyle = "#555";
        ctx.font = "12px Inter, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("Waiting for data...", W / 2, H / 2);
        return;
    }

    const minY = opts.minY ?? Math.min(...valid.map((p) => p.y));
    const maxY = opts.maxY ?? Math.max(...valid.map((p) => p.y));
    const rangeY = Math.max(maxY - minY, 0.01);
    const N = data.length;

    // Map to canvas coords
    function toX(i) { return pad.left + (i / Math.max(N - 1, 1)) * plotW; }
    function toY(v) { return pad.top + plotH - ((v - minY) / rangeY) * plotH; }

    // Grid lines
    ctx.strokeStyle = "#222";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
        const y = pad.top + (plotH / 4) * i;
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(pad.left + plotW, y);
        ctx.stroke();
    }

    // Y-axis labels
    ctx.fillStyle = "#666";
    ctx.font = "10px IBM Plex Mono, monospace";
    ctx.textAlign = "right";
    for (let i = 0; i <= 4; i++) {
        const val = maxY - (rangeY / 4) * i;
        const y = pad.top + (plotH / 4) * i;
        ctx.fillText(val.toFixed(opts.decimals ?? 2), pad.left - 6, y + 4);
    }

    // X-axis labels
    ctx.textAlign = "center";
    const step = Math.max(Math.floor(N / 6), 1);
    for (let i = 0; i < N; i += step) {
        const lbl = labels[i] != null ? `${labels[i]}s` : "";
        ctx.fillText(lbl, toX(i), H - 6);
    }

    // Line (or single-point chart)
    const color = opts.color || "#5a9bd5";
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < N; i++) {
        if (data[i] == null) continue;
        const x = toX(i);
        const y = toY(data[i]);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
    }
    if (valid.length >= 2) {
        ctx.stroke();
    } else {
        const p = valid[0];
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(toX(p.x), toY(p.y), 3.5, 0, Math.PI * 2);
        ctx.fill();
    }

    // Fill under line
    if (started && valid.length >= 2) {
        ctx.lineTo(toX(N - 1), pad.top + plotH);
        ctx.lineTo(toX(0), pad.top + plotH);
        ctx.closePath();
        ctx.fillStyle = color.replace(")", ",0.08)").replace("rgb", "rgba");
        ctx.fill();
    }

    // Axis lines
    ctx.strokeStyle = "#333";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top);
    ctx.lineTo(pad.left, pad.top + plotH);
    ctx.lineTo(pad.left + plotW, pad.top + plotH);
    ctx.stroke();
}

function drawScatterChart(canvasId, points, opts = {}) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const W = rect.width - 32;
    const H = 220;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    ctx.scale(dpr, dpr);
    const pad = { top: 16, right: 16, bottom: 28, left: 48 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;
    ctx.clearRect(0, 0, W, H);

    ctx.strokeStyle = "#222";
    for (let i = 0; i <= 4; i++) {
        const y = pad.top + (plotH / 4) * i;
        const x = pad.left + (plotW / 4) * i;
        ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + plotW, y); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(x, pad.top); ctx.lineTo(x, pad.top + plotH); ctx.stroke();
    }
    ctx.strokeStyle = "#333";
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top);
    ctx.lineTo(pad.left, pad.top + plotH);
    ctx.lineTo(pad.left + plotW, pad.top + plotH);
    ctx.stroke();

    function toX(v) { return pad.left + (Math.max(0, Math.min(1, v)) * plotW); }
    function toY(v) { return pad.top + plotH - (Math.max(0, Math.min(1, v)) * plotH); }
    if (!points.length) return;
    ctx.strokeStyle = opts.color || "#5a9bd5";
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((p, i) => {
        const x = toX(p.x), y = toY(p.y);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
}

function drawBarChart(canvasId, labels, values, opts = {}) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    const W = rect.width - 32;
    const H = 220;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);
    const maxV = Math.max(...values, 1);
    const pad = { top: 16, right: 16, bottom: 28, left: 32 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;
    const bw = plotW / Math.max(labels.length, 1) * 0.7;
    labels.forEach((label, i) => {
        const x = pad.left + (i + 0.15) * (plotW / labels.length);
        const h = (values[i] / maxV) * plotH;
        const y = pad.top + plotH - h;
        ctx.fillStyle = opts.color || "#d69e2e";
        ctx.fillRect(x, y, bw, h);
        ctx.fillStyle = "#777";
        ctx.font = "10px IBM Plex Mono, monospace";
        ctx.textAlign = "center";
        ctx.fillText(label, x + bw / 2, H - 6);
    });
}

/* ------------------------------------------------------------------ */
/* Polling & rendering                                                */
/* ------------------------------------------------------------------ */

async function refresh() {
    try {
        const data = await api("/api/analytics");
        const labels = data.labels || [];
        const wwp = data.wwp || [];
        const ttc = data.ttc || [];
        const risk = data.risk || [];
        const monteCarlo = data.monte_carlo || [];
        const kalman = data.kalman || [];
        const evaluation = data.evaluation || {};
        const roc = data.roc || [];
        const confidenceDistribution = data.confidence_distribution || {};

        drawLineChart("chart-wwp", labels, wwp, { minY: 0, maxY: 1, color: "#d69e2e", decimals: 2 });
        drawLineChart("chart-ttc", labels, ttc, { minY: 0, maxY: 30, color: "#e53e3e", decimals: 1 });
        drawLineChart("chart-risk", labels, risk, { minY: 0, maxY: 1, color: "#5a9bd5", decimals: 2 });
        drawLineChart("chart-monte-carlo", labels, monteCarlo, { minY: 0, maxY: 1, color: "#ed8936", decimals: 2 });
        drawLineChart("chart-kalman", labels, kalman, { minY: 0, maxY: 1, color: "#805ad5", decimals: 2 });
        drawScatterChart("chart-roc", roc.map((p) => ({ x: p.fpr, y: p.tpr })), { color: "#e53e3e" });
        const confLabels = Object.keys(confidenceDistribution);
        drawBarChart("chart-confidence", confLabels, confLabels.map((k) => confidenceDistribution[k] || 0), { color: "#38a169" });

        // Stats
        const lastWwp = wwp.length ? wwp[wwp.length - 1] : 0;
        const lastTtc = ttc.length ? ttc[ttc.length - 1] : null;
        const lastRisk = risk.length ? risk[risk.length - 1] : 0;

        document.getElementById("stat-wwp").textContent = (lastWwp || 0).toFixed(3);
        document.getElementById("stat-ttc").textContent = lastTtc != null ? `${lastTtc}s` : "—";
        document.getElementById("stat-risk").textContent = (lastRisk || 0).toFixed(3);
        document.getElementById("stat-points").textContent = labels.length;
        document.getElementById("stat-accuracy").textContent = Number(evaluation.accuracy || 0).toFixed(3);
        document.getElementById("stat-fpr").textContent = Number(evaluation.fpr || 0).toFixed(3);
        document.getElementById("cm-tp").textContent = evaluation.tp ?? 0;
        document.getElementById("cm-fp").textContent = evaluation.fp ?? 0;
        document.getElementById("cm-tn").textContent = evaluation.tn ?? 0;
        document.getElementById("cm-fn").textContent = evaluation.fn ?? 0;

        const statusEl = document.getElementById("analytics-status");
        const hasEval = evaluation && Object.keys(evaluation).length > 0;
        statusEl.textContent = labels.length ? `${labels.length} data points` : (hasEval ? "Live evaluation active" : "Waiting for data...");
        statusEl.style.color = (labels.length || hasEval) ? "#38a169" : "#888";
    } catch (err) {
        console.error(err);
        document.getElementById("analytics-status").textContent = "Fetch failed";
        document.getElementById("analytics-status").style.color = "#e53e3e";
    }
}

function init() {
    refresh();
    setInterval(refresh, pollMs);
}

window.addEventListener("DOMContentLoaded", init);
