// Aviary — the two Chart.js charts shared by the dashboard and the species page.
(function () {
  const { getJson, cssVar, tint, chartError, commonOptions } = window.Aviary;

  async function perDayChart(params) {
    try {
      const perDay = await getJson("/per-day", params);
      const accent = cssVar("--accent", "#2f7d5b");
      new Chart(document.getElementById("perDay"), {
        type: "line",
        data: {
          labels: perDay.data.map((d) => d.day),
          datasets: [{
            data: perDay.data.map((d) => d.count),
            borderColor: accent, backgroundColor: tint(accent, 0.15),
            fill: true, tension: 0.3, pointRadius: 2,
          }],
        },
        options: commonOptions(),
      });
    } catch (e) { console.error(e); chartError("perDay"); }
  }

  // Sunrise/sunset on the hourly bar charts. A per-chart plugin rather than the Chart.js
  // annotation plugin (not vendored; this is thirty lines). `sun` is the server's
  // {sunrise, sunset, *_label} in fractional local hours, or null when the location is
  // unknown — then nothing is drawn and the chart is exactly what it was.
  function sunPlugin(sun) {
    if (!sun) return [];
    // The category scale centres bar i on getPixelForValue(i), so fractional hour h sits
    // half a step left of bar 0 plus h steps.
    function px(x, h) {
      const step = x.getPixelForValue(1) - x.getPixelForValue(0);
      return x.getPixelForValue(0) - step / 2 + h * step;
    }
    function clamp(a, v) { return Math.min(a.right, Math.max(a.left, v)); }
    return [{
      id: "aviarySun",
      beforeDatasetsDraw(chart) {
        const { ctx, chartArea: a, scales: { x } } = chart;
        if (!a || !x) return;
        ctx.save();
        ctx.fillStyle = cssVar("--ribbon-night", "rgba(28,37,48,.09)");
        if (sun.sunrise != null) {
          const X = clamp(a, px(x, sun.sunrise));
          ctx.fillRect(a.left, a.top, X - a.left, a.bottom - a.top);
        }
        if (sun.sunset != null) {
          const X = clamp(a, px(x, sun.sunset));
          ctx.fillRect(X, a.top, a.right - X, a.bottom - a.top);
        }
        ctx.restore();
      },
      afterDraw(chart) {
        const { ctx, chartArea: a, scales: { x } } = chart;
        if (!a || !x) return;
        const warn = cssVar("--warn", "#d9a441");
        ctx.save();
        ctx.strokeStyle = warn; ctx.fillStyle = warn; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
        ctx.font = "11px " + cssVar("--font-body", "sans-serif"); ctx.textBaseline = "top";
        [["sunrise", "\u2600 ", "left"], ["sunset", "\u263D ", "right"]].forEach(([k, glyph, align]) => {
          if (sun[k] == null) return;
          const X = px(x, sun[k]);
          if (X < a.left || X > a.right) return;
          ctx.beginPath(); ctx.moveTo(X, a.top); ctx.lineTo(X, a.bottom); ctx.stroke();
          ctx.textAlign = align;
          ctx.fillText(glyph + (sun[k + "_label"] || ""), X + (align === "left" ? 4 : -4), a.top + 2);
        });
        ctx.restore();
      },
    }];
  }

  async function hourlyChart(params, sun) {
    try {
      const hourly = await getJson("/hourly", params);
      new Chart(document.getElementById("hourly"), {
        type: "bar",
        data: {
          labels: hourly.data.map((d) => String(d.hour).padStart(2, "0")),
          datasets: [{
            data: hourly.data.map((d) => d.count),
            backgroundColor: cssVar("--frigate", "#3b6ea5"),
          }],
        },
        options: commonOptions(),
        plugins: sunPlugin(sun),
      });
    } catch (e) { console.error(e); chartError("hourly"); }
  }

  Object.assign(window.Aviary, { perDayChart, hourlyChart });
})();
