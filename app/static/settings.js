/* Setup wizard (/setup) and Settings page (/settings). Each block only wires up
   if its elements exist on the page. Needs common.js. */
"use strict";

const PREF_FIELDS = ["units", "timezone", "theme", "include_cycling", "race_enabled", "race_name", "race_date",
  "race_distance", "race_custom_km", "race_goal_time", "weather_place", "weather_lat", "weather_lon"];
const AI_FIELDS = ["ai_provider", "ai_model", "ollama_url"];
const NOTIFY_FIELDS = ["notify_morning", "notify_weekly", "notify_problems"];
let state = null;

function fieldValue(form, name) {
  const el = form.elements[name];
  if (!el) return undefined;
  if (el.type === "checkbox") return el.checked;
  if (name === "race_custom_km" || name === "weather_lat" || name === "weather_lon") return el.value === "" ? null : Number(el.value);
  return el.value;
}

function fillForm(form, prefs, names) {
  for (const name of names) {
    const el = form.elements[name];
    if (!el || !(name in prefs)) continue;
    if (el.type === "checkbox") el.checked = !!prefs[name];
    else el.value = prefs[name] == null ? "" : String(prefs[name]);
  }
}

function showCustomKm(form) {
  const row = $("#customKmRow");
  if (row) row.hidden = form.elements.race_distance.value !== "custom";
  const fields = $("#raceFields");
  if (fields && form.elements.race_enabled) fields.hidden = !form.elements.race_enabled.checked;
}

function renderStatus() {
  const keyStatus = $("#keyStatus");
  if (keyStatus) {
    keyStatus.textContent = state.api_key_configured ? "✓ Connected" : "Not connected yet";
    keyStatus.className = "pill " + (state.api_key_configured ? "on" : "");
  }
  const connectStatus = $("#connectStatus");
  if (connectStatus) {
    connectStatus.textContent = state.connect_configured ? "✓ Connected" : "Not connected";
    connectStatus.className = "pill " + (state.connect_configured ? "on" : "");
  }
  for (const el of $$("[data-show-if-connected]")) el.hidden = !state.connect_configured;
  for (const el of $$("[data-show-if-key]")) el.hidden = !state.api_key_configured;
  for (const el of $$("[data-show-if-discord]")) el.hidden = !state.discord_saved;
  const ds = $("#discordStatus");
  if (ds) { ds.textContent = state.discord_saved ? "✓ Connected" : "Not connected"; ds.className = "pill " + (state.discord_saved ? "on" : ""); }
  const user = $("#username");
  if (user) user.textContent = state.username || "";
}

async function loadState() {
  state = await api("/api/settings");
  applyTheme(state.prefs.theme);
  const prefsForm = $("#prefsForm");
  if (prefsForm) { fillForm(prefsForm, state.prefs, PREF_FIELDS); showCustomKm(prefsForm); showPlace(state.prefs.weather_place); }
  const nf = $("#notifyForm");
  if (nf) fillForm(nf, state.prefs, NOTIFY_FIELDS);
  const aiForm = $("#aiForm");
  if (aiForm) { fillForm(aiForm, state.prefs, AI_FIELDS); showProvider(); $("#aiModelSearch").value = state.prefs.ai_model || ""; }
  renderStatus();
}

/* ---------------------------------------------------------------- Personal API key */
function wireApiKey() {
  const btn = $("#keySave");
  if (!btn) return;
  btn.addEventListener("click", () => busy(btn, async () => {
    const input = $("#keyInput");
    const key = input.value.trim();
    if (!key) return say($("#keyMsg"), "Paste your key first.", "err");
    say($("#keyMsg"), "Checking the key with Garmin…");
    try {
      await api("/api/garmin/api-key", { method: "POST", body: { key } });
      input.value = "";
      state.api_key_configured = true;
      renderStatus();
      say($("#keyMsg"), "Key saved. Your data starts syncing within a minute.", "ok");
    } catch (e) { say($("#keyMsg"), e.message, "err"); }
  }));
  const rm = $("#keyRemove");
  if (rm) rm.addEventListener("click", () => busy(rm, async () => {
    if (!confirm("Remove the Personal API key? Syncing of steps, sleep, heart rate and activities stops until you add one again.")) return;
    try {
      await api("/api/garmin/api-key/remove", { method: "POST" });
      state.api_key_configured = false; renderStatus();
      say($("#keyMsg"), "Key removed.", "ok");
    } catch (e) { say($("#keyMsg"), e.message, "err"); }
  }));
}

/* ---------------------------------------------------------------- Garmin Connect sign-in */
function wireConnect() {
  const btn = $("#connectBtn");
  if (!btn) return;
  let pending = null;
  const msg = $("#connectMsg");
  btn.addEventListener("click", () => busy(btn, async () => {
    const email = $("#cEmail").value.trim();
    const passwordEl = $("#cPassword");
    if (!email || !passwordEl.value) return say(msg, "Enter your Garmin email and password.", "err");
    say(msg, "Signing in to Garmin… (this can take up to a minute)");
    try {
      const res = await api("/api/garmin/connect", { method: "POST", body: { email, password: passwordEl.value } });
      passwordEl.value = "";  // never keep it around in the page
      if (res.status === "mfa") {
        pending = res.pending;
        $("#connectForm").hidden = true;
        $("#mfaForm").hidden = false;
        $("#mfaCode").focus();
        say(msg, "Garmin sent you a code (email or authenticator app). Enter it within 5 minutes.");
      } else { connected(); }
    } catch (e) { passwordEl.value = ""; say(msg, e.message, "err"); }
  }));
  $("#mfaBtn").addEventListener("click", () => busy($("#mfaBtn"), async () => {
    const code = $("#mfaCode").value.trim();
    if (!/^\d{4,10}$/.test(code)) return say(msg, "Enter the number Garmin sent you.", "err");
    try {
      await api("/api/garmin/connect/mfa", { method: "POST", body: { pending, code } });
      connected();
    } catch (e) {
      say(msg, e.message, "err");
      if (/Start/.test(e.message)) resetConnect();  // expired or out of tries
    }
  }));
  function resetConnect() {
    pending = null;
    $("#mfaCode").value = "";
    $("#mfaForm").hidden = true;
    $("#connectForm").hidden = false;
  }
  function connected() {
    resetConnect();
    $("#cEmail").value = "";
    state.connect_configured = true; renderStatus();
    say(msg, "Connected. VO2 max and training data appear after the next sync (a few minutes).", "ok");
  }
  const rm = $("#connectRemove");
  if (rm) rm.addEventListener("click", () => busy(rm, async () => {
    if (!confirm("Remove the Garmin Connect sign-in? Your stored training history is kept.")) return;
    try {
      await api("/api/garmin/connect/remove", { method: "POST" });
      state.connect_configured = false; renderStatus();
      say(msg, "Garmin Connect sign-in removed.", "ok");
    } catch (e) { say(msg, e.message, "err"); }
  }));
}

/* ---------------------------------------------------------------- preferences */
async function savePrefs(form, names, msgEl) {
  const values = {};
  for (const n of names) {
    const v = fieldValue(form, n);
    if (v !== undefined) values[n] = v;
  }
  try {
    const res = await api("/api/settings", { method: "POST", body: { values } });
    state.prefs = res.prefs;
    applyTheme(res.prefs.theme);
    say(msgEl, "Saved.", "ok");
    return true;
  } catch (e) {
    say(msgEl, e.message, "err");
    if (e.field && form.elements[e.field]) form.elements[e.field].focus();
    return false;
  }
}

function wirePrefs() {
  const form = $("#prefsForm");
  if (!form) return;
  const tzList = $("#tzList");
  if (tzList && Intl.supportedValuesOf) {
    for (const tz of Intl.supportedValuesOf("timeZone")) {
      const o = document.createElement("option"); o.value = tz; tzList.append(o);
    }
  }
  form.elements.race_distance.addEventListener("change", () => showCustomKm(form));
  if (form.elements.race_enabled) form.elements.race_enabled.addEventListener("change", () => showCustomKm(form));
  form.elements.theme.addEventListener("change", () => applyTheme(form.elements.theme.value));
  wirePlaceSearch(form);
  const btn = $("#prefsSave");
  if (btn) btn.addEventListener("click", () => busy(btn, () => savePrefs(form, PREF_FIELDS, $("#prefsMsg"))));
}

/* ---------------------------------------------------------------- weather location */
function showPlace(label, pending) {
  const el = $("#placeNow");
  if (!el) return;
  el.textContent = label ? `Using ${label}${pending ? ". Press Save to keep it." : ""}` : "No location set, so the weather box is off.";
}
function wirePlaceSearch(form) {
  const btn = $("#placeFind"), list = $("#placeList"), q = $("#placeQ");
  if (!btn || !form.elements.weather_place) return;
  const search = () => busy(btn, async () => {
    list.hidden = false; list.replaceChildren();
    const note = (text) => { const p = document.createElement("p"); p.className = "note"; p.textContent = text; list.append(p); };
    try {
      const r = await api("/api/weather/places?q=" + encodeURIComponent(q.value.trim()));
      if (!r.places.length) return note("No places found. Try the city name only.");
      for (const pl of r.places) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "btn"; b.textContent = pl.label;
        b.addEventListener("click", () => {
          form.elements.weather_place.value = pl.label;
          form.elements.weather_lat.value = String(pl.lat);
          form.elements.weather_lon.value = String(pl.lon);
          showPlace(pl.label, true); list.hidden = true; q.value = "";
        });
        list.append(b);
      }
    } catch (e) { note(e.message); }
  });
  btn.addEventListener("click", search);
  q.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); search(); } });
}

function provider() { const f = $("#aiForm"); return f ? f.elements.ai_provider.value || "none" : "none"; }

/* ---------------------------------------------------------------- model picker
   A searchable dropdown filled with the models your key (or Ollama server) can actually use. */
const MODEL_ID_RE = /^[A-Za-z0-9._:\/-]{1,120}$/;
const picker = { choices: [], suggested: null, shown: [], active: -1, provider: null };
const modelCache = {};

function chosenModel() { return $("#aiForm").elements.ai_model.value; }
function labelFor(id) { const c = picker.choices.find((x) => x.id === id); return c ? c.label : id; }

function setModel(id) {
  $("#aiForm").elements.ai_model.value = id || "";
  $("#aiModelSearch").value = id ? labelFor(id) : "";
  const c = picker.choices.find((x) => x.id === id);
  const note = $("#modelNote");
  if (!id) note.textContent = "";
  else if (!c && picker.choices.length) note.textContent = `${id} isn't in the list for this key, but it will be tried as typed.`;
  else note.textContent = [c && c.label !== id ? id : "", c && c.note, id === picker.suggested ? "Recommended for the coach" : ""].filter(Boolean).join(" · ");
}

function fillModels(choices, suggested) {
  picker.choices = (choices || []).filter((c) => c && c.id);
  picker.suggested = suggested || null;
  const search = $("#aiModelSearch");
  search.disabled = !picker.choices.length;
  search.placeholder = picker.choices.length ? `Search ${picker.choices.length} models` : "No models found";
  const cur = chosenModel();
  setModel(suggested && (!cur || !picker.choices.some((c) => c.id === cur)) ? suggested : cur);
}

function emptyPicker(text) {
  picker.choices = []; picker.suggested = null;
  const search = $("#aiModelSearch");
  search.disabled = true; search.placeholder = text;
  search.value = chosenModel() || "";
  $("#modelNote").textContent = "";
  closeOptions();
}

function renderOptions(query) {
  const list = $("#modelOptions");
  const words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  const ordered = [...picker.choices].sort((a, b) => (b.id === picker.suggested) - (a.id === picker.suggested));
  picker.shown = ordered.filter((c) => words.every((w) => (c.id + " " + c.label).toLowerCase().includes(w)));
  const typed = String(query || "").trim();
  if (typed && MODEL_ID_RE.test(typed) && !picker.choices.some((c) => c.id === typed) && !picker.shown.length) {
    picker.shown = [{ id: typed, label: `Use "${typed}"`, note: "not in your list", custom: true }];
  }
  list.replaceChildren(...picker.shown.map((c, i) => {
    const li = document.createElement("li");
    li.id = `modelOpt${i}`; li.setAttribute("role", "option");
    li.setAttribute("aria-selected", String(c.id === chosenModel()));
    const top = document.createElement("span"); top.className = "opt-l"; top.textContent = c.label;
    if (c.id === picker.suggested) { const b = document.createElement("span"); b.className = "opt-tag"; b.textContent = "Recommended"; top.append(b); }
    li.append(top);
    const sub = [c.label !== c.id && !c.custom ? c.id : "", c.note].filter(Boolean).join(" · ");
    if (sub) { const d = document.createElement("span"); d.className = "opt-d"; d.textContent = sub; li.append(d); }
    li.addEventListener("mousedown", (e) => e.preventDefault());  // keep focus in the search box
    li.addEventListener("click", () => pick(i));
    return li;
  }));
  if (!picker.shown.length) {
    const li = document.createElement("li"); li.className = "opt-empty"; li.textContent = "No models match";
    list.append(li);
  }
  picker.active = picker.shown.findIndex((c) => c.id === chosenModel());
  if (picker.active < 0 && picker.shown.length && words.length) picker.active = 0;
  markActive();
}

function markActive() {
  const items = $$("#modelOptions [role=option]");
  items.forEach((li, i) => li.classList.toggle("on", i === picker.active));
  const search = $("#aiModelSearch");
  if (picker.active >= 0 && items[picker.active]) {
    search.setAttribute("aria-activedescendant", items[picker.active].id);
    items[picker.active].scrollIntoView({ block: "nearest" });
  } else search.removeAttribute("aria-activedescendant");
}

function openOptions(query) {
  if (!picker.choices.length) return;
  renderOptions(query);
  $("#modelOptions").hidden = false;
  $("#aiModelSearch").setAttribute("aria-expanded", "true");
}
function closeOptions() {
  const list = $("#modelOptions");
  if (list) list.hidden = true;
  const search = $("#aiModelSearch");
  if (search) { search.setAttribute("aria-expanded", "false"); search.removeAttribute("aria-activedescendant"); }
}
function pick(i) {
  const c = picker.shown[i];
  if (!c) return;
  setModel(c.id);
  closeOptions();
  say($("#aiMsg"), "Press Save to use this model.");
}

function wireModelPicker() {
  const search = $("#aiModelSearch");
  if (!search) return;
  search.addEventListener("focus", () => { search.select(); openOptions(""); });
  search.addEventListener("input", () => openOptions(search.value));
  search.addEventListener("keydown", (e) => {
    const open = !$("#modelOptions").hidden;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) return openOptions("");
      const n = picker.shown.length;
      if (n) { picker.active = (picker.active + (e.key === "ArrowDown" ? 1 : -1) + n) % n; markActive(); }
    } else if (e.key === "Enter") {
      e.preventDefault(); e.stopPropagation();  // pick a model, don't submit the form
      if (open && picker.active >= 0) pick(picker.active);
    } else if (e.key === "Escape" && open) {
      e.preventDefault(); closeOptions(); setModel(chosenModel());
    }
  });
  search.addEventListener("blur", () => { closeOptions(); setModel(chosenModel()); });
  $("#modelToggle").addEventListener("mousedown", (e) => e.preventDefault());
  $("#modelToggle").addEventListener("click", () => {
    if ($("#modelOptions").hidden) { search.focus(); openOptions(""); } else closeOptions();
  });
}

function showProvider() {
  const p = provider();
  const info = (state.ai_providers || {})[p];
  $("#aiCloud").hidden = !(info && info.keyed);
  $("#aiLocal").hidden = p !== "ollama";
  $("#aiModelRow").hidden = p === "none";
  if (info && info.keyed) {
    const saved = state.ai_keys_saved && state.ai_keys_saved[p];
    $("#aiKeyStatus").textContent = saved ? "✓ Saved" : "Not saved";
    $("#aiKeyStatus").className = "pill " + (saved ? "on" : "");
    $("#aiKeyRemove").hidden = !saved;
    const help = $("#aiKeyHelp");
    help.replaceChildren("Get a key from ");
    const a = document.createElement("a");
    a.href = info.key_url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = info.label;
    help.append(a, ". It's stored privately on your server and never shown again.");
  }
}

async function loadModels(p) {
  const body = { provider: p };
  if (p === "ollama") body.url = $("#aiForm").elements.ollama_url.value.trim();
  const search = $("#aiModelSearch");
  search.disabled = true; search.placeholder = "Loading models…";
  const res = await api("/api/ai/models", { method: "POST", body });
  modelCache[p] = res;
  if (provider() === p) fillModels(res.choices, res.suggested);
  return res.models;
}

/* Fill the model list for the chosen provider: from memory, from the server, or explain what's needed. */
async function refreshModels() {
  const p = provider();
  if (p === "none") return;
  if (modelCache[p]) return fillModels(modelCache[p].choices, modelCache[p].suggested);
  const keyed = (state.ai_providers[p] || {}).keyed;
  if (keyed && !(state.ai_keys_saved && state.ai_keys_saved[p])) return emptyPicker("Save your API key to see models");
  if (p === "ollama" && !$("#aiForm").elements.ollama_url.value.trim()) return emptyPicker("Enter the Ollama address, then Find models");
  try { await loadModels(p); }
  catch (e) { emptyPicker("Couldn't load models"); say($("#aiMsg"), e.message, "err"); }
}

function wireAi() {
  const form = $("#aiForm");
  if (!form) return;
  const msg = $("#aiMsg");
  wireModelPicker();
  for (const r of form.querySelectorAll("input[name=ai_provider]")) {
    r.addEventListener("change", () => { showProvider(); say(msg, ""); refreshModels(); });
  }
  if (provider() !== "none") refreshModels();  // ready before you open the list
  $("#aiKeySave").addEventListener("click", () => busy($("#aiKeySave"), async () => {
    const key = $("#aiKey").value.trim();
    if (!key) return say(msg, "Paste your API key first.", "err");
    say(msg, "Checking the key…");
    try {
      const res = await api("/api/ai/key", { method: "POST", body: { provider: provider(), key } });
      $("#aiKey").value = "";
      state.ai_keys_saved[provider()] = true; showProvider();
      modelCache[provider()] = res;
      fillModels(res.choices, res.suggested);
      say(msg, `Key saved. ${res.models.length} models available${res.suggested ? `, ${labelFor(res.suggested)} picked for you` : ""}. Press Save to use it.`, "ok");
    } catch (e) { say(msg, e.message, "err"); }
  }));
  $("#aiKeyRemove").addEventListener("click", () => busy($("#aiKeyRemove"), async () => {
    if (!confirm("Remove this API key from the dashboard?")) return;
    try {
      await api("/api/ai/key/remove", { method: "POST", body: { provider: provider() } });
      state.ai_keys_saved[provider()] = false; showProvider();
      delete modelCache[provider()]; emptyPicker("Save your API key to see models");
      say(msg, "Key removed.", "ok");
    } catch (e) { say(msg, e.message, "err"); }
  }));
  form.elements.ollama_url.addEventListener("change", () => { delete modelCache.ollama; });
  $("#aiTest").addEventListener("click", () => busy($("#aiTest"), async () => {
    say(msg, "Contacting Ollama…");
    try {
      const models = await loadModels("ollama");
      say(msg, models.length ? `Found: ${models.join(", ")}` : "Connected, but no models installed. On that machine run: ollama pull llama3.1:8b", models.length ? "ok" : "err");
    } catch (e) { say(msg, e.message, "err"); }
  }));
  $("#aiSave").addEventListener("click", () => busy($("#aiSave"), async () => {
    const p = provider();
    if (p !== "none" && !form.elements.ai_model.value.trim()) return say(msg, "Choose a model from the list first.", "err");
    if (await savePrefs(form, AI_FIELDS, msg) && p !== "none") say(msg, "Saved. Your coach writes its first insight within a minute or two.", "ok");
  }));
}

/* ---------------------------------------------------------------- customize */
function wireCustomize() {
  const root = $("#editor");
  if (!root) return;
  const titles = Object.fromEntries(Object.entries(state.widgets).map(([k, v]) => [k, v.title]));
  let current = state.prefs.layout;
  const msg = $("#layoutMsg");
  const ed = GDGrid.editor(root, { items: current, titles, onChange: (m) => { current = m; say(msg, "Unsaved changes"); } });
  $("#layoutSave").addEventListener("click", () => busy($("#layoutSave"), async () => {
    try {
      const res = await api("/api/settings", { method: "POST", body: { values: { layout: current } } });
      state.prefs = res.prefs; current = res.prefs.layout;
      say(msg, "Layout saved.", "ok");
    } catch (e) { say(msg, e.message, "err"); }
  }));
  $("#layoutReset").addEventListener("click", () => {
    current = Object.entries(state.widgets).map(([id, w]) => ({ id, section: w.section, w: w.w, h: w.h }));
    ed.load(current);
    say(msg, "Default layout restored. Press Save layout to keep it.");
  });
}

/* ---------------------------------------------------------------- notifications */
function wireNotify() {
  if (!$("#hookSave")) return;
  const msg = $("#hookMsg");
  $("#hookSave").addEventListener("click", () => busy($("#hookSave"), async () => {
    const url = $("#hookInput").value.trim();
    if (!url) return say(msg, "Paste the webhook URL first.", "err");
    say(msg, "Sending a test message to Discord…");
    try {
      await api("/api/notify/webhook", { method: "POST", body: { url } });
      $("#hookInput").value = ""; state.discord_saved = true; renderStatus();
      say(msg, "Connected. Check your Discord channel for the test message.", "ok");
    } catch (e) { say(msg, e.message, "err"); }
  }));
  $("#hookTest").addEventListener("click", () => busy($("#hookTest"), async () => {
    try { await api("/api/notify/test", { method: "POST" }); say(msg, "Sent.", "ok"); }
    catch (e) { say(msg, e.message, "err"); }
  }));
  $("#hookRemove").addEventListener("click", () => busy($("#hookRemove"), async () => {
    if (!confirm("Remove the Discord webhook? Notifications stop.")) return;
    try { await api("/api/notify/webhook/remove", { method: "POST" }); state.discord_saved = false; renderStatus(); say(msg, "Removed.", "ok"); }
    catch (e) { say(msg, e.message, "err"); }
  }));
  $("#notifySave").addEventListener("click", () => busy($("#notifySave"), () => savePrefs($("#notifyForm"), NOTIFY_FIELDS, $("#notifyMsg"))));
}

/* ---------------------------------------------------------------- panels */
function wirePanels() {
  const links = $$(".side-nav a");
  if (!links.length) return;
  const show = () => {
    const want = (location.hash || "#general").slice(1);
    const name = links.some((a) => a.dataset.panel === want) ? want : "general";
    for (const p of $$("section.panel[data-panel]")) p.hidden = p.dataset.panel !== name;
    for (const a of links) a.classList.toggle("on", a.dataset.panel === name);
  };
  window.addEventListener("hashchange", show);
  show();
}

/* ---------------------------------------------------------------- account */
function wireAccount() {
  const btn = $("#pwSave");
  if (btn) btn.addEventListener("click", () => busy(btn, async () => {
    const current = $("#pwCurrent").value, next = $("#pwNew").value, again = $("#pwNew2").value;
    if (next !== again) return say($("#pwMsg"), "The new passwords didn't match.", "err");
    try {
      const res = await api("/api/account/password", { method: "POST", body: { current, new: next } });
      for (const id of ["#pwCurrent", "#pwNew", "#pwNew2"]) $(id).value = "";
      say($("#pwMsg"), "Password changed." + (res.signed_out ? ` Signed out ${res.signed_out} other device(s).` : ""), "ok");
    } catch (e) { say($("#pwMsg"), e.message, "err"); }
  }));
  const lo = $("#logoutOthers");
  if (lo) lo.addEventListener("click", () => busy(lo, async () => {
    try {
      const res = await api("/api/account/logout-others", { method: "POST" });
      say($("#logoutMsg"), `Signed out ${res.signed_out} other session(s).`, "ok");
    } catch (e) { say($("#logoutMsg"), e.message, "err"); }
  }));
}

/* ---------------------------------------------------------------- data */
function wireData() {
  const delData = $("#delDataBtn");
  if (!delData) return;
  delData.addEventListener("click", () => busy(delData, async () => {
    const password = $("#delPw1").value;
    if (!password) return say($("#dataMsg"), "Enter your password to confirm.", "err");
    if (!confirm("Delete all stored Garmin data from this dashboard? Your account and Garmin connections are kept, and data re-syncs from Garmin.")) return;
    try {
      await api("/api/data/delete", { method: "POST", body: { password, scope: "garmin_data" } });
      $("#delPw1").value = "";
      say($("#dataMsg"), "Stored Garmin data deleted. It will re-sync from Garmin on the next sync.", "ok");
    } catch (e) { say($("#dataMsg"), e.message, "err"); }
  }));
  const delAll = $("#delAllBtn");
  delAll.addEventListener("click", () => busy(delAll, async () => {
    const password = $("#delPw2").value;
    if ($("#delConfirm").value.trim() !== "DELETE") return say($("#dataMsg"), "Type DELETE to confirm.", "err");
    if (!password) return say($("#dataMsg"), "Enter your password to confirm.", "err");
    try {
      const res = await api("/api/data/delete", { method: "POST", body: { password, scope: "everything" } });
      window.location.href = res.next || "/welcome";
    } catch (e) { say($("#dataMsg"), e.message, "err"); }
  }));
}

/* ---------------------------------------------------------------- setup wizard */
function wireWizard() {
  const steps = $$(".step");
  if (!steps.length) return;
  let i = 0;
  const dots = $("#stepDots");
  function show(n) {
    i = Math.max(0, Math.min(steps.length - 1, n));
    steps.forEach((s, k) => { s.hidden = k !== i; });
    if (dots) {
      dots.replaceChildren(...steps.map((s, k) => {
        const d = document.createElement("span");
        d.className = "dot" + (k === i ? " cur" : k < i ? " done" : "");
        d.title = s.dataset.title || "";
        return d;
      }));
    }
    window.scrollTo(0, 0);
  }
  for (const b of $$("[data-next]")) b.addEventListener("click", () => show(i + 1));
  for (const b of $$("[data-prev]")) b.addEventListener("click", () => show(i - 1));
  const prefsNext = $("#prefsNext");
  if (prefsNext) prefsNext.addEventListener("click", () => busy(prefsNext, async () => {
    if (await savePrefs($("#prefsForm"), PREF_FIELDS, $("#prefsMsg"))) show(i + 1);
  }));
  $("#finishBtn").addEventListener("click", () => busy($("#finishBtn"), async () => {
    await api("/api/setup/done", { method: "POST" });
    window.location.href = "/";
  }));
  show(0);
}

(async function init() {
  // Never let the browser submit these forms itself (that could put a password in a URL).
  // Enter presses the form's main button instead.
  for (const f of $$("form")) {
    f.addEventListener("submit", (e) => {
      e.preventDefault();
      const primary = f.querySelector("[data-primary]:not([hidden])");
      if (primary && !primary.disabled) primary.click();
    });
  }
  try { await loadState(); } catch (e) { say($("#pageMsg"), "Couldn't load settings: " + e.message, "err"); return; }
  wirePanels(); wireApiKey(); wireConnect(); wirePrefs(); wireAi(); wireCustomize(); wireNotify(); wireAccount(); wireData(); wireWizard();
})();
