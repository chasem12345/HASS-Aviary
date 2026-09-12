// Aviary — settings page: identification health, probe, iNaturalist, blacklist.
(function () {
  const { API, getJson } = window.Aviary;

  // -------------------------------------------------------------------- settings

  // Reports the identification service's state on the settings page. The facts chosen
  // are the ones that actually go wrong: running on CPU when a GPU was expected (a bad
  // torch wheel or a missing container toolkit), and a species count that reveals the
  // eBird region silently fell back to the bundled list.
  async function loadInatStatus() {
    const el = document.getElementById("inat-status");
    if (!el) return;
    try {
      const data = await getJson("/inat/status");
      const acct = data.account || {};
      if (!data.configured) { el.className = "id-health"; el.textContent = "Not configured"; return; }
      if (!acct.ok) {
        el.className = "id-health bad";
        el.textContent = "Account check failed" + (acct.error ? " — " + acct.error : "");
        return;
      }
      el.className = "id-health good";
      el.textContent = "Signed in as " + (acct.login || "?") +
        (data.location_known ? "" : " — Home Assistant location unknown, posting will fail") +
        (data.last_error ? " · last error: " + data.last_error : "");
    } catch (err) {
      el.className = "id-health bad";
      el.textContent = "Account check failed — " + err;
    }
  }

  async function loadIdentifyHealth() {
    const el = document.getElementById("identify-health");
    if (!el) return;
    try {
      const data = await getJson("/identify-health");
      if (!data.ok) {
        el.className = "id-health bad";
        el.textContent = "Unreachable" + (data.error ? " — " + data.error : "");
        return;
      }
      const device = data.cuda ? data.device : "CPU" + (data.cpu_only ? " (forced)" : "");
      el.className = "id-health" + (data.cuda || data.cpu_only ? " good" : " warn");
      el.textContent =
        "Online · " + device +
        " · " + (data.species_count || 0) + " species from " + (data.species_source || "?");
      if (!data.cuda && !data.cpu_only) {
        el.textContent += " — no GPU detected; check nvidia-container-toolkit and the cu126 torch wheel";
      }
    } catch (err) {
      el.className = "id-health bad";
      el.textContent = "Unreachable — " + err;
    }
  }

  // What the few-shot probe has learned. Shown alongside service health because "is it
  // learning?" is the natural follow-up to "is it running?".
  async function loadProbeStats() {
    const el = document.getElementById("probe-stats");
    if (!el) return;
    try {
      const d = await getJson("/probe");
      if (!d.species) {
        el.className = "id-health";
        el.textContent = "No examples yet — confirm a few species, or use ✎ to name one, " +
          "and it will start matching against your own birds.";
        return;
      }
      const top = (d.top || []).slice(0, 5)
        .map((s) => s.species + " (" + s.examples + ")").join(", ");
      el.className = "id-health good";
      el.textContent = d.species + " species learned from " + d.examples +
        " confirmed detection(s)" + (top ? " · most examples: " + top : "");
    } catch (err) {
      el.className = "id-health";
      el.textContent = "Probe status unavailable — " + err;
    }
  }

  // Leave-one-out accuracy over the user's own confirmed birds — the "is my labelling
  // working?" button. On demand rather than on load: it re-scores every stored example.
  async function evaluateProbe() {
    const btn = document.getElementById("probe-evaluate");
    const out = document.getElementById("probe-evaluate-result");
    if (!btn || !out) return;
    btn.disabled = true;
    out.hidden = false;
    out.className = "id-health";
    out.textContent = "Evaluating…";
    try {
      const d = await getJson("/probe/evaluate");
      if (!d.ok) {
        out.textContent = "Could not evaluate — " + (d.error || "unknown error");
        return;
      }
      if (!d.evaluated) {
        out.textContent = "Nothing to evaluate yet — species need at least two " +
          "confirmed examples (or one plus reference photos).";
        return;
      }
      const overall = Math.round((d.accuracy || 0) * 100);
      const per = (d.species || []).slice(0, 8)
        .map((s) => s.species + " " + s.correct + "/" + s.n).join(", ");
      out.className = "id-health" + (overall >= 80 ? " good" : overall >= 60 ? "" : " warn");
      out.textContent = overall + "% of " + d.evaluated +
        " held-out example(s) identified correctly" + (per ? " · " + per : "");
    } catch (err) {
      out.textContent = "Could not evaluate — " + err;
    } finally {
      btn.disabled = false;
    }
  }

  window.aviaryInitSettings = function () {
    loadIdentifyHealth();
    loadInatStatus();
    loadProbeStats();
    const evalBtn = document.getElementById("probe-evaluate");
    if (evalBtn) evalBtn.addEventListener("click", evaluateProbe);
    document.querySelectorAll(".blacklist-remove").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const species = btn.dataset.species;
        btn.disabled = true;
        try {
          const res = await fetch(API + "/blacklist/" + encodeURIComponent(species), {
            method: "DELETE",
          });
          const data = await res.json();
          if (!data.ok) {
            alert("Couldn't un-blacklist: " + (data.error || res.status));
            btn.disabled = false;
            return;
          }
          window.location.reload();
        } catch (err) {
          alert("Couldn't un-blacklist: " + err);
          btn.disabled = false;
        }
      });
    });
  };
})();
