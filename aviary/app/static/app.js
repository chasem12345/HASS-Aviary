// Charts + live refresh. Fetches JSON from the ingress-aware API base.
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

  function axisColor() {
    const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    return dark ? "#93a1af" : "#6b7785";
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

  async function perDayChart(params) {
    try {
      const perDay = await getJson("/per-day", params);
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
        options: commonOptions(),
      });
    } catch (e) { console.error(e); chartError("perDay"); }
  }

  async function hourlyChart(params) {
    try {
      const hourly = await getJson("/hourly", params);
      new Chart(document.getElementById("hourly"), {
        type: "bar",
        data: {
          labels: hourly.data.map((d) => String(d.hour).padStart(2, "0")),
          datasets: [{ data: hourly.data.map((d) => d.count), backgroundColor: "#3b6ea5" }],
        },
        options: commonOptions(),
      });
    } catch (e) { console.error(e); chartError("hourly"); }
  }

  window.aviaryInitDashboard = function (opts) {
    perDayChart({ source: opts.source, days: opts.days, since: opts.since });
    hourlyChart({ source: opts.source, days: opts.days, since: opts.since });
    bindTestNotify();
  };

  function bindTestNotify() {
    const btn = document.getElementById("testNotify");
    if (!btn) return;
    const out = document.getElementById("testNotifyResult");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      out.classList.remove("err");
      out.textContent = "Sending… (can take ~10s while fetching the image)";
      try {
        const res = await fetch(API + "/test-notification", { method: "POST" });
        const data = await res.json();
        if (data.fired) {
          out.textContent = "Event fired" + (data.image ? " with image " + data.image : " (no image)") +
            ". If no notification arrived, check the blueprint automation.";
        } else {
          out.classList.add("err");
          out.textContent = "Failed: " + (data.error || "unknown error");
        }
      } catch (e) {
        out.classList.add("err");
        out.textContent = "Failed: " + e;
      } finally {
        btn.disabled = false;
      }
    });
  }

  async function loadSpeciesInfo(name, scientific) {
    const el = document.getElementById("about");
    if (!el) return;
    try {
      const info = await getJson("/species-info", { name: name, sci: scientific });
      if (!info || !info.ok || (!info.extract && !info.family)) return;
      const set = (sel, text) => {
        const node = el.querySelector(sel);
        if (node && text) node.textContent = text;
      };
      set(".about-descriptor", info.descriptor);
      set(".about-extract", info.extract);

      const tax = el.querySelector(".about-tax");
      [["Order", info.order], ["Family", info.family], ["Status", info.conservation]]
        .forEach(([k, v]) => {
          if (!v) return;
          const chip = document.createElement("span");
          chip.className = "tax-chip";
          chip.textContent = k + ": " + v;
          tax.appendChild(chip);
        });

      const link = el.querySelector(".about-link");
      if (info.wiki_url) { link.href = info.wiki_url; link.hidden = false; }
      el.hidden = false;
    } catch (e) { /* leave the About card hidden */ }
  }

  window.aviaryInitSpecies = function (opts) {
    perDayChart({ species: opts.species, days: 30 });
    hourlyChart({ species: opts.species, days: 3650 });
    loadSpeciesInfo(opts.species, opts.scientific);
  };

  // ------------------------------------------------------------- live refresh

  window.aviaryInitRecent = function (opts) {
    if (opts.paged) return; // never auto-refresh while browsing older pages
    const groupsEl = document.getElementById("groups");
    const note = document.getElementById("live-note");
    if (!groupsEl) return;
    let newest = Number(opts.newest) || 0;
    let refreshing = false;

    function mediaPlaying() {
      return Array.from(groupsEl.querySelectorAll("video, audio"))
        .some((el) => !el.paused && !el.ended);
    }

    async function tick() {
      if (refreshing || document.hidden) return;
      refreshing = true;
      try {
        const marker = await getJson("/latest", { source: opts.source, species: opts.species });
        const markerNewest = Number(marker.newest) || 0;
        if (markerNewest > newest) {
          if (mediaPlaying()) {
            if (note) {
              note.textContent = "New detections available — the list will refresh when playback stops.";
              note.hidden = false;
            }
          } else {
            const url = withParams(BASE + "/recent/partial", {
              source: opts.source,
              species: opts.species,
              range: opts.range,
              highlight_after: newest,
            });
            const res = await fetch(url);
            if (res.ok) {
              groupsEl.innerHTML = await res.text();
              newest = markerNewest;
              if (note) note.hidden = true;
            }
          }
        }
      } catch (e) { /* transient — retry on next tick */ }
      refreshing = false;
    }

    setInterval(tick, 30000);
  };
})();
