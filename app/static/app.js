/* Training dashboard. Plain JS + Chart.js; boxes are placed by GDGrid from your saved layout.
   Every value from the server is escaped (esc) or set with textContent. */
"use strict";
const S = { days: 90, status: null, daily: [], training: [], weekly: [], rides: [], acts: [], ov: null, insight: null, weather: null };
const charts = {};
const $ = (sel, root = document) => root.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 0) => (n == null || isNaN(n) ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
const shortDate = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
const hm = (sec) => { if (sec == null) return "–"; const m = Math.round(sec / 60); return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`; };
const mmss = (s) => { if (s == null) return "–"; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60; return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`; };
const avg = (a) => { const v = a.filter((x) => x != null); return v.length ? v.reduce((p, q) => p + q, 0) / v.length : null; };
const metric = () => S.status && S.status.units === "metric";
const U = {
  dist: (m) => (m == null ? null : metric() ? m / 1000 : m / 1609.344),
  label: () => (metric() ? "km" : "mi"),
  pace(ms) { if (!ms) return "–"; const s = Math.round((metric() ? 1000 : 1609.344) / ms); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; },
  speed: (ms) => (ms ? (metric() ? ms * 3.6 : ms * 2.23694) : null),
  speedLabel: () => (metric() ? "km/h" : "mph"),
};

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  if (r.status === 401) { location.href = "/login"; throw new Error("Signed out"); }
  let data = null;
  try { data = await r.json(); } catch { /* no body */ }
  if (!r.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `Request failed (${r.status})`);
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}),
  headers: { "X-Requested-With": "dashboard", "Content-Type": "application/json" } });
function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  try { localStorage.setItem("gd-theme", t || "auto"); } catch { /* ignore */ }
}

/* ---------------------------------------------------------------- data */
async function load() {
  const weeks = Math.max(8, Math.min(52, Math.ceil(S.days / 7)));
  const [status, daily, training, weekly, rides, acts, ov, insight] = await Promise.all([
    api("/api/status"), api(`/api/daily?days=${S.days}`), api(`/api/training?days=${Math.max(S.days, 60)}`),
    api(`/api/weekly?weeks=${weeks}&type=run`), api(`/api/weekly?weeks=${weeks}&type=ride`),
    api("/api/activities?limit=20"), api(`/api/overview?days=${S.days}`), api("/api/insight"),
  ]);
  Object.assign(S, { status, daily, training, weekly, rides, acts, ov, insight, weeks });
  applyTheme(status.theme);
  renderHeader();
  renderBoards();
  loadWeather();
  if (!S.printed) { S.printed = true; setTimeout(() => document.body.classList.remove("press"), 3400); }  // first load only
}

/* Weather can take a few seconds the first time, so it fills its own box when it arrives. */
async function loadWeather() {
  try { S.weather = await api("/api/weather"); }
  catch (e) { S.weather = { ok: false, error: e.message }; }
  for (const body of document.querySelectorAll(".box-weather > .box-b")) {
    try { W.weather.render(body); } catch (e) { body.textContent = "Couldn't draw this box: " + e.message; }
  }
}

const latest = (rows, key) => { for (let i = rows.length - 1; i >= 0; i--) if (rows[i][key] != null) return rows[i]; return null; };
const valueAgo = (rows, key, days) => {
  const cut = new Date(); cut.setDate(cut.getDate() - days);
  const iso = cut.toLocaleDateString("en-CA");
  let found = null;
  for (const r of rows) { if (r.date <= iso && r[key] != null) found = r; }
  return found;
};

function renderHeader() {
  const s = S.status, ls = s.last_sync;
  $("#today").textContent = new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  const r = s.race_enabled && s.race;
  $("#raceWeek").textContent = r && r.days_to_go === 0 ? " · Race day" : r && r.days_to_go > 0 && r.days_to_go <= 7 ? " · Race week" : "";
  let synced = "Not synced yet";
  if (ls) {
    const t = new Date(ls.finished_at || ls.started_at);
    const sameDay = t.toDateString() === new Date().toDateString();
    synced = "Synced " + (sameDay ? t.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
      : t.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }));
  }
  $("#syncInfo").textContent = s.syncing ? "Syncing with Garmin…" : synced;
  $("#syncBtn").disabled = s.syncing;
  $("#syncBtn").classList.toggle("spin", !!s.syncing);
  $("#counts").textContent = `${fmt(s.days_stored)} days · ${fmt(s.activities_stored)} activities`;
  const b = [];
  if (!s.api_key_configured) b.push(`<div class="banner warn"><strong>Garmin isn't connected yet.</strong> Add your Personal API key in <a href="/settings#garmin">Settings</a>.</div>`);
  if (ls && ls.status === "partial" && ls.message) b.push(`<div class="banner warn"><strong>Last sync had problems.</strong> ${esc(String(ls.message).slice(0, 300))}</div>`);
  $("#banners").innerHTML = b.join("");
}

/* ---------------------------------------------------------------- charts
   Ink for the data, the accent only for "now" (the latest point or bar), greys for context. */
const C = () => ({ ink: css("--c-ink"), mid: css("--c-mid"), faint: css("--c-faint"), accent: css("--accent") });
function baseOptions(extra = {}) {
  const muted = css("--muted");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false, labels: { color: css("--ink-2"), boxWidth: 9, boxHeight: 9, usePointStyle: true, pointStyle: "rectRounded" } },
      tooltip: { backgroundColor: css("--ink"), titleColor: css("--page"), bodyColor: css("--page"),
        borderWidth: 0, padding: 10, cornerRadius: 8, boxPadding: 4 },
    },
    scales: {
      x: { grid: { display: false }, border: { display: false }, ticks: { color: muted, maxRotation: 0, autoSkipPadding: 22, font: { size: 11 } } },
      y: { grid: { color: css("--grid") }, border: { display: false }, ticks: { color: muted, maxTicksLimit: 4, font: { size: 11 } } },
    },
    ...extra,
  };
}
const line = (c, x = {}) => ({ borderColor: c, backgroundColor: c, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25, spanGaps: true, ...x });
const bar = (c, x = {}) => ({ backgroundColor: c, borderRadius: { topLeft: 3, topRight: 3 }, borderSkipped: "bottom", maxBarThickness: 20, ...x });
const legend = (o) => { o.plugins.legend.display = true; o.plugins.legend.position = "top"; o.plugins.legend.align = "end"; return o; };
const lastIndex = (data) => { for (let i = data.length - 1; i >= 0; i--) if (data[i] != null) return i; return -1; };
/* A line with a dot on its latest value. */
function inkLine(data, x = {}) {
  const c = C(), last = lastIndex(data);
  return { data, ...line(c.ink, { pointRadius: data.map((_, i) => (i === last ? 4 : 0)), pointBackgroundColor: c.accent,
    pointBorderColor: css("--surface"), pointBorderWidth: 2, ...x }) };
}
/* Bars in ink, the latest one in the accent. */
function inkBars(data, x = {}) {
  const c = C(), last = lastIndex(data);
  return { data, ...bar(data.map((_, i) => (i === last ? c.accent : c.ink)), x) };
}

function chart(host, id, config) {
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
  const hasData = config.data.datasets.some((ds) => ds.data.some((v) => v != null && (!Array.isArray(v) || v[0] != null)));
  if (!hasData) { host.innerHTML = `<div class="empty-mini">No data in this range yet</div>`; return; }
  const c = document.createElement("canvas");
  host.replaceChildren(c);
  charts[id] = new Chart(c, config);
}
const labels = (rows) => rows.map((r) => shortDate(r.date));

/* ---------------------------------------------------------------- box helpers */
function emptyState(body, title, text, href, linkText) {
  body.innerHTML = `<div class="empty-state"><strong>${esc(title)}</strong><p>${esc(text)}</p>${href ? `<a class="btn primary" href="${esc(href)}">${esc(linkText)}</a>` : ""}</div>`;
}
/* "+1.0 in 30 days", green when it's the good direction. */
function delta(diff, unit, lowerIsBetter, digits = 1, context = "", asTime = false) {
  if (diff == null || isNaN(diff)) return "";
  if (Math.abs(diff) < (asTime ? 1 : 0.05)) return `<span class="delta">No change${context ? " " + esc(context) : ""}</span>`;
  const good = lowerIsBetter ? diff < 0 : diff > 0;
  const n = asTime ? mmss(Math.abs(diff)) : fmt(Math.abs(diff), digits);
  return `<span class="delta"><b class="${good ? "good-t" : "bad-t"}">${diff > 0 ? "+" : "−"}${n}${esc(unit)}</b>${context ? " " + esc(context) : ""}</span>`;
}
const joinSub = (...parts) => parts.filter(Boolean).join(`<span class="sep"> · </span>`);
function metricBox(body, { value, unit, sub, change }) {
  body.innerHTML = `<div class="metric-v">${value}${unit ? `<small>${esc(unit)}</small>` : ""}</div>
    <div class="metric-sub">${joinSub(change, sub)}</div><div class="chart"></div>`;
  return $(".chart", body);
}

/* ---------------------------------------------------------------- widgets */
const word = (v) => { const t = String(v ?? "").replace(/_/g, " ").trim(); return t ? t.charAt(0).toUpperCase() + t.slice(1).toLowerCase() : ""; };
const ENGINE_LABEL = (v) => v == null ? "" : v >= 85 ? "Superior for your age" : v >= 70 ? "Excellent for your age" : v >= 55 ? "Good for your age" : v >= 40 ? "Fair for your age" : "Below average for your age";
/* Weather arrives in °F and mph; times are the forecast place's own clock ("2026-11-11T14:00"). */
const temp = (f) => (f == null ? "–" : `${fmt(metric() ? (f - 32) * 5 / 9 : f)}°`);
const wind = (mph) => (metric() ? `${fmt(mph * 1.609344)} km/h` : `${fmt(mph)} mph`);
const paceOf = (sPerKm) => { if (!sPerKm) return "–"; const v = Math.round(metric() ? sPerKm : sPerKm * 1.609344); return `${Math.floor(v / 60)}:${String(v % 60).padStart(2, "0")}`; };
const hourOf = (t) => { const h = +String(t).slice(11, 13); return `${h % 12 || 12} ${h < 12 ? "AM" : "PM"}`; };
const clock = (t) => { const h = +String(t).slice(11, 13), m = String(t).slice(14, 16); return `${h % 12 || 12}:${m} ${h < 12 ? "AM" : "PM"}`; };
const ARROW = `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M5 12h14M12 5l7 7-7 7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const longDate = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });

const W = {
  weather: {
    render(body) {
      const w = S.weather;
      if (!w) { body.innerHTML = `<div class="empty-mini">Checking the weather…</div>`; return; }
      if (!w.ok) return emptyState(body, "No weather yet", w.error || "Choose a weather location in Settings.", "/settings#general", "Weather settings");
      const c = w.current || {}, h = w.heat || {}, b = w.best;
      const city = String(w.place || "").split(",")[0];
      const pct = (v) => (v ? `+${fmt(v, 1)}%` : "no change");
      const rows = (w.paces || []).map((p) => `<tr><td>${esc(p.name)}</td><td class="num">${paceOf(p.s_per_km)}</td>
          <td class="num"><b>${paceOf(p.now_s_per_km)}</b></td>${b ? `<td class="num">${paceOf(p.best_s_per_km)}</td>` : ""}</tr>`).join("");
      body.innerHTML = `<div class="wx">
        <div class="eyebrow">${joinSub("Weather", esc(city), w.updated_local ? "as of " + esc(clock(w.updated_local)) : "")}</div>
        <div class="wx-now"><div class="metric-v">${temp(c.temp_f)}</div>
          <div><div class="wx-sky">${esc(c.sky || "")}</div>
          <div class="metric-sub">${joinSub("Dew point " + temp(c.dew_f), c.humidity != null ? fmt(c.humidity) + "% humidity" : "", c.wind_mph != null ? "Wind " + esc(wind(c.wind_mph)) : "")}</div></div></div>
        <div class="wx-heat">
          <div class="wx-grade"><span class="eyebrow">Heat grade</span><span class="wx-grade-n">${esc(h.grade || "–")}</span><b class="${h.pct ? "rs-accent" : ""}">${pct(h.pct)}</b></div>
          <div class="wx-scale" role="img" aria-label="Pace slowdown ${fmt(h.pct || 0, 1)} percent on a 0 to 10 percent scale"><span class="wx-fill" data-w="${Math.min(100, (h.pct || 0) * 10)}"></span></div>
          <div class="wx-scale-l"><span>0%</span><span>5%</span><span>10%+</span></div>
          <p class="wx-advice${h.too_hot ? " bad-t" : ""}">${esc(h.advice || "")}</p>
        </div>
        ${rows ? `<table class="wx-paces"><thead><tr><th>Heat-graded pace /${U.label()}</th><th class="num">Cool</th><th class="num">Now</th>${b ? `<th class="num">${esc(hourOf(b.time))}</th>` : ""}</tr></thead><tbody>${rows}</tbody></table>`
          : `<p class="metric-sub">Your heat-graded paces show up once there's a 5K estimate.</p>`}
        ${b ? `<div class="wx-best">Coolest time to run: <b>${esc(hourOf(b.time))}${String(b.time).slice(0, 10) !== String(w.updated_local || "").slice(0, 10) ? " tomorrow" : ""}</b>, ${temp(b.temp_f)} with dew point ${temp(b.dew_f)}, ${pct(b.pct)}</div>` : ""}
        <div class="chart wx-chart"></div>
        <p class="wx-foot">Temperature + dew point rule of thumb (now ${esc(String(h.sum ?? "–"))} in °F).
          Weather data by <a href="https://open-meteo.com/" target="_blank" rel="noopener noreferrer">Open-Meteo.com</a>${w.stale ? ". This may be out of date." : ""}</p>
      </div>`;
      for (const f of body.querySelectorAll(".wx-fill")) f.style.width = f.dataset.w + "%";
      const hrs = w.hours || [], col = C();
      const o = baseOptions(); o.scales.y.min = 0; o.scales.y.suggestedMax = 6;
      o.scales.y.ticks.callback = (v) => `${v}%`;
      o.plugins.tooltip.callbacks = { label: (x) => { const r = hrs[x.dataIndex]; return ` ${temp(r.temp_f)}, dew ${temp(r.dew_f)}: ${pct(r.pct)}`; } };
      chart($(".wx-chart", body), "weather", { type: "bar", data: { labels: hrs.map((r) => hourOf(r.time)), datasets: [{ label: "Slow down",
        data: hrs.map((r) => r.pct), ...bar(hrs.map((r) => (b && r.time === b.time ? col.accent : col.ink)), { minBarLength: 2, maxBarThickness: 14 }) }] }, options: o });
    },
  },

  coach: {
    render(body) {
      const ins = S.insight;
      if (!ins.configured) return emptyState(body, "Get a personal coach",
        "Connect Claude, Gemini, ChatGPT or a local AI model and get a written insight every morning, plus answers to your questions.",
        "/settings#ai", "Connect an AI coach");
      const i = ins.insight;
      let lead;
      if (i) {
        const when = i.generated ? new Date(String(i.generated).replace(" ", "T")) : null;
        const stamp = when && !isNaN(when) ? (i.date === ins.today ? when.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
          : "from " + when.toLocaleDateString(undefined, { month: "short", day: "numeric" })) : "";
        lead = `<div class="eyebrow">${joinSub("Today's coaching", esc(i.provider_label || ""), esc(stamp))}</div>
          <h2 class="coach-head">${esc(i.headline || "Today's insight")}</h2>
          ${i.today ? `<p class="coach-today">${esc(i.today)}</p>` : ""}
          ${i.insights && i.insights.length ? `<ol class="coach-notes">${i.insights.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : ""}
          ${i.focus ? `<p class="coach-focus"><span>This week</span>${esc(i.focus)}</p>` : ""}`;
      } else {
        lead = `<div class="eyebrow">Today's coaching</div><h2 class="coach-head">No insight yet today</h2>
          <p class="coach-today">Your coach writes one each morning after 6 a.m. You can also ask for one now.</p>`;
      }
      body.innerHTML = `<div class="coach">${lead}
        <div class="coach-foot">
          <form class="coach-ask" id="coachAsk"><label class="sr-only" for="coachQ">Ask your coach</label>
            <input id="coachQ" maxlength="500" autocomplete="off" placeholder="Ask your coach anything">
            <button type="submit" class="ask-btn">Ask ${ARROW}</button></form>
          <div class="coach-answer" id="coachAnswer" hidden></div>
          <div class="coach-meta"><span>${i && i.model ? esc(i.model) + " · " : ""}Writes a new insight every morning</span>
            <span><span class="coach-msg" id="coachMsg"></span> <button type="button" class="link-btn" id="coachRefresh">${i ? "Rewrite" : "Write today's insight"}</button></span></div>
        </div></div>`;
      // One thing at a time: a rewrite re-draws this box, so it can't run while a question is open, and vice versa.
      const controls = () => [$("#coachRefresh", body), $("#coachQ", body), $("#coachAsk button", body)];
      const lock = (on) => controls().forEach((el) => { el.disabled = on; });
      $("#coachRefresh", body).addEventListener("click", async () => {
        lock(true); $("#coachMsg", body).textContent = "Writing… this can take a moment";
        try { S.insight.insight = (await post("/api/insight/refresh")).insight; W.coach.render(body); }
        catch (err) { $("#coachMsg", body).textContent = err.message; lock(false); }
      });
      $("#coachAsk", body).addEventListener("submit", async (e) => {
        e.preventDefault();
        const q = $("#coachQ", body).value.trim(); if (q.length < 2) return;
        const out = $("#coachAnswer", body); out.hidden = false; out.textContent = "Thinking…";
        lock(true);
        try { const r = await post("/api/insight/ask", { question: q }); out.textContent = r.answer; }
        catch (err) { out.textContent = err.message; }
        finally { lock(false); }
      });
    },
  },

  vo2: {
    render(body) {
      const l = latest(S.training, "vo2max");
      if (!l) return emptyState(body, "No VO2 max yet", "VO2 max comes from the Garmin Connect sign-in.", "/settings#garmin", "Connect");
      const old = valueAgo(S.training, "vo2max", 30);
      const host = metricBox(body, { value: fmt(l.vo2max, 1), unit: "", change: old ? delta(l.vo2max - old.vo2max, "", false, 1, "in 30 days") : "",
        sub: esc(ENGINE_LABEL(S.ov.vo2 && S.ov.vo2.for_age)) });
      const rows = S.training.filter((r) => r.date >= since());
      chart(host, "vo2", { type: "line", data: { labels: labels(rows), datasets: [{ label: "VO2 max", ...inkLine(rows.map((r) => r.vo2max)) }] }, options: baseOptions() });
    },
  },

  readiness: {
    render(body) {
      const l = latest(S.training, "readiness_score");
      if (!l) return emptyState(body, "No readiness yet", "Training readiness comes from the Garmin Connect sign-in.", "/settings#garmin", "Connect");
      const rows = S.training.filter((r) => r.date >= since());
      const wk = avg(rows.slice(-7).map((r) => r.readiness_score));
      const rec = l.recovery_min != null ? (l.recovery_min <= 0 ? "fully recovered" : `${fmt(l.recovery_min / 60)} h recovery left`) : "";
      const host = metricBox(body, { value: fmt(l.readiness_score), unit: "/100",
        change: esc([word(l.readiness_level), rec].filter(Boolean).join(", ")), sub: wk != null ? `7-day avg ${fmt(wk)}` : "" });
      const o = baseOptions(); o.scales.y.min = 0; o.scales.y.max = 100;
      chart(host, "readiness", { type: "bar", data: { labels: labels(rows), datasets: [{ label: "Readiness", ...inkBars(rows.map((r) => r.readiness_score), { maxBarThickness: 10 }) }] }, options: o });
    },
  },

  sleep: {
    render(body) {
      const l = latest(S.daily, "sleep_score") || latest(S.daily, "sleep_s");
      if (!l) return emptyState(body, "No sleep data yet", "Sleep appears after your watch syncs a night.");
      const wk = avg(S.daily.slice(-7).map((r) => r.sleep_score));
      const host = metricBox(body, { value: l.sleep_score != null ? fmt(l.sleep_score) : hm(l.sleep_s), unit: l.sleep_score != null ? "/100" : "",
        change: esc(`${hm(l.sleep_s)} asleep`), sub: wk != null ? `7-day avg ${fmt(wk)}` : "" });
      chart(host, "sleep", { type: "line", data: { labels: labels(S.daily), datasets: [{ label: "Sleep score", ...inkLine(S.daily.map((r) => r.sleep_score)) }] }, options: baseOptions() });
    },
  },

  five_k: {
    render(body) {
      const f = S.ov.five_k;
      if (!f.seconds) return emptyState(body, "No 5K estimate yet", "Garmin's race predictor needs the Garmin Connect sign-in; otherwise log a few runs of 3 km or more.", "/settings#garmin", "Connect");
      const host = metricBox(body, { value: esc(f.display), unit: "",
        change: f.change_30d_s != null ? delta(f.change_30d_s, "", true, 0, "in 30 days", true) : "",
        sub: f.source === "garmin" ? "Garmin race predictor" : "From your recent runs" });
      const rows = f.series;
      const o = baseOptions();
      o.scales.y.reverse = true;  // faster is higher
      o.scales.y.ticks.callback = (v) => mmss(v);
      o.plugins.tooltip.callbacks = { label: (c) => ` ${mmss(c.raw)}` };
      chart(host, "five_k", { type: "line", data: { labels: labels(rows), datasets: [{ label: "5K", ...inkLine(rows.map((r) => r.seconds)) }] }, options: o });
    },
  },

  race: {
    available: () => S.status.race_enabled && S.status.race,
    render(body) {
      const r = S.status.race;
      const n = r.days_to_go > 0 ? String(r.days_to_go) : r.days_to_go === 0 ? "Today" : "Done";
      const nl = r.days_to_go > 1 ? "days to go" : r.days_to_go === 1 ? "day to go" : r.days_to_go === 0 ? "race day" : "race complete";
      let gap = "";
      if (r.gap_s != null) gap = r.gap_s < 0 ? `, ${mmss(-r.gap_s)} ahead of goal` : r.gap_s === 0 ? ", right on goal" : `, ${mmss(r.gap_s)} to find`;
      body.innerHTML = `<div class="race">
        <div class="race-title"><div class="eyebrow">${joinSub("Race day", esc(longDate(r.date)), esc(r.distance || ""))}</div><div class="race-name">${esc(r.name)}</div></div>
        <div><div class="rs-n">${esc(n)}</div><div class="rs-l">${esc(nl)}</div></div>
        ${r.goal ? `<div><div class="rs-n">${esc(r.goal)}</div><div class="rs-l">goal${r.goal_pace ? ", " + esc(r.goal_pace) : ""}</div></div>` : "<div></div>"}
        ${r.estimate ? `<div><div class="rs-n rs-accent">${esc(r.estimate)}</div><div class="rs-l">predicted${esc(gap)}</div></div>` : "<div></div>"}
      </div>`;
    },
  },

  weekly_run: { render: (body) => weeklyBox(body, S.weekly, "run") },
  weekly_ride: { available: () => S.status.include_cycling, render: (body) => weeklyBox(body, S.rides, "ride") },

  rhr: {
    render(body) {
      const l = latest(S.daily, "resting_hr");
      const v = S.daily.map((r) => r.resting_hr);
      const r7 = v.map((_, i) => { const w = v.slice(Math.max(0, i - 6), i + 1).filter((x) => x != null); return w.length >= 3 ? +(w.reduce((a, b) => a + b, 0) / w.length).toFixed(1) : null; });
      const host = metricBox(body, { value: l ? fmt(l.resting_hr) : "–", unit: "bpm", sub: l ? esc(shortDate(l.date)) : "No resting heart rate in this range yet" });
      const c = C();
      chart(host, "rhr", { type: "line", data: { labels: labels(S.daily), datasets: [
        { label: "7-day avg", data: r7, ...line(c.ink) },
        { label: "Daily", data: v, ...line(c.mid, { showLine: false, pointRadius: 2 }) },
      ] }, options: legend(baseOptions()) });
    },
  },

  hrv: {
    render(body) {
      const l = latest(S.daily, "hrv_last_night");
      const host = metricBox(body, { value: l ? fmt(l.hrv_last_night) : "–", unit: "ms",
        sub: l ? esc(`${shortDate(l.date)}${l.hrv_status ? ", " + String(l.hrv_status).toLowerCase() : ""}`) : "" });
      const c = C();
      chart(host, "hrv", { type: "line", data: { labels: labels(S.daily), datasets: [
        { label: "7-day avg", data: S.daily.map((r) => r.hrv_weekly), ...line(c.ink) },
        { label: "Overnight", data: S.daily.map((r) => r.hrv_last_night), ...line(c.mid, { borderWidth: 1.5 }) },
      ] }, options: legend(baseOptions()) });
    },
  },

  sleep_stages: {
    render(body) {
      body.innerHTML = `<div class="chart"></div>`;
      const c = C();
      const h = (k) => S.daily.map((r) => (r[k] == null ? null : +(r[k] / 3600).toFixed(2)));
      const st = (col) => ({ backgroundColor: col, borderColor: css("--surface"), borderWidth: { top: 1 }, borderSkipped: false, maxBarThickness: 12, stack: "s" });
      const o = legend(baseOptions()); o.scales.x.stacked = true; o.scales.y.stacked = true;
      o.plugins.tooltip.callbacks = { label: (x) => `${x.dataset.label}: ${hm(x.raw * 3600)}` };
      chart($(".chart", body), "sleep_stages", { type: "bar", data: { labels: labels(S.daily), datasets: [
        { label: "Deep", data: h("deep_s"), ...st(c.ink) }, { label: "Light", data: h("light_s"), ...st(c.mid) },
        { label: "REM", data: h("rem_s"), ...st(c.accent) }, { label: "Awake", data: h("awake_s"), ...st(c.faint) },
      ] }, options: o });
    },
  },

  body_battery: {
    render(body) {
      const l = latest(S.daily, "bb_high");
      const host = metricBox(body, { value: l ? fmt(l.bb_high) : "–", unit: "high", sub: l ? esc(`low ${fmt(l.bb_low)}, ${shortDate(l.date)}`) : "" });
      const o = baseOptions(); o.scales.y.min = 0; o.scales.y.max = 100;
      o.plugins.tooltip.callbacks = { label: (x) => `Low ${x.raw[0]} → high ${x.raw[1]}` };
      chart(host, "body_battery", { type: "bar", data: { labels: labels(S.daily), datasets: [{ label: "Body Battery",
        ...inkBars(S.daily.map((r) => (r.bb_low != null && r.bb_high != null ? [r.bb_low, r.bb_high] : null)), { borderRadius: 3, borderSkipped: false, maxBarThickness: 10 }) }] }, options: o });
    },
  },

  steps: {
    render(body) {
      const l = latest(S.daily, "steps");
      const today = new Date().toLocaleDateString("en-CA");
      const host = metricBox(body, { value: l ? fmt(l.steps) : "–", unit: "",
        sub: l ? esc(`${l.date === today ? "today so far" : shortDate(l.date)}${l.step_goal ? ", goal " + fmt(l.step_goal) : ""}`) : "" });
      chart(host, "steps", { type: "bar", data: { labels: labels(S.daily), datasets: [
        { label: "Steps", ...inkBars(S.daily.map((r) => r.steps), { maxBarThickness: 10 }) },
        { type: "line", label: "Goal", data: S.daily.map((r) => r.step_goal), ...line(css("--muted"), { borderWidth: 1.5, borderDash: [5, 4], tension: 0, pointHoverRadius: 0 }) },
      ] }, options: baseOptions() });
    },
  },

  stress: {
    render(body) {
      const l = latest(S.daily, "avg_stress");
      const host = metricBox(body, { value: l ? fmt(l.avg_stress) : "–", unit: "avg", sub: l ? esc(shortDate(l.date)) : "" });
      const o = baseOptions(); o.scales.y.min = 0;
      chart(host, "stress", { type: "line", data: { labels: labels(S.daily), datasets: [{ label: "Avg stress", ...inkLine(S.daily.map((r) => r.avg_stress)) }] }, options: o });
    },
  },

  training_load: {
    render(body) {
      const rows = S.training.filter((r) => r.date >= since());
      const l = latest(rows, "training_status");
      body.innerHTML = `${l ? `<div class="metric-v">${esc(word(l.training_status))}</div>` : ""}
        <div class="metric-sub">${l ? (l.load_balance ? esc(word(l.load_balance)) : "") : "Training load comes from the Garmin Connect sign-in."}</div><div class="chart"></div>`;
      const c = C();
      chart($(".chart", body), "training_load", { type: "line", data: { labels: labels(rows), datasets: [
        { label: "Acute (7-day)", data: rows.map((r) => r.acute_load), ...line(c.accent) },
        { label: "Chronic (28-day)", data: rows.map((r) => r.chronic_load), ...line(c.ink) },
      ] }, options: legend(baseOptions()) });
    },
  },

  activities: {
    render(body) {
      if (!S.acts.length) return emptyState(body, "No activities yet", "They'll appear after your first sync.");
      const rows = S.acts.map((a) => {
        const run = /run/.test(a.type || "");
        const ps = run ? `${U.pace(a.avg_speed_ms)} /${U.label()}` : a.avg_speed_ms ? `${fmt(U.speed(a.avg_speed_ms), 1)} ${U.speedLabel()}` : "–";
        const when = a.start_local ? new Date(a.start_local.replace(" ", "T")).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) : "–";
        return `<tr><td>${esc(when)}</td><td class="t-name">${esc(a.name || "")}<span class="t-type">${esc((a.type || "").replace(/_/g, " "))}</span></td>
          <td class="num">${a.distance_m ? fmt(U.dist(a.distance_m), 2) + " " + U.label() : "–"}</td><td class="num act-time">${a.duration_s ? mmss(a.duration_s) : "–"}</td>
          <td class="num">${esc(ps)}</td><td class="num act-hr">${a.avg_hr ? fmt(a.avg_hr) : "–"}</td></tr>`;
      }).join("");
      body.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Activity</th><th class="num">Distance</th><th class="num act-time">Time</th><th class="num">Pace / speed</th><th class="num act-hr">Avg HR</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    },
  },
};

function weeklyBox(body, rows, kind) {
  const monday = (() => { const d = new Date(); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d.toLocaleDateString("en-CA"); })();
  const cur = rows.length && rows[rows.length - 1].week === monday ? rows[rows.length - 1] : null;
  const host = metricBox(body, { value: fmt(cur ? U.dist(cur.distance_m) : 0, 1), unit: U.label(),
    sub: `this week, ${cur ? cur.count : 0} ${kind}${cur && cur.count === 1 ? "" : "s"}` });
  const data = rows.map((w) => +U.dist(w.distance_m).toFixed(1));
  const ds = cur ? inkBars(data) : { data, ...bar(C().ink) };
  const o = baseOptions();
  o.plugins.tooltip.callbacks = { title: (i) => `Week of ${i[0].label}`, afterBody: (i) => { const w = rows[i[0].dataIndex]; return `${w.count} ${kind}${w.count === 1 ? "" : "s"}`; } };
  chart(host, "weekly_" + kind, { type: "bar", data: { labels: rows.map((w) => shortDate(w.week)), datasets: [{ label: `Distance (${U.label()})`, ...ds }] }, options: o });
}
function since() { const d = new Date(); d.setDate(d.getDate() - S.days + 1); return d.toLocaleDateString("en-CA"); }

/* ---------------------------------------------------------------- boards */
const TITLES = { weather: "Weather", coach: "AI coach", vo2: "VO2 max", readiness: "Training readiness", sleep: "Sleep score",
  five_k: "5K estimate", race: "Goal race", weekly_run: "Weekly running", weekly_ride: "Weekly cycling",
  rhr: "Resting heart rate", hrv: "HRV", sleep_stages: "Sleep stages", body_battery: "Body Battery", steps: "Steps", stress: "Stress",
  training_load: "Training load", activities: "Recent activities" };

function renderBoards() {
  const layout = S.status.layout || [];
  const cols = GDGrid.columnsNow();
  for (const section of ["main", "details"]) {
    const board = document.getElementById("sec-" + section);
    board.replaceChildren();
    let n = 0;
    for (const item of layout.filter((i) => i.section === section)) {
      const w = W[item.id];
      if (!w || (w.available && !w.available())) continue;
      const box = document.createElement("article");
      box.className = "box box-" + item.id;
      box.dataset.id = item.id;
      const head = document.createElement("header");
      head.className = "box-h";
      head.textContent = TITLES[item.id] || item.id;
      const body = document.createElement("div");
      body.className = "box-b";
      box.append(head, body);
      GDGrid.sizeBox(box, item.w, item.h, cols);
      box.style.setProperty("--i", String(n++));
      board.append(box);
      try { w.render(body); } catch (e) { body.textContent = "Couldn't draw this box: " + e.message; }
    }
    if (!board.children.length) board.innerHTML = `<div class="board-empty">Nothing here yet. <a href="/settings#customize">Add boxes in Settings → Customize</a>.</div>`;
  }
  if (document.body.classList.contains("press")) countUp(document.getElementById("sec-" + (curTab || "main")));
}

/* ---------------------------------------------------------------- motion */
const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
/* Big plain numbers count up as their box inks in (first load only). */
function countUp(root) {
  if (reduceMotion() || !root) return;
  for (const el of root.querySelectorAll(".metric-v")) {
    const node = el.firstChild;
    if (!node || node.nodeType !== Node.TEXT_NODE) continue;
    const txt = node.textContent.trim();
    if (!/^\d[\d,]*(\.\d+)?$/.test(txt)) continue;
    const target = parseFloat(txt.replace(/,/g, "")), dec = (txt.split(".")[1] || "").length;
    const show = (v) => { node.textContent = v.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec }); };
    const box = el.closest(".box");
    const start = performance.now() + (parseFloat(box && box.style.getPropertyValue("--i")) || 0) * 80 + 650, dur = 1100;
    show(0);
    const step = (now) => {
      const t = Math.min(1, Math.max(0, (now - start) / dur));
      show(t >= 1 ? target : target * (1 - Math.pow(1 - t, 3)));
      if (t < 1) requestAnimationFrame(step); else node.textContent = txt;
    };
    requestAnimationFrame(step);
  }
}


/* ---------------------------------------------------------------- tabs, range, sync */
let curTab = null;
function showTab() {
  const tab = location.hash === "#details" ? "details" : "main";
  curTab = tab;
  $("#sec-main").hidden = tab !== "main";
  $("#sec-details").hidden = tab !== "details";
  for (const a of document.querySelectorAll("[data-tab]")) a.classList.toggle("on", a.dataset.tab === tab);
}
window.addEventListener("hashchange", showTab);
for (const b of document.querySelectorAll("#range button")) {
  b.addEventListener("click", () => {
    for (const x of document.querySelectorAll("#range button")) x.setAttribute("aria-pressed", x === b);
    S.days = +b.dataset.days;
    try { localStorage.setItem("gd-days", S.days); } catch { /* ignore */ }
    load().catch(showError);
  });
}
$("#syncBtn").addEventListener("click", async () => {
  const btn = $("#syncBtn");
  const stop = (msg) => { $("#syncInfo").textContent = msg; btn.disabled = false; btn.classList.remove("spin"); };
  btn.disabled = true; btn.classList.add("spin");
  try {
    const r = await post("/api/sync");
    if (!r.started) return stop(r.reason || "Sync not started");
  } catch (e) { return stop(e.message); }
  $("#syncInfo").textContent = "Syncing with Garmin…";
  const poll = setInterval(async () => {
    try { const s = await api("/api/status"); if (!s.syncing) { clearInterval(poll); load().catch(showError); } }
    catch { clearInterval(poll); }
  }, 3000);
});
let lastCols = GDGrid.columnsNow();
window.addEventListener("resize", () => { const c = GDGrid.columnsNow(); if (c !== lastCols) { lastCols = c; renderBoards(); } });
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => renderBoards());
function showError(e) { $("#syncInfo").textContent = "Couldn't load data: " + e.message; }

(function init() {
  try { const d = +localStorage.getItem("gd-days"); if ([30, 90, 180, 365].includes(d)) S.days = d; } catch { /* ignore */ }
  for (const x of document.querySelectorAll("#range button")) x.setAttribute("aria-pressed", +x.dataset.days === S.days);
  Chart.defaults.font.family = css("--sans") || "system-ui, sans-serif";
  showTab();
  load().catch(showError);
})();
