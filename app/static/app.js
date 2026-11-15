const STATES = {
  0: "Heating: no demand", 1: "Heating: fan start", 2: "Heating: pump pre-run",
  3: "Heating: ignition", 4: "Heating: burner on", 5: "Heating: fan & pump overrun",
  6: "Heating: fan overrun", 7: "Heating: pump overrun", 8: "Heating: anti-cycling",
  10: "HW: demand", 11: "HW: fan start", 13: "HW: ignition", 14: "HW: burner on",
  15: "HW: fan & pump overrun", 16: "HW: fan overrun", 17: "HW: pump overrun",
  30: "No heating demand (thermostat)", 31: "No heating demand (summer / controller)",
  32: "Waiting: fan speed out of range", 34: "Frost protection",
  36: "No heating demand (setpoint < 20 °C)", 39: "Floor contact open",
  41: "Water pressure too high", 53: "Waiting: flow/return ΔT too large",
  54: "Waiting: temperature gradient too steep", 96: "Return sensor test",
  97: "Pressure sensor test", 98: "Flow/return sensor test",
};
const stateText = s => s == null ? "—" : `S.${s}  ${STATES[Number(s)] || ""}`;

// ---------------------------------------------------------------- gauges
// key, label, unit, min, max, decimals. A negative min gives a centre-zero gauge
// (zero at 12 o'clock, negative values to the left).
const GAUGES = [
  ["FlowTemp", "Flow (d.40)", "°C", 0, 90, 1],
  ["ReturnTemp", "Return (d.41)", "°C", 0, 90, 1],
  ["HwcTemp", "Hot water", "°C", 0, 70, 1],
  ["StorageTemp", "Storage", "°C", 0, 70, 1],
  ["FanSpeed", "Fan (d.34)", "rpm", 0, 6000, 0],
  ["HwcWaterflow", "HW flow (d.36)", "l/min", 0, 15, 2],
  ["PumpPower", "Pump", "%", 0, 100, 0],
  ["WaterPressure", "Pressure", "bar", 0, 3, 2],
];
const LAMPS = ["Flame", "Gasvalve", "Ignitor", "HwcDemand"];
const A0 = 150, SWEEP = 240, MID = A0 + SWEEP / 2, MAX_LOG = 500;

function num(raw) {
  if (raw == null) return null;
  const f = parseFloat(String(raw).split(";")[0]);
  return isNaN(f) ? null : f;
}

function arc(a0, a1) {
  const r = 40, cx = 50, cy = 52;
  const p = a => [cx + r * Math.cos(a * Math.PI / 180), cy + r * Math.sin(a * Math.PI / 180)];
  const [x0, y0] = p(a0), [x1, y1] = p(a1);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

const gauges = {};
for (const [key, label, unit, min, max, dp] of GAUGES) {
  const div = document.createElement("div");
  div.className = "gauge";
  div.innerHTML =
    `<svg viewBox="0 0 100 82">` +
    `<path d="${arc(A0, A0 + SWEEP)}" stroke="var(--track)" stroke-width="8" fill="none" stroke-linecap="round"/>` +
    `<path class="fill" stroke="var(--ok)" stroke-width="8" fill="none" stroke-linecap="round"/>` +
    (min < 0 ? `<line x1="50" y1="6" x2="50" y2="18" stroke="var(--muted)" stroke-width="1.5"/>` : "") +
    `<text class="val" x="50" y="56" text-anchor="middle">—</text>` +
    `<text class="unit" x="50" y="70" text-anchor="middle"></text></svg>` +
    `<div class="lbl"></div>`;
  div.querySelector(".unit").textContent = unit;
  div.querySelector(".lbl").textContent = label;
  document.getElementById("gauges").appendChild(div);
  gauges[key] = { min, max, dp, fill: div.querySelector(".fill"), val: div.querySelector(".val") };
}

function setGauge(key, v) {
  const g = gauges[key];
  if (v == null) {
    g.val.textContent = "—";
    g.fill.setAttribute("d", "");
    return;
  }
  g.val.textContent = v.toFixed(g.dp);
  let a0, a1, frac;
  if (g.min < 0) {
    const f = v >= 0 ? Math.min(1, v / g.max) : -Math.min(1, v / g.min);
    [a0, a1] = f >= 0 ? [MID, MID + SWEEP / 2 * f] : [MID + SWEEP / 2 * f, MID];
    frac = Math.abs(f);
  } else {
    frac = Math.min(1, Math.max(0, (v - g.min) / (g.max - g.min)));
    [a0, a1] = [A0, A0 + SWEEP * frac];
  }
  g.fill.setAttribute("d", frac < 0.002 ? "" : arc(a0, a1));
  let colour = "var(--ok)";
  if (key === "rise") colour = v >= 5 ? "var(--bad)" : v >= 3 ? "var(--warn)" : v < 0 ? "var(--s1)" : "var(--ok)";
  g.fill.setAttribute("stroke", colour);
}

function applyValues(values, rise) {
  document.getElementById("state").textContent = stateText(values.Statenumber);
  for (const name of LAMPS) {
    const v = values[name];
    document.getElementById("l-" + name).classList.toggle("on", v === "on" || v === "yes");
  }
  for (const el of document.querySelectorAll("[data-v]")) {
    const v = values[el.dataset.v];
    el.textContent = v == null ? "—" : v;
  }
  for (const key in gauges) setGauge(key, key === "rise" ? rise : num(values[key]));
}

// ---------------------------------------------------------------- charts
// One chart per measure (no shared y-scales); every chart shares the time axis and crosshair.
const CHARTS = [
  { title: "Temperatures", unit: "°C", dp: 1, minSpan: 4,
    series: [["FlowTemp", "Flow (d.40)", "var(--s1)"], ["ReturnTemp", "Return (d.41)", "var(--s2)"], ["HwcTemp", "Hot water", "var(--s3)"]] },
  { title: "Boiler instructions (SetMode)", commands: true,
    legend: [["heating allowed (on)", "var(--on)"], ["heating blocked (off)", "var(--s1)"], ["send failed", "var(--bad)"], ["override", "var(--on)", "dot"]] },
  { title: "Burner & demand (gas valve d.30, HW demand d.22)",
    strips: [["Flame", "Flame"], ["Gasvalve", "Gas valve"], ["HwcDemand", "HW demand"]] },
  { title: "Flow rise", unit: "°C/s", dp: 1, minSpan: 2, zero: true, series: [["rise", "Flow rise", "var(--s1)"]] },
  { title: "Fan", d: 34, unit: "rpm", dp: 0, minSpan: 500, series: [["FanSpeed", "Fan", "var(--s1)"]] },
  { title: "Hot water flow", d: 36, unit: "l/min", dp: 2, minSpan: 1, series: [["HwcWaterflow", "HW flow", "var(--s1)"]] },
  { title: "Pump", unit: "%", dp: 0, minSpan: 20, series: [["PumpPower", "Pump", "var(--s1)"]] },
  { title: "Pressure", unit: "bar", dp: 2, minSpan: 4, range: [0, 4],
    series: [["WaterPressure", "Pressure", null]],
    zones: [[0, 2.5, "var(--ok)", "normal", ""], [2.5, 3, "var(--yellow)", "high", ""],
            [3, 4, "var(--red)", "too high", ""]] },
  // d.44: 0-102, > 80 no flame, < 40 even flame (ecoTEC plus manual, table 8.2); 80 itself counts as unstable
  { title: "Ionisation (d.44)", unit: "", dp: 1, minSpan: 102, range: [0, 102],
    series: [["IonisationVoltageLevel", "Ionisation", null]],
    zones: [[0, 40, "var(--ok)", "even flame", ""], [40, 80 + 1e-9, "var(--red)", "unstable flame", ""],
            [80 + 1e-9, 103, "var(--lgrey)", "no flame", ""]] },
  // d.35: 0 = heating, 40 = centre, 100 = hot water (ecoTEC plus manual, table 8.2)
  { title: "Diverter valve (d.35)", unit: "", dp: 0, minSpan: 100, range: [0, 100],
    series: [["PositionValveSet", "Diverter", null]],
    zones: [[0, 30, "var(--red)", "heating", "CH"], [30, 80, "var(--yellow)", "middle", "mid"],
            [80, 101, "var(--red)", "hot water", "HW"]] },
];
const ML = 80, MR = 58, MT = 8, MB = 20, LINE_H = 130, ROW_H = 18;
const GAP_MS = 15000;  // longer gaps between raw samples break the line
const WINDOWS = [1, 2, 5, 10, 15, 30, 60, 120, 240, 1440, 2880, 10080, 43200];
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

let keyIndex = {}, samples = [], historyMinutes = 60, skew = 0, tiers = [];
let windowMin = 10, hoverT = null, hoverChart = null, src = null;

// A data source gives the charts rows plus accessors, so raw samples (one value per key)
// and bucket summaries (mean/min/max per key) draw through the same code.
function rawSource() {
  const v = (r, k) => r[keyIndex[k]];
  return { rows: samples, bucketMs: 0, gap: GAP_MS, t: r => r[0], mean: v, lo: v, hi: v };
}

function aggSource(tier) {
  const b = k => 1 + 3 * (keyIndex[k] - 1);
  return {
    rows: tier.rows || [], bucketMs: tier.bucketMs, gap: tier.bucketMs * 2.5, loading: !tier.rows,
    t: r => r[0] + tier.bucketMs / 2,
    mean: (r, k) => r[b(k)], lo: (r, k) => r[b(k) + 1], hi: (r, k) => r[b(k) + 2],
  };
}

function currentSource() {
  if (windowMin <= historyMinutes || !tiers.length) return rawSource();
  const tier = tiers.find(t => t.keepMinutes >= windowMin) || tiers[tiers.length - 1];
  loadTier(tier);
  return aggSource(tier);
}

function loadTier(tier) {
  if (tier.rows || tier.loading || Date.now() < tier.retryAt) return;
  tier.loading = true;
  api(`/api/agg?tier=${encodeURIComponent(tier.name)}`)
    .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(d => {
      tier.rows = d.rows;
      for (const row of tier.buffer) addBucket(tier, row);
      tier.buffer = [];
    })
    .catch(() => { tier.retryAt = Date.now() + 10000; })
    .finally(() => { tier.loading = false; scheduleDraw(); });
}

function addBucket(tier, row) {
  if (!tier.rows) {  // not fetched yet: keep it in case a fetch is in flight
    if (tier.buffer.length < 100) tier.buffer.push(row);
    return;
  }
  const rows = tier.rows;
  if (rows.length && row[0] <= rows[rows.length - 1][0]) return;
  rows.push(row);
  const cutoff = row[0] - tier.keepMinutes * 60000;
  while (rows.length && rows[0][0] < cutoff) rows.shift();
}

function windowLabel(m) {
  if (m < 60) return `${m} min`;
  if (m < 1440) return `${m / 60} h`;
  return { 1440: "1 day", 10080: "1 week", 43200: "1 month" }[m] || `${m / 1440} days`;
}

const chartsEl = document.getElementById("charts");
const charts = CHARTS.map((c, n) => {
  const card = document.createElement("div");
  card.className = "chart";
  const head = document.createElement("div");
  head.className = "chead";
  const title = document.createElement("span");
  title.className = "ctitle";
  title.textContent = c.title + (c.unit ? ` (${c.unit})` : "") + (c.d ? ` (d.${c.d})` : "");
  head.append(title);
  const legend = c.legend || (c.series && c.series.length > 1 ? c.series.map(([, label, col]) => [label, col]) : []);
  {
    for (const [label, col, shape] of legend) {
      const item = document.createElement("span");
      item.className = "legend";
      const sw = document.createElement("i");
      if (shape) sw.className = shape;
      sw.style.background = col;
      item.append(sw, document.createTextNode(label));
      head.append(item);
    }
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const height = c.strips ? MT + c.strips.length * ROW_H + MB : c.commands ? MT + 2 * ROW_H + MB : LINE_H;
  svg.setAttribute("height", height);
  card.append(head, svg);
  chartsEl.append(card);
  const ch = { c, svg, height, id: n };
  svg.addEventListener("mousemove", e => onHover(e, ch));
  svg.addEventListener("mouseleave", () => { hoverT = null; tip.hidden = true; scheduleDraw(); });
  return ch;
});

function lowerBound(s, t) {  // first row with time >= t
  let lo = 0, hi = s.rows.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (s.t(s.rows[m]) < t) lo = m + 1; else hi = m; }
  return lo;
}

function nearest(s, t) {
  const rows = s.rows, i = lowerBound(s, t);
  if (i >= rows.length) return rows[rows.length - 1];
  if (i > 0 && t - s.t(rows[i - 1]) < s.t(rows[i]) - t) return rows[i - 1];
  return rows[i];
}

function niceStep(x) {
  const p = Math.pow(10, Math.floor(Math.log10(x))), f = x / p;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
}

const pad2 = n => String(n).padStart(2, "0");
function clock(t, secs) {
  const d = new Date(t);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}` + (secs ? `:${pad2(d.getSeconds())}` : "");
}

function tickLabel(t, step, span) {
  const d = new Date(t);
  if (step >= 86400e3) return `${DAYS[d.getDay()]} ${d.getDate()}`;
  if (span > 86400e3) return `${DAYS[d.getDay()]} ${clock(t)}`;
  return clock(t, step < 60e3);
}

function timeAxis(x, t0, t1, W, H, baseline = true) {  // lane charts skip the baseline: it looks like another lane
  const steps = [5e3, 10e3, 15e3, 30e3, 60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3,
                 7200e3, 10800e3, 21600e3, 43200e3, 86400e3, 172800e3, 604800e3];
  const step = steps.find(s => s >= (t1 - t0) / 6) || 604800e3;
  const off = -new Date(t1).getTimezoneOffset() * 60000;  // put hour/day ticks on local time
  let out = baseline ? `<line class="base" x1="${ML}" x2="${W - MR}" y1="${H - MB}" y2="${H - MB}"/>` : "";
  for (let t = Math.ceil((t0 + off) / step) * step - off; t <= t1; t += step) {
    const px = x(t).toFixed(1);
    out += `<line class="gridl" x1="${px}" x2="${px}" y1="${MT}" y2="${H - MB}"/>` +
           `<text class="xlab" x="${px}" y="${H - 5}" text-anchor="middle">${tickLabel(t, step, t1 - t0)}</text>`;
  }
  return out;
}

function frame(ch, t0, t1) {
  const W = ch.svg.clientWidth || 600, H = ch.height;
  const x = t => ML + (t - t0) / (t1 - t0) * (W - ML - MR);
  const clip = `<clipPath id="clip${ch.id}"><rect x="${ML}" y="0" width="${Math.max(0, W - ML - MR)}" height="${H}"/></clipPath>`;
  return { W, H, x, clip };
}

function crosshair(x, H) {
  if (hoverT == null) return "";
  const px = x(hoverT).toFixed(1);
  return `<line class="cross" x1="${px}" x2="${px}" y1="${MT}" y2="${H - MB}"/>`;
}

// zones: [from (inclusive), to (exclusive), colour, name, short name]; values past the last zone use the last zone
function zoneOf(c, v) {
  return c.zones.find(([a, b]) => v >= a && v < b) || c.zones[c.zones.length - 1];
}

function drawLines(ch, s, t0, t1) {
  const { c } = ch, { W, H, x, clip } = frame(ch, t0, t1);
  let lo = Infinity, hi = -Infinity;
  for (const r of s.rows) {
    if (s.t(r) < t0) continue;
    for (const [k] of c.series) {
      const a = s.lo(r, k), b = s.hi(r, k);
      if (a != null) lo = Math.min(lo, a);
      if (b != null) hi = Math.max(hi, b);
    }
  }
  if (lo === Infinity) { lo = 0; hi = c.minSpan; }
  if (c.range) [lo, hi] = c.range;
  if (c.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (hi - lo < c.minSpan) { const m = (hi + lo) / 2; lo = m - c.minSpan / 2; hi = m + c.minSpan / 2; }
  const padY = (hi - lo) * 0.08;
  lo -= padY; hi += padY;
  const y = v => MT + (hi - v) / (hi - lo) * (H - MT - MB);

  let out = clip + timeAxis(x, t0, t1, W, H);
  const step = niceStep((hi - lo) / 3), tdp = Math.max(0, Math.min(3, -Math.floor(Math.log10(step))));
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    const py = y(v).toFixed(1);
    out += `<line class="gridl" x1="${ML}" x2="${W - MR}" y1="${py}" y2="${py}"/>` +
           `<text class="ylab" x="${ML - 6}" y="${(+py + 4).toFixed(1)}" text-anchor="end">${v.toFixed(tdp)}</text>`;
  }
  if (c.zero) out += `<line class="base" x1="${ML}" x2="${W - MR}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"/>`;

  const labels = [];
  if (c.zones) {  // step line coloured by the zone each value falls in, over faint zone bands
    const zone = v => zoneOf(c, v);
    for (const [a, b, col] of c.zones) {
      const ya = y(Math.max(a, lo)), yb = y(Math.min(b, hi));
      out += `<rect class="zone" x="${ML}" y="${yb.toFixed(1)}" width="${Math.max(0, W - ML - MR)}" height="${(ya - yb).toFixed(1)}" fill="${col}"/>`;
    }
    for (const [k] of c.series) {
      const segs = {};  // colour -> path
      let rise = "", prev = null, lastV = null;
      for (const r of s.rows) {
        const v = s.mean(r, k), t = s.t(r);
        if (v == null) { prev = null; continue; }
        if (prev && t - prev[0] < s.gap) {
          const col = zone(prev[1])[2], x0 = x(prev[0]).toFixed(1), x1 = x(t).toFixed(1);
          segs[col] = (segs[col] || "") + `M${x0} ${y(prev[1]).toFixed(1)}H${x1}`;
          if (v !== prev[1]) rise += `M${x1} ${y(prev[1]).toFixed(1)}V${y(v).toFixed(1)}`;
        }
        prev = [t, v]; lastV = v;
      }
      if (prev) {  // hold the last value to the right-hand edge
        const col = zone(prev[1])[2];
        segs[col] = (segs[col] || "") + `M${x(prev[0]).toFixed(1)} ${y(prev[1]).toFixed(1)}H${x(Math.min(t1, prev[0] + s.gap)).toFixed(1)}`;
      }
      out += `<path class="line" clip-path="url(#clip${ch.id})" stroke="var(--muted)" stroke-width="1" d="${rise}"/>`;
      for (const col in segs) out += `<path class="line" clip-path="url(#clip${ch.id})" stroke="${col}" d="${segs[col]}"/>`;
      if (lastV != null) labels.push({ y: y(lastV), text: `${lastV.toFixed(c.dp)} ${zone(lastV)[4]}`.trim(), col: zone(lastV)[2] });
    }
  }
  if (!c.zones) c.series.forEach(([k, , col]) => {
    // mean line; for bucket summaries also a min-max band behind it
    let d = "", band = "", pen = false, lastT = -Infinity, lastV = null, top = [], bot = [];
    const closeBand = () => {
      if (top.length > 1) band += "M" + top.join("L") + "L" + bot.reverse().join("L") + "Z";
      top = []; bot = [];
    };
    for (const r of s.rows) {
      const v = s.mean(r, k), t = s.t(r);
      if (v == null) { pen = false; closeBand(); continue; }
      const joined = pen && t - lastT < s.gap;
      if (!joined) closeBand();
      const px = x(t).toFixed(1);
      d += `${joined ? "L" : "M"}${px} ${y(v).toFixed(1)}`;
      if (s.bucketMs) {
        top.push(`${px} ${y(s.hi(r, k)).toFixed(1)}`);
        bot.push(`${px} ${y(s.lo(r, k)).toFixed(1)}`);
      }
      pen = true; lastT = t; lastV = v;
    }
    closeBand();
    if (band) out += `<path class="band" clip-path="url(#clip${ch.id})" fill="${col}" d="${band}"/>`;
    out += `<path class="line" clip-path="url(#clip${ch.id})" stroke="${col}" d="${d}"/>`;
    if (lastV != null) labels.push({ y: y(lastV), text: lastV.toFixed(c.dp), col });
  });

  // current values in the right margin, nudged apart so they never overlap
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 13);
  for (const l of labels) {
    const py = Math.min(H - MB, Math.max(MT + 4, l.y));
    out += `<line x1="${W - MR + 4}" x2="${W - MR + 12}" y1="${py.toFixed(1)}" y2="${py.toFixed(1)}" stroke="${l.col}" stroke-width="2"/>` +
           `<text class="vlab" x="${W - MR + 16}" y="${(py + 4).toFixed(1)}">${l.text}</text>`;
  }

  out += crosshair(x, H);
  if (hoverT != null && src && src.rows.length) {
    const r = nearest(src, hoverT);
    for (const [k, , col] of c.series) {
      const v = s.mean(r, k);
      if (v != null) out += `<circle class="dot" r="4" fill="${c.zones ? zoneOf(c, v)[2] : col}" cx="${x(s.t(r)).toFixed(1)}" cy="${y(v).toFixed(1)}"/>`;
    }
  }
  return out;
}

function drawStrips(ch, s, t0, t1) {
  const { c } = ch, { W, H, x, clip } = frame(ch, t0, t1);
  let out = clip + timeAxis(x, t0, t1, W, H, false);
  c.strips.forEach(([k, label], n) => {
    const top = MT + n * ROW_H;
    out += `<text class="ylab" x="${ML - 6}" y="${top + 13}" text-anchor="end">${label}</text>` +
           `<rect class="lane" x="${ML}" y="${top + 3}" width="${Math.max(0, W - ML - MR)}" height="12" rx="2"/>`;
    const bar = (a, b) => {
      const x0 = x(a), w = Math.max(2, x(b) - x0);
      out += `<rect class="run" clip-path="url(#clip${ch.id})" x="${x0.toFixed(1)}" y="${top + 3}" width="${w.toFixed(1)}" height="12" rx="2"/>`;
    };
    if (s.bucketMs) {  // a bucket counts as "on" if it was on at any moment; adjacent buckets merge
      let a = null, b = null;
      for (const r of s.rows) {
        if (s.hi(r, k) !== 1) continue;
        if (a !== null && r[0] <= b) { b = r[0] + s.bucketMs; continue; }
        if (a !== null) bar(a, b);
        a = r[0]; b = r[0] + s.bucketMs;
      }
      if (a !== null) bar(a, b);
    } else {
      let start = null, prevT = null;
      const flush = end => { if (start !== null) { bar(start, end); start = null; } };
      for (const r of s.rows) {
        if (prevT !== null && r[0] - prevT > s.gap) flush(prevT);
        if (s.mean(r, k) === 1) { if (start === null) start = r[0]; } else flush(r[0]);
        prevT = r[0];
      }
      flush(prevT);
    }
  });
  return out + crosshair(x, H);
}

// ---------------------------------------------------------------- boiler instructions chart
// Top lane: what the boiler was told, held from each accepted instruction until the next one,
// or until the boiler's own timeout (holdSeconds) if nothing followed. Bottom lane: every send,
// one tick each (red if ebusd refused it or could not be reached); omitted beyond 2 days.
let commands = [], holdMs = 900000;

function loadCommands() {
  api("/api/commands").then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    commands = d.rows;
    holdMs = d.holdSeconds * 1000;
    scheduleDraw();
  }).catch(() => {});
}

function addCommand(row) {
  if (commands.length && row[0] <= commands[commands.length - 1][0]) return;
  commands.push(row);
  const cutoff = row[0] - 31 * 86400e3;
  while (commands.length && commands[0][0] < cutoff) commands.shift();
  scheduleDraw();
}

// accepted instructions as [from, to, state] runs
function commandRuns(t1) {
  const ok = commands.filter(c => c[2] === 1), runs = [];
  ok.forEach((c, i) => {
    const end = Math.min(i + 1 < ok.length ? ok[i + 1][0] : Infinity, c[0] + holdMs, t1);
    const last = runs[runs.length - 1];
    if (last && last[2] === c[1] && last[1] >= c[0]) last[1] = end;
    else runs.push([c[0], end, c[1]]);
  });
  return runs;
}

function drawCommands(ch, t0, t1) {
  const { W, H, x, clip } = frame(ch, t0, t1);
  const colour = st => st === "on" ? "var(--on)" : "var(--s1)";
  let out = clip + timeAxis(x, t0, t1, W, H, false);
  const lane = (n, label) => {
    const top = MT + n * ROW_H;
    out += `<text class="ylab" x="${ML - 6}" y="${top + 13}" text-anchor="end">${label}</text>` +
           `<rect class="lane" x="${ML}" y="${top + 3}" width="${Math.max(0, W - ML - MR)}" height="12" rx="2"/>`;
    return top;
  };
  let top = lane(0, "Boiler told");
  for (const [a, b, st] of commandRuns(t1)) {
    if (b < t0) continue;
    const x0 = x(Math.max(a, t0)), w = Math.max(2, x(b) - x0);
    out += `<rect clip-path="url(#clip${ch.id})" fill="${colour(st)}" x="${x0.toFixed(1)}" y="${top + 3}" ` +
           `width="${w.toFixed(1)}" height="12" rx="2"/>`;
  }
  top = lane(1, "Sends");
  if (t1 - t0 <= 2 * 86400e3) {
    for (const [ms, st, ok, source] of commands) {
      if (ms < t0) continue;
      if (ok && (source || "").startsWith("override:")) {  // overrides: a filled circle, ringed so it stands clear
        out += `<circle clip-path="url(#clip${ch.id})" fill="var(--on)" stroke="var(--card)" stroke-width="1.5" ` +
               `cx="${x(ms).toFixed(1)}" cy="${top + 9}" r="5.5"/>`;
        continue;
      }
      out += `<rect clip-path="url(#clip${ch.id})" fill="${ok ? colour(st) : "var(--bad)"}" ` +
             `x="${(x(ms) - 1).toFixed(1)}" y="${top + (ok ? 3 : 0)}" width="2" height="${ok ? 12 : 18}"/>`;
    }
  } else {
    out += `<text class="ylab" x="${ML + 6}" y="${top + 13}">individual sends shown for windows up to 2 days</text>`;
  }
  return out + crosshair(x, H);
}

function sourceText(source) {
  return source && source.startsWith("override:") ? ` (override by ${source.slice(9)})` : "";
}

function fillCommandTip() {
  const t = hoverT, rows = [];
  const add = (text, col, cls) => {
    const d = document.createElement("div");
    if (cls) d.className = cls;
    if (col) { const sw = document.createElement("i"); sw.style.background = col; d.append(sw); }
    d.append(document.createTextNode(text));
    rows.push(d);
  };
  add(clock(t, true), null, "tt");
  const run = commandRuns(Date.now() + skew).find(([a, b]) => a <= t && t < b);
  add(run ? `boiler told: heating ${run[2] === "on" ? "allowed (on)" : "blocked (off)"}`
          : "boiler told: nothing (own controls)", run ? (run[2] === "on" ? "var(--on)" : "var(--s1)") : null);
  let prev = null;
  for (const c of commands) { if (c[0] > t) break; prev = c; }
  if (prev) add(`last send ${clock(prev[0], true)}: ${prev[1]}${sourceText(prev[3])}${prev[2] ? "" : " (failed)"}`,
                prev[2] ? null : "var(--bad)");
  tip.replaceChildren(...rows);
}

const view = () => { const t1 = Date.now() + skew; return [t1 - windowMin * 60000, t1]; };

function draw() {
  const [t0, t1] = view();
  src = currentSource();
  const s = { ...src, rows: src.rows.slice(Math.max(0, lowerBound(src, t0) - 1)) };
  for (const ch of charts) {
    ch.svg.innerHTML = ch.c.commands ? drawCommands(ch, t0, t1)
      : ch.c.strips ? drawStrips(ch, s, t0, t1) : drawLines(ch, s, t0, t1);
  }
  const tier = src.bucketMs ? tiers.find(t => t.bucketMs === src.bucketMs) : null;
  document.getElementById("res").textContent = src.loading ? "loading…"
    : tier ? `${tier.name} averages · shaded band = min–max` : "every reading";
  if (hoverT != null) fillTip();
}

let drawPending = false;
function scheduleDraw() {
  if (drawPending) return;
  drawPending = true;
  requestAnimationFrame(() => { drawPending = false; draw(); });
}

// ---------------------------------------------------------------- hover tooltip
const tip = document.getElementById("tip");

function onHover(e, ch) {
  const r = ch.svg.getBoundingClientRect(), px = e.clientX - r.left;
  if (px < ML || px > r.width - MR || (!ch.c.commands && (!src || !src.rows.length))) {
    hoverT = null; tip.hidden = true; scheduleDraw(); return;
  }
  const [t0, t1] = view();
  hoverT = t0 + (px - ML) / (r.width - ML - MR) * (t1 - t0);
  hoverChart = ch;
  tip.hidden = false;
  fillTip();
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  tip.style.left = (e.clientX + 14 + tw > innerWidth ? e.clientX - 14 - tw : e.clientX + 14) + "px";
  tip.style.top = (e.clientY + 14 + th > innerHeight ? e.clientY - 14 - th : e.clientY + 14) + "px";
  scheduleDraw();
}

function fillTip() {
  if (hoverChart.c.commands) { fillCommandTip(); return; }
  if (!src || !src.rows.length) return;
  const s = src, r = nearest(s, hoverT), c = hoverChart.c, agg = s.bucketMs > 0;
  const fmt = v => v == null ? "—" : v.toFixed(c.dp);
  const rows = [];
  const time = document.createElement("div");
  time.className = "tt";
  const when = agg
    ? `${DAYS[new Date(r[0]).getDay()]} ${clock(r[0])}–${clock(r[0] + s.bucketMs)}`
    : clock(r[0], true);
  time.textContent = `${when}  ·  ${stateText(s.mean(r, "Statenumber"))}${agg ? " (last)" : ""}`;
  rows.push(time);
  if (c.series) {
    for (const [k, label, col] of c.series) {
      const row = document.createElement("div"), sw = document.createElement("i"), v = s.mean(r, k);
      const z = c.zones && v != null ? zoneOf(c, v) : null;
      sw.style.background = z ? z[2] : col;
      const text = (agg
        ? `${label}: ${fmt(v)} ${c.unit} (${fmt(s.lo(r, k))}–${fmt(s.hi(r, k))})`
        : `${label}: ${fmt(v)} ${c.unit}`) + (z ? ` · ${z[3]}` : "");
      row.append(sw, document.createTextNode(text));
      rows.push(row);
    }
  } else {
    for (const [k, label] of c.strips) {
      const row = document.createElement("div"), v = s.mean(r, k);
      row.textContent = agg
        ? `${label}: ${v == null ? "—" : `on ${Math.round(v * 100)}% of the time`}`
        : `${label}: ${v === 1 ? "on" : "off"}`;
      rows.push(row);
    }
  }
  tip.replaceChildren(...rows);
}

// ---------------------------------------------------------------- window selector
const winSel = document.getElementById("win");
function buildWindows() {
  const wanted = Number(new URLSearchParams(location.search).get("minutes")) ||
                 Number(localStorage.getItem("boilerWindow")) || windowMin;
  const opts = WINDOWS.filter(m => m <= historyMinutes || tiers.some(t => t.keepMinutes >= m));
  if (!opts.includes(historyMinutes)) opts.push(historyMinutes);
  opts.sort((a, b) => a - b);
  windowMin = opts.includes(wanted) ? wanted : Math.min(10, historyMinutes);
  winSel.replaceChildren(...opts.map(m => {
    const o = document.createElement("option");
    o.value = m;
    o.textContent = windowLabel(m);
    o.selected = m === windowMin;
    return o;
  }));
}
winSel.addEventListener("change", () => {
  windowMin = Number(winSel.value);
  localStorage.setItem("boilerWindow", windowMin);
  scheduleDraw();
});

// ---------------------------------------------------------------- heating schedule
// Everyone sees it; admins get an editor. The server checks the role again on save.
const DAY_LABELS = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const JS_DAY = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
let sched = null, editing = false, control = null;

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
const minutes = t => +t.slice(0, 2) * 60 + +t.slice(3);

function loadSchedule() {
  if (editing) return;
  api("/api/schedule").then(r => r.ok ? r.json() : null).then(d => {
    if (!d || editing) return;
    sched = d;
    if (d.status) control = d.status;
    renderSchedule();
  }).catch(() => {});
}

function renderSchedule() {
  const view = document.getElementById("sched-view");
  document.getElementById("sched-edit").hidden = editing || !sched || (getAuth() || {}).role !== "admin";
  showEnabled();
  showControl(control);
  if (!sched) return;
  const now = new Date(), today = JS_DAY[now.getDay()], nowMin = now.getHours() * 60 + now.getMinutes();
  const rows = [];
  const hours = el("div", "hours");
  for (const h of [0, 3, 6, 9, 12, 15, 18, 21, 24]) {
    const s = el("span", null, String(h).padStart(2, "0"));
    s.style.left = (h / 24 * 100) + "%";
    hours.append(s);
  }
  const head = el("div", "srow");
  head.append(el("span"), hours);
  rows.push(head);
  for (const day of sched.dayOrder) {
    const row = el("div", "srow");
    const strip = el("div", "strip");
    for (const [on, off] of sched.days[day]) {
      const s = el("span");
      s.style.left = (minutes(on) / 1440 * 100) + "%";
      s.style.width = ((minutes(off) - minutes(on)) / 1440 * 100) + "%";
      s.title = `${on}–${off}`;
      strip.append(s);
    }
    if (day === today) {
      const mark = el("i");
      mark.style.left = (nowMin / 1440 * 100) + "%";
      mark.title = "now";
      strip.append(mark);
    }
    row.append(el("span", "day" + (day === today ? " today" : ""), DAY_LABELS[day]), strip);
    rows.push(row);
  }
  view.replaceChildren(...rows);
  view.style.opacity = sched.enabled ? "" : "0.45";
}

// "Last instruction" in the status bar: what the boiler last accepted, how long ago, and any later failure
function ago(iso) {
  const secs = Math.max(0, (Date.now() + skew - new Date(iso).getTime()) / 1000);
  return secs < 60 ? `${Math.round(secs)} s ago` : secs < 3600 ? `${Math.floor(secs / 60)} min ago`
    : `${Math.floor(secs / 3600)} h ${Math.floor(secs % 3600 / 60)} min ago`;
}

function showInstruction() {
  const state = document.getElementById("cmd-state"), when = document.getElementById("cmd-when"),
        fail = document.getElementById("cmd-fail"), ok = control && control.lastOk, last = control && control.lastSend;
  state.className = "cmd-state" + (ok ? " " + ok.state : "");
  state.textContent = ok ? `heating ${ok.state.toUpperCase()}`
    : !control ? "unknown (no status from the app yet)"
    : !control.enabled ? "none: the heating schedule is disabled" : "none sent yet";
  when.textContent = ok ? `at ${ok.time.slice(11, 19)} (${ago(ok.time)})${sourceText(ok.source)}` : "";
  fail.textContent = last && !last.ok ? `· ${last.state} failed at ${last.time.slice(11, 19)}: ${last.result}` : "";

  const next = document.getElementById("cmd-next"), nextWhen = document.getElementById("cmd-next-when");
  const due = control && control.nextSend;
  next.className = "cmd-state" + (due ? " " + control.nextState : "");
  next.textContent = due ? `heating ${control.nextState.toUpperCase()}`
    : !control ? "unknown" : !control.enabled ? "none: the heating schedule is disabled" : "—";
  nextWhen.textContent = due ? `at ${control.nextSend.slice(11, 19)} (${until(control.nextSend)})` : "";
}

function until(iso) {
  const secs = Math.max(0, (new Date(iso).getTime() - Date.now() - skew) / 1000);
  return secs < 60 ? `in ${Math.round(secs)} s` : `in ${Math.floor(secs / 60)} min ${Math.round(secs % 60)} s`;
}

function showControl(c) {
  control = c;
  showEnabled();
  showInstruction();
  showOverride();
}

// Override button: users and admins (the server checks the role too)
function showOverride() {
  const role = (getAuth() || {}).role, btn = document.getElementById("override");
  const info = document.getElementById("override-info"), o = control && control.override;
  btn.hidden = role !== "user" && role !== "admin";
  btn.textContent = o ? "Restart override" : "Heating ON for 14 min";
  info.textContent = o ? `override by ${o.by}: heating ON until ${o.until.slice(11, 19)} (${until(o.until)})`
    + (o.sent ? "" : " · not sent yet, retrying") : "";
  info.classList.toggle("err", !!(o && !o.sent));
}

document.getElementById("override").addEventListener("click", () => {
  const btn = document.getElementById("override"), info = document.getElementById("override-info");
  btn.disabled = true;
  info.textContent = "sending…";
  api("/api/override", { method: "POST" }).then(async r => {
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `override failed (${r.status})`);
    if (d.status) showControl(d.status);
  }).catch(ex => { info.textContent = ex.message; info.classList.add("err"); })
    .finally(() => { btn.disabled = false; });
});

function showEnabled() {
  const enabled = control ? control.enabled : sched ? sched.enabled : null;
  const box = document.getElementById("sched-on");
  box.checked = !!enabled;
  box.indeterminate = enabled == null;
}

function periodRow(on = "", off = "") {
  const p = el("span", "period");
  const a = el("input"), b = el("input"), x = el("button", "btn small", "✕");
  // text, not type=time: time inputs can't hold 24:00, the end of the day
  for (const [input, value, pattern] of [[a, on, "([01]\\d|2[0-3]):[0-5]\\d"],
                                         [b, off, "([01]\\d|2[0-3]):[0-5]\\d|24:00"]]) {
    input.type = "text";
    input.required = true;
    input.value = value;
    input.pattern = pattern;
    input.placeholder = "HH:MM";
    input.maxLength = 5;
    input.size = 5;
    input.inputMode = "numeric";
  }
  a.title = "on time, 00:00–23:59";
  b.title = "off time, 00:01–24:00 (24:00 = end of the day)";
  a.setAttribute("aria-label", "on");
  b.setAttribute("aria-label", "off");
  x.type = "button";
  x.title = "remove";
  x.addEventListener("click", () => { const box = p.parentElement; p.remove(); updateAdd(box); });
  p.append(a, el("span", "note", "–"), b, x);
  return p;
}

function updateAdd(box) {
  box.querySelector(".add").disabled = box.querySelectorAll(".period").length >= sched.maxPeriods;
}

function readDay(box) {
  return [...box.querySelectorAll(".period")].map(p => {
    const [a, b] = p.querySelectorAll("input");
    return [a.value.trim(), b.value.trim()];
  });
}

function openEditor() {
  editing = true;
  document.getElementById("sched-enabled").checked = sched.enabled;
  const days = sched.dayOrder.map(day => {
    const row = el("div", "eday"), box = el("div", "periods");
    box.dataset.day = day;
    for (const [on, off] of sched.days[day]) box.append(periodRow(on, off));
    const add = el("button", "btn small add", "+ add"), copy = el("button", "btn small", "copy to all days");
    add.type = copy.type = "button";
    add.addEventListener("click", () => { add.before(periodRow()); updateAdd(box); });
    copy.addEventListener("click", () => {
      const periods = readDay(box);
      for (const other of document.querySelectorAll("#sched-days .periods")) {
        if (other === box) continue;
        other.querySelectorAll(".period").forEach(p => p.remove());
        const otherAdd = other.querySelector(".add");
        for (const [on, off] of periods) otherAdd.before(periodRow(on, off));
        updateAdd(other);
      }
    });
    box.append(add, copy);
    row.append(el("span", "day", DAY_LABELS[day]), box);
    updateAdd(box);
    return row;
  });
  document.getElementById("sched-days").replaceChildren(...days);
  document.getElementById("sched-err").textContent = "";
  document.getElementById("sched-view").hidden = true;
  document.getElementById("sched-form").hidden = false;
  renderSchedule();
}

function closeEditor() {
  editing = false;
  document.getElementById("sched-form").hidden = true;
  document.getElementById("sched-view").hidden = false;
  renderSchedule();
}

document.getElementById("sched-edit").addEventListener("click", openEditor);
document.getElementById("sched-cancel").addEventListener("click", closeEditor);
document.getElementById("sched-form").addEventListener("submit", e => {
  e.preventDefault();
  const days = {};
  for (const box of document.querySelectorAll("#sched-days .periods")) days[box.dataset.day] = readDay(box);
  const err = document.getElementById("sched-err");
  err.textContent = "saving…";
  api("/api/schedule", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: document.getElementById("sched-enabled").checked, days }),
  }).then(async r => {
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `save failed (${r.status})`);
    sched = d;
    if (d.status) control = d.status;
    closeEditor();
  }).catch(ex => { err.textContent = ex.message; });
});

loadSchedule();
setInterval(loadSchedule, 60000);  // picks up edits made elsewhere
setInterval(() => { if (!editing) renderSchedule(); }, 60000);  // move the "now" mark

// ---------------------------------------------------------------- change list
const log = document.getElementById("log");
function addChange(c) {
  const li = document.createElement("li");
  const t = document.createElement("span");
  t.className = "t";
  t.textContent = c.time.slice(11) + ": ";
  t.title = c.time;
  const n = document.createElement("span");
  n.className = "n";
  n.textContent = c.name;
  li.append(t, n, document.createTextNode(`: ${c.old} -> ${c.new}`));
  log.prepend(li);
  while (log.children.length > MAX_LOG) log.lastChild.remove();
}

// ---------------------------------------------------------------- auth
// Every API call carries the token; a 401 means it expired or was rejected, so log in again.
// The token is refreshed once a quarter of its lifetime is left (checked every 30 s and when
// the tab becomes visible). If the PC slept past expiry, the next check sends you to /login.
function api(url, opts = {}) {
  const a = getAuth();
  if (!a) { toLogin(); return new Promise(() => {}); }
  return fetch(url, { ...opts, headers: { ...(opts.headers || {}), Authorization: "Bearer " + a.token } })
    .then(r => { if (r.status === 401) { toLogin(); return new Promise(() => {}); } return r; });
}

function saveAuth(info) {
  localStorage.setItem(AUTH_KEY, JSON.stringify({
    token: info.token, user: info.user, role: info.role,
    expiresAt: Date.now() + info.expiresIn * 1000, lifetime: info.expiresIn * 1000,
  }));
}

let refreshing = false;
function checkToken() {
  const a = getAuth();
  if (!a) { toLogin(); return; }
  document.getElementById("who").textContent = `${a.user} (${a.role})`;
  if (refreshing || a.expiresAt - Date.now() > Math.min(a.lifetime / 4, 3600e3)) return;
  refreshing = true;
  api("/api/refresh", { method: "POST" })
    .then(r => r.ok ? r.json().then(saveAuth) : null)
    .catch(() => {})  // network trouble: try again next time
    .finally(() => { refreshing = false; });
}
checkToken();
setInterval(checkToken, 30000);
document.getElementById("logout").addEventListener("click", toLogin);

// ---------------------------------------------------------------- live updates
// Server-sent events read through fetch(), because EventSource cannot send the token.
// After a PC suspend/resume or a network drop, a dead connection can look "open" for minutes.
// So: reconnect whenever nothing has arrived for STALL_MS (the server sends at least a ping
// every 5 s), when the stream ends (the server also ends it when the token expires), and
// straight away when the page becomes visible again or the network comes back.
const STALL_MS = 12000;
let lastRx = 0, lastMsg = 0, connected = false, stream = null, streamOpen = false, retryMs = 1000, retryAt = 0;

function connect() {
  if (stream) stream.abort();
  const ctl = new AbortController();
  stream = ctl;
  streamOpen = false;
  lastMsg = Date.now();
  api("/api/events", { signal: ctl.signal, cache: "no-store" }).then(async r => {
    if (!r.ok || !r.body) throw new Error(r.status);
    streamOpen = true;
    retryMs = 1000;
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value;
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const event = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const data = event.split("\n").filter(l => l.startsWith("data: ")).map(l => l.slice(6)).join("\n");
        if (data) onMessage({ data });
      }
    }
  }).catch(() => {}).finally(() => {
    if (stream !== ctl) return;  // superseded by a newer connection
    streamOpen = false;
    scheduleReconnect();
  });
}

function scheduleReconnect() {
  if (retryAt) return;
  retryAt = Date.now() + retryMs;
  retryMs = Math.min(retryMs * 2, 30000);
}

function checkConnection(force) {
  const stalled = Date.now() - lastMsg > STALL_MS;
  if (force || stalled) { retryAt = 0; connect(); return; }
  if (retryAt && Date.now() >= retryAt) { retryAt = 0; connect(); }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  checkToken();
  if (Date.now() - lastMsg > 3000) checkConnection(true);
});
addEventListener("online", () => checkConnection(true));
addEventListener("pageshow", e => { if (e.persisted) checkConnection(true); });  // restored from back/forward cache

function onMessage(ev) {
  lastMsg = Date.now();
  const m = JSON.parse(ev.data);
  if (m.type === "ping") return;
  if (m.type === "change") { addChange(m); return; }
  if (m.type === "control") { showControl(m.control); return; }
  if (m.type === "command") { addCommand(m.row); return; }
  if (m.type === "bucket") {
    const tier = tiers.find(t => t.name === m.tier);
    if (tier) { addBucket(tier, m.row); scheduleDraw(); }
    return;
  }
  skew = m.ts - Date.now();
  if (m.type === "snapshot") {
    log.replaceChildren();
    for (const c of m.changes) addChange(c);
    keyIndex = {};
    m.sampleKeys.forEach((k, i) => { keyIndex[k] = i + 1; });
    samples = m.history;
    historyMinutes = m.historyMinutes;
    tiers = (m.tiers || []).map(t => ({ ...t, rows: null, loading: false, buffer: [], retryAt: 0 }));
    showControl(m.control);
    loadCommands();
    buildWindows();
  } else if (m.sample) {
    samples.push(m.sample);
    const cutoff = m.ts - historyMinutes * 60000;
    while (samples.length && samples[0][0] < cutoff) samples.shift();
  }
  connected = m.connected;
  lastRx = m.time ? Date.now() : 0;
  applyValues(m.values, m.rise);
  showAge();
  scheduleDraw();
}
connect();

function showAge() {
  const age = document.getElementById("age");
  const live = streamOpen && Date.now() - lastMsg <= STALL_MS;
  const secs = lastRx ? (Date.now() - lastRx) / 1000 : null;
  age.textContent = !live ? "web connection lost, reconnecting…"
    : !connected ? "ebusd disconnected"
    : secs == null ? "waiting…" : `${secs.toFixed(1)} s ago`;
  age.classList.toggle("stale", !live || !connected || secs == null || secs > 5);
}

setInterval(() => {
  checkConnection(false);
  showAge();
  showInstruction();
  showOverride();
  scheduleDraw();  // keep the charts scrolling between updates
}, 1000);
addEventListener("resize", scheduleDraw);
