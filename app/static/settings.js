/* Setup wizard (/setup) and Settings page (/settings). Each block only wires up
   if its elements exist on the page. Needs common.js. */
"use strict";

const PREF_FIELDS = ["units", "timezone", "theme", "include_cycling", "race_name", "race_date",
  "race_distance", "race_custom_km", "race_goal_time"];
const AI_FIELDS = ["ai_enabled", "ollama_url", "ollama_model"];
let state = null;

function fieldValue(form, name) {
  const el = form.elements[name];
  if (!el) return undefined;
  if (el.type === "checkbox") return el.checked;
  if (name === "race_custom_km") return el.value === "" ? null : Number(el.value);
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
  const user = $("#username");
  if (user) user.textContent = state.username || "";
}

async function loadState() {
  state = await api("/api/settings");
  applyTheme(state.prefs.theme);
  const prefsForm = $("#prefsForm");
  if (prefsForm) { fillForm(prefsForm, state.prefs, PREF_FIELDS); showCustomKm(prefsForm); }
  const aiForm = $("#aiForm");
  if (aiForm) fillForm(aiForm, state.prefs, AI_FIELDS);
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
  form.elements.theme.addEventListener("change", () => applyTheme(form.elements.theme.value));
  const btn = $("#prefsSave");
  if (btn) btn.addEventListener("click", () => busy(btn, () => savePrefs(form, PREF_FIELDS, $("#prefsMsg"))));
}

function wireAi() {
  const form = $("#aiForm");
  if (!form) return;
  $("#aiTest").addEventListener("click", () => busy($("#aiTest"), async () => {
    const url = form.elements.ollama_url.value.trim();
    say($("#aiMsg"), "Contacting Ollama…");
    try {
      const res = await api("/api/coach/ai/test", { method: "POST", body: { url } });
      const list = $("#modelList");
      list.replaceChildren(...res.models.map((m) => { const o = document.createElement("option"); o.value = m; return o; }));
      if (!form.elements.ollama_model.value && res.models[0]) form.elements.ollama_model.value = res.models[0];
      say($("#aiMsg"), res.models.length ? `Connected. Models found: ${res.models.join(", ")}` :
        "Connected, but no models are installed. On the server run: ollama pull llama3.1:8b", res.models.length ? "ok" : "err");
    } catch (e) { say($("#aiMsg"), e.message, "err"); }
  }));
  $("#aiSave").addEventListener("click", () => busy($("#aiSave"), () => savePrefs(form, AI_FIELDS, $("#aiMsg"))));
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
  wireApiKey(); wireConnect(); wirePrefs(); wireAi(); wireAccount(); wireData(); wireWizard();
})();
