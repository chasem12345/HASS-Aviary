// Aviary — dashboard page: charts + the test-notification button.
(function () {
  const { API, perDayChart, hourlyChart } = window.Aviary;

  window.aviaryInitDashboard = function (opts) {
    perDayChart({ source: opts.source, days: opts.days, since: opts.since });
    hourlyChart({ source: opts.source, days: opts.days, since: opts.since }, opts.sun);
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

  // The dex entry's ORDER / FAMILY / STATUS rows are fed by the same species-info
  // fetch as the About card — no second request. Filled before the About card's
  // early-returns so the rows resolve to "unknown" instead of sitting on placeholders.
})();
