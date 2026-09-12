// Aviary — shared helpers: URL bases, JSON fetch, chart colours, theme toggle.
// Loaded on every page before the others; they read window.Aviary.
(function () {
  "use strict";

  const API = window.AVIARY_API || "api";
  const BASE = window.AVIARY_BASE || "";

  function withParams(url, params) {
    const u = new URL(url, window.location.href);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== null && v !== undefined && v !== "") u.searchParams.set(k, v);
    });
    return u.toString();
  }

  async function getJson(path, params) {
    const res = await fetch(withParams(API + path, params));
    if (!res.ok) throw new Error("fetch failed: " + path);
    return res.json();
  }

  // Read theme colors from the live CSS custom properties rather than duplicating them
  // here, so charts follow whichever theme is active (including the dex theme, which
  // ignores the OS light/dark preference entirely).
  function cssVar(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (value || "").trim() || fallback;
  }

  function axisColor() {
    return cssVar("--muted", "#6b7785");
  }

  // rgba() from a #rrggbb custom property, for chart area fills.
  function tint(hex, alpha) {
    const m = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(hex.trim());
    if (!m) return hex;
    const [r, g, b] = m.slice(1).map((h) => parseInt(h, 16));
    return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
  }

  function chartError(canvasId) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const div = document.createElement("div");
    div.className = "chart-error";
    div.textContent = "Couldn't load chart data.";
    canvas.replaceWith(div);
  }

  function commonOptions() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: axisColor() }, grid: { display: false } },
        y: { ticks: { color: axisColor(), precision: 0 }, beginAtZero: true },
      },
    };
  }

  // ---------------------------------------------------------------- theme toggle
  // The theme is stored server-side (so server-rendered pages can stamp it without a
  // flash), which means switching is a POST followed by a reload.

  async function setTheme(next) {
    try {
      const res = await fetch(API + "/theme?theme=" + encodeURIComponent(next), {
        method: "POST",
      });
      if (!res.ok) throw new Error(res.status);
      window.location.reload();
    } catch (e) {
      alert("Couldn't switch theme: " + e);
    }
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-theme-next]");
    if (!btn) return;
    e.preventDefault();
    setTheme(btn.dataset.themeNext);
  });

  window.Aviary = Object.assign(window.Aviary || {}, {
    API, BASE, withParams, getJson, cssVar, axisColor, tint, chartError, commonOptions,
  });
})();
