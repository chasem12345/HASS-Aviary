// Dashboard charts. Fetches JSON from the ingress-aware API base and renders Chart.js.
(function () {
  "use strict";

  function apiUrl(path, params) {
    const base = window.AVIARY_API || "api";
    const u = new URL(base + path, window.location.href);
    Object.entries(params || {}).forEach(([k, v]) => {
      if (v !== null && v !== undefined) u.searchParams.set(k, v);
    });
    return u.toString();
  }

  async function getJson(path, params) {
    const res = await fetch(apiUrl(path, params));
    if (!res.ok) throw new Error("fetch failed: " + path);
    return res.json();
  }

  function axisColor() {
    const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    return dark ? "#93a1af" : "#6b7785";
  }

  async function initDashboard(source, days) {
    const common = {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: axisColor() }, grid: { display: false } },
        y: { ticks: { color: axisColor() }, beginAtZero: true },
      },
    };

    try {
      const perDay = await getJson("/per-day", { source, days });
      new Chart(document.getElementById("perDay"), {
        type: "line",
        data: {
          labels: perDay.data.map((d) => d.day),
          datasets: [{
            data: perDay.data.map((d) => d.count),
            borderColor: "#2f7d5b", backgroundColor: "rgba(47,125,91,.15)",
            fill: true, tension: 0.3, pointRadius: 2,
          }],
        },
        options: common,
      });
    } catch (e) { console.error(e); }

    try {
      const hourly = await getJson("/hourly", { source, days });
      new Chart(document.getElementById("hourly"), {
        type: "bar",
        data: {
          labels: hourly.data.map((d) => String(d.hour).padStart(2, "0")),
          datasets: [{ data: hourly.data.map((d) => d.count), backgroundColor: "#3b6ea5" }],
        },
        options: common,
      });
    } catch (e) { console.error(e); }
  }

  window.aviaryInitDashboard = initDashboard;
})();
