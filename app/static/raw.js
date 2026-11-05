/* Raw data viewer: builds DOM nodes with textContent only (no HTML from data). */
async function getJson(url) {
  const r = await fetch(url, { credentials: "same-origin" });
  if (r.status === 401) { window.location.href = "/login"; throw new Error("Signed out"); }
  if (!r.ok) throw new Error(url + ": " + r.status);
  return r.json();
}
function cell(tag, text, cls) { const el = document.createElement(tag); el.textContent = text; if (cls) el.className = cls; return el; }

getJson("/api/raw").then((rows) => {
  const table = document.getElementById("list");
  const head = document.createElement("tr");
  head.append(cell("th", "Endpoint"), cell("th", "Responses", "num"), cell("th", "Last fetched"));
  table.append(head);
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = cell("td", "Nothing stored yet. Run a sync first.", "empty");
    td.colSpan = 3; tr.append(td); table.append(tr);
    return;
  }
  for (const r of rows) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    const a = cell("a", r.endpoint);
    a.href = "#";
    a.addEventListener("click", async (ev) => {
      ev.preventDefault();
      const d = await getJson("/api/raw/" + encodeURIComponent(r.endpoint));
      document.getElementById("which").textContent = `${r.endpoint} · ${d.key}`;
      document.getElementById("body").textContent = JSON.stringify(d.body, null, 2);
    });
    td.append(a);
    tr.append(td, cell("td", String(r.n), "num"), cell("td", r.last));
    table.append(tr);
  }
}).catch((e) => { document.getElementById("body").textContent = e.message; });
