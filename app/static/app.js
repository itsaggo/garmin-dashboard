/* Garmin dashboard front-end. No build step: plain JS + Chart.js. */
const state = { days: 90, units: "imperial", status: null, daily: [], training: [], weekly: [], rides: [], acts: [], coach: null };
const charts = {};

const $ = (sel) => document.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmt = (n, d = 0) => (n == null || isNaN(n) ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
const avg = (arr) => { const v = arr.filter((x) => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
const hm = (sec) => { if (sec == null) return "–"; const m = Math.round(sec / 60); return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`; };
const shortDate = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });

const U = {
  dist: (m) => (m == null ? null : state.units === "metric" ? m / 1000 : m / 1609.344),
  distLabel: () => (state.units === "metric" ? "km" : "mi"),
  pace(speedMs) { // returns "m:ss /mi"
    if (!speedMs) return "–";
    const secPer = (state.units === "metric" ? 1000 : 1609.344) / speedMs;
    return `${Math.floor(secPer / 60)}:${String(Math.round(secPer % 60)).padStart(2, "0")}`;
  },
  speed: (ms) => (ms ? (state.units === "metric" ? ms * 3.6 : ms * 2.23694) : null),
  speedLabel: () => (state.units === "metric" ? "km/h" : "mph"),
  elev: (m) => (m == null ? null : state.units === "metric" ? m : m * 3.28084),
  elevLabel: () => (state.units === "metric" ? "m" : "ft"),
};

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  if (r.status === 401) { window.location.href = "/login"; throw new Error("Signed out"); }
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* no JSON body */ }
    throw new Error(typeof detail === "string" && detail ? detail : `${path}: ${r.status}`);
  }
  return r.json();
}
const postJson = (path, body) => api(path, {
  method: "POST", body: JSON.stringify(body || {}),
  headers: { "X-Requested-With": "dashboard", "Content-Type": "application/json" },
});
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
  else root.removeAttribute("data-theme");
  try { localStorage.setItem("gd-theme", theme || "auto"); } catch { /* ignore */ }
}

/* ---------------------------------------------------------------- loading */
async function load() {
  const weeks = Math.max(8, Math.min(52, Math.ceil(state.days / 7)));
  const [status, daily, training, weekly, rides, acts, coach] = await Promise.all([
    api("/api/status"),
    api(`/api/daily?days=${state.days}`),
    api(`/api/training?days=${Math.max(state.days, 90)}`),
    api(`/api/weekly?weeks=${weeks}&type=run`),
    api(`/api/weekly?weeks=${weeks}&type=ride`),
    api("/api/activities?limit=15"),
    api("/api/coach"),
  ]);
  Object.assign(state, { status, daily, training, weekly, rides, acts, coach, units: status.units });
  applyTheme(status.theme);
  $("#weeklyNote").textContent = `Last ${weeks} weeks · Monday-start weeks · ${U.distLabel()}`;
  $("#ridesNote").textContent = `Last ${weeks} weeks · ${U.distLabel()}`;
  renderHeader();
  renderCoach();
  renderTiles();
  renderTraining();
  renderCharts();
  renderActivities();
}

function renderHeader() {
  const s = state.status;
  const ls = s.last_sync;
  let txt = "Never synced";
  if (ls) {
    const when = new Date(ls.finished_at || ls.started_at);
    txt = `Last sync ${when.toLocaleString()} · ${ls.status}`;
  }
  if (s.syncing) txt = "Syncing with Garmin…";
  $("#syncInfo").textContent = txt;
  $("#syncBtn").disabled = s.syncing;
  $("#counts").textContent = `${fmt(s.days_stored)} days · ${fmt(s.activities_stored)} activities stored`;
  $("#connectCredit").textContent = s.connect_configured ? " + Garmin Connect (unofficial)" : "";

  const banners = [];
  if (!s.api_key_configured) banners.push(`<div class="banner warn"><strong>No Garmin Personal API key yet.</strong> Add it under <a href="/settings">Settings → Garmin connections</a>.</div>`);
  if (ls && ls.status !== "ok" && ls.status !== "running" && ls.status !== "setup" && ls.message) banners.push(`<div class="banner warn"><strong>Last sync: ${escapeHtml(ls.status)}.</strong> ${escapeHtml(String(ls.message).slice(0, 700))}</div>`);
  $("#banners").innerHTML = banners.join("");
}

/* ---------------------------------------------------------------- coach */
const CALL_CLASS = { rest: "bad", easy: "warn", normal: "neutral", hard_ok: "good", race: "good" };
const list = (items) => items.length ? `<ul class="reasons">${items.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>` : "";

function renderCoach() {
  const c = state.coach;
  if (!c) return;
  const cards = [];
  const t = c.today;
  cards.push(`<div class="card coach-card">
    <div class="label">Today</div>
    <h3><span class="call ${CALL_CLASS[t.call] || "neutral"}">${escapeHtml(t.title)}</span></h3>
    ${list(t.reasons)}
    <p>${escapeHtml(t.advice)}</p>
    ${c.data_days < 21 ? `<p class="note">Still learning your normal: comparisons get better after about 3 weeks of data.</p>` : ""}
  </div>`);
  const r = c.race;
  if (r) {
    const when = r.days_to_go === 0 ? "today" : r.days_to_go > 0 ? `${r.days_to_go} day${r.days_to_go === 1 ? "" : "s"} to go` : "finished";
    cards.push(`<div class="card coach-card">
      <div class="label">Goal race</div>
      <h3>${escapeHtml(r.name)} <span class="muted-tag">${escapeHtml(when)}</span></h3>
      <div class="race-stats">
        ${r.goal ? `<div><div class="label">Goal</div><div class="big">${escapeHtml(r.goal)}</div><div class="detail">${escapeHtml(r.goal_pace || "")}</div></div>` : ""}
        ${r.estimate ? `<div><div class="label">Current estimate</div><div class="big">${escapeHtml(r.estimate)}</div><div class="detail">from recent runs</div></div>` : ""}
        <div><div class="label">Phase</div><div class="big">${escapeHtml(r.phase)}</div><div class="detail">${escapeHtml(r.distance || "")}</div></div>
      </div>
      ${list(r.lines)}
      <p>${escapeHtml(r.advice)}</p>
    </div>`);
  } else {
    cards.push(`<div class="card coach-card"><div class="label">Goal race</div>
      <p>Set a goal race and time in <a href="/settings">Settings</a> to get pace targets, a fitness estimate and taper reminders.</p></div>`);
  }
  const w = c.week;
  cards.push(`<div class="card coach-card">
    <div class="label">Weekly check-in · week of ${escapeHtml(shortDate(w.week_of))}</div>
    ${list(w.lines)}
    <p><strong>${escapeHtml(w.suggestion)}</strong></p>
  </div>`);
  $("#coachCards").innerHTML = cards.join("");
  $("#coachDisclaimer").textContent = c.disclaimer;
  $("#aiBox").hidden = !state.status.ai_enabled;
}

async function askAi(question) {
  const out = $("#aiOut"), btns = [$("#aiNote"), $("#aiAsk")];
  btns.forEach((b) => (b.disabled = true));
  out.hidden = false;
  out.textContent = "Your coach is thinking… (a local model can take a minute)";
  try {
    const res = await postJson("/api/coach/ai", question ? { question } : {});
    out.textContent = res.text;  // plain text only, never HTML
    $("#aiMeta").textContent = `${res.model} · ${res.generated}${res.question ? " · you asked: " + res.question : ""}`;
  } catch (e) {
    out.textContent = e.message;
  } finally { btns.forEach((b) => (b.disabled = false)); }
}

/* ---------------------------------------------------------------- tiles */
function latest(key) {
  for (let i = state.daily.length - 1; i >= 0; i--) if (state.daily[i][key] != null) return state.daily[i];
  return null;
}
function lastN(key, n, offset = 0) {
  const vals = state.daily.map((d) => d[key]).filter((v) => v != null);
  return vals.slice(Math.max(0, vals.length - n - offset), vals.length - offset);
}
function deltaHtml(diff, unit, lowerIsBetter) {
  if (diff == null || isNaN(diff) || Math.abs(diff) < 0.05) return "";
  const good = lowerIsBetter ? diff < 0 : diff > 0;
  const arrow = diff > 0 ? "▲" : "▼";
  return `<span class="delta ${good ? "good" : "bad"}">${arrow} ${fmt(Math.abs(diff), 1)}${unit}</span>`;
}

function renderTiles() {
  const t = [];
  const st = latest("steps");
  t.push(tile("Steps", st ? fmt(st.steps) : "–", "",
    st ? `${st.date === new Date().toLocaleDateString("en-CA") ? "today so far" : shortDate(st.date)}${st.step_goal ? ` · goal ${fmt(st.step_goal)}` : ""}` : "no data"));

  const rhr = latest("resting_hr");
  const rhr7 = avg(lastN("resting_hr", 7)), rhrPrev = avg(lastN("resting_hr", 28, 7));
  t.push(tile("Resting HR", rhr ? fmt(rhr.resting_hr) : "–", "bpm",
    rhr ? `7-day ${fmt(rhr7, 1)} ${deltaHtml(rhr7 && rhrPrev ? rhr7 - rhrPrev : null, "", true)}` : "no data"));

  const sl = latest("sleep_s");
  t.push(tile("Sleep", sl ? hm(sl.sleep_s) : "–", "h",
    sl ? `${shortDate(sl.date)}${sl.sleep_score ? ` · score ${fmt(sl.sleep_score)}` : ""}` : "no data"));

  const hv = latest("hrv_last_night");
  t.push(tile("HRV", hv ? fmt(hv.hrv_last_night) : "–", "ms",
    hv ? `7-day ${fmt(hv.hrv_weekly)}${hv.hrv_status ? ` · <span class="pill">${escapeHtml(String(hv.hrv_status).toLowerCase())}</span>` : ""}` : "no data"));

  const bb = latest("bb_high");
  t.push(tile("Body Battery", bb ? fmt(bb.bb_high) : "–", "high",
    bb ? `low ${fmt(bb.bb_low)} · ${shortDate(bb.date)}` : "no data"));

  const wk = state.weekly;
  const thisWeek = wk.length ? wk[wk.length - 1] : null;
  const mondayIso = (() => { const d = new Date(); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d.toLocaleDateString("en-CA"); })();
  const cur = thisWeek && thisWeek.week === mondayIso ? thisWeek : null;
  t.push(tile("Run distance", fmt(cur ? U.dist(cur.distance_m) : 0, 1), U.distLabel(),
    `this week · ${cur ? cur.count : 0} run${cur && cur.count === 1 ? "" : "s"}`));
  if (state.status.include_cycling) {
    const rw = state.rides.length ? state.rides[state.rides.length - 1] : null;
    const rcur = rw && rw.week === mondayIso ? rw : null;
    t.push(tile("Ride distance", fmt(rcur ? U.dist(rcur.distance_m) : 0, 1), U.distLabel(),
      `this week · ${rcur ? rcur.count : 0} ride${rcur && rcur.count === 1 ? "" : "s"}`));
  }

  $("#tiles").innerHTML = t.join("");
}
const tile = (label, value, unit, detail) =>
  `<div class="tile"><div class="label">${label}</div><div class="value">${value}<small>${unit}</small></div><div class="detail">${detail}</div></div>`;

/* ---------------------------------------------------------------- training (Garmin Connect) */
function latestIn(rows, key) {
  for (let i = rows.length - 1; i >= 0; i--) if (rows[i][key] != null) return rows[i];
  return null;
}
function renderTraining() {
  const t = state.training;
  const has = t.some((r) => r.vo2max != null || r.readiness_score != null || r.training_status != null);
  $("#trainingSection").hidden = !(has || state.status.connect_configured);
  if ($("#trainingSection").hidden) return;
  if (!has) {
    $("#trainingTiles").innerHTML = `<div class="tile"><div class="label">Training metrics</div><div class="detail">Connected — data appears after the next sync.</div></div>`;
    return;
  }
  const tiles = [];
  const v = latestIn(t, "vo2max");
  let vDetail = v ? shortDate(v.date) : "no data";
  if (v) {
    const cutoff = new Date(v.date + "T12:00:00"); cutoff.setDate(cutoff.getDate() - 30);
    const old = t.filter((r) => r.vo2max != null && new Date(r.date + "T12:00:00") <= cutoff).pop();
    if (old) vDetail += ` · 30 days ${deltaHtml(v.vo2max - old.vo2max, "", false) || "no change"}`;
  }
  tiles.push(tile("VO2 max", v ? fmt(v.vo2max, 1) : "–", "", vDetail));

  const st = latestIn(t, "training_status");
  tiles.push(tile("Training status", st ? escapeHtml(st.training_status) : "–", "",
    st ? `${shortDate(st.date)}${st.load_balance ? ` · ${escapeHtml(st.load_balance)}` : ""}` : "no data"));

  const rd = latestIn(t, "readiness_score");
  tiles.push(tile("Readiness", rd ? fmt(rd.readiness_score) : "–", "",
    rd ? `${rd.readiness_level ? escapeHtml(rd.readiness_level) + " · " : ""}${shortDate(rd.date)}` : "no data"));

  const rc = latestIn(t, "recovery_min");
  let recValue = "–", recUnit = "", recDetail = "no data";
  if (rc) {
    if (rc.recovery_min <= 0) { recValue = "Recovered"; }
    else { recValue = fmt(rc.recovery_min / 60, 0); recUnit = "h"; }
    recDetail = rc.readiness_time ? `as of ${new Date(rc.readiness_time.replace(" ", "T")).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}` : shortDate(rc.date);
  }
  tiles.push(tile("Recovery time", recValue, recUnit, recDetail));
  $("#trainingTiles").innerHTML = tiles.join("");

  // Charts cover the selected range
  const since = new Date(); since.setDate(since.getDate() - state.days + 1);
  const sinceIso = since.toLocaleDateString("en-CA");
  const rows = t.filter((r) => r.date >= sinceIso);
  const labels = rows.map((r) => shortDate(r.date));
  const s1 = css("--s1"), s2 = css("--s2");
  draw("cVo2", { type: "line", data: { labels, datasets: [{ label: "VO2 max", data: rows.map((r) => r.vo2max), ...line(s1, { pointRadius: 2.5 }) }] }, options: baseOptions() });
  const readyOpts = baseOptions(); readyOpts.scales.y.min = 0; readyOpts.scales.y.max = 100;
  draw("cReady", { type: "bar", data: { labels, datasets: [{ label: "Readiness", data: rows.map((r) => r.readiness_score), ...bar(s1, { maxBarThickness: 14 }) }] }, options: readyOpts });
  draw("cLoad", { type: "line", data: { labels, datasets: [
    { label: "Acute load", data: rows.map((r) => r.acute_load), ...line(s2) },
    { label: "Chronic load", data: rows.map((r) => r.chronic_load), ...line(s1) },
  ] }, options: withLegend(baseOptions()) });
}

/* ---------------------------------------------------------------- charts */
function baseOptions(extra = {}) {
  const muted = css("--muted"), grid = css("--grid");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false, labels: { color: css("--ink-2"), boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: "rectRounded" } },
      tooltip: {
        backgroundColor: css("--surface"), titleColor: css("--ink"), bodyColor: css("--ink-2"),
        borderColor: css("--axis"), borderWidth: 1, padding: 10, cornerRadius: 8, boxPadding: 4,
      },
    },
    scales: {
      x: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: muted, maxRotation: 0, autoSkipPadding: 18 } },
      y: { grid: { color: grid }, border: { display: false }, ticks: { color: muted, maxTicksLimit: 5 } },
    },
    ...extra,
  };
}
function draw(id, config) {
  if (charts[id]) charts[id].destroy();
  const canvas = document.getElementById(id);
  const box = canvas.parentElement;
  box.querySelector(".empty")?.remove();
  const hasData = config.data.datasets.some((ds) => ds.data.some((v) => v != null && (!Array.isArray(v) || v[0] != null)));
  if (!hasData) {
    canvas.style.display = "none";
    box.insertAdjacentHTML("beforeend", `<div class="empty">No data yet for this range</div>`);
    return;
  }
  canvas.style.display = "";
  charts[id] = new Chart(canvas, config);
}
function rolling(vals, n = 7) {
  return vals.map((_, i) => {
    const w = vals.slice(Math.max(0, i - n + 1), i + 1).filter((v) => v != null);
    return w.length >= Math.min(3, n) ? w.reduce((a, b) => a + b, 0) / w.length : null;
  });
}
const bar = (color, extra = {}) => ({ backgroundColor: color, borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", maxBarThickness: 28, ...extra });
const line = (color, extra = {}) => ({ borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, tension: 0.25, spanGaps: true, ...extra });

function renderCharts() {
  const d = state.daily;
  const labels = d.map((r) => shortDate(r.date));
  const s1 = css("--s1"), s2 = css("--s2"), s3 = css("--s3"), sleepDeep = css("--sleep-deep"), sleepLight = css("--sleep-light"), muted = css("--muted");

  // Weekly running distance
  const wk = state.weekly;
  draw("cWeekly", {
    type: "bar",
    data: { labels: wk.map((w) => shortDate(w.week)), datasets: [{ label: `Distance (${U.distLabel()})`, data: wk.map((w) => +U.dist(w.distance_m).toFixed(1)), ...bar(s1) }] },
    options: baseOptions({ plugins: { ...baseOptions().plugins, tooltip: { ...baseOptions().plugins.tooltip, callbacks: {
      title: (items) => `Week of ${items[0].label}`,
      afterBody: (items) => { const w = wk[items[0].dataIndex]; return `${w.count} run${w.count === 1 ? "" : "s"} · ${hm(w.duration_s)} h`; },
    } } } }),
  });

  // Weekly cycling distance
  $("#ridesCard").hidden = !state.status.include_cycling;
  if (state.status.include_cycling) {
    const rw = state.rides;
    draw("cRides", {
      type: "bar",
      data: { labels: rw.map((w) => shortDate(w.week)), datasets: [{ label: `Distance (${U.distLabel()})`, data: rw.map((w) => +U.dist(w.distance_m).toFixed(1)), ...bar(css("--s3")) }] },
      options: baseOptions({ plugins: { ...baseOptions().plugins, tooltip: { ...baseOptions().plugins.tooltip, callbacks: {
        title: (items) => `Week of ${items[0].label}`,
        afterBody: (items) => { const w = rw[items[0].dataIndex]; return `${w.count} ride${w.count === 1 ? "" : "s"} · ${hm(w.duration_s)} h`; },
      } } } }),
    });
  }

  // Steps
  draw("cSteps", {
    type: "bar",
    data: { labels, datasets: [
      { label: "Steps", data: d.map((r) => r.steps), ...bar(s1, { maxBarThickness: 14 }) },
      { type: "line", label: "Goal", data: d.map((r) => r.step_goal), ...line(muted, { borderWidth: 1.5, borderDash: [5, 4], tension: 0, pointHoverRadius: 0 }) },
    ] },
    options: baseOptions(),
  });

  // Resting HR
  const rhr = d.map((r) => r.resting_hr);
  draw("cRhr", {
    type: "line",
    data: { labels, datasets: [
      { label: "7-day avg", data: rolling(rhr).map((v) => (v == null ? null : +v.toFixed(1))), ...line(s2) },
      { label: "Daily", data: rhr, ...line(s2, { borderWidth: 0, showLine: false, pointRadius: 2.5, backgroundColor: s2 + "66" }) },
    ] },
    options: withLegend(baseOptions()),
  });

  // HRV
  draw("cHrv", {
    type: "line",
    data: { labels, datasets: [
      { label: "Overnight", data: d.map((r) => r.hrv_last_night), ...line(s3, { borderWidth: 1.5 }) },
      { label: "7-day avg", data: d.map((r) => r.hrv_weekly), ...line(s1) },
    ] },
    options: withLegend(baseOptions()),
  });

  // Sleep stages (stacked, hours)
  const h = (k) => d.map((r) => (r[k] == null ? null : +(r[k] / 3600).toFixed(2)));
  const stack = (color) => ({ backgroundColor: color, borderColor: css("--surface"), borderWidth: { top: 2 }, borderSkipped: false, maxBarThickness: 14, stack: "s" });
  const sleepOpts = withLegend(baseOptions());
  sleepOpts.scales.x.stacked = true; sleepOpts.scales.y.stacked = true;
  sleepOpts.plugins.tooltip.callbacks = {
    label: (c) => `${c.dataset.label}: ${hm(c.raw * 3600)}`,
    footer: (items) => { const r = d[items[0].dataIndex]; return `Total ${hm(r.sleep_s)}${r.sleep_score ? ` · score ${r.sleep_score}` : ""}`; },
  };
  draw("cSleep", {
    type: "bar",
    data: { labels, datasets: [
      { label: "Deep", data: h("deep_s"), ...stack(sleepDeep) },
      { label: "Light", data: h("light_s"), ...stack(sleepLight) },
      { label: "REM", data: h("rem_s"), ...stack(s3) },
      { label: "Awake", data: h("awake_s"), ...stack(s2) },
    ] },
    options: sleepOpts,
  });

  // Body Battery floating range
  const bbOpts = baseOptions();
  bbOpts.scales.y.min = 0; bbOpts.scales.y.max = 100;
  bbOpts.plugins.tooltip.callbacks = { label: (c) => `Low ${c.raw[0]} → high ${c.raw[1]}` };
  draw("cBB", {
    type: "bar",
    data: { labels, datasets: [{ label: "Body Battery", data: d.map((r) => (r.bb_low != null && r.bb_high != null ? [r.bb_low, r.bb_high] : null)), ...bar(s1, { borderRadius: 4, borderSkipped: false, maxBarThickness: 14 }) }] },
    options: bbOpts,
  });

  // Stress
  const stOpts = baseOptions();
  stOpts.scales.y.min = 0;
  draw("cStress", {
    type: "line",
    data: { labels, datasets: [{ label: "Avg stress", data: d.map((r) => r.avg_stress), ...line(s4OrFallback(), { fill: { target: "origin", above: s4OrFallback() + "22" } }) }] },
    options: stOpts,
  });
}
const s4OrFallback = () => css("--s4") || "#eda100";
function withLegend(o) { o.plugins.legend.display = true; o.plugins.legend.position = "top"; o.plugins.legend.align = "end"; return o; }

/* ---------------------------------------------------------------- table */
function renderActivities() {
  const rows = state.acts;
  if (!rows.length) { $("#acts").innerHTML = `<tr><td class="empty">No activities synced yet</td></tr>`; return; }
  const head = `<tr><th>Date</th><th>Activity</th><th>Type</th><th class="num">Distance</th><th class="num">Time</th><th class="num">Pace / speed</th><th class="num">Avg HR</th><th class="num">Elev</th></tr>`;
  const body = rows.map((a) => {
    const isRun = /run/.test(a.type || "");
    const ps = isRun ? `${U.pace(a.avg_speed_ms)} /${U.distLabel()}` : a.avg_speed_ms ? `${fmt(U.speed(a.avg_speed_ms), 1)} ${U.speedLabel()}` : "–";
    const when = a.start_local ? new Date(a.start_local.replace(" ", "T")).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) : "–";
    return `<tr><td>${when}</td><td>${escapeHtml(a.name || "")}</td><td><span class="pill">${escapeHtml((a.type || "").replace(/_/g, " "))}</span></td>
      <td class="num">${a.distance_m ? fmt(U.dist(a.distance_m), 2) + " " + U.distLabel() : "–"}</td>
      <td class="num">${a.duration_s ? hms(a.duration_s) : "–"}</td><td class="num">${ps}</td>
      <td class="num">${fmt(a.avg_hr)}</td><td class="num">${a.elev_gain_m != null ? fmt(U.elev(a.elev_gain_m)) + " " + U.elevLabel() : "–"}</td></tr>`;
  }).join("");
  $("#acts").innerHTML = head + body;
}
const hms = (s) => { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60; return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`; };
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ---------------------------------------------------------------- events */
document.querySelectorAll("#range button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#range button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    state.days = +b.dataset.days;
    try { localStorage.setItem("gd-days", state.days); } catch {}
    load();
  }),
);
$("#syncBtn").addEventListener("click", async () => {
  $("#syncBtn").disabled = true;
  $("#syncInfo").textContent = "Syncing with Garmin…";
  let res;
  try { res = await api("/api/sync", { method: "POST", headers: { "X-Requested-With": "dashboard" } }); }
  catch (e) { $("#syncInfo").textContent = "Couldn't start sync: " + e.message; $("#syncBtn").disabled = false; return; }
  if (!res.started) { $("#syncInfo").textContent = res.reason || "Sync not started"; $("#syncBtn").disabled = false; return; }
  const poll = setInterval(async () => {
    try {
      const s = await api("/api/status");
      if (!s.syncing) { clearInterval(poll); load(); }
    } catch { clearInterval(poll); }
  }, 3000);
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { renderCharts(); renderTraining(); });

$("#aiNote").addEventListener("click", () => askAi(null));
$("#aiAsk").addEventListener("click", () => {
  const q = $("#aiQuestion").value.trim();
  if (q) askAi(q);
});
$("#aiQuestion").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#aiAsk").click(); } });

(function init() {
  try { const saved = +localStorage.getItem("gd-days"); if ([30, 90, 180, 365].includes(saved)) state.days = saved; } catch {}
  document.querySelectorAll("#range button").forEach((x) => x.setAttribute("aria-pressed", +x.dataset.days === state.days));
  load().catch((e) => { $("#syncInfo").textContent = "Couldn't load data: " + e.message; });
})();
