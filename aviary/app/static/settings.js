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
        " example(s) in use" +
        (d.labelled && d.labelled !== d.examples ? " of " + d.labelled + " labelled" : "") +
        (d.excluded ? " · " + d.excluded + " excluded" : "") +
        (d.auto_ignored ? " · " + d.auto_ignored + " automatic answer(s) not learned from" : "") +
        (top ? " · most examples: " + top : "");
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


  // ------------------------------------------------------- learning example browser

  const BASE = window.Aviary.BASE || "";

  function cos(v) { return v == null ? "—" : Number(v).toFixed(2); }

  function fmtWhen(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
      d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }

  async function loadProbeSpecies(keep) {
    const sel = document.getElementById("probe-species");
    if (!sel) return;
    try {
      const d = await getJson("/probe/examples");
      const current = keep || sel.value;
      sel.innerHTML = '<option value="">choose a species…</option>';
      (d.species || []).forEach((s) => {
        if (!s.labelled) return;
        const opt = document.createElement("option");
        opt.value = s.species;
        opt.textContent = s.species + " — " + s.used + " in use of " + s.labelled +
          (s.suspicious ? " · " + s.suspicious + " suspicious" : "") +
          (s.excluded ? " · " + s.excluded + " excluded" : "");
        sel.appendChild(opt);
      });
      if (current) { sel.value = current; if (sel.value) loadProbeExamples(current); }
    } catch (err) {
      sel.innerHTML = '<option value="">unavailable — ' + err + "</option>";
    }
  }

  function exampleRow(ex) {
    const tr = document.createElement("tr");
    if (ex.excluded) tr.classList.add("excluded");
    if (ex.suspicious || ex.suspicious_now) tr.classList.add("suspicious");
    const crop = BASE + "/media/frigate/" + encodeURIComponent(ex.source_ref || "") +
      (ex.subject_idx ? "/crop/" + ex.subject_idx + ".jpg" : "/crop.jpg");
    const status = ex.excluded ? "excluded" : ex.used ? "used" :
      ex.dropped_reason === "duplicate" ? "duplicate frame" :
      ex.dropped_reason === "visit_cap" ? "over the visit cap" : ex.dropped_reason || "";
    const badges = [];
    badges.push('<span class="ex-badge">' + (ex.label_source === "manual" ? "by hand" : "auto") + "</span>");
    if (ex.untargeted) badges.push('<span class="ex-badge warn" title="Frame chosen for the identifier\'s winner, not for this label — re-embed to fix">untargeted</span>');
    if (ex.suspicious || ex.suspicious_now) badges.push('<span class="ex-badge warn">suspicious</span>');
    if (ex.subject_idx) badges.push('<span class="ex-badge">other bird ' + ex.subject_idx + "</span>");
    const near = (ex.own_score != null || ex.other_score != null)
      ? "own " + cos(ex.own_score) + " · other " + cos(ex.other_score) +
        (ex.other_species ? " (" + ex.other_species + ")" : "")
      : "";
    const link = BASE + "/detection/" + ex.detection_id;
    tr.innerHTML =
      '<td class="thumb-cell"><a href="' + link + '">' +
        '<img class="thumb" loading="lazy" alt="" src="' + crop + '" onerror="this.style.visibility=\'hidden\'"></a></td>' +
      '<td><a href="' + link + '">' + fmtWhen(ex.start_time) + "</a>" +
        '<div class="ex-meta">' + badges.join("") + status + "</div></td>" +
      '<td class="ex-meta">' + near + "</td>" +
      '<td class="ex-actions">' +
        '<button type="button" class="btn btn-sm ex-toggle" data-id="' + ex.detection_id +
          '" data-idx="' + ex.subject_idx + '" data-excluded="' + (ex.excluded ? 1 : 0) + '">' +
          (ex.excluded ? "Include" : "Exclude") + "</button>" +
        '<button type="button" class="btn btn-sm ex-reembed" data-id="' + ex.detection_id +
          '" data-idx="' + ex.subject_idx + '">Re-embed</button>' +
      "</td>";
    return tr;
  }

  async function loadProbeExamples(species) {
    const out = document.getElementById("probe-examples");
    if (!out) return;
    if (!species) { out.innerHTML = ""; return; }
    out.innerHTML = '<p class="empty">Loading…</p>';
    try {
      const d = await getJson("/probe/examples/" + encodeURIComponent(species));
      const rows = d.examples || [];
      if (!rows.length) { out.innerHTML = '<p class="empty">No labelled examples.</p>'; return; }
      const table = document.createElement("table");
      table.className = "leaders";
      table.innerHTML = "<thead><tr><th></th><th>detection</th><th>nearest example</th><th></th></tr></thead>";
      const body = document.createElement("tbody");
      rows.forEach((ex) => body.appendChild(exampleRow(ex)));
      table.appendChild(body);
      out.innerHTML = "";
      out.appendChild(table);
    } catch (err) {
      out.innerHTML = '<p class="empty">Could not load — ' + err + "</p>";
    }
  }

  // The audit: which examples sit nearer another species' examples than their own. On
  // demand — it compares every example against every other.
  async function flagSuspicious() {
    const btn = document.getElementById("probe-flag");
    const out = document.getElementById("probe-flag-result");
    if (!btn || !out) return;
    btn.disabled = true;
    out.hidden = false;
    out.className = "id-health";
    out.textContent = "Auditing…";
    try {
      const res = await fetch(API + "/probe/flag", { method: "POST" });
      const d = await res.json();
      if (!d.ok) { out.textContent = "Could not audit — " + (d.error || res.status); return; }
      const per = (d.species || []).slice(0, 8).map((s) => s.species + " " + s.flagged).join(", ");
      out.className = "id-health" + (d.flagged ? " warn" : " good");
      out.textContent = d.flagged
        ? d.flagged + " example(s) look mislabelled" + (per ? " · " + per : "") +
          " — pick a species below to review them"
        : "Nothing looks mislabelled.";
      loadProbeSpecies();
      loadProbeStats();
    } catch (err) {
      out.textContent = "Could not audit — " + err;
    } finally {
      btn.disabled = false;
    }
  }

  async function forgetLearning() {
    const btn = document.getElementById("probe-forget");
    const out = document.getElementById("probe-flag-result");
    if (!btn || !out) return;
    if (!window.confirm(
      "Forget every learned example?

The probe's memory of your birds is wiped. Card " +
      "names, the species list and history are kept. Hand-named cards whose media Frigate " +
      "still has are re-learned in the background, each with a frame chosen for its name.")) {
      return;
    }
    btn.disabled = true;
    out.hidden = false;
    out.className = "id-health";
    out.textContent = "Forgetting…";
    try {
      const res = await fetch(API + "/probe/forget", { method: "POST" });
      const d = await res.json();
      if (!d.ok) { out.textContent = "Could not forget — " + (d.error || res.status); return; }
      const f = d.forgotten || {};
      out.className = "id-health good";
      out.textContent = "Forgot " + (f.detections || 0) + " example(s) and " + (f.subjects || 0) +
        " other-bird example(s)." + (d.reharvesting
          ? " Re-learning hand-named cards in the background — refresh in a few minutes."
          : "");
      loadProbeStats();
      loadProbeSpecies();
    } catch (err) {
      out.textContent = "Could not forget — " + err;
    } finally {
      btn.disabled = false;
    }
  }

  async function exampleAction(btn) {
    const id = btn.dataset.id, idx = btn.dataset.idx;
    const sel = document.getElementById("probe-species");
    btn.disabled = true;
    try {
      let res;
      if (btn.classList.contains("ex-toggle")) {
        res = await fetch(API + "/probe/examples/" + id + "/" + idx + "/exclude",
          { method: btn.dataset.excluded === "1" ? "DELETE" : "POST" });
      } else {
        btn.textContent = "Re-embedding…";
        res = await fetch(API + "/probe/examples/" + id + "/" + idx + "/reembed", { method: "POST" });
      }
      const d = await res.json();
      if (!d.ok) alert("Couldn't do that: " + (d.error || res.status));
    } catch (err) {
      alert("Couldn't do that: " + err);
    }
    loadProbeStats();
    loadProbeSpecies(sel ? sel.value : "");
  }

  window.aviaryInitSettings = function () {
    loadIdentifyHealth();
    loadInatStatus();
    loadProbeStats();
    const evalBtn = document.getElementById("probe-evaluate");
    if (evalBtn) evalBtn.addEventListener("click", evaluateProbe);
    const flagBtn = document.getElementById("probe-flag");
    if (flagBtn) flagBtn.addEventListener("click", flagSuspicious);
    const forgetBtn = document.getElementById("probe-forget");
    if (forgetBtn) forgetBtn.addEventListener("click", forgetLearning);
    const speciesSel = document.getElementById("probe-species");
    if (speciesSel) {
      loadProbeSpecies();
      speciesSel.addEventListener("change", () => loadProbeExamples(speciesSel.value));
    }
    const browser = document.getElementById("probe-examples");
    if (browser) {
      browser.addEventListener("click", (e) => {
        const btn = e.target.closest(".ex-toggle, .ex-reembed");
        if (btn) { e.preventDefault(); exampleAction(btn); }
      });
    }
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
