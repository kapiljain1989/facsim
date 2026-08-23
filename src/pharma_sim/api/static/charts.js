/* Chart primitives, hand-built as SVG.
 *
 * Written directly rather than pulled from a charting library so the mark specs
 * are exact: 2px lines, ≥8px markers with a 2px surface ring, hairline solid
 * gridlines, a 2px surface gap between touching bars, 4px rounded data-ends, and
 * selective direct labels instead of a value on every point.
 *
 * Two rules are enforced structurally here, not left to the caller:
 *   - No dual axes. A chart has one y-scale. Different units become separate
 *     charts (see smallMultiples).
 *   - At most three series on one plot. The fourth categorical slot fails the
 *     normal-vision separation floor against the second, so beyond three the
 *     caller must facet.
 */

const SVG_NS = "http://www.w3.org/2000/svg";
export const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];
export const MAX_SERIES = SERIES.length;

function el(name, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  if (parent) parent.appendChild(node);
  return node;
}

function niceTicks(min, max, count = 5) {
  if (!isFinite(min) || !isFinite(max)) return [0, 1];
  if (min === max) {
    const pad = Math.abs(min) > 1 ? Math.abs(min) * 0.1 : 0.5;
    min -= pad;
    max += pad;
  }
  const span = max - min;
  const raw = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const start = Math.floor(min / step) * step;
  const ticks = [];
  for (let value = start; value <= max + step * 0.5; value += step) ticks.push(value);
  return ticks;
}

export function fmt(value, digits) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  const abs = Math.abs(value);
  if (digits !== undefined) return value.toFixed(digits);
  if (abs >= 1e9) return (value / 1e9).toFixed(1) + "B";
  if (abs >= 1e6) return (value / 1e6).toFixed(1) + "M";
  if (abs >= 1e4) return (value / 1e3).toFixed(0) + "k";
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 1) return value.toFixed(1);
  if (abs === 0) return "0";
  return value.toFixed(3);
}

function timeLabel(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function dayLabel(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

/* ------------------------------------------------------------------ tooltip */

let tooltipNode = null;
function tooltip() {
  if (!tooltipNode) {
    tooltipNode = document.createElement("div");
    tooltipNode.className = "tooltip";
    tooltipNode.hidden = true;
    document.body.appendChild(tooltipNode);
  }
  return tooltipNode;
}

function showTooltip(event, html) {
  const node = tooltip();
  node.innerHTML = html;
  node.hidden = false;
  const rect = node.getBoundingClientRect();
  let left = event.clientX + 14;
  let top = event.clientY - 10;
  if (left + rect.width > window.innerWidth - 8) left = event.clientX - rect.width - 14;
  if (top + rect.height > window.innerHeight - 8) top = window.innerHeight - rect.height - 8;
  node.style.left = `${Math.max(8, left)}px`;
  node.style.top = `${Math.max(8, top)}px`;
}

export function hideTooltip() {
  if (tooltipNode) tooltipNode.hidden = true;
}

/* ------------------------------------------------------------- legend block */

export function legend(container, entries, { line = true } = {}) {
  // A legend is always present for two or more series; a single series relies on
  // the card title instead, so a one-swatch box never restates it.
  if (entries.length < 2) return;
  const node = document.createElement("div");
  node.className = "legend";
  node.innerHTML = entries
    .map(
      (entry, index) =>
        `<span class="item"><span class="swatch ${line ? "line" : ""}" style="background:${
          entry.color || SERIES[index % MAX_SERIES]
        }"></span>${entry.label}</span>`
    )
    .join("");
  container.appendChild(node);
}

/* --------------------------------------------------------------- line chart */

/**
 * Multi-series line chart on a single y-axis.
 *
 * @param {HTMLElement} container
 * @param {Array<{label:string, points:Array<{t:string,v:number}>, color?:string}>} series
 * @param {object} options  unit, height, limits[], valueDigits, xLabel
 */
export function lineChart(container, series, options = {}) {
  container.innerHTML = "";
  const live = series.filter((s) => s.points && s.points.length);
  if (!live.length) {
    container.innerHTML = '<div class="empty">no data in this window</div>';
    return;
  }
  if (live.length > MAX_SERIES) {
    // Refuse rather than cycle hues: past three slots the palette cannot keep
    // adjacent series distinguishable.
    throw new Error(
      `lineChart accepts at most ${MAX_SERIES} series; facet into small multiples instead`
    );
  }

  const height = options.height || 210;
  const padding = { top: 12, right: options.labelSpace ?? 74, bottom: 24, left: 46 };
  const width = Math.max(320, container.clientWidth || container.offsetWidth || 560);
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  const times = live.flatMap((s) => s.points.map((p) => new Date(p.t).getTime()));
  const minTime = Math.min(...times);
  const maxTime = Math.max(...times);
  const values = live.flatMap((s) => s.points.map((p) => p.v)).filter((v) => v !== null);
  const declared = (options.limits || []).filter(
    (l) => l.value !== null && l.value !== undefined
  );
  const dataMin = Math.min(...values);
  const dataMax = Math.max(...values);
  // Pad the data range a little, then keep only the limits that fall inside it.
  // A limit far outside the observed range is not drawn: it would flatten the
  // signal, and "no line visible" already says the value is nowhere near it.
  const pad = Math.max((dataMax - dataMin) * 0.35, Math.abs(dataMax) * 0.02, 1e-6);
  const limits = declared.filter(
    (l) => l.value >= dataMin - pad && l.value <= dataMax + pad
  );
  let minValue = Math.min(dataMin, ...limits.map((l) => l.value));
  let maxValue = Math.max(dataMax, ...limits.map((l) => l.value));
  const ticks = niceTicks(minValue, maxValue, options.ticks || 4);
  minValue = Math.min(minValue, ticks[0]);
  maxValue = Math.max(maxValue, ticks[ticks.length - 1]);

  const xOf = (t) =>
    padding.left +
    (maxTime === minTime ? plotWidth : ((t - minTime) / (maxTime - minTime)) * plotWidth);
  const yOf = (v) =>
    padding.top +
    (maxValue === minValue ? plotHeight / 2 : (1 - (v - minValue) / (maxValue - minValue)) * plotHeight);

  const svg = el("svg", {
    class: "chart",
    viewBox: `0 0 ${width} ${height}`,
    height,
    preserveAspectRatio: "none",
  });

  // Gridlines: hairline, solid, recessive.
  for (const tick of ticks) {
    const y = yOf(tick);
    el("line", { class: "grid-line", x1: padding.left, x2: padding.left + plotWidth, y1: y, y2: y }, svg);
    el(
      "text",
      { class: "tick-label", x: padding.left - 7, y: y + 3.5, "text-anchor": "end" },
      svg
    ).textContent = fmt(tick, options.valueDigits);
  }
  el(
    "line",
    {
      class: "axis-line",
      x1: padding.left,
      x2: padding.left + plotWidth,
      y1: padding.top + plotHeight,
      y2: padding.top + plotHeight,
    },
    svg
  );

  // Specification limits, drawn as reference lines in status colours with a
  // label, so they read as thresholds rather than as another series.
  const limitLabelRows = [];
  const labelBackings = [];
  for (const limit of [...limits].sort((a, b) => a.value - b.value)) {
    const y = yOf(limit.value);
    el(
      "line",
      {
        class: "limit-line",
        x1: padding.left,
        x2: padding.left + plotWidth,
        y1: y,
        y2: y,
        stroke: limit.critical ? "var(--critical)" : "var(--warning)",
      },
      svg
    );
    // Two limits a few units apart would print on top of each other.
    let labelY = y - 4;
    while (limitLabelRows.some((other) => Math.abs(other - labelY) < 10)) labelY -= 10;
    limitLabelRows.push(labelY);
    // A limit label can land anywhere, including across the series line. Back it
    // with the surface colour — the same idea as the surface ring on a marker —
    // so it stays legible instead of being clipped or hidden.
    const labelNode = el(
      "text",
      {
        class: "limit-label",
        x: padding.left + plotWidth - 2,
        y: Math.max(9, labelY),
        "text-anchor": "end",
      },
      svg
    );
    labelNode.textContent = limit.label;
    labelBackings.push(labelNode);
  }

  // Time axis: first, middle and last only — a tick per point is noise.
  const spanHours = (maxTime - minTime) / 3600000;
  const labeller = spanHours > 48 ? dayLabel : timeLabel;
  for (const fraction of [0, 0.5, 1]) {
    const t = minTime + (maxTime - minTime) * fraction;
    el(
      "text",
      {
        class: "tick-label",
        x: xOf(t),
        y: height - 7,
        "text-anchor": fraction === 0 ? "start" : fraction === 1 ? "end" : "middle",
      },
      svg
    ).textContent = labeller(new Date(t).toISOString());
  }

  // Track where end labels land so overlapping ones can be nudged apart: two
  // series ending on the same value would otherwise print on top of each other.
  const labelRows = [];
  live.forEach((entry, index) => {
    const color = entry.color || SERIES[index % MAX_SERIES];
    const points = entry.points.filter((p) => p.v !== null);
    if (!points.length) return;
    const path = points
      .map((p, i) => `${i === 0 ? "M" : "L"}${xOf(new Date(p.t).getTime()).toFixed(1)},${yOf(p.v).toFixed(1)}`)
      .join(" ");

    if (options.area && live.length === 1) {
      const baseline = padding.top + plotHeight;
      el(
        "path",
        {
          class: "series-area",
          fill: color,
          d: `${path} L${xOf(new Date(points[points.length - 1].t).getTime()).toFixed(1)},${baseline} L${xOf(
            new Date(points[0].t).getTime()
          ).toFixed(1)},${baseline} Z`,
        },
        svg
      );
    }
    el("path", { class: "series-line", stroke: color, d: path }, svg);

    // End marker plus a direct label. This is also the relief for the palette's
    // sub-3:1 contrast warning: every series is named in text, not colour alone.
    const last = points[points.length - 1];
    const lastX = xOf(new Date(last.t).getTime());
    const lastY = yOf(last.v);
    el("circle", { class: "end-dot", cx: lastX, cy: lastY, r: 4, fill: color }, svg);
    if (padding.right > 20) {
      let labelY = Math.max(11, Math.min(lastY + 3.5, height - 4));
      while (labelRows.some((y) => Math.abs(y - labelY) < 12)) labelY += 12;
      labelRows.push(labelY);
      const label = el(
        "text",
        {
          class: "end-label",
          x: Math.min(lastX + 9, width - 2),
          y: labelY,
          fill: color,
        },
        svg
      );
      label.textContent = `${fmt(last.v, options.valueDigits)}${
        options.unit ? " " + options.unit : ""
      }`;
    }
  });

  // Crosshair + tooltip: an SVG chart is interactive by default.
  const overlay = el(
    "rect",
    {
      x: padding.left,
      y: padding.top,
      width: plotWidth,
      height: plotHeight,
      fill: "transparent",
      style: "cursor:crosshair",
    },
    svg
  );
  const crosshair = el(
    "line",
    { class: "crosshair", y1: padding.top, y2: padding.top + plotHeight, opacity: 0 },
    svg
  );
  const hoverDots = live.map((entry, index) =>
    el(
      "circle",
      {
        class: "hover-dot",
        r: 4.5,
        fill: entry.color || SERIES[index % MAX_SERIES],
        opacity: 0,
      },
      svg
    )
  );

  overlay.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    const scale = width / rect.width;
    const x = (event.clientX - rect.left) * scale;
    const t = minTime + ((x - padding.left) / plotWidth) * (maxTime - minTime);
    crosshair.setAttribute("x1", x);
    crosshair.setAttribute("x2", x);
    crosshair.setAttribute("opacity", 1);

    const rows = [];
    let nearestTime = live[0].points[live[0].points.length - 1].t;
    live.forEach((entry, index) => {
      const points = entry.points.filter((p) => p.v !== null);
      if (!points.length) {
        hoverDots[index].setAttribute("opacity", 0);
        return;
      }
      let nearest = points[0];
      let best = Infinity;
      for (const point of points) {
        const distance = Math.abs(new Date(point.t).getTime() - t);
        if (distance < best) {
          best = distance;
          nearest = point;
        }
      }
      hoverDots[index].setAttribute("cx", xOf(new Date(nearest.t).getTime()));
      hoverDots[index].setAttribute("cy", yOf(nearest.v));
      hoverDots[index].setAttribute("opacity", 1);
      rows.push(
        `<div class="tt-row"><span class="swatch" style="background:${
          entry.color || SERIES[index % MAX_SERIES]
        }"></span>${entry.label}<span class="tt-val">${fmt(
          nearest.v,
          options.valueDigits
        )}${options.unit ? " " + options.unit : ""}</span></div>`
      );
      if (index === 0) nearestTime = nearest.t;
    });
    showTooltip(
      event,
      `<div class="tt-title">${new Date(nearestTime).toLocaleString()}</div>${rows.join("")}`
    );
  });
  overlay.addEventListener("mouseleave", () => {
    crosshair.setAttribute("opacity", 0);
    hoverDots.forEach((dot) => dot.setAttribute("opacity", 0));
    hideTooltip();
  });

  container.appendChild(svg);

  // Measurable only after the text is in the document.
  for (const node of labelBackings) {
    let textWidth = 0;
    try {
      textWidth = node.getComputedTextLength();
    } catch {
      textWidth = String(node.textContent).length * 5;
    }
    const x = Number(node.getAttribute("x"));
    const y = Number(node.getAttribute("y"));
    const backing = el("rect", {
      x: x - textWidth - 3,
      y: y - 9,
      width: textWidth + 6,
      height: 12,
      fill: "var(--surface-1)",
      opacity: 0.9,
    });
    svg.insertBefore(backing, node);
  }
}

/* ---------------------------------------------------------------- bar chart */

/**
 * Horizontal bars, one series, one colour for every bar.
 *
 * Deliberately not a value ramp: colouring bars darker-where-bigger would
 * double-encode length as hue and burn the only free channel on information the
 * bar length already carries.
 */
export function barChart(container, rows, options = {}) {
  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = '<div class="empty">no data</div>';
    return;
  }
  const barHeight = Math.min(24, options.barHeight || 18);
  const gap = 8;
  const labelWidth = options.labelWidth || 112;
  const valueWidth = 58;
  const width = Math.max(300, container.clientWidth || 560);
  const height = rows.length * (barHeight + gap) + 6;
  const plotWidth = width - labelWidth - valueWidth - 10;
  const maxValue = Math.max(...rows.map((r) => Math.abs(r.value || 0)), options.min || 0) || 1;
  const color = options.color || SERIES[0];

  const svg = el("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, height });

  rows.forEach((row, index) => {
    const y = index * (barHeight + gap) + 3;
    const barWidth = Math.max(1, (Math.abs(row.value || 0) / maxValue) * plotWidth);

    el(
      "text",
      { class: "cat-label", x: 0, y: y + barHeight / 2 + 3.5 },
      svg
    ).textContent = row.label;

    // 4px rounded data-end, square at the baseline.
    const radius = Math.min(4, barWidth / 2);
    const x0 = labelWidth;
    const path = `M${x0},${y} H${x0 + barWidth - radius} a${radius},${radius} 0 0 1 ${radius},${radius} V${
      y + barHeight - radius
    } a${radius},${radius} 0 0 1 ${-radius},${radius} H${x0} Z`;
    const bar = el(
      "path",
      { class: "bar", d: path, fill: row.color || color },
      svg
    );
    bar.addEventListener("mousemove", (event) =>
      showTooltip(
        event,
        `<div class="tt-title">${row.label}</div><div class="tt-row">${
          options.metric || "value"
        }<span class="tt-val">${fmt(row.value, options.valueDigits)}${
          options.unit ? " " + options.unit : ""
        }</span></div>${row.detail ? `<div class="tt-row muted">${row.detail}</div>` : ""}`
      )
    );
    bar.addEventListener("mouseleave", hideTooltip);

    el(
      "text",
      {
        class: "bar-label",
        x: labelWidth + barWidth + 7,
        y: y + barHeight / 2 + 3.5,
      },
      svg
    ).textContent = `${fmt(row.value, options.valueDigits)}${options.unit ? " " + options.unit : ""}`;
  });

  container.appendChild(svg);
}

/* ------------------------------------------------------- stacked status bar */

/**
 * One horizontal bar split by state, using the reserved status palette.
 * Segments are separated by a 2px surface gap rather than by strokes.
 */
export function statusBar(container, segments, options = {}) {
  container.innerHTML = "";
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  if (!total) {
    container.innerHTML = '<div class="empty">no machines</div>';
    return;
  }
  const height = options.height || 34;
  const width = Math.max(240, container.clientWidth || 560);
  const gapPx = 2;
  const visible = segments.filter((s) => s.value > 0);
  const available = width - gapPx * Math.max(0, visible.length - 1);

  const svg = el("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, height });
  let x = 0;
  visible.forEach((segment) => {
    const segmentWidth = (segment.value / total) * available;
    const radius = 4;
    const rect = el(
      "rect",
      {
        x,
        y: 0,
        width: Math.max(2, segmentWidth),
        height: height - 12,
        rx: Math.min(radius, segmentWidth / 2),
        fill: segment.color,
      },
      svg
    );
    rect.addEventListener("mousemove", (event) =>
      showTooltip(
        event,
        `<div class="tt-title">${segment.label}</div><div class="tt-row">machines<span class="tt-val">${
          segment.value
        }</span></div><div class="tt-row">share<span class="tt-val">${(
          (segment.value / total) *
          100
        ).toFixed(0)}%</span></div>`
      )
    );
    rect.addEventListener("mouseleave", hideTooltip);

    // Label the segment only when it genuinely fits; never clip.
    if (segmentWidth > 46) {
      el(
        "text",
        { class: "tick-label", x: x + 3, y: height - 2 },
        svg
      ).textContent = `${segment.label} ${segment.value}`;
    }
    x += segmentWidth + gapPx;
  });
  container.appendChild(svg);
}

/* ------------------------------------------------------- small multiples */

/**
 * One chart per measure. This is the answer to "several sensors at once":
 * different units must never share an axis, and beyond three series the palette
 * cannot keep them apart.
 */
export function smallMultiples(container, panels, options = {}) {
  container.innerHTML = "";
  const grid = document.createElement("div");
  grid.className = "spark-grid";
  container.appendChild(grid);

  const pending = [];
  for (const panel of panels) {
    const card = document.createElement("div");
    card.className = "card";
    const title = document.createElement("h3");
    title.textContent = panel.title;
    card.appendChild(title);
    const sub = document.createElement("p");
    sub.className = "card-sub";
    sub.textContent = panel.subtitle || "";
    card.appendChild(sub);
    const plot = document.createElement("div");
    card.appendChild(plot);
    grid.appendChild(card);
    pending.push([plot, panel]);
  }

  const draw = () => {
    for (const [plot, panel] of pending) {
      // Single series per panel, so no legend box is needed — the title names it.
      lineChart(plot, [{ label: panel.title, points: panel.points }], {
        height: options.height || 130,
        ticks: 3,
        unit: panel.unit,
        limits: panel.limits,
        area: true,
        valueDigits: panel.valueDigits,
        labelSpace: 66,
      });
    }
  };
  requestAnimationFrame(draw);
}
