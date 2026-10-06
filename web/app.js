const MAX_POINTS = 120; // ~30s at 250ms push interval

function fmtPct(v) { return v == null ? "-" : v.toFixed(0) + "%"; }
function fmtMB(v) { return v == null ? "-" : v.toFixed(0) + " MB"; }
function fmtMbps(bps) { return bps == null ? "-" : (bps * 8 / 1e6).toFixed(1) + " Mbps"; }
function fmtC(v) { return v == null ? "-" : v.toFixed(0) + "°C"; }
function fmtW(v) { return v == null ? "-" : v.toFixed(0) + " W"; }

// Per-process CPU arrives as % of ONE core (600 = six cores busy). Shown
// as a share of the whole CPU, like the CPU total, plus the core count.
function fmtProcCpu(pct, cpuCount) {
    if (pct == null) return "-";
    const cores = pct / 100;
    return cpuCount ? `${(pct / cpuCount).toFixed(0)}% of CPU (${cores.toFixed(1)} cores)` : `${cores.toFixed(1)} cores`;
}

// Hides a stat row entirely when its value doesn't exist on this PC (e.g.
// P/E core groups on a non-hybrid CPU, temperature without NVIDIA stats),
// rather than showing a permanent "-".
function showRow(id, visible) {
    const el = document.getElementById(id);
    if (el) el.hidden = !visible;
}
function fmtAge(sec) {
    if (sec == null) return "";
    if (sec < 1.5) return "just now";
    if (sec < 60) return `${sec.toFixed(0)}s ago`;
    return `${(sec / 60).toFixed(0)}m ago`;
}

// Flashes an element's background briefly whenever the text it's given
// actually changes, so the dashboard visibly "breathes" even when a
// number happens to repeat between two pushes.
function setText(id, text) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.textContent !== text) {
        el.textContent = text;
        el.classList.remove("flash");
        // eslint-disable-next-line no-unused-expressions
        void el.offsetWidth; // restart the CSS animation
        el.classList.add("flash");
    }
}

function makeChart(canvasId, datasets) {
    const ctx = document.getElementById(canvasId).getContext("2d");
    return new Chart(ctx, {
        type: "line",
        data: { labels: [], datasets: datasets.map(d => ({
            label: d.label,
            data: [],
            borderColor: d.color,
            backgroundColor: d.color,
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.15,
            spanGaps: true,
        })) },
        options: {
            animation: false,
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: { display: false },
                y: { beginAtZero: true, ticks: { color: "#8b93a3", font: { size: 10 } }, grid: { color: "#262b36" } },
            },
            plugins: {
                legend: { display: datasets.length > 1, labels: { color: "#8b93a3", font: { size: 10 }, boxWidth: 10 } },
            },
        },
    });
}

function pushPoint(chart, label, values) {
    chart.data.labels.push(label);
    values.forEach((v, i) => chart.data.datasets[i].data.push(v));
    if (chart.data.labels.length > MAX_POINTS) {
        chart.data.labels.shift();
        chart.data.datasets.forEach(ds => ds.data.shift());
    }
    chart.update("none");
}

const charts = {
    cpu: makeChart("chart-cpu", [
        { label: "Total", color: "#4da3ff" },
        { label: "Busiest core", color: "#e0a92d" },
        { label: "P-core", color: "#35c07a" },
        { label: "E-core", color: "#e0503b" },
    ]),
    gpu: makeChart("chart-gpu", [
        { label: "GPU total", color: "#4da3ff" },
        { label: "GPU (iRacing)", color: "#35c07a" },
        { label: "Encoder", color: "#e0a92d" },
        { label: "Decoder", color: "#8b93a3" },
    ]),
    ram: makeChart("chart-ram", [{ label: "RAM used %", color: "#4da3ff" }]),
    disk: makeChart("chart-disk", [
        { label: "Read MB/s", color: "#4da3ff" },
        { label: "Write MB/s", color: "#e0a92d" },
    ]),
    net: makeChart("chart-net", [
        { label: "Sent Mbps", color: "#4da3ff" },
        { label: "Recv Mbps", color: "#35c07a" },
    ]),
    fps: makeChart("chart-fps", [{ label: "FPS", color: "#4da3ff" }]),
};

// --- data-source health strip -----------------------------------------
//
// Each source tracks the client-side moment its underlying value last
// actually changed (a "fingerprint" string derived from the snapshot).
// A ticking timer (independent of WebSocket message arrival) then
// re-renders "Xs ago" and flips state to warn/bad if a collector thread
// has stopped updating - so the strip stays honest even between pushes.

const SOURCES = [
    { key: "system", title: "System", panelDot: "dot-cpu" },
    { key: "gpu", title: "GPU", panelDot: "dot-gpu" },
    { key: "sim", title: "iRacing Sim", panelDot: "dot-iracing" },
    { key: "telemetry", title: "iRacing Telemetry" },
];

const sourceState = {};
for (const s of SOURCES) {
    sourceState[s.key] = { fingerprint: null, lastChangeAt: null, state: "unknown", stateText: "waiting for data...", detail: "" };
}

const stripEl = document.getElementById("source-strip");
for (const s of SOURCES) {
    const chip = document.createElement("div");
    chip.className = "source-chip";
    chip.id = `chip-${s.key}`;
    chip.innerHTML = `<div class="chip-title"><span class="dot unknown" id="chip-dot-${s.key}"></span>${s.title}</div>
        <div class="chip-state" id="chip-state-${s.key}">waiting...</div>
        <div class="chip-detail" id="chip-detail-${s.key}"></div>`;
    stripEl.appendChild(chip);
}

function touchSource(key, fingerprint, stateFn) {
    const st = sourceState[key];
    if (fingerprint !== st.fingerprint) {
        st.fingerprint = fingerprint;
        st.lastChangeAt = performance.now();
    }
    Object.assign(st, stateFn(st));
}

function renderSources() {
    const now = performance.now();
    for (const s of SOURCES) {
        const st = sourceState[s.key];
        const ageSec = st.lastChangeAt != null ? (now - st.lastChangeAt) / 1000 : null;

        // If a thread has gone quiet for a while, downgrade regardless of
        // whatever explicit state was last computed on message arrival.
        let dotClass = st.state;
        let stateText = st.stateText;
        if (st.state === "ok" && ageSec != null && ageSec > 3) {
            dotClass = "warn";
            stateText = "stalled - no update";
        }
        if (st.state === "ok" && ageSec != null && ageSec > 10) {
            dotClass = "bad";
            stateText = "not responding";
        }

        const dotEl = document.getElementById(`chip-dot-${s.key}`);
        if (dotEl) {
            dotEl.className = `dot ${dotClass}` + (dotClass === "ok" ? " pulse" : "");
        }
        const panelDotEl = s.panelDot ? document.getElementById(s.panelDot) : null;
        if (panelDotEl) {
            panelDotEl.className = `dot ${dotClass}` + (dotClass === "ok" ? " pulse" : "");
        }
        const stateEl = document.getElementById(`chip-state-${s.key}`);
        if (stateEl) stateEl.textContent = stateText;
        const detailEl = document.getElementById(`chip-detail-${s.key}`);
        if (detailEl) detailEl.textContent = ageSec != null ? `${st.detail} - ${fmtAge(ageSec)}`.replace(/^ - /, "") : st.detail;
    }
}
setInterval(renderSources, 500);

function updateSourcesFromSnapshot(snap) {
    const s = snap.system;
    touchSource("system", s ? s.ts : null, () => s
        ? { state: "ok", stateText: "collecting", detail: "CPU/RAM/disk/net" }
        : { state: "unknown", stateText: "waiting for first sample", detail: "" });

    const gpuAvail = snap.gpu_available;
    const g = snap.gpu;
    if (gpuAvail === false) {
        touchSource("gpu", "unavailable", () => ({ state: "bad", stateText: "unavailable", detail: "no GPU stats source could be opened" }));
    } else {
        const info = snap.gpu_info;
        touchSource("gpu", g ? g.ts : null, () => g
            ? { state: "ok", stateText: "collecting", detail: info ? `via ${info.backend}` : "util / encoder / decoder" }
            : { state: "unknown", stateText: "starting...", detail: "" });
        if (info) setText("gpu-name", info.name);
    }

    // Fingerprint on .ts (updates every poll tick) rather than on pid/
    // connected, which stays constant for the whole session - otherwise
    // a perfectly healthy "running"/"connected" state would look
    // "stalled" after 3s just because nothing about it changed.
    const p = snap.process;
    touchSource("sim", p ? p.ts : "not-running", () => p
        ? { state: "ok", stateText: "running", detail: `PID ${p.pid}, ${fmtProcCpu(p.cpu_pct, snap.cpu_count)}` }
        : { state: "unknown", stateText: "not running", detail: "iRacingSim64DX11.exe" });

    const ir = snap.iracing;
    touchSource("telemetry", ir ? ir.ts : null, () => {
        if (!ir) return { state: "unknown", stateText: "starting...", detail: "" };
        if (ir.connected) return { state: "ok", stateText: "connected", detail: ir.frame_rate != null ? `${ir.frame_rate.toFixed(0)} FPS` : "SDK connected" };
        return { state: "warn", stateText: "sim not connected", detail: "waiting for iRacing telemetry" };
    });

    renderSources();
}

function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/live`);

    ws.onopen = () => {
        document.getElementById("conn-dot").classList.add("connected");
        document.getElementById("conn-text").textContent = "live";
    };
    ws.onclose = () => {
        document.getElementById("conn-dot").classList.remove("connected");
        document.getElementById("conn-text").textContent = "disconnected - retrying...";
        setTimeout(connect, 1500);
    };
    ws.onerror = () => ws.close();

    ws.onmessage = (evt) => {
        const snap = JSON.parse(evt.data);
        const label = new Date().toLocaleTimeString();
        document.getElementById("clock").textContent = label;
        document.getElementById("session-text").textContent = snap.session_id
            ? `session #${snap.session_id} active`
            : "no active session";

        const badge = document.getElementById("recording-badge");
        if (snap.recording) {
            badge.className = "badge recording";
            badge.textContent = "Recording";
        } else {
            badge.className = "badge idle";
            badge.textContent = snap.process ? "Not recording" : "Not recording - waiting for iRacing";
        }

        updateSourcesFromSnapshot(snap);

        const s = snap.system;
        if (s) {
            setText("cpu-total", fmtPct(s.cpu_total_pct));
            setText("cpu-max", fmtPct(s.cpu_max_core_pct));
            setText("cpu-p", fmtPct(s.cpu_p_pct));
            setText("cpu-e", fmtPct(s.cpu_e_pct));
            showRow("row-cpu-p", s.cpu_p_pct != null);
            showRow("row-cpu-e", s.cpu_e_pct != null);
            setText("ram-used", s.ram_total_mb ? `${fmtMB(s.ram_used_mb)} / ${fmtMB(s.ram_total_mb)}` : "-");
            setText("disk-read", s.disk_read_bps != null ? (s.disk_read_bps / 1e6).toFixed(1) + " MB/s" : "-");
            setText("disk-write", s.disk_write_bps != null ? (s.disk_write_bps / 1e6).toFixed(1) + " MB/s" : "-");
            setText("net-sent", fmtMbps(s.net_sent_bps));
            setText("net-recv", fmtMbps(s.net_recv_bps));

            pushPoint(charts.cpu, label, [s.cpu_total_pct, s.cpu_max_core_pct, s.cpu_p_pct, s.cpu_e_pct]);
            const ramPct = s.ram_total_mb ? (100 * s.ram_used_mb / s.ram_total_mb) : null;
            pushPoint(charts.ram, label, [ramPct]);
            pushPoint(charts.disk, label, [
                s.disk_read_bps != null ? s.disk_read_bps / 1e6 : null,
                s.disk_write_bps != null ? s.disk_write_bps / 1e6 : null,
            ]);
            pushPoint(charts.net, label, [
                s.net_sent_bps != null ? s.net_sent_bps * 8 / 1e6 : null,
                s.net_recv_bps != null ? s.net_recv_bps * 8 / 1e6 : null,
            ]);
        }

        const g = snap.gpu;
        if (g) {
            setText("gpu-util", fmtPct(g.gpu_util_pct));
            setText("gpu-iracing-util", fmtPct(g.iracing_sm_util_pct));
            setText("gpu-enc", fmtPct(g.encoder_util_pct));
            setText("gpu-dec", fmtPct(g.decoder_util_pct));
            setText("gpu-vram", g.vram_total_mb ? `${fmtMB(g.vram_used_mb)} / ${fmtMB(g.vram_total_mb)}` : "-");
            setText("gpu-iracing-vram", fmtMB(g.iracing_vram_mb));
            setText("gpu-temp", fmtC(g.temp_c));
            setText("gpu-power", fmtW(g.power_draw_w));
            setText("gpu-throttle", g.throttled ? (g.throttle_reasons || []).join(", ") || "yes" : "no");
            document.getElementById("gpu-throttle").style.color = g.throttled ? "#e0503b" : "";
            showRow("row-gpu-temp", g.temp_c != null);
            showRow("row-gpu-power", g.power_draw_w != null);
            showRow("row-gpu-throttle", g.throttled != null);
            pushPoint(charts.gpu, label, [g.gpu_util_pct, g.iracing_sm_util_pct, g.encoder_util_pct, g.decoder_util_pct]);
        }

        const ir = snap.iracing;
        const p = snap.process;
        setText("ir-track", ir?.track_name ? `${ir.track_name}${ir.car_name ? " / " + ir.car_name : ""}` : "-");
        setText("ir-process", p ? "running" : "not running");
        setText("ir-connected", ir?.connected ? "connected" : "not connected");
        setText("ir-fps", ir?.frame_rate != null ? ir.frame_rate.toFixed(0) : "-");
        setText("ir-ontrack", ir?.on_track ? "yes" : "no");
        setText("ir-lap", String(ir?.lap ?? "-"));
        pushPoint(charts.fps, label, [ir?.frame_rate ?? null]);

        setText("proc-cpu", p ? fmtProcCpu(p.cpu_pct, snap.cpu_count) : "not running");

        renderTradingPaints(snap.trading_paints, snap.cpu_count);
    };
}

// --- Trading Paints panel ---------------------------------------------------

function fmtBytes(b) {
    if (b == null) return "-";
    if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
    if (b >= 1e6) return (b / 1e6).toFixed(0) + " MB";
    return (b / 1e3).toFixed(0) + " KB";
}

function fmtWhen(ts) {
    const sec = Date.now() / 1000 - ts;
    if (sec < 90) return "just now";
    if (sec < 3600) return `${(sec / 60).toFixed(0)}m ago`;
    if (sec < 86400) return `${(sec / 3600).toFixed(0)}h ago`;
    return fmtEventTime(ts);
}

function fmtBurst(b, verb) {
    if (!b) return "none yet";
    return `${b.count} file${b.count === 1 ? "" : "s"}, ${fmtBytes(b.bytes)}${verb} - ${fmtWhen(b.ts)}`;
}

let tpProblemsKey = null;

function renderTradingPaints(tp, cpuCount) {
    const dot = document.getElementById("dot-tp");
    if (!tp) return;
    const st = tp.status || {};
    dot.className = tp.running ? "dot ok pulse" : "dot unknown";
    setText("tp-name", st.process_name && st.process_name !== "Trading Paints" ? st.process_name.replace(/^Trading Paints\s*/, "") : "");
    setText("tp-app", tp.running ? `running (PID ${tp.pid})` : "not running");
    setText("tp-usage", tp.running ? `${fmtProcCpu(tp.cpu_pct, cpuCount)} / ${fmtMB(tp.rss_mb)}` : "-");
    setText("tp-download", fmtBurst(st.last_download, ""));
    setText("tp-cleanup", fmtBurst(st.last_cleanup, " freed"));
    setText("tp-folder", st.paint_folder ? `${st.paint_folder.count} files, ${fmtBytes(st.paint_folder.bytes)}` : "-");

    // Only rebuild the list when it actually changed - this runs 4x a second.
    const problems = st.problems || [];
    const key = problems.map(e => e.ts + e.kind).join("|");
    if (key === tpProblemsKey) return;
    tpProblemsKey = key;
    const list = document.getElementById("tp-problems");
    if (!problems.length) {
        list.innerHTML = '<div class="empty">none</div>';
        return;
    }
    list.replaceChildren(...problems.map(e => {
        const row = document.createElement("div");
        row.className = "event";
        row.title = "Click for the full message";
        const time = document.createElement("span");
        time.className = "ts";
        time.textContent = fmtEventTime(e.ts);
        const label = document.createElement("span");
        label.className = "label";
        label.textContent = e.detail;
        row.append(time, label);
        row.addEventListener("click", () => showText(
            e.kind === "log_error" ? "Trading Paints log error" : "Trading Paints " + e.kind,
            fmtEventTime(e.ts) + (e.kind === "log_error"
                ? " - when vrmon saw it in Trading Paints' log (its lines carry no times of their own)" : ""),
            [e.detail]));
        return row;
    }));
}

connect();

// --- recent events feed -------------------------------------------------
// Polled separately from the live WebSocket - these change rarely.

const EVENT_POLL_MS = 15000;

function fmtEventTime(ts) {
    const d = new Date(ts * 1000);
    const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return d.toDateString() === new Date().toDateString()
        ? time
        : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

const rangeSelect = document.getElementById("event-range");
try {
    const saved = localStorage.getItem("eventRangeHours");
    if (saved && [...rangeSelect.options].some(o => o.value === saved)) rangeSelect.value = saved;
} catch (e) { /* storage unavailable - keep the default */ }
rangeSelect.addEventListener("change", () => {
    try { localStorage.setItem("eventRangeHours", rangeSelect.value); } catch (e) { /* ignore */ }
    refreshEvents();
});

async function refreshEvents() {
    const feed = document.getElementById("event-feed");
    try {
        const events = await (await fetch(`/api/events/recent?hours=${rangeSelect.value}`)).json();
        if (!events.length) {
            feed.innerHTML = `<div class="empty">nothing in the ${rangeSelect.selectedOptions[0].text}</div>`;
            return;
        }
        feed.replaceChildren(...events.map(ev => {
            const row = document.createElement("div");
            row.className = `event ${ev.level}`;
            if (ev.ref) {
                row.classList.add("clickable");
                row.title = "Click for details";
                row.addEventListener("click", () => showEvent(ev));
            } else {
                row.title = ev.detail || "";
            }
            const time = document.createElement("span");
            time.className = "ts";
            time.textContent = fmtEventTime(ev.ts);
            const source = document.createElement("span");
            source.className = "source";
            source.textContent = ev.source;
            const label = document.createElement("span");
            label.className = "label";
            label.textContent = ev.label;
            row.append(time, source, label);
            return row;
        }));
    } catch (e) {
        feed.innerHTML = '<div class="empty">couldn\'t load events</div>';
    }
}

refreshEvents();
setInterval(refreshEvents, EVENT_POLL_MS);

// --- reports list -----------------------------------------------------------
// A session's report is built a little after the sim exits, so this polls
// too - a new report appears at the top by itself.

const REPORT_POLL_MS = 30000;

function reportLink(file, text, className) {
    const a = document.createElement("a");
    a.href = `/reports/${encodeURIComponent(file)}`;
    a.target = "_blank";
    a.rel = "noopener";
    a.className = className;
    a.textContent = text;
    return a;
}

async function refreshReports() {
    const list = document.getElementById("report-list");
    try {
        const reports = await (await fetch("/api/reports")).json();
        if (!reports.length) {
            list.innerHTML = '<div class="empty">no reports yet - one is built automatically each time you exit the sim</div>';
            return;
        }
        list.replaceChildren(...reports.map(r => {
            const row = document.createElement("div");
            row.className = "report";
            row.append(reportLink(r.file, r.title, "title"));
            const sub = document.createElement("span");
            sub.className = "subtitle";
            sub.textContent = r.subtitle;
            row.append(sub);
            if (r.pdf) row.append(reportLink(r.pdf, "PDF", "pdf"));
            return row;
        }));
    } catch (e) {
        list.innerHTML = '<div class="empty">couldn\'t load reports</div>';
    }
}

refreshReports();
setInterval(refreshReports, REPORT_POLL_MS);

// --- event details window -------------------------------------------------
// Opening the source (Event Viewer / Notepad / Explorer) happens on the PC
// running the app, so the button only works when browsing on that PC - the
// server enforces this too.

const modal = document.getElementById("event-modal");
const isLocalBrowser = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
let currentRef = null;

function closeEvent() {
    modal.hidden = true;
    currentRef = null;
}

// The same window for text the page already has - nothing to fetch or open.
function showText(titleText, metaText, lines) {
    currentRef = null;
    document.getElementById("modal-title").textContent = titleText;
    document.getElementById("modal-meta").textContent = metaText;
    document.getElementById("modal-body").replaceChildren(...lines.map(text => {
        const line = document.createElement("div");
        line.textContent = text || " ";
        return line;
    }));
    document.getElementById("modal-note").textContent = "";
    document.getElementById("modal-open").hidden = true;
    modal.hidden = false;
}

async function showEvent(ev) {
    currentRef = ev.ref;
    const params = new URLSearchParams({ kind: ev.ref.kind, key: ev.ref.key || "", file: ev.ref.file || "", line: ev.ref.line ?? 0 });
    const title = document.getElementById("modal-title");
    const meta = document.getElementById("modal-meta");
    const body = document.getElementById("modal-body");
    const openBtn = document.getElementById("modal-open");
    const note = document.getElementById("modal-note");
    title.textContent = ev.label;
    meta.textContent = "loading...";
    body.replaceChildren();
    note.textContent = "";
    openBtn.hidden = true;
    modal.hidden = false;

    try {
        const res = await fetch(`/api/events/detail?${params}`);
        if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
        const d = await res.json();
        title.textContent = d.title;
        meta.textContent = d.meta;
        body.replaceChildren(...d.lines.map((text, i) => {
            const line = document.createElement("div");
            line.textContent = text || " ";
            if (i === d.highlight) line.className = "highlight";
            return line;
        }));
        body.querySelector(".highlight")?.scrollIntoView({ block: "center" });
        openBtn.textContent = d.open_label;
        openBtn.hidden = false;
        openBtn.disabled = !isLocalBrowser;
        if (!isLocalBrowser) note.textContent = "Opening files only works on the PC running iRacing Monitor.";
    } catch (e) {
        meta.textContent = `Couldn't load details: ${e.message}`;
    }
}

document.getElementById("modal-open").addEventListener("click", async () => {
    if (!currentRef) return;
    const note = document.getElementById("modal-note");
    const params = new URLSearchParams({ kind: currentRef.kind, key: currentRef.key || "", file: currentRef.file || "" });
    try {
        const res = await fetch(`/api/events/open?${params}`, { method: "POST" });
        note.textContent = res.ok ? "Opened on this PC." : `Couldn't open it: ${(await res.json()).detail}`;
    } catch (e) {
        note.textContent = `Couldn't open it: ${e.message}`;
    }
});
document.getElementById("modal-close").addEventListener("click", closeEvent);
modal.addEventListener("click", e => { if (e.target === modal) closeEvent(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && !modal.hidden) closeEvent(); });
