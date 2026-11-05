/* Training dashboard. Plain JS + Chart.js; boxes are placed by GDGrid from your saved layout.
   Every value from the server is escaped (esc) or set with textContent. */
"use strict";
const S = { days: 90, status: null, daily: [], training: [], weekly: [], rides: [], acts: [], ov: null, insight: null };
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
  $("#syncInfo").textContent = s.syncing ? "Syncing with Garmin…" :
    ls ? `Updated ${new Date(ls.finished_at || ls.started_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}` : "Not synced yet";
  $("#syncBtn").disabled = s.syncing;
  $("#syncBtn").classList.toggle("spin", !!s.syncing);
  $("#counts").textContent = `${fmt(s.days_stored)} days · ${fmt(s.activities_stored)} activities`;
  const b = [];
  if (!s.api_key_configured) b.push(`<div class="banner warn"><strong>Garmin isn't connected yet.</strong> Add your Personal API key in <a href="/settings#garmin">Settings</a>.</div>`);
  if (ls && ls.status === "partial" && ls.message) b.push(`<div class="banner warn"><strong>Last sync had problems.</strong> ${esc(String(ls.message).slice(0, 300))}</div>`);
  $("#banners").innerHTML = b.join("");
}

/* ---------------------------------------------------------------- charts */
function baseOptions(extra = {}) {
  const muted = css("--muted");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false, labels: { color: css("--ink-2"), boxWidth: 9, boxHeight: 9, usePointStyle: true, pointStyle: "rectRounded" } },
      tooltip: { backgroundColor: css("--surface"), titleColor: css("--ink"), bodyColor: css("--ink-2"),
        borderColor: css("--axis"), borderWidth: 1, padding: 10, cornerRadius: 10, boxPadding: 4 },
    },
    scales: {
      x: { grid: { display: false }, border: { display: false }, ticks: { color: muted, maxRotation: 0, autoSkipPadding: 22, font: { size: 11 } } },
      y: { grid: { color: css("--grid") }, border: { display: false }, ticks: { color: muted, maxTicksLimit: 4, font: { size: 11 } } },
    },
    ...extra,
  };
}
const line = (c, x = {}) => ({ borderColor: c, backgroundColor: c, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.3, spanGaps: true, ...x });
const area = (c, x = {}) => line(c, { fill: { target: "origin", above: c + "22", below: c + "22" }, ...x });
const bar = (c, x = {}) => ({ backgroundColor: c, borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", maxBarThickness: 22, ...x });
const legend = (o) => { o.plugins.legend.display = true; o.plugins.legend.position = "top"; o.plugins.legend.align = "end"; return o; };

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
function chip(diff, unit, lowerIsBetter, digits = 1) {
  if (diff == null || isNaN(diff) || Math.abs(diff) < 0.05) return `<span class="chip flat">no change</span>`;
  const good = lowerIsBetter ? diff < 0 : diff > 0;
  return `<span class="chip ${good ? "good" : "bad"}">${diff > 0 ? "▲" : "▼"} ${fmt(Math.abs(diff), digits)}${unit}</span>`;
}
function metricBox(body, { value, unit, sub, change }) {
  body.innerHTML = `<div class="metric"><div class="metric-v">${value}<small>${esc(unit || "")}</small></div>${change || ""}</div>
    <div class="metric-sub">${sub || ""}</div><div class="chart"></div>`;
  return $(".chart", body);
}

/* ---------------------------------------------------------------- widgets */
const TIER_COLOR = { Peak: "--s3", Strong: "--s3", Solid: "--s1", Building: "--s4", Recharge: "--s2" };
const ENGINE_LABEL = (v) => v == null ? "" : v >= 85 ? "Superior for your age" : v >= 70 ? "Excellent for your age" : v >= 55 ? "Good for your age" : v >= 40 ? "Fair for your age" : "Below average for your age";

const W = {
  fitness: {
    render(body) {
      const t = S.ov.today;
      if (t.score == null) return emptyState(body, "Building your score", "Your fitness score appears after a few days of data have synced.");
      const r = 52, c = 2 * Math.PI * r, frac = t.score / 100;
      const color = css(TIER_COLOR[t.tier] || "--s1");
      const pill = Object.entries(t.pillar_names).map(([k, name]) => {
        const v = t.pillars[k];
        return `<div class="pillar"><span class="p-n">${esc(name)}</span><span class="p-bar"><span class="p-fill" data-w="${v ?? 0}"></span></span><span class="p-v">${v == null ? "–" : v}</span></div>`;
      }).join("");
      const drivers = (t.drivers || []).map((d) => `<span class="chip ${d.delta > 0 ? "good" : "bad"}">${esc(d.pillar)} ${d.delta > 0 ? "▲" : "▼"} ${Math.abs(d.delta)}</span>`).join("");
      const notes = [...(t.notes.recovery || []), ...(t.notes.balance || []), ...(t.notes.engine || [])].slice(0, 3);
      body.innerHTML = `<div class="fit">
        <div class="ring-wrap">
          <svg class="ring" viewBox="0 0 120 120" aria-label="Fitness score ${t.score} of 100">
            <circle cx="60" cy="60" r="${r}" class="ring-bg"></circle>
            <circle cx="60" cy="60" r="${r}" class="ring-fg" transform="rotate(-90 60 60)"></circle>
          </svg>
          <div class="ring-c"><div class="ring-n">${t.score}</div><div class="ring-t">${esc(t.tier)}</div></div>
        </div>
        <div class="fit-r">
          ${t.change_7d != null ? `<div class="fit-change">${chip(t.change_7d, " pts", false, 0)} <span class="muted">vs last week</span></div>` : ""}
          <div class="pillars">${pill}</div>
        </div></div>
        ${drivers ? `<div class="drivers">${drivers}</div>` : ""}
        ${notes.length ? `<ul class="fit-notes">${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
        <details class="fit-how"><summary>How it's calculated</summary>
          <p><b>Engine</b> (35%): VO2 max against norms for your age. <b>Recovery</b> (25%): HRV and resting heart rate against your own normal, plus sleep, readiness and Body Battery. <b>Consistency</b> (25%): active weeks, step goals and weekly intensity minutes. <b>Training balance</b> (15%): this week's load against what you're used to.</p></details>`;
      const fg = $(".ring-fg", body);
      fg.setAttribute("stroke", color);
      fg.setAttribute("stroke-dasharray", `${c * frac} ${c}`);
      for (const f of body.querySelectorAll(".p-fill")) f.style.width = f.dataset.w + "%";
    },
  },

  coach: {
    render(body) {
      const ins = S.insight;
      if (!ins.configured) return emptyState(body, "Get a personal coach",
        "Connect Claude, Gemini, ChatGPT or a local AI model and get a written insight every morning, plus answers to your questions.",
        "/settings#ai", "Connect an AI coach");
      const i = ins.insight;
      const head = i ? `<div class="coach-head">${esc(i.headline || "Today's insight")}</div>
          ${i.today ? `<div class="coach-today"><span class="dot"></span><p>${esc(i.today)}</p></div>` : ""}
          ${i.insights && i.insights.length ? `<ul class="coach-list">${i.insights.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
          ${i.focus ? `<div class="coach-focus"><span>This week</span>${esc(i.focus)}</div>` : ""}
          <div class="coach-meta">${esc(i.provider_label || "")} · ${esc(i.model || "")} · ${esc(i.generated || "")}${i.date !== ins.today ? " · from a previous day" : ""}</div>`
        : `<div class="empty-state"><strong>No insight yet today</strong><p>Your coach writes one each morning after 6 a.m. You can also ask for one now.</p></div>`;
      body.innerHTML = `<div class="coach">${head}
        <div class="coach-actions"><button type="button" class="btn sm" id="coachRefresh">${i ? "Rewrite" : "Write today's insight"}</button><span class="coach-msg" id="coachMsg"></span></div>
        <form class="coach-ask" id="coachAsk"><input id="coachQ" maxlength="500" autocomplete="off" placeholder="Ask your coach, e.g. should I run tomorrow?"><button type="submit" class="btn primary sm">Ask</button></form>
        <div class="coach-answer" id="coachAnswer" hidden></div></div>`;
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
      const host = metricBox(body, { value: fmt(l.vo2max, 1), unit: "", change: old ? chip(l.vo2max - old.vo2max, "", false) : "",
        sub: esc(ENGINE_LABEL(S.ov.today.pillars.engine)) });
      const rows = S.training.filter((r) => r.date >= since());
      chart(host, "vo2", { type: "line", data: { labels: labels(rows), datasets: [{ label: "VO2 max", data: rows.map((r) => r.vo2max), ...area(css("--s1")) }] }, options: baseOptions() });
    },
  },

  readiness: {
    render(body) {
      const l = latest(S.training, "readiness_score");
      if (!l) return emptyState(body, "No readiness yet", "Training readiness comes from the Garmin Connect sign-in.", "/settings#garmin", "Connect");
      const rows = S.training.filter((r) => r.date >= since());
      const wk = avg(rows.slice(-7).map((r) => r.readiness_score));
      const rec = l.recovery_min != null ? (l.recovery_min <= 0 ? "Fully recovered" : `${fmt(l.recovery_min / 60)} h recovery left`) : "";
      const host = metricBox(body, { value: fmt(l.readiness_score), unit: "/100",
        change: wk != null ? `<span class="chip flat">7-day ${fmt(wk)}</span>` : "",
        sub: esc([l.readiness_level, rec].filter(Boolean).join(" · ")) });
      const o = baseOptions(); o.scales.y.min = 0; o.scales.y.max = 100;
      chart(host, "readiness", { type: "bar", data: { labels: labels(rows), datasets: [{ label: "Readiness", data: rows.map((r) => r.readiness_score), ...bar(css("--s3"), { maxBarThickness: 12 }) }] }, options: o });
    },
  },

  sleep: {
    render(body) {
      const l = latest(S.daily, "sleep_score") || latest(S.daily, "sleep_s");
      if (!l) return emptyState(body, "No sleep data yet", "Sleep appears after your watch syncs a night.");
      const wk = avg(S.daily.slice(-7).map((r) => r.sleep_score));
      const host = metricBox(body, { value: l.sleep_score != null ? fmt(l.sleep_score) : hm(l.sleep_s), unit: l.sleep_score != null ? "/100" : "",
        change: wk != null ? `<span class="chip flat">7-day ${fmt(wk)}</span>` : "",
        sub: esc(`${shortDate(l.date)} · ${hm(l.sleep_s)}`) });
      chart(host, "sleep", { type: "line", data: { labels: labels(S.daily), datasets: [{ label: "Sleep score", data: S.daily.map((r) => r.sleep_score), ...area(css("--s7") || css("--s1")) }] }, options: baseOptions() });
    },
  },

  five_k: {
    render(body) {
      const f = S.ov.five_k;
      if (!f.seconds) return emptyState(body, "No 5K estimate yet", "Garmin's race predictor needs the Garmin Connect sign-in; otherwise log a few runs of 3 km or more.", "/settings#garmin", "Connect");
      const host = metricBox(body, { value: esc(f.display), unit: "",
        change: f.change_30d_s != null ? chip(f.change_30d_s / 60, " min", true) : "",
        sub: f.source === "garmin" ? "Garmin race predictor" : "Estimated from your recent runs" });
      const rows = f.series;
      const o = baseOptions();
      o.scales.y.reverse = true;
      o.scales.y.ticks.callback = (v) => mmss(v);
      o.plugins.tooltip.callbacks = { label: (c) => ` ${mmss(c.raw)}` };
      chart(host, "five_k", { type: "line", data: { labels: labels(rows), datasets: [{ label: "5K", data: rows.map((r) => r.seconds), ...line(css("--s2")) }] }, options: o });
    },
  },

  race: {
    available: () => S.status.race_enabled && S.status.race,
    render(body) {
      const r = S.status.race;
      const when = r.days_to_go > 0 ? `${r.days_to_go}` : r.days_to_go === 0 ? "Today" : "Done";
      body.innerHTML = `<div class="race">
        <div class="race-count"><div class="race-n">${esc(when)}</div><div class="muted">${r.days_to_go > 0 ? (r.days_to_go === 1 ? "day to go" : "days to go") : ""}</div></div>
        <div class="race-info"><div class="race-name">${esc(r.name)}</div><div class="muted">${esc(new Date(r.date + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" }))}${r.distance ? " · " + esc(r.distance) : ""}</div>
          <div class="race-stats">
            ${r.goal ? `<div><span>Goal</span><b>${esc(r.goal)}</b><em>${esc(r.goal_pace || "")}</em></div>` : ""}
            ${r.estimate ? `<div><span>Current estimate</span><b>${esc(r.estimate)}</b><em>${esc(r.estimate_pace || "")}</em></div>` : ""}
            ${r.gap_s != null ? `<div><span>Gap</span><b class="${r.gap_s <= 0 ? "good-t" : ""}">${r.gap_s <= 0 ? "On track" : "+" + esc(mmss(r.gap_s))}</b><em>${r.gap_s <= 0 ? "ahead of goal" : "to find"}</em></div>` : ""}
          </div></div></div>`;
    },
  },

  weekly_run: { render: (body) => weeklyBox(body, S.weekly, "run", css("--s1")) },
  weekly_ride: { available: () => S.status.include_cycling, render: (body) => weeklyBox(body, S.rides, "ride", css("--s3")) },

  fitness_trend: {
    render(body) {
      const h = S.ov.history;
      body.innerHTML = `<div class="chart"></div>`;
      const o = baseOptions(); o.scales.y.min = 0; o.scales.y.max = 100;
      chart($(".chart", body), "fitness_trend", { type: "line", data: { labels: labels(h), datasets: [{ label: "Fitness score", data: h.map((r) => r.score), ...area(css("--s3")) }] }, options: o });
    },
  },

  rhr: {
    render(body) {
      const l = latest(S.daily, "resting_hr");
      const v = S.daily.map((r) => r.resting_hr);
      const r7 = v.map((_, i) => { const w = v.slice(Math.max(0, i - 6), i + 1).filter((x) => x != null); return w.length >= 3 ? +(w.reduce((a, b) => a + b, 0) / w.length).toFixed(1) : null; });
      const host = metricBox(body, { value: l ? fmt(l.resting_hr) : "–", unit: "bpm", sub: l ? esc(shortDate(l.date)) : "No resting heart rate in this range yet" });
      chart(host, "rhr", { type: "line", data: { labels: labels(S.daily), datasets: [
        { label: "7-day avg", data: r7, ...line(css("--s2")) },
        { label: "Daily", data: v, ...line(css("--s2"), { showLine: false, pointRadius: 2, backgroundColor: css("--s2") + "66" }) },
      ] }, options: legend(baseOptions()) });
    },
  },

  hrv: {
    render(body) {
      const l = latest(S.daily, "hrv_last_night");
      const host = metricBox(body, { value: l ? fmt(l.hrv_last_night) : "–", unit: "ms",
        sub: l ? esc(`${shortDate(l.date)}${l.hrv_status ? " · " + String(l.hrv_status).toLowerCase() : ""}`) : "" });
      chart(host, "hrv", { type: "line", data: { labels: labels(S.daily), datasets: [
        { label: "Overnight", data: S.daily.map((r) => r.hrv_last_night), ...line(css("--s3"), { borderWidth: 1.5 }) },
        { label: "7-day avg", data: S.daily.map((r) => r.hrv_weekly), ...line(css("--s1")) },
      ] }, options: legend(baseOptions()) });
    },
  },

  sleep_stages: {
    render(body) {
      body.innerHTML = `<div class="chart"></div>`;
      const h = (k) => S.daily.map((r) => (r[k] == null ? null : +(r[k] / 3600).toFixed(2)));
      const st = (c) => ({ backgroundColor: c, borderColor: css("--surface"), borderWidth: { top: 1 }, borderSkipped: false, maxBarThickness: 12, stack: "s" });
      const o = legend(baseOptions()); o.scales.x.stacked = true; o.scales.y.stacked = true;
      o.plugins.tooltip.callbacks = { label: (c) => `${c.dataset.label}: ${hm(c.raw * 3600)}` };
      chart($(".chart", body), "sleep_stages", { type: "bar", data: { labels: labels(S.daily), datasets: [
        { label: "Deep", data: h("deep_s"), ...st(css("--sleep-deep")) }, { label: "Light", data: h("light_s"), ...st(css("--sleep-light")) },
        { label: "REM", data: h("rem_s"), ...st(css("--s3")) }, { label: "Awake", data: h("awake_s"), ...st(css("--s2")) },
      ] }, options: o });
    },
  },

  body_battery: {
    render(body) {
      const l = latest(S.daily, "bb_high");
      const host = metricBox(body, { value: l ? fmt(l.bb_high) : "–", unit: "high", sub: l ? esc(`low ${fmt(l.bb_low)} · ${shortDate(l.date)}`) : "" });
      const o = baseOptions(); o.scales.y.min = 0; o.scales.y.max = 100;
      o.plugins.tooltip.callbacks = { label: (c) => `Low ${c.raw[0]} → high ${c.raw[1]}` };
      chart(host, "body_battery", { type: "bar", data: { labels: labels(S.daily), datasets: [{ label: "Body Battery",
        data: S.daily.map((r) => (r.bb_low != null && r.bb_high != null ? [r.bb_low, r.bb_high] : null)), ...bar(css("--s1"), { borderRadius: 4, borderSkipped: false, maxBarThickness: 10 }) }] }, options: o });
    },
  },

  steps: {
    render(body) {
      const l = latest(S.daily, "steps");
      const today = new Date().toLocaleDateString("en-CA");
      const host = metricBox(body, { value: l ? fmt(l.steps) : "–", unit: "",
        sub: l ? esc(`${l.date === today ? "today so far" : shortDate(l.date)}${l.step_goal ? " · goal " + fmt(l.step_goal) : ""}`) : "" });
      chart(host, "steps", { type: "bar", data: { labels: labels(S.daily), datasets: [
        { label: "Steps", data: S.daily.map((r) => r.steps), ...bar(css("--s1"), { maxBarThickness: 10 }) },
        { type: "line", label: "Goal", data: S.daily.map((r) => r.step_goal), ...line(css("--muted"), { borderWidth: 1.5, borderDash: [5, 4], tension: 0, pointHoverRadius: 0 }) },
      ] }, options: baseOptions() });
    },
  },

  stress: {
    render(body) {
      const l = latest(S.daily, "avg_stress");
      const host = metricBox(body, { value: l ? fmt(l.avg_stress) : "–", unit: "avg", sub: l ? esc(shortDate(l.date)) : "" });
      const o = baseOptions(); o.scales.y.min = 0;
      chart(host, "stress", { type: "line", data: { labels: labels(S.daily), datasets: [{ label: "Avg stress", data: S.daily.map((r) => r.avg_stress), ...area(css("--s4")) }] }, options: o });
    },
  },

  training_load: {
    render(body) {
      const rows = S.training.filter((r) => r.date >= since());
      const l = latest(rows, "training_status");
      body.innerHTML = `<div class="metric-sub">${l ? `Status: <b>${esc(l.training_status)}</b>${l.load_balance ? " · " + esc(l.load_balance) : ""}` : "Training load comes from the Garmin Connect sign-in."}</div><div class="chart"></div>`;
      chart($(".chart", body), "training_load", { type: "line", data: { labels: labels(rows), datasets: [
        { label: "Acute (7-day)", data: rows.map((r) => r.acute_load), ...line(css("--s2")) },
        { label: "Chronic (28-day)", data: rows.map((r) => r.chronic_load), ...line(css("--s1")) },
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
          <td class="num">${a.distance_m ? fmt(U.dist(a.distance_m), 2) + " " + U.label() : "–"}</td><td class="num">${a.duration_s ? mmss(a.duration_s) : "–"}</td>
          <td class="num">${esc(ps)}</td><td class="num">${a.avg_hr ? fmt(a.avg_hr) : "–"}</td></tr>`;
      }).join("");
      body.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Activity</th><th class="num">Distance</th><th class="num">Time</th><th class="num">Pace / speed</th><th class="num">Avg HR</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    },
  },
};

function weeklyBox(body, rows, kind, color) {
  const monday = (() => { const d = new Date(); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d.toLocaleDateString("en-CA"); })();
  const cur = rows.length && rows[rows.length - 1].week === monday ? rows[rows.length - 1] : null;
  const host = metricBox(body, { value: fmt(cur ? U.dist(cur.distance_m) : 0, 1), unit: U.label(),
    sub: `this week · ${cur ? cur.count : 0} ${kind}${cur && cur.count === 1 ? "" : "s"}` });
  chart(host, "weekly_" + kind, { type: "bar", data: { labels: rows.map((w) => shortDate(w.week)),
    datasets: [{ label: `Distance (${U.label()})`, data: rows.map((w) => +U.dist(w.distance_m).toFixed(1)), ...bar(color) }] },
    options: baseOptions({ plugins: { ...baseOptions().plugins, tooltip: { ...baseOptions().plugins.tooltip, callbacks: {
      title: (i) => `Week of ${i[0].label}`, afterBody: (i) => { const w = rows[i[0].dataIndex]; return `${w.count} ${kind}${w.count === 1 ? "" : "s"}`; } } } } }) });
}
function since() { const d = new Date(); d.setDate(d.getDate() - S.days + 1); return d.toLocaleDateString("en-CA"); }

/* ---------------------------------------------------------------- boards */
const TITLES = { fitness: "Fitness score", coach: "AI coach", vo2: "VO2 max", readiness: "Training readiness", sleep: "Sleep score",
  five_k: "5K estimate", race: "Goal race", weekly_run: "Weekly running", weekly_ride: "Weekly cycling", fitness_trend: "Fitness score trend",
  rhr: "Resting heart rate", hrv: "HRV", sleep_stages: "Sleep stages", body_battery: "Body Battery", steps: "Steps", stress: "Stress",
  training_load: "Training load", activities: "Recent activities" };

function renderBoards() {
  const layout = S.status.layout || [];
  const cols = GDGrid.columnsNow();
  for (const section of ["main", "details"]) {
    const board = document.getElementById("sec-" + section);
    board.replaceChildren();
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
      board.append(box);
      try { w.render(body); } catch (e) { body.textContent = "Couldn't draw this box: " + e.message; }
    }
    if (!board.children.length) board.innerHTML = `<div class="board-empty">Nothing here yet. <a href="/settings#customize">Add boxes in Settings → Customize</a>.</div>`;
  }
}

/* ---------------------------------------------------------------- tabs, range, sync */
function showTab() {
  const tab = location.hash === "#details" ? "details" : "main";
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
  showTab();
  load().catch(showError);
})();
