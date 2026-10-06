/* ==========================================================================
   JIZO dashboard - renderer.
   Reads the embedded snapshot and paints the board. Computes nothing: every
   number already came from backend/dashboard.py's snapshot().
   ========================================================================== */

"use strict";

const COLORS = {
  breakers: "#ff0055", failures: "#ff4d6d", successes: "#00ff66",
  timeouts: "#ff8fab", fallbacks: "#00f0ff", duplicates: "#ffb800",
  calls: "#94a3b8", errorrate: "#ff0055", accent: "#00f0ff",
};

const state = { payload: null, selected: null };

function $(id) { return document.getElementById(id); }

function readBoot() {
  try {
    return JSON.parse($("boot").textContent);
  } catch (err) {
    return { focus: null, apiKeys: [], views: {}, latencyByApi: [],
             database: { ok: false, error: "not rendered by backend.dashboard" } };
  }
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function fmtPct(v) { return v === null || v === undefined ? "—" : v.toFixed(1) + "%"; }
function fmtMs(v) { return v === null || v === undefined ? "—" : Number(v).toFixed(1) + "ms"; }

/* --- the single interactive control ------------------------------------ */
function renderDropdown(payload) {
  const select = $("apiKeySelect");
  clear(select);
  (payload.apiKeys || []).forEach((entry) => {
    const option = el("option", null, entry.name);
    option.value = entry.apiKey;
    select.appendChild(option);
  });
  const wanted = state.selected || payload.focus ||
    (payload.apiKeys[0] && payload.apiKeys[0].apiKey);
  if (wanted) select.value = wanted;
  state.selected = select.value || null;
}

function renderFocusFields(view) {
  $("focusService").textContent = view ? view.name : "—";
  const breaker = view && view.breakers ? view.breakers.state : null;
  const stateEl = $("focusState");
  stateEl.textContent = breaker || "—";
  if (breaker === "OPEN") {
    stateEl.style.color = "var(--pink)";
    stateEl.style.textShadow = "0 0 12px rgba(255,0,85,0.6)";
  } else if (breaker === "HALF_OPEN") {
    stateEl.style.color = "var(--amber)";
    stateEl.style.textShadow = "0 0 12px rgba(255,184,0,0.5)";
  } else {
    stateEl.style.color = "var(--cyan)";
    stateEl.style.textShadow = "0 0 12px rgba(0,240,255,0.5)";
  }
  $("focusCrit").textContent = (view && view.criticality) || "—";
  $("focusOwner").textContent = (view && view.owner) || "—";
  const win = view && view.trendWindow;
  $("focusWindow").textContent = win ? (clockTime(win.start) + " – " + clockTime(win.end)) : "—";
}

/* --- row 1 -------------------------------------------------------------- */
function renderOverall(view) {
  const body = $("overallBody");
  clear(body);
  if (!view) { body.appendChild(el("div", "empty", "no data")); return; }
  body.appendChild(el("div", "hero-value tone-" + view.overall.status, view.overall.label));
  body.appendChild(el("div", "hero-reason", view.overall.reason));
}

function renderError(view) {
  const body = $("errorBody");
  clear(body);
  if (!view) { body.appendChild(el("div", "empty", "no data")); return; }
  const rate = view.errorRate;
  const cls = rate >= 50 ? "tone-bad" : rate > 5 ? "tone-warn" : "tone-ok";
  body.appendChild(el("div", "bignum " + cls, fmtPct(rate)));
  body.appendChild(el("div", "bignum-sub", "TS " + fmtPct(view.tsRate)));
}

function renderLatencyTable(bodyId, metric, payload) {
  const body = $(bodyId);
  clear(body);
  const rows = payload.latencyByApi || [];
  if (!rows.length) { body.appendChild(el("div", "empty", "no calls yet")); return; }

  const table = el("table", "metric-table");
  const head = el("tr");
  head.appendChild(el("th", null, "Metric"));
  head.appendChild(el("th", null, "Current"));
  table.appendChild(head);

  rows.forEach((row) => {
    const tr = el("tr", row.apiKey === state.selected ? "focused" : "dim");
    const nameCell = el("td");
    const dot = el("span", "dot");
    dot.style.background = row.apiKey === state.selected ? COLORS.accent : "#5b6678";
    nameCell.appendChild(dot);
    nameCell.appendChild(document.createTextNode(row.name));
    tr.appendChild(nameCell);
    tr.appendChild(el("td", "value", fmtMs(row[metric])));
    table.appendChild(tr);
  });
  body.appendChild(table);
}

/* --- charts ------------------------------------------------------------- */
function clockTime(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  return String(d.getHours()).padStart(2, "0") + ":" +
    String(d.getMinutes()).padStart(2, "0");
}

function timeLabels(win, count) {
  const out = [];
  if (!win || !win.start || !win.end) {
    for (let i = 0; i < count; i++) out.push("");
    return out;
  }
  for (let i = 0; i < count; i++) {
    out.push(clockTime(win.start + ((win.end - win.start) * i) / (count - 1)));
  }
  return out;
}

function sparkline(series, color) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("preserveAspectRatio", "none");
  if (!series || !series.length) return svg;

  const max = Math.max.apply(null, series.concat([1]));
  const step = series.length > 1 ? 100 / (series.length - 1) : 100;
  const pts = series.map((value, i) => [i * step, 96 - (value / max) * 88]);
  const line = pts.map((p, i) => (i ? "L" : "M") + p[0] + " " + p[1]).join(" ");

  const fill = document.createElementNS(ns, "path");
  fill.setAttribute("d", line + " L100 100 L0 100 Z");
  fill.setAttribute("fill", color);
  fill.setAttribute("opacity", "0.15");
  svg.appendChild(fill);

  const stroke = document.createElementNS(ns, "path");
  stroke.setAttribute("d", line);
  stroke.setAttribute("fill", "none");
  stroke.setAttribute("stroke", color);
  stroke.setAttribute("stroke-width", "1.5");
  stroke.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(stroke);
  return svg;
}

function renderChartCard(counter, view) {
  const color = COLORS[counter.key] || COLORS.accent;
  const series = (view.trends && view.trends[counter.key]) || [];
  const max = series.length ? Math.max.apply(null, series) : 0;

  const card = el("article", "card glass");
  card.appendChild(el("h2", "card-title", counter.label));

  const body = el("div", "card-body chart");
  const top = el("div", "chart-top");
  top.appendChild(el("span", "chart-value", String(counter.value)));
  top.appendChild(el("span", "chart-max", "max " + max));
  body.appendChild(top);

  const plot = el("div", "chart-plot");
  [25, 50, 75].forEach((pct) => {
    const grid = el("div", "chart-grid");
    grid.style.top = pct + "%";
    plot.appendChild(grid);
  });
  plot.appendChild(sparkline(series, color));
  body.appendChild(plot);

  const axis = el("div", "chart-axis");
  timeLabels(view.trendWindow, 5).forEach((label) => axis.appendChild(el("span", null, label)));
  body.appendChild(axis);

  const legend = el("div", "chart-legend");
  const swatch = el("span", "legend-line");
  swatch.style.background = color;
  legend.appendChild(swatch);
  legend.appendChild(document.createTextNode(view.name));
  legend.appendChild(el("b", null, String(counter.value)));
  body.appendChild(legend);

  card.appendChild(body);
  return card;
}

function renderCharts(view) {
  const rowA = $("chartsRowA");
  const rowB = $("chartsRowB");
  clear(rowA); clear(rowB);
  if (!view) return;
  const counters = view.counters || [];
  counters.slice(0, 4).forEach((c) => rowA.appendChild(renderChartCard(c, view)));
  counters.slice(4, 8).forEach((c) => rowB.appendChild(renderChartCard(c, view)));
}

/* --- proof panels ------------------------------------------------------- */
function renderScorecards(view) {
  const body = $("scorecardsBody");
  clear(body);
  if (!view || !(view.scorecards || []).length) { body.appendChild(el("div", "empty", "no graded runs")); return; }
  view.scorecards.forEach((card) => {
    const row = el("div", "sc-row");
    row.appendChild(el("div", "sc-pattern", card.pattern.replace(/_/g, " ")));
    const bar = el("div", "sc-bar");
    const fill = el("i");
    const rate = card.tsRate === null ? 0 : card.tsRate;
    fill.style.width = Math.max(0, Math.min(100, rate)) + "%";
    fill.style.background = rate >= 99 ? COLORS.successes : rate >= 50 ? COLORS.failures : COLORS.breakers;
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el("div", "sc-pct", fmtPct(card.tsRate)));
    row.appendChild(el("div", "sc-runs", card.pass + "/" + card.runs));
    body.appendChild(row);
  });
}

function renderRadar(view) {
  const body = $("radarBody");
  clear(body);
  const axes = (view && view.radar) || [];
  if (!axes.length) { body.appendChild(el("div", "empty", "no drills to score")); return; }

  const size = 200, cx = 100, cy = 96, radius = 66, ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "radar");
  svg.setAttribute("viewBox", "0 0 " + size + " " + size);

  const point = (i, r) => {
    const angle = (Math.PI * 2 * i) / axes.length - Math.PI / 2;
    return [cx + Math.cos(angle) * r, cy + Math.sin(angle) * r];
  };
  [0.25, 0.5, 0.75, 1].forEach((ring) => {
    const poly = document.createElementNS(ns, "polygon");
    poly.setAttribute("points", axes.map((_, i) => point(i, radius * ring).join(",")).join(" "));
    poly.setAttribute("fill", "none");
    poly.setAttribute("stroke", "rgba(255,255,255,0.07)");
    svg.appendChild(poly);
  });
  axes.forEach((_, i) => {
    const [x, y] = point(i, radius);
    const spoke = document.createElementNS(ns, "line");
    spoke.setAttribute("x1", cx); spoke.setAttribute("y1", cy);
    spoke.setAttribute("x2", x); spoke.setAttribute("y2", y);
    spoke.setAttribute("stroke", "rgba(255,255,255,0.07)");
    svg.appendChild(spoke);
  });
  // Only MEASURED axes get a vertex. Plotting a `null` axis at the centre
  // would visually claim "0/100" for a metric nobody measured - the same
  // false claim the data layer is careful to avoid.
  const measured = axes
    .map((axis, i) => ({ axis, i }))
    .filter((a) => a.axis.value !== null && a.axis.value !== undefined);

  if (measured.length >= 3) {
    const shape = document.createElementNS(ns, "polygon");
    shape.setAttribute("points", measured.map(({ axis, i }) =>
      point(i, radius * Math.max(0, Math.min(1, axis.value / 100))).join(",")
    ).join(" "));
    shape.setAttribute("fill", "rgba(0,240,255,0.20)");
    shape.setAttribute("stroke", COLORS.accent);
    shape.setAttribute("stroke-width", "1.8");
    svg.appendChild(shape);
  } else {
    measured.forEach(({ axis, i }) => {
      const [x, y] = point(i, radius * Math.max(0, Math.min(1, axis.value / 100)));
      const dot = document.createElementNS(ns, "circle");
      dot.setAttribute("cx", x); dot.setAttribute("cy", y);
      dot.setAttribute("r", "3"); dot.setAttribute("fill", COLORS.accent);
      svg.appendChild(dot);
    });
  }
  axes.forEach((axis, i) => {
    const [x, y] = point(i, radius + 18);
    const text = document.createElementNS(ns, "text");
    text.setAttribute("x", x); text.setAttribute("y", y);
    text.setAttribute("fill", "#94a3b8"); text.setAttribute("font-size", "8");
    text.setAttribute("text-anchor", "middle"); text.setAttribute("dominant-baseline", "middle");
    text.textContent = axis.label.split(" ").pop();
    svg.appendChild(text);
  });
  body.appendChild(svg);

  const legend = el("div", "radar-legend");
  axes.forEach((axis) => {
    const item = el("span", "item");
    item.appendChild(el("b", null, axis.value === null || axis.value === undefined ? "—" : axis.value.toFixed(0)));
    item.appendChild(document.createTextNode(" " + axis.label.split(" ").pop()));
    legend.appendChild(item);
  });
  body.appendChild(legend);
}

function renderCompare(view) {
  const body = $("compareBody");
  clear(body);
  const compare = view && view.compare;
  if (!compare || !compare.hasData) {
    body.appendChild(el("div", "empty", "no control/experiment traffic yet"));
    return;
  }
  const grid = el("div", "cmp-grid");
  const metrics = [
    ["success_rate", "Success", true], ["fallback_rate", "Fallback", false],
    ["duplicate_rate", "Duplicates", false], ["ts_rate", "TS", true],
  ];
  metrics.forEach(([field, label, higherIsBetter]) => {
    const rawControl = compare.control[field];
    const rawExperiment = compare.experiment[field];
    const measured = rawControl !== null && rawControl !== undefined &&
      rawExperiment !== null && rawExperiment !== undefined;
    const control = measured ? rawControl : 0;
    const experiment = measured ? rawExperiment : 0;

    const row = el("div", "cmp-row");
    row.appendChild(el("div", "cmp-label", label));
    const bars = el("div", "cmp-bars");
    [["control", control], ["experiment", experiment]].forEach(([which, value]) => {
      const line = el("div", "cmp-line");
      const bar = el("div", "cmp-bar " + which);
      const fill = el("i");
      fill.style.width = Math.max(0, Math.min(100, value * 100)) + "%";
      bar.appendChild(fill);
      line.appendChild(bar);
      line.appendChild(el("span", "cmp-val", measured ? Math.round(value * 100) + "%" : "—"));
      bars.appendChild(line);
    });
    row.appendChild(bars);

    const delta = (experiment - control) * 100;
    const cls = !measured || delta === 0 ? "" : (delta > 0) === higherIsBetter ? "up" : "down";
    row.appendChild(el("div", "cmp-delta " + cls,
      measured ? (delta >= 0 ? "+" : "") + delta.toFixed(1) + "%" : "—"));
    grid.appendChild(row);
  });
  body.appendChild(grid);
}

/* --- top level ---------------------------------------------------------- */
function render() {
  const payload = state.payload;
  const view = payload && payload.views ? payload.views[state.selected] : null;

  const db = $("dbChip");
  db.textContent = "db: " + (payload && payload.database && payload.database.ok ? "ok" : "down");
  db.className = "chip" + (payload && payload.database && payload.database.ok ? "" : " db-bad");

  renderFocusFields(view);
  renderOverall(view);
  renderError(view);
  renderLatencyTable("meanBody", "mean", payload || {});
  renderLatencyTable("p90Body", "p90", payload || {});
  renderLatencyTable("p99Body", "p99", payload || {});
  renderCharts(view);
  renderScorecards(view);
  renderRadar(view);
  renderCompare(view);
}

function apply(payload, focus) {
  state.payload = payload;
  if (focus) state.selected = focus;
  renderDropdown(payload);
  render();
}

async function refreshViaHost() {
  if (!window.pywebview || !window.pywebview.api) return;
  try {
    const fresh = await window.pywebview.api.get_snapshot(state.selected);
    apply(fresh, state.selected);
  } catch (err) { /* keep the last good render */ }
}

function init() {
  state.payload = readBoot();
  state.selected = state.payload.focus;
  renderDropdown(state.payload);
  render();

  $("apiKeySelect").addEventListener("change", (event) => {
    state.selected = event.target.value;
    render();
  });

  setInterval(refreshViaHost, 5000);
  window.addEventListener("pywebviewready", refreshViaHost);
}

document.addEventListener("DOMContentLoaded", init);
