/* Applies the saved theme before the page paints (no flash of the wrong theme). */
(function () {
  try {
    var t = localStorage.getItem("gd-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage unavailable: follow the device theme */ }
})();
