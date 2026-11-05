/* Shared helpers for the dashboard pages. Values from the server are always
   inserted with textContent (never as HTML). */
"use strict";
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (method !== "GET") {
    opts.headers["X-Requested-With"] = "dashboard";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const r = await fetch(path, opts);
  if (r.status === 401) { window.location.href = "/login"; throw new Error("Signed out"); }
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");  // validation errors
    const err = new Error(msg || `Request failed (${r.status})`);
    err.field = data && data.field;
    throw err;
  }
  return data;
}

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
  else root.removeAttribute("data-theme");
  try { localStorage.setItem("gd-theme", theme || "auto"); } catch { /* ignore */ }
}

/* Show a status line under a form: kind is "ok", "err" or "" (neutral). */
function say(el, text, kind = "") {
  if (!el) return;
  el.textContent = text || "";
  el.className = "msg" + (kind ? " " + kind : "");
}

/* Disable a button while an async action runs. */
async function busy(button, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Working…";
  try { return await fn(); } finally { button.disabled = false; button.textContent = label; }
}
