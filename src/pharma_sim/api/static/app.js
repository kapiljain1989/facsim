/* Dashboard application logic.
 *
 * Read-only throughout: every interaction is a query or a view change. There is
 * no form, no mutation and no auth surface.
 *
 * In live mode a WebSocket carries the same messages the MQTT and JSONL sinks
 * receive, and the sensor charts append from it. In historical mode the same
 * charts read the stored series over REST. One rendering path, two sources.
 */

import {
  SERIES,
  barChart,
  fmt,
  hideTooltip,
  legend,
  lineChart,
  smallMultiples,
  statusBar,
} from "/static/charts.js";

/* --------------------------------------------------------------- constants */

/* Machine states mapped to the reserved status palette. Every use is paired
 * with an icon and the state's own name, so colour never carries meaning alone. */
const STATE_STATUS = {
  RUNNING:    { tone: "good",     mark: "●", color: "var(--good)" },
  PRODUCING:  { tone: "good",     mark: "●", color: "var(--good)" },
  WARNING:    { tone: "warning",  mark: "▲", color: "var(--warning)" },
  DEGRADED:   { tone: "warning",  mark: "▲", color: "var(--warning)" },
  FAULT:      { tone: "critical", mark: "■", color: "var(--critical)" },
  MAINTENANCE:{ tone: "serious",  mark: "✚", color: "var(--serious)" },
  SERVICE:    { tone: "serious",  mark: "✚", color: "var(--serious)" },
  CLEANING:   { tone: "neutral",  mark: "◌", color: "var(--neutral)" },
  CHANGEOVER: { tone: "neutral",  mark: "◌", color: "var(--neutral)" },
  STARTING:   { tone: "neutral",  mark: "▸", color: "var(--series-1)" },
  IDLE:       { tone: "neutral",  mark: "○", color: "var(--neutral)" },
  READY:      { tone: "neutral",  mark: "○", color: "var(--neutral)" },
  OFFLINE:    { tone: "neutral",  mark: "◻", color: "var(--axis)" },
  DOWN:       { tone: "neutral",  mark: "◻", color: "var(--axis)" },
};

const RESULT_STATUS = {
  PASS: { tone: "good", mark: "✓" },
  OOT:  { tone: "warning", mark: "▲" },
  FAIL: { tone: "critical", mark: "✕" },
  OOS:  { tone: "critical", mark: "✕" },
  WARNING: { tone: "warning", mark: "▲" },
};

const SEVERITY_TONE = {
  INFO: "neutral", MINOR: "warning", MAJOR: "serious", CRITICAL: "critical",
};

/* ------------------------------------------------------------------- state */

const state = {
  live: false,
  plant: null,
  units: [],
  machines: [],
  selectedMachine: null,
  machineWindow: 6,
  batches: [],
  selectedBatch: null,
  /* Live telemetry, keyed "machine:tag" -> [{t,v,q}] */
  series: new Map(),
  liveEvents: [],
  socket: null,
  dropped: 0,
};

const MAX_LIVE_POINTS = 720;

/* ---------------------------------------------------------------- helpers */

const $ = (id) => document.getElementById(id);

async function api(path) {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText} on ${path}`);
  }
  return response.json();
}

function banner(message, isError = false) {
  const node = $("banner");
  if (!message) {
    node.hidden = true;
    return;
  }
  node.hidden = false;
  node.className = `banner${isError ? " error" : ""}`;
  node.textContent = message;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
  );
}

function chip(label, tone = "neutral", mark = "•") {
  return `<span class="chip ${tone}"><span class="mark" aria-hidden="true">${mark}</span>${escapeHtml(
    label
  )}</span>`;
}

function stateChip(stateName) {
  if (!stateName) return `<span class="muted">–</span>`;
  const meta = STATE_STATUS[stateName] || { tone: "neutral", mark: "•" };
  return chip(stateName, meta.tone, meta.mark);
}

function resultChip(result) {
  if (!result) return `<span class="muted">–</span>`;
  const meta = RESULT_STATUS[result] || { tone: "neutral", mark: "•" };
  return chip(result, meta.tone, meta.mark);
}

function severityChip(severity) {
  if (!severity) return "";
  return chip(severity, SEVERITY_TONE[severity] || "neutral", "•");
}

function when(value) {
  if (!value) return "–";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 19);
  return date.toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function hours(seconds) {
  return seconds ? (seconds / 3600).toFixed(1) : "0.0";
}

function tile(label, value, unit = "", foot = "", tone = "") {
  return `<div class="card tile">
    <span class="label">${escapeHtml(label)}</span>
    <span class="value">${value}${unit ? `<span class="unit"> ${unit}</span>` : ""}</span>
    ${foot ? `<span class="foot ${tone}">${foot}</span>` : ""}
  </div>`;
}

function renderRows(tbody, rows, columns, emptyMessage = "nothing recorded yet") {
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="${columns}" class="empty">${emptyMessage}</td></tr>`;
    return false;
  }
  return true;
}

function parsePayload(payload) {
  if (!payload) return {};
  if (typeof payload === "object") return payload;
  try {
    return JSON.parse(payload);
  } catch {
    return {};
  }
}

function payloadSummary(payload) {
  const data = parsePayload(payload);
  const parts = Object.entries(data)
    .filter(([, value]) => value !== null && value !== "" && typeof value !== "object")
    .slice(0, 4)
    .map(([key, value]) => `${key}=${typeof value === "number" ? fmt(value) : value}`);
  return parts.join("  ");
}

/* ------------------------------------------------------------ navigation */

function showView(name, target = null) {
  for (const section of document.querySelectorAll(".view")) {
    section.hidden = section.id !== `view-${name}`;
  }
  for (const button of $("nav").querySelectorAll("button")) {
    if (button.dataset.view === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  hideTooltip();
  location.hash = target ? `${name}/${target}` : name;
  loadView(name).then(() => {
    if (!target) return;
    if (name === "machines") return selectMachine(target);
    if (name === "batches") return selectBatch(target);
  });
}

$("nav").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (button) showView(button.dataset.view);
});

$("theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const current = root.getAttribute("data-theme");
  const next = current === "dark" ? "light" : current === "light" ? "" : "dark";
  root.setAttribute("data-theme", next);
  localStorage.setItem("pharma-theme", next);
  // Charts embed resolved colours, so redraw on a theme change.
  loadView(currentView(), true);
});

const savedTheme = localStorage.getItem("pharma-theme");
if (savedTheme !== null) document.documentElement.setAttribute("data-theme", savedTheme);

function currentView() {
  const active = $("nav").querySelector('button[aria-current="page"]');
  return active ? active.dataset.view : "plant";
}

/* ------------------------------------------------------------- plant view */

async function loadHeader() {
  const { plant } = await api("/api/plant");
  state.plant = plant;
  $("plant-name").textContent = plant.name || "Pharmaceutical Factory Simulator";
  $("plant-sub").textContent = [
    plant.location,
    `${plant.unit_count ?? "?"} units`,
    `${plant.machine_count ?? "?"} machines`,
    plant.run?.run_id ? `run ${plant.run.run_id}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  $("footer-note").textContent = plant.storage
    ? `Stores — ${Object.entries(plant.storage).map(([k, v]) => `${k}: ${v}`).join("  ·  ")}. `
    : "";
}

async function loadPlant() {
  const [{ plant, summary }, trend, oeeTrend, units, events] = await Promise.all([
    api("/api/plant"),
    api("/api/trends/production?limit=60"),
    api("/api/trends/oee?scope=PLANT&limit=60"),
    api("/api/units"),
    api("/api/events?limit=40"),
  ]);
  state.plant = plant;
  state.units = units;

  $("plant-name").textContent = plant.name || "Pharmaceutical Factory Simulator";
  $("plant-sub").textContent = [
    plant.location,
    `${plant.unit_count ?? units.length} units`,
    `${plant.machine_count ?? "?"} machines`,
    plant.run?.run_id ? `run ${plant.run.run_id}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  $("footer-note").textContent = plant.storage
    ? `Stores — ${Object.entries(plant.storage).map(([k, v]) => `${k}: ${v}`).join("  ·  ")}. `
    : "";

  const byState = summary.machines_by_state || {};
  const running = Object.entries(byState)
    .filter(([name]) => (STATE_STATUS[name] || {}).tone === "good")
    .reduce((sum, [, count]) => sum + count, 0);
  const warning = (byState.WARNING || 0) + (byState.DEGRADED || 0);
  const fault = (byState.FAULT || 0) + (byState.SERVICE || 0);
  const production = summary.production || {};
  const oee = summary.oee || {};
  const batches = summary.batches || {};
  const qc = summary.qc || {};
  const deviations = summary.deviations || {};
  const reliability = summary.reliability || {};

  const totalProduced = (production.good || 0) + (production.reject || 0);
  const rejectRate = totalProduced ? (production.reject || 0) / totalProduced : 0;

  $("plant-tiles").innerHTML = [
    tile("Machines producing", running, `of ${summary.machine_count}`,
      `${warning} in warning · ${fault} down`, fault ? "bad" : ""),
    tile("Plant OEE", oee.oee ? (oee.oee * 100).toFixed(1) : "–", "%",
      oee.availability
        ? `A ${(oee.availability * 100).toFixed(0)} · P ${(oee.performance * 100).toFixed(0)} · Q ${(oee.quality * 100).toFixed(0)}`
        : ""),
    tile("Good units", fmt(production.good || 0), "",
      `${fmt(production.reject || 0)} rejected (${(rejectRate * 100).toFixed(2)}%)`),
    tile("Batches completed", batches.total || 0, "",
      `${batches.released || 0} released · ${batches.rejected || 0} rejected`,
      batches.rejected ? "bad" : "good"),
    tile("QC failures", qc.failed || 0, `of ${fmt(qc.total || 0)}`,
      qc.total ? `${(((qc.failed || 0) / qc.total) * 100).toFixed(2)}% of tests` : ""),
    tile("Open deviations", deviations.open || 0, `of ${deviations.total || 0}`,
      "investigations and CAPAs"),
    tile("Failures", reliability.failures || 0, "",
      `${fmt(reliability.downtime_minutes || 0)} min downtime`),
    tile("Utilisation", oee.utilisation ? (oee.utilisation * 100).toFixed(1) : "–", "%",
      `${hours(production.runtime || 0)} h running of ${hours(
        (production.runtime || 0) + (production.downtime || 0) + (production.unscheduled || 0)
      )} h`),
    tile("Downtime", hours(production.downtime || 0), "h", "unplanned stops"),
  ].join("");
  $("plant-hint").textContent = state.live
    ? "live — figures update as the plant runs"
    : "historical — from the stored dataset";

  // Machine-state distribution.
  const segments = Object.entries(byState)
    .sort((a, b) => b[1] - a[1])
    .map(([name, count]) => ({
      label: name,
      value: count,
      color: (STATE_STATUS[name] || {}).color || "var(--neutral)",
    }));
  document.querySelector("#view-plant .card h3").textContent = state.live
    ? "Machine state right now"
    : "Machine state at the end of the run";
  statusBar($("state-bar"), segments);
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  $("state-table").innerHTML = segments
    .map(
      (segment) =>
        `<tr><td>${stateChip(segment.label)}</td><td class="num">${segment.value}</td>
         <td class="num">${((segment.value / total) * 100).toFixed(0)}%</td></tr>`
    )
    .join("");

  // Production per shift — two series, same unit, one axis.
  const ordered = [...trend].reverse();
  $("legend-production").innerHTML = "";
  legend($("legend-production"), [{ label: "Good units" }, { label: "Rejected units" }]);
  lineChart(
    $("chart-production"),
    [
      { label: "Good units", points: ordered.map((r) => ({ t: r.start_time, v: r.good_quantity || 0 })) },
      { label: "Rejected units", points: ordered.map((r) => ({ t: r.start_time, v: r.reject_quantity || 0 })) },
    ],
    { height: 200 }
  );

  // OEE components — all ratios, so sharing an axis is honest.
  const oeeOrdered = [...oeeTrend].reverse();
  $("legend-oee").innerHTML = "";
  legend($("legend-oee"), [
    { label: "Availability" }, { label: "Performance" }, { label: "Quality" },
  ]);
  lineChart(
    $("chart-oee"),
    [
      { label: "Availability", points: oeeOrdered.map((r) => ({ t: r.start_time, v: r.availability })) },
      { label: "Performance", points: oeeOrdered.map((r) => ({ t: r.start_time, v: r.performance })) },
      { label: "Quality", points: oeeOrdered.map((r) => ({ t: r.start_time, v: r.quality })) },
    ],
    { height: 200, valueDigits: 2 }
  );

  barChart(
    $("chart-unit-oee"),
    units.map((unit) => ({
      label: unit.unit_id,
      value: (unit.oee || 0) * 100,
      detail: unit.name,
    })),
    { unit: "%", valueDigits: 1, metric: "OEE" }
  );

  renderTimeline($("plant-timeline"), events);
}

function renderTimeline(node, events) {
  if (!events.length) {
    node.innerHTML = '<li class="empty">no events yet</li>';
    return;
  }
  node.innerHTML = events
    .map((event) => {
      const tone = SEVERITY_TONE[event.severity] || "neutral";
      const target = [event.machine_id, event.batch_id, event.employee_id]
        .filter(Boolean)
        .join(" · ");
      const detail = payloadSummary(event.payload);
      return `<li>
        <span class="when">${when(event.timestamp)}</span>
        <span class="rail"><span class="node ${tone}"></span></span>
        <span class="what"><strong>${escapeHtml(event.event_type)}</strong>
          ${target ? ` <span class="muted">${escapeHtml(target)}</span>` : ""}
          ${detail ? `<div class="detail">${escapeHtml(detail)}</div>` : ""}
        </span></li>`;
    })
    .join("");
}

/* ------------------------------------------------------------- units view */

async function loadUnits() {
  const units = await api("/api/units");
  state.units = units;
  const tbody = $("units-table");
  if (!renderRows(tbody, units, 10)) return;
  tbody.innerHTML = units
    .map(
      (unit) => `<tr>
      <td class="mono">${escapeHtml(unit.unit_id)}</td>
      <td>${escapeHtml(unit.name)}</td>
      <td>${escapeHtml(unit.process_stage)}</td>
      <td class="num">${unit.machines}</td>
      <td class="num">${unit.worker_count}</td>
      <td class="num">${unit.oee ? (unit.oee * 100).toFixed(1) + "%" : "–"}</td>
      <td class="num">${fmt(unit.good_quantity || 0)}</td>
      <td class="num">${fmt(unit.reject_quantity || 0)}</td>
      <td class="num">${hours(unit.downtime_seconds)}</td>
      <td class="num">${unit.failures || 0}</td></tr>`
    )
    .join("");

  barChart(
    $("chart-unit-output"),
    units.map((u) => ({ label: u.unit_id, value: u.good_quantity || 0, detail: u.name })),
    { metric: "good units" }
  );
  barChart(
    $("chart-unit-downtime"),
    units.map((u) => ({
      label: u.unit_id,
      value: (u.downtime_seconds || 0) / 3600,
      detail: u.name,
    })),
    { unit: "h", valueDigits: 1, metric: "downtime" }
  );
}

/* ---------------------------------------------------------- machines view */

async function loadMachines() {
  const machines = await api("/api/machines");
  state.machines = machines;

  const unitFilter = $("machine-unit-filter");
  if (unitFilter.options.length <= 1) {
    const units = state.units.length ? state.units : await api("/api/units");
    state.units = units;
    for (const unit of units) {
      unitFilter.add(new Option(`${unit.unit_id} — ${unit.name}`, unit.unit_id));
    }
  }
  const stateFilter = $("machine-state-filter");
  if (stateFilter.options.length <= 1) {
    for (const name of [...new Set(machines.map((m) => m.state).filter(Boolean))].sort()) {
      stateFilter.add(new Option(name, name));
    }
  }
  renderMachinesTable();
}

function renderMachinesTable() {
  const unit = $("machine-unit-filter").value;
  const stateName = $("machine-state-filter").value;
  const search = $("machine-search").value.trim().toLowerCase();

  const rows = state.machines.filter(
    (machine) =>
      (!unit || machine.unit_id === unit) &&
      (!stateName || machine.state === stateName) &&
      (!search ||
        machine.machine_id.toLowerCase().includes(search) ||
        (machine.equipment_class || "").toLowerCase().includes(search))
  );

  const tbody = $("machines-table");
  if (!renderRows(tbody, rows, 11, "no machines match these filters")) return;
  tbody.innerHTML = rows
    .map(
      (machine) => `<tr class="clickable" data-machine="${escapeHtml(machine.machine_id)}">
      <td class="mono">${escapeHtml(machine.machine_id)}</td>
      <td>${escapeHtml(machine.equipment_class)}</td>
      <td>${escapeHtml(machine.unit_id)}</td>
      <td>${stateChip(machine.state)}</td>
      <td class="num">${machine.oee ? (machine.oee * 100).toFixed(1) + "%" : "–"}</td>
      <td class="num">${fmt(machine.good_quantity || 0)}</td>
      <td class="num">${fmt(machine.reject_quantity || 0)}</td>
      <td class="num">${hours(machine.runtime_seconds)}</td>
      <td class="num">${hours(machine.downtime_seconds)}</td>
      <td class="num">${machine.failures || 0}</td>
      <td class="num">${machine.sensor_count || 0}</td></tr>`
    )
    .join("");
}

for (const id of ["machine-unit-filter", "machine-state-filter", "machine-search"]) {
  $(id).addEventListener("input", renderMachinesTable);
}

$("machines-table").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-machine]");
  if (row) {
    location.hash = `machines/${row.dataset.machine}`;
    selectMachine(row.dataset.machine);
  }
});

$("machine-window").addEventListener("change", () => {
  state.machineWindow = Number($("machine-window").value);
  if (state.selectedMachine) selectMachine(state.selectedMachine);
});

async function selectMachine(machineId) {
  state.selectedMachine = machineId;
  const panel = $("machine-detail");
  panel.hidden = false;

  const [machine, timeline] = await Promise.all([
    api(`/api/machines/${encodeURIComponent(machineId)}`),
    api(`/api/machines/${encodeURIComponent(machineId)}/timeline?limit=40`),
  ]);

  $("machine-detail-title").textContent = `${machine.machine_id} — ${machine.name || machine.equipment_class}`;
  $("machine-detail-sub").textContent = `${machine.unit_name} · ${machine.process_stage} · commissioned ${
    machine.commissioned_on || "?"
  }`;

  $("machine-facts").innerHTML = `
    <dt>State</dt><dd>${stateChip(machine.current_state)}</dd>
    <dt>Equipment class</dt><dd>${escapeHtml(machine.equipment_class)}</dd>
    <dt>Sensor profile</dt><dd>${escapeHtml(machine.sensor_profile || "inline only")}</dd>
    <dt>PLC</dt><dd class="mono">${escapeHtml(machine.plc_id || "–")}</dd>
    <dt>Nominal rate</dt><dd>${fmt(machine.nominal_rate_per_hour)} /h</dd>
    <dt>PM interval</dt><dd>${fmt(machine.pm_interval_hours)} h</dd>
    <dt>Sensors</dt><dd>${machine.sensors.length}</dd>
    <dt>Failures</dt><dd>${machine.failures.length}</dd>
    <dt>Maintenance</dt><dd>${machine.maintenance.length} actions</dd>`;

  renderTimeline($("machine-timeline"), timeline.slice(0, 14));

  // Prefer the tags a diagnostic reader cares about, then fill up to eight.
  const preferred = [
    "vibration", "motor_current", "temperature", "torque", "main_compression_force",
    "tablet_weight", "pressure", "airflow", "spray_rate", "fill_volume_ml",
    "pump_pressure", "line_speed", "differential_pressure", "room_temperature",
  ];
  const analog = machine.sensors.filter((sensor) => !sensor.derived_from);
  analog.sort((a, b) => {
    const rank = (tag) => {
      const index = preferred.indexOf(tag);
      return index === -1 ? 99 : index;
    };
    return rank(a.tag) - rank(b.tag) || a.tag.localeCompare(b.tag);
  });
  const chosen = analog.slice(0, 8);

  const panels = [];
  const values = [];
  for (const sensor of chosen) {
    let points = [];
    if (state.live) {
      points = state.series.get(`${machineId}:${sensor.tag}`) || [];
    }
    if (!points.length) {
      try {
        const series = await api(
          `/api/machines/${encodeURIComponent(machineId)}/sensors/${encodeURIComponent(
            sensor.tag
          )}/series?hours=${state.machineWindow}&limit=900`
        );
        points = series.points || [];
      } catch (error) {
        points = [];
      }
    }
    const limits = [];
    if (sensor.warn_high !== null && sensor.warn_high !== undefined)
      limits.push({ value: sensor.warn_high, label: "warn high" });
    if (sensor.warn_low !== null && sensor.warn_low !== undefined)
      limits.push({ value: sensor.warn_low, label: "warn low" });
    if (sensor.alarm_high !== null && sensor.alarm_high !== undefined)
      limits.push({ value: sensor.alarm_high, label: "alarm high", critical: true });
    if (sensor.alarm_low !== null && sensor.alarm_low !== undefined)
      limits.push({ value: sensor.alarm_low, label: "alarm low", critical: true });

    panels.push({
      title: sensor.tag,
      subtitle: sensor.unit ? `${sensor.unit} · ${points.length} points` : `${points.length} points`,
      unit: sensor.unit,
      points,
      limits,
    });
    const last = points.length ? points[points.length - 1] : null;
    values.push({ tag: sensor.tag, unit: sensor.unit, last });
  }

  $("machine-values").innerHTML =
    values
      .map((row) => {
        const quality = row.last ? row.last.q : null;
        const tone = quality === "BAD" ? "critical" : quality === "UNCERTAIN" ? "warning" : "good";
        const mark = quality === "BAD" ? "✕" : quality === "UNCERTAIN" ? "▲" : "✓";
        return `<tr><td class="mono">${escapeHtml(row.tag)}</td>
          <td class="num">${row.last ? fmt(row.last.v) : "–"}</td>
          <td class="muted">${escapeHtml(row.unit || "")}</td>
          <td>${quality ? chip(quality, tone, mark) : '<span class="muted">–</span>'}</td></tr>`;
      })
      .join("") || `<tr><td colspan="4" class="empty">no readings</td></tr>`;

  smallMultiples($("machine-charts"), panels, { height: 132 });
}

/* ----------------------------------------------------------- batches view */

async function loadBatches() {
  const disposition = $("batch-disposition").value;
  const batches = await api(
    `/api/batches?limit=400${disposition ? `&disposition=${disposition}` : ""}`
  );
  state.batches = batches;
  const tbody = $("batches-table");
  if (!renderRows(tbody, batches, 8, "no batches yet — run a simulation first")) return;
  tbody.innerHTML = batches
    .map((batch) => {
      const tone =
        batch.disposition === "RELEASED"
          ? "good"
          : batch.disposition === "REJECTED"
          ? "critical"
          : batch.disposition === "QUARANTINED"
          ? "warning"
          : "neutral";
      const mark = tone === "good" ? "✓" : tone === "critical" ? "✕" : tone === "warning" ? "▲" : "◌";
      return `<tr class="clickable" data-batch="${escapeHtml(batch.batch_id)}">
      <td class="mono">${escapeHtml(batch.batch_id)}</td>
      <td>${escapeHtml(batch.product_id)}</td>
      <td>${chip(batch.disposition, tone, mark)}</td>
      <td class="num">${batch.stages_completed || 0}</td>
      <td class="num">${batch.qc_test_count || 0}</td>
      <td class="num">${batch.qc_failure_count || 0}</td>
      <td>${when(batch.started_at)}</td>
      <td>${when(batch.completed_at)}</td></tr>`;
    })
    .join("");
}

$("batch-disposition").addEventListener("change", loadBatches);
$("batches-table").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-batch]");
  if (row) {
    location.hash = `batches/${row.dataset.batch}`;
    selectBatch(row.dataset.batch);
  }
});

async function selectBatch(batchId) {
  state.selectedBatch = batchId;
  $("batch-detail").hidden = false;
  const [batch, timeline] = await Promise.all([
    api(`/api/batches/${encodeURIComponent(batchId)}`),
    api(`/api/batches/${encodeURIComponent(batchId)}/timeline`),
  ]);

  $("batch-detail-title").textContent = `${batch.batch_id} — ${batch.product_id}`;
  $("batch-detail-sub").textContent = [
    batch.disposition,
    `${batch.stages_completed || 0} stages`,
    `${batch.qc_test_count || 0} QC tests`,
    batch.machines_used ? `${batch.machines_used.split(",").length} machines` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  $("batch-stages").innerHTML = batch.stages
    .map((stage) => {
      const parameters = parsePayload(stage.parameters);
      const rendered = Object.entries(parameters)
        .map(([key, value]) => `${key} ${fmt(value)}`)
        .join(", ");
      const deviating = stage.deviating_parameters
        ? ` <span class="chip warning"><span class="mark">▲</span>${escapeHtml(
            stage.deviating_parameters
          )}</span>`
        : "";
      return `<tr><td class="num">${stage.sequence}</td>
        <td>${escapeHtml(stage.stage)}</td>
        <td class="mono">${escapeHtml(stage.machine_id)}</td>
        <td>${resultChip(stage.result)}</td>
        <td class="wrap">${escapeHtml(rendered)}${deviating}</td></tr>`;
    })
    .join("") || `<tr><td colspan="5" class="empty">no stages</td></tr>`;

  $("batch-qc").innerHTML = batch.qc_results
    .map(
      (result) => `<tr>
      <td>${escapeHtml(result.parameter_name || result.parameter)}</td>
      <td class="muted">${escapeHtml(result.phase)}</td>
      <td class="num">${fmt(result.actual_value)}</td>
      <td class="num">${fmt(result.target)}</td>
      <td class="muted">${result.lower_limit ?? "–"} … ${result.upper_limit ?? "–"}</td>
      <td>${resultChip(result.result)}</td></tr>`
    )
    .join("") || `<tr><td colspan="6" class="empty">no QC results</td></tr>`;

  const investigations = [];
  for (const deviation of batch.deviations) {
    investigations.push(`<div class="card" style="margin-bottom:10px">
      <h3>${escapeHtml(deviation.deviation_id)} — ${escapeHtml(deviation.title)}</h3>
      <p class="card-sub">${severityChip(deviation.severity)} ${chip(
      deviation.status,
      deviation.status === "CLOSED" ? "good" : "warning",
      deviation.status === "CLOSED" ? "✓" : "▲"
    )}</p>
      <div class="muted">${escapeHtml(deviation.description || "")}</div>
    </div>`);
  }
  for (const report of batch.rca || []) {
    const whys = (report.five_why || "")
      .split(" | ")
      .filter(Boolean)
      .map((why) => `<li>${escapeHtml(why)}</li>`)
      .join("");
    investigations.push(`<div class="card" style="margin-bottom:10px">
      <h3>${escapeHtml(report.rca_id)} — root cause: ${escapeHtml(report.root_cause)}</h3>
      <p class="card-sub">confidence ${(report.confidence * 100).toFixed(0)}% ·
        ${escapeHtml(report.method)} · ${escapeHtml(report.fishbone_category)}</p>
      <div class="muted">${escapeHtml(report.evidence_summary || "")}</div>
      ${whys ? `<ol class="why-list">${whys}</ol>` : ""}
      <div style="margin-top:8px"><strong>Corrective:</strong> ${escapeHtml(
        report.corrective_action || "–"
      )}</div>
      <div><strong>Preventive:</strong> ${escapeHtml(report.preventive_action || "–")}</div>
    </div>`);
  }
  for (const capa of batch.capa || []) {
    investigations.push(`<div class="card">
      <h3>${escapeHtml(capa.capa_id)} — ${escapeHtml(capa.status)}</h3>
      <p class="card-sub">verification ${capa.verification_batches_passed}/${
      capa.verification_batches_required
    } batches</p>
      <div class="muted">${escapeHtml(capa.preventive_action || "")}</div></div>`);
  }
  $("batch-investigations").innerHTML =
    investigations.join("") ||
    '<div class="empty">no deviation was raised for this batch</div>';

  renderTimeline($("batch-timeline"), timeline.slice().reverse().slice(0, 30));
}

/* ----------------------------------------------------------- quality view */

async function loadQuality() {
  const [byParameter, deviations, qc] = await Promise.all([
    api("/api/qc/by-parameter"),
    api("/api/deviations?limit=200"),
    api(`/api/qc?limit=300${$("qc-result-filter").value ? `&result=${$("qc-result-filter").value}` : ""}`),
  ]);

  const tests = byParameter.reduce((sum, row) => sum + (row.tests || 0), 0);
  const failures = byParameter.reduce((sum, row) => sum + (row.failures || 0), 0);
  const oot = byParameter.reduce((sum, row) => sum + (row.out_of_trend || 0), 0);
  const open = deviations.filter((d) => d.status !== "CLOSED").length;
  $("quality-tiles").innerHTML = [
    tile("QC tests", fmt(tests)),
    tile("Failures", failures, "", tests ? `${((failures / tests) * 100).toFixed(2)}% of tests` : "",
      failures ? "bad" : "good"),
    tile("Out of trend", oot, "", "inside limits but drifting"),
    tile("Deviations", deviations.length, "", `${open} still open`),
  ].join("");

  const failing = byParameter.filter((row) => (row.failures || 0) > 0);
  barChart(
    $("chart-qc-failures"),
    failing.map((row) => ({
      label: row.parameter,
      value: row.failures,
      detail: `${row.tests} tests`,
    })),
    { metric: "failures", valueDigits: 0 }
  );

  $("qc-parameter-table").innerHTML = byParameter
    .map(
      (row) => `<tr>
      <td>${escapeHtml(row.parameter_name || row.parameter)}</td>
      <td class="muted">${escapeHtml(row.phase)}</td>
      <td class="num">${row.tests}</td>
      <td class="num">${row.failures || 0}</td>
      <td class="num">${row.out_of_trend || 0}</td>
      <td class="num">${fmt(row.mean_value)}</td>
      <td class="num">${fmt(row.target)}</td></tr>`
    )
    .join("");

  $("deviations-table").innerHTML =
    deviations
      .map(
        (deviation) => `<tr>
      <td class="mono">${escapeHtml(deviation.deviation_id)}</td>
      <td>${severityChip(deviation.severity)}</td>
      <td>${chip(
        deviation.status,
        deviation.status === "CLOSED" ? "good" : "warning",
        deviation.status === "CLOSED" ? "✓" : "▲"
      )}</td>
      <td class="mono">${escapeHtml(deviation.machine_id || "–")}</td>
      <td class="mono">${escapeHtml(deviation.batch_id || "–")}</td>
      <td class="wrap">${escapeHtml(deviation.description || "")}</td>
      <td class="mono">${escapeHtml(deviation.rca_id || "–")}</td>
      <td class="mono">${escapeHtml(deviation.capa_id || "–")}</td></tr>`
      )
      .join("") || `<tr><td colspan="8" class="empty">no deviations</td></tr>`;

  $("qc-table").innerHTML =
    qc
      .map(
        (result) => `<tr>
      <td class="mono">${escapeHtml(result.test_id)}</td>
      <td class="mono">${escapeHtml(result.batch_id)}</td>
      <td>${escapeHtml(result.parameter)}</td>
      <td class="muted">${escapeHtml(result.phase)}</td>
      <td class="num">${fmt(result.actual_value)}</td>
      <td class="num">${fmt(result.target)}</td>
      <td class="muted">${result.lower_limit ?? "–"} … ${result.upper_limit ?? "–"}</td>
      <td>${resultChip(result.result)}</td>
      <td>${when(result.timestamp)}</td></tr>`
      )
      .join("") || `<tr><td colspan="9" class="empty">no QC results</td></tr>`;
}

$("qc-result-filter").addEventListener("change", loadQuality);

/* ------------------------------------------------------- reliability view */

async function loadReliability() {
  const [failures, byCategory, rca, capa, maintenance] = await Promise.all([
    api("/api/failures?limit=200"),
    api("/api/failures/by-category"),
    api("/api/rca?limit=200"),
    api("/api/capa?limit=200"),
    api("/api/maintenance?limit=200"),
  ]);

  const downtime = byCategory.reduce((sum, row) => sum + (row.downtime_minutes || 0), 0);
  const closed = capa.filter((c) => c.status === "CLOSED").length;
  const preventive = maintenance.filter((m) => m.maintenance_type === "PREVENTIVE").length;
  $("reliability-tiles").innerHTML = [
    tile("Failures", failures.length, "", `${byCategory.length} categories`),
    tile("Downtime", fmt(downtime), "min", "across all failures"),
    tile("Investigations", rca.length, "", `${capa.length} CAPAs raised`),
    tile("CAPAs closed", closed, `of ${capa.length}`, "verified by good batches",
      closed ? "good" : ""),
    tile("Maintenance", maintenance.length, "", `${preventive} preventive`),
  ].join("");

  barChart(
    $("chart-failure-downtime"),
    byCategory.map((row) => ({
      label: row.category,
      value: row.downtime_minutes || 0,
      detail: `${row.count} failures`,
    })),
    { unit: "min", valueDigits: 0, metric: "downtime" }
  );

  const causeCounts = new Map();
  for (const report of rca) {
    causeCounts.set(report.root_cause, (causeCounts.get(report.root_cause) || 0) + 1);
  }
  barChart(
    $("chart-root-causes"),
    [...causeCounts.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 10)
      .map(([cause, count]) => ({ label: cause, value: count })),
    { metric: "investigations", valueDigits: 0, labelWidth: 190 }
  );

  $("failures-table").innerHTML =
    failures
      .map(
        (failure) => `<tr>
      <td class="mono">${escapeHtml(failure.failure_id)}</td>
      <td class="mono">${escapeHtml(failure.machine_id)}</td>
      <td>${escapeHtml(failure.category)}</td>
      <td>${severityChip(failure.severity)}</td>
      <td class="wrap">${escapeHtml(failure.symptom || "")}</td>
      <td class="num">${fmt(failure.downtime_minutes || 0)}</td>
      <td>${when(failure.detected_at)}</td>
      <td>${when(failure.resolved_at)}</td></tr>`
      )
      .join("") || `<tr><td colspan="8" class="empty">no failures recorded</td></tr>`;

  $("rca-table").innerHTML =
    rca
      .map(
        (report) => `<tr>
      <td class="mono">${escapeHtml(report.rca_id)}</td>
      <td class="mono">${escapeHtml(report.machine_id || "–")}</td>
      <td>${escapeHtml(report.root_cause)}</td>
      <td class="num">${(report.confidence * 100).toFixed(0)}%</td>
      <td class="muted">${escapeHtml(report.fishbone_category)}</td>
      <td class="wrap">${escapeHtml(report.evidence_summary || "")}</td></tr>`
      )
      .join("") || `<tr><td colspan="6" class="empty">no investigations</td></tr>`;

  $("capa-table").innerHTML =
    capa
      .map(
        (item) => `<tr>
      <td class="mono">${escapeHtml(item.capa_id)}</td>
      <td>${chip(
        item.status,
        item.status === "CLOSED" ? "good" : "warning",
        item.status === "CLOSED" ? "✓" : "▲"
      )}</td>
      <td>${escapeHtml(item.root_cause)}</td>
      <td class="num">${item.verification_batches_passed}/${item.verification_batches_required}</td>
      <td class="wrap">${escapeHtml(item.corrective_action || "")}</td>
      <td class="wrap">${escapeHtml(item.preventive_action || "")}</td></tr>`
      )
      .join("") || `<tr><td colspan="6" class="empty">no CAPAs</td></tr>`;

  $("maintenance-table").innerHTML =
    maintenance
      .map(
        (record) => `<tr>
      <td class="mono">${escapeHtml(record.maintenance_id)}</td>
      <td class="mono">${escapeHtml(record.machine_id)}</td>
      <td>${escapeHtml(record.maintenance_type)}</td>
      <td>${chip(
        record.status,
        record.status === "COMPLETED" ? "good" : record.status === "DEFERRED" ? "warning" : "neutral",
        record.status === "COMPLETED" ? "✓" : record.status === "DEFERRED" ? "▲" : "◌"
      )}</td>
      <td class="num">${fmt(record.duration_hours)}</td>
      <td class="num">${fmt(record.cost)}</td>
      <td class="wrap">${escapeHtml(record.parts_replaced || "")}</td>
      <td>${when(record.scheduled_time)}</td></tr>`
      )
      .join("") || `<tr><td colspan="8" class="empty">no maintenance recorded</td></tr>`;
}

/* ----------------------------------------------------------- people view */

async function loadPeople() {
  const [shifts, employees, units] = await Promise.all([
    api("/api/shifts?limit=120"),
    api("/api/employees"),
    state.units.length ? Promise.resolve(state.units) : api("/api/units"),
  ]);
  state.units = units;

  const filter = $("people-unit-filter");
  if (filter.options.length <= 1) {
    for (const unit of units) filter.add(new Option(`${unit.unit_id} — ${unit.name}`, unit.unit_id));
  }

  $("shifts-table").innerHTML =
    shifts
      .map(
        (shift) => `<tr>
      <td class="mono">${escapeHtml(shift.shift_instance_id)}</td>
      <td>${escapeHtml(shift.shift_code)}</td>
      <td>${escapeHtml(String(shift.business_date || "").slice(0, 10))}</td>
      <td>${when(shift.start_time)}</td>
      <td>${when(shift.end_time)}</td>
      <td class="num">${shift.roster_size}</td>
      <td class="num">${shift.present_count}</td>
      <td class="num">${shift.absent_count}</td>
      <td class="num">${shift.oee ? (shift.oee * 100).toFixed(1) + "%" : "–"}</td></tr>`
      )
      .join("") || `<tr><td colspan="9" class="empty">no shifts yet</td></tr>`;

  const selected = filter.value;
  const rows = employees.filter((e) => !selected || e.unit_id === selected);
  $("employees-table").innerHTML =
    rows
      .map(
        (employee) => `<tr>
      <td class="mono">${escapeHtml(employee.employee_id)}</td>
      <td>${escapeHtml(employee.name)}</td>
      <td>${escapeHtml(employee.unit_id || "plant-wide")}</td>
      <td>${escapeHtml(employee.role)}</td>
      <td>${escapeHtml(employee.skill_level)}</td>
      <td>${escapeHtml(employee.shift_code)}</td>
      <td class="num">${fmt(employee.experience_years, 1)}</td>
      <td class="num">${(employee.attendance_probability * 100).toFixed(1)}%</td>
      <td class="wrap muted">${escapeHtml(employee.machine_certifications || "")}</td></tr>`
      )
      .join("") || `<tr><td colspan="9" class="empty">no employees</td></tr>`;
}

$("people-unit-filter").addEventListener("change", loadPeople);

/* ----------------------------------------------------------- events view */

async function loadEvents() {
  const category = $("event-category").value;
  const severity = $("event-severity").value;
  const events = await api(
    `/api/events?limit=500${category ? `&category=${category}` : ""}${
      severity ? `&severity=${severity}` : ""
    }`
  );

  const categorySelect = $("event-category");
  if (categorySelect.options.length <= 1) {
    const all = await api("/api/events?limit=1500");
    for (const name of [...new Set(all.map((e) => e.category).filter(Boolean))].sort()) {
      categorySelect.add(new Option(name, name));
    }
  }

  $("events-hint").textContent = state.live
    ? `live — ${state.liveEvents.length} received this session`
    : "from the stored dataset";

  $("events-table").innerHTML =
    events
      .map(
        (event) => `<tr>
      <td>${when(event.timestamp)}</td>
      <td><strong>${escapeHtml(event.event_type)}</strong></td>
      <td>${severityChip(event.severity)}</td>
      <td class="mono">${escapeHtml(event.unit_id || "–")}</td>
      <td class="mono">${escapeHtml(event.machine_id || "–")}</td>
      <td class="mono">${escapeHtml(event.batch_id || "–")}</td>
      <td class="wrap muted">${escapeHtml(payloadSummary(event.payload))}</td></tr>`
      )
      .join("") || `<tr><td colspan="7" class="empty">no events</td></tr>`;
}

for (const id of ["event-category", "event-severity"]) {
  $(id).addEventListener("change", loadEvents);
}

/* ------------------------------------------------------------- live feed */

function ingest(messages) {
  for (const message of messages) {
    if (message.kind === "telemetry") {
      const key = `${message.machine_id}:${message.tag}`;
      let points = state.series.get(key);
      if (!points) {
        points = [];
        state.series.set(key, points);
      }
      points.push({ t: message.timestamp, v: message.value, q: message.quality });
      if (points.length > MAX_LIVE_POINTS) points.splice(0, points.length - MAX_LIVE_POINTS);
    } else if (message.kind === "event") {
      state.liveEvents.unshift(message);
      if (state.liveEvents.length > 400) state.liveEvents.length = 400;
    }
  }
}

function connectLive() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws/live`);
  state.socket = socket;

  socket.addEventListener("open", () => setLiveBadge("live", "live"));
  socket.addEventListener("close", () => {
    setLiveBadge("idle", "disconnected");
    setTimeout(connectLive, 4000);
  });
  socket.addEventListener("error", () => setLiveBadge("error", "feed error"));
  socket.addEventListener("message", (event) => {
    const frame = JSON.parse(event.data);
    ingest(frame.messages || []);
    if (frame.dropped) state.dropped = frame.dropped;
  });
}

function setLiveBadge(mode, text) {
  const badge = $("live-badge");
  badge.classList.toggle("is-live", mode === "live");
  badge.classList.toggle("is-error", mode === "error");
  $("live-text").textContent =
    state.dropped && mode === "live" ? `${text} · ${state.dropped} dropped` : text;
}

/* --------------------------------------------------------------- dispatch */

const LOADERS = {
  plant: loadPlant,
  units: loadUnits,
  machines: loadMachines,
  batches: loadBatches,
  quality: loadQuality,
  reliability: loadReliability,
  people: loadPeople,
  events: loadEvents,
};

let loading = false;
async function loadView(name, force = false) {
  if (loading && !force) return;
  loading = true;
  try {
    banner("");
    await LOADERS[name]?.();
  } catch (error) {
    banner(`Could not load the ${name} view: ${error.message}`, true);
    console.error(error);
  } finally {
    loading = false;
  }
}

async function boot() {
  try {
    const health = await api("/api/health");
    state.live = Boolean(health.live);
    if (health.error) banner(`Live simulation error: ${health.error}`, true);
  } catch (error) {
    banner(`API unreachable: ${error.message}`, true);
  }

  try {
    await loadHeader();
  } catch (error) {
    console.error(error);
  }

  if (state.live) {
    setLiveBadge("idle", "connecting");
    connectLive();
    // The stored dataset also grows as the live plant runs, so refresh the
    // active view periodically rather than only on navigation.
    setInterval(() => {
      const view = currentView();
      if (view === "machines" && state.selectedMachine) {
        selectMachine(state.selectedMachine).catch(() => {});
      } else {
        loadView(view, true);
      }
    }, 12000);
  } else {
    setLiveBadge("idle", "historical");
  }

  const [initial, target] = (location.hash || "#plant").slice(1).split("/");
  showView(LOADERS[initial] ? initial : "plant", target ? decodeURIComponent(target) : null);
}

window.addEventListener("resize", () => {
  clearTimeout(window.__resizeTimer);
  window.__resizeTimer = setTimeout(() => loadView(currentView(), true), 250);
});

boot();
