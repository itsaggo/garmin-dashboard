/* Box layout: used by the dashboard (render) and Settings → Customize (drag & resize editor).
   Layout = [{id, section: "main"|"details"|"hidden", w: 2-12 columns, h: 2-12 rows}] in display order.
   Uses Pointer Events, so dragging works with a mouse, pen or finger. */
"use strict";
const GDGrid = (() => {
  const COLS = 12;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function columnsNow() {
    if (window.matchMedia("(max-width: 700px)").matches) return 1;
    if (window.matchMedia("(max-width: 1100px)").matches) return 6;
    return COLS;
  }

  function sizeBox(el, w, h, cols = columnsNow()) {
    const span = cols === COLS ? w : cols === 1 ? 1 : clamp(Math.round(w / 2), 2, cols);
    el.style.setProperty("--span", String(span));
    el.style.setProperty("--rows", String(h));
    el.dataset.w = String(w);
    el.dataset.h = String(h);
  }

  /* ------------------------------------------------------------ editor */
  function editor(root, { items, titles, onChange }) {
    const zones = {};
    for (const z of root.querySelectorAll("[data-zone]")) zones[z.dataset.zone] = z;

    function tile(item) {
      const t = document.createElement("div");
      t.className = "tile";
      t.dataset.id = item.id;
      t.tabIndex = 0;
      const title = document.createElement("span");
      title.className = "tile-t";
      title.textContent = titles[item.id] || item.id;
      const size = document.createElement("span");
      size.className = "tile-s";
      const sel = document.createElement("select");
      sel.className = "tile-sec";
      sel.setAttribute("aria-label", "Move to");
      for (const [v, label] of [["main", "Main page"], ["details", "Details"], ["hidden", "Hidden"]]) {
        const o = document.createElement("option"); o.value = v; o.textContent = label; sel.append(o);
      }
      sel.value = item.section;
      sel.addEventListener("change", () => { zones[sel.value].append(t); changed(); });
      const rz = document.createElement("span");
      rz.className = "rz";
      rz.title = "Drag to resize";
      t.append(title, size, sel, rz);
      setSize(t, item.w, item.h);
      t.addEventListener("pointerdown", (e) => startDrag(e, t));
      rz.addEventListener("pointerdown", (e) => startResize(e, t));
      t.addEventListener("keydown", (e) => keyResize(e, t));
      return t;
    }

    function setSize(t, w, h) {
      t.style.setProperty("--span", String(w));
      t.style.setProperty("--rows", String(h));
      t.dataset.w = String(w); t.dataset.h = String(h);
      t.querySelector(".tile-s").textContent = `${w} × ${h}`;
    }

    function model() {
      const out = [];
      for (const [section, z] of Object.entries(zones)) {
        for (const t of z.querySelectorAll(":scope > .tile")) {
          out.push({ id: t.dataset.id, section, w: +t.dataset.w, h: +t.dataset.h });
          t.querySelector(".tile-sec").value = section;
        }
      }
      return out;
    }
    const changed = () => onChange(model());

    function load(list) {
      Object.values(zones).forEach((z) => z.replaceChildren());
      for (const it of list) (zones[it.section] || zones.main).append(tile(it));
    }

    /* ---- drag to move */
    function startDrag(e, t) {
      if (e.target.closest(".rz, select")) return;
      if (e.pointerType === "mouse" && e.button !== 0) return;
      const sx = e.clientX, sy = e.clientY;
      let dragging = false, ph = null, rect = null, scroller = null;
      t.setPointerCapture(e.pointerId);

      const begin = () => {
        dragging = true;
        rect = t.getBoundingClientRect();
        ph = document.createElement("div");
        ph.className = "tile ph";
        ph.style.setProperty("--span", t.dataset.w);
        ph.style.setProperty("--rows", t.dataset.h);
        t.before(ph);
        t.classList.add("dragging");
        Object.assign(t.style, { position: "fixed", left: rect.left + "px", top: rect.top + "px",
          width: rect.width + "px", height: rect.height + "px", zIndex: "1000", pointerEvents: "none" });
      };
      const move = (ev) => {
        if (!dragging) {
          if (Math.hypot(ev.clientX - sx, ev.clientY - sy) < 6) return;
          begin();
        }
        ev.preventDefault();
        t.style.left = rect.left + ev.clientX - sx + "px";
        t.style.top = rect.top + ev.clientY - sy + "px";
        // gentle auto-scroll near the window edges
        clearInterval(scroller);
        const edge = 70;
        const dir = ev.clientY < edge ? -1 : ev.clientY > window.innerHeight - edge ? 1 : 0;
        if (dir) scroller = setInterval(() => window.scrollBy(0, dir * 14), 16);
        const under = document.elementFromPoint(ev.clientX, ev.clientY);
        if (!under) return;
        const target = under.closest(".tile:not(.ph):not(.dragging)");
        const zone = under.closest("[data-zone]");
        if (target && root.contains(target)) {
          const r = target.getBoundingClientRect();
          const wide = r.width > (target.parentElement.clientWidth * 0.75);
          const before = wide ? ev.clientY < r.top + r.height / 2 : ev.clientX < r.left + r.width / 2;
          if (before) { if (target.previousElementSibling !== ph) target.before(ph); }
          else if (target.nextElementSibling !== ph) target.after(ph);
        } else if (zone && root.contains(zone) && ph.parentElement !== zone) {
          zone.append(ph);
        }
      };
      const up = () => {
        t.removeEventListener("pointermove", move);
        t.removeEventListener("pointerup", up);
        t.removeEventListener("pointercancel", up);
        clearInterval(scroller);
        if (!dragging) return;
        ph.replaceWith(t);
        t.classList.remove("dragging");
        for (const k of ["position", "left", "top", "width", "height", "zIndex", "pointerEvents"]) t.style[k] = "";
        changed();
      };
      t.addEventListener("pointermove", move);
      t.addEventListener("pointerup", up);
      t.addEventListener("pointercancel", up);
    }

    /* ---- drag the corner to resize */
    function startResize(e, t) {
      e.stopPropagation();
      e.preventDefault();
      t.setPointerCapture(e.pointerId);
      const zone = t.parentElement;
      const cs = getComputedStyle(zone);
      const gap = parseFloat(cs.columnGap) || 12;
      const rowH = parseFloat(cs.gridAutoRows) || 44;
      const colW = (zone.clientWidth - gap * (COLS - 1)) / COLS;
      const sx = e.clientX, sy = e.clientY, sw = +t.dataset.w, sh = +t.dataset.h;
      t.classList.add("resizing");
      const move = (ev) => {
        const w = clamp(Math.round(sw + (ev.clientX - sx) / (colW + gap)), 2, COLS);
        const h = clamp(Math.round(sh + (ev.clientY - sy) / (rowH + gap)), 2, 12);
        if (w !== +t.dataset.w || h !== +t.dataset.h) setSize(t, w, h);
      };
      const up = () => {
        t.removeEventListener("pointermove", move);
        t.removeEventListener("pointerup", up);
        t.removeEventListener("pointercancel", up);
        t.classList.remove("resizing");
        changed();
      };
      t.addEventListener("pointermove", move);
      t.addEventListener("pointerup", up);
      t.addEventListener("pointercancel", up);
    }

    /* ---- keyboard: arrows resize, Alt+arrows reorder */
    function keyResize(e, t) {
      if (e.target !== t) return;
      let w = +t.dataset.w, h = +t.dataset.h;
      if (e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowUp")) { t.previousElementSibling?.before(t); }
      else if (e.altKey && (e.key === "ArrowRight" || e.key === "ArrowDown")) { t.nextElementSibling?.after(t); }
      else if (e.key === "ArrowRight") w++; else if (e.key === "ArrowLeft") w--;
      else if (e.key === "ArrowDown") h++; else if (e.key === "ArrowUp") h--;
      else return;
      e.preventDefault();
      setSize(t, clamp(w, 2, COLS), clamp(h, 2, 12));
      t.focus();
      changed();
    }

    load(items);
    return { load, model };
  }

  return { columnsNow, sizeBox, editor };
})();
