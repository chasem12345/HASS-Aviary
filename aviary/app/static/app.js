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

  async function hourlyChart(params) {
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

  // The dex entry's ORDER / FAMILY / STATUS rows are fed by the same species-info
  // fetch as the About card — no second request. Filled before the About card's
  // early-returns so the rows resolve to "unknown" instead of sitting on placeholders.
  function fillDexTaxonomy(info) {
    const t = (info && info.traits) || {};
    const fields = {
      "dex-order": info && info.order,
      "dex-family": info && info.family,
      "dex-status": info && info.conservation,
      "dex-diet": t.food,
      "dex-forages": t.foraging,
      "dex-habitat": t.habitat,
    };
    Object.keys(fields).forEach((id) => {
      const node = document.getElementById(id);
      if (!node) return;
      node.textContent = fields[id] || "unknown";
      node.classList.remove("pending");
      // The AVONET term behind the plain-English diet description.
      if (id === "dex-diet" && t.niche && t.niche !== t.food) node.title = t.niche;
    });
  }

  async function loadSpeciesInfo(name, scientific) {
    const el = document.getElementById("about");
    if (!el) return;
    try {
      const info = await getJson("/species-info", { name: name, sci: scientific });
      fillDexTaxonomy(info);
      // Show the card if there is anything to put in it. Bundled traits alone are worth
      // showing, so this can't depend on Wikipedia having an article.
      const t = (info && info.traits) || {};
      const hasTraits = !!(t.food || t.foraging || t.habitat);
      if (!info || (!info.extract && !info.family && !hasTraits)) return;
      const set = (sel, text) => {
        const node = el.querySelector(sel);
        if (node && text) node.textContent = text;
      };
      set(".about-descriptor", info.descriptor);
      set(".about-extract", info.extract);

      const tax = el.querySelector(".about-tax");
      [["Order", info.order], ["Family", info.family], ["Status", info.conservation],
       ["Eats", t.food], ["Forages", t.foraging], ["Habitat", t.habitat]]
        .forEach(([k, v]) => {
          if (!v) return;
          const chip = document.createElement("span");
          chip.className = "tax-chip";
          chip.textContent = k + ": " + v;
          if (k === "Eats" && t.niche && t.niche !== v) chip.title = t.niche;
          tax.appendChild(chip);
        });

      const link = el.querySelector(".about-link");
      if (info.wiki_url) { link.href = info.wiki_url; link.hidden = false; }
      el.hidden = false;
    } catch (e) { /* leave the About card hidden */ }
  }

  // Variant order and labels. "CRY" is kept for the untyped iNaturalist fallback so a
  // species without xeno-canto coverage looks exactly as it always did.
  const REF_ORDER = ["song", "call", "any"];
  const REF_LABELS = { song: "SONG", call: "CALL", any: "CRY" };
  const REF_PROVIDERS = { "xeno-canto": "xeno-canto", inaturalist: "iNaturalist" };

  /** Credit line for one variant, or "" when there's nothing to attribute. */
  function refCredit(v) {
    if (!v) return "";
    // The provider's attribution may already name the licence ("... some rights
    // reserved (CC BY-NC)"), so the bare code is only appended when it would
    // otherwise go unstated.
    const license = (v.license_code || "").toUpperCase();
    const attribution = v.attribution || "";
    const stated = license && attribution.toUpperCase().replace(/[\s-]/g, "")
      .includes(license.replace(/[\s-]/g, ""));
    return [attribution, !stated ? license : ""].filter(Boolean).join(" · ");
  }

  async function loadReferencePhotos(name, scientific) {
    const el = document.getElementById("ref-photos");
    if (!el) return;
    try {
      const info = await getJson("/reference-photos", { name: name, sci: scientific });
      const photos = (info && info.photos) || [];
      const strip = el.querySelector(".ref-photo-strip");
      let shown = 0;
      photos.forEach((p) => {
        // Attribution is a licence condition for these CC photos, so one we can't
        // credit is simply not shown.
        if (!p.media_url || !p.attribution) return;
        const fig = document.createElement("figure");
        fig.className = "ref-photo";
        const img = document.createElement("img");
        img.loading = "lazy";
        img.alt = name;
        img.src = p.media_url;
        // A photo that fails to load takes its whole credit block with it, rather than
        // leaving an orphaned attribution for a missing image.
        img.addEventListener("error", () => fig.remove());
        const cap = document.createElement("figcaption");
        cap.className = "ref-credit";
        const who = document.createElement("span");
        who.className = "ref-attribution";
        who.textContent = p.attribution;
        cap.appendChild(who);
        if (p.source_url) {
          const link = document.createElement("a");
          link.className = "ref-link";
          link.href = p.source_url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = "iNaturalist →";
          cap.appendChild(link);
        }
        fig.appendChild(img);
        fig.appendChild(cap);
        strip.appendChild(fig);
        shown += 1;
      });
      if (shown) el.hidden = false;
    } catch (e) { /* leave the reference photos hidden */ }
  }

  async function loadReferenceAudio(name, scientific) {
    const el = document.getElementById("ref-audio");
    if (!el) return;
    try {
      const info = await getJson("/reference-audio", { name: name, sci: scientific });
      const variants = (info && info.variants) || {};
      // Attribution is a licence condition for these CC recordings, so a variant is
      // only offered once we have something to credit for it.
      const kinds = REF_ORDER.filter((k) => variants[k] && variants[k].media_url &&
        refCredit(variants[k]));
      if (!kinds.length) return;

      const audio = el.querySelector("audio");
      const box = el.querySelector(".ref-buttons");
      const creditEl = el.querySelector(".ref-attribution");
      const link = el.querySelector(".ref-link");
      const buttons = [];
      let active = "";

      // Each recording has its own recordist, licence and page, so the credit has to
      // follow the clip rather than being set once.
      function activate(kind, play) {
        if (active !== kind) {
          audio.pause();
          audio.currentTime = 0;
          audio.src = variants[kind].media_url;
          active = kind;
          creditEl.textContent = refCredit(variants[kind]);
          const url = variants[kind].source_url;
          if (url) {
            link.href = url;
            link.textContent =
              (REF_PROVIDERS[variants[kind].provider] || "Source") + " →";
            link.hidden = false;
          } else {
            link.hidden = true;
          }
          buttons.forEach((b) => b.classList.toggle("active", b.dataset.kind === kind));
        }
        if (play) audio.play().catch(() => { /* autoplay policy / decode failure */ });
      }

      kinds.forEach((kind) => {
        const b = document.createElement("button");
        b.type = "button";
        // Both classes always: .dex-cry is styled only under the dex theme, .btn only
        // outside it, so one button works in both.
        b.className = "btn btn-sm ref-cry dex-cry";
        b.dataset.kind = kind;
        b.innerHTML = '<span class="dex-tri dex-tri-r" aria-hidden="true"></span>';
        b.appendChild(document.createTextNode(" " + (REF_LABELS[kind] || "CRY")));
        b.addEventListener("click", () => {
          if (active === kind && !audio.paused) {
            audio.pause();
            audio.currentTime = 0;
            return;
          }
          activate(kind, true);
        });
        box.appendChild(b);
        buttons.push(b);
      });

      // The playing indicator belongs to whichever button is currently selected.
      audio.addEventListener("play", () =>
        buttons.forEach((b) => b.classList.toggle("playing", b.dataset.kind === active)));
      ["pause", "ended"].forEach((ev) => audio.addEventListener(ev, () =>
        buttons.forEach((b) => b.classList.remove("playing"))));

      activate(kinds[0], false);
      el.hidden = false;
    } catch (e) { /* leave the reference card hidden */ }
  }

  window.aviaryInitSpecies = function (opts) {
    perDayChart({ species: opts.species, days: 30 });
    hourlyChart({ species: opts.species, days: 3650 });
    loadSpeciesInfo(opts.species, opts.scientific);
    loadReferencePhotos(opts.species, opts.scientific);
    loadReferenceAudio(opts.species, opts.scientific);
  };

  // ------------------------------------------------------------------ dex mode
  // Enhancements only: the registry rows and the prev/next steps are real links, so
  // both screens stay fully navigable with JS disabled.

  /** True when the element would rather handle arrow keys itself. */
  function ownsArrowKeys(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toLowerCase();
    // The clip player seeks with the arrows; without this, ←/→ would also step the dex
    // entry and navigate the page out from under an open player.
    if (el.closest && el.closest(".clip-player")) return true;
    // select/input: the filter controls. video/audio: arrows seek the clip.
    return el.isContentEditable ||
      ["input", "select", "textarea", "video", "audio", "button"].indexOf(tag) !== -1;
  }

  window.aviaryInitDexRegistry = function () {
    const list = document.getElementById("dexRegistry");
    if (!list) return;
    const rows = Array.prototype.slice.call(list.querySelectorAll(".dex-reg-row"));
    if (!rows.length) return;

    // Roving tabindex: the whole registry is a single Tab stop, then arrows move the
    // cursor within it. Enter needs no handler — the rows are anchors.
    let idx = 0;
    rows.forEach((row, i) => {
      row.tabIndex = i === 0 ? 0 : -1;
      row.addEventListener("focus", () => {
        rows[idx].tabIndex = -1;
        idx = i;
        row.tabIndex = 0;
      });
    });

    function move(next) {
      if (next < 0 || next >= rows.length) return;
      rows[next].focus();  // the focus handler keeps `idx` and tabindex in sync
    }

    // Scoped to the list, so arrow keys still scroll the page until a row is focused.
    list.addEventListener("keydown", (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
      if (step) { e.preventDefault(); move(idx + step); return; }
      if (e.key === "Home") { e.preventDefault(); move(0); }
      else if (e.key === "End") { e.preventDefault(); move(rows.length - 1); }
    });
  };

  window.aviaryInitDexEntry = function () {
    // The CRY/SONG/CALL buttons are created and wired by loadReferenceAudio() once it
    // knows which variants exist, so there is nothing to bind here.
    const steps = document.getElementById("dexSteps");
    if (!steps) return;
    document.addEventListener("keydown", (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || ownsArrowKeys(e.target)) return;
      const rel = { ArrowLeft: "prev", ArrowRight: "next" }[e.key];
      if (!rel) return;
      const link = steps.querySelector('a[rel="' + rel + '"]');
      if (link) { e.preventDefault(); window.location = link.href; }
    });
  };

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

  // -------------------------------------------------------------------- settings

  // Reports the identification service's state on the settings page. The facts chosen
  // are the ones that actually go wrong: running on CPU when a GPU was expected (a bad
  // torch wheel or a missing container toolkit), and a species count that reveals the
  // eBird region silently fell back to the bundled list.
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

  // ------------------------------------------------------------- live refresh

  window.aviaryInitRecent = function (opts) {
    if (opts.paged) return; // never auto-refresh while browsing older pages
    const groupsEl = document.getElementById("groups");
    const note = document.getElementById("live-note");
    if (!groupsEl) return;
    let newest = Number(opts.newest) || 0;
    let refreshing = false;

    // Also true while the clip player is open: the refresh replaces #groups wholesale,
    // and doing that under someone who is paused mid-scrub is worse than a late refresh.
    function mediaPlaying() {
      if (playerOpen()) return true;
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

  // ------------------------------------------------------------------ clip player
  // Frigate's clips are fragmented MP4s with a zero-duration header, so a browser can't
  // seek them — the progress bar just grows as it plays. /play.mp4 serves an ffmpeg
  // faststart remux instead, and this player pulls that down ONCE into a blob: every seek
  // after that is against bytes already in memory, so scrubbing is instant and the server
  // is never touched again. That is what makes remux-per-request affordable.
  //
  // It also gives the cramped 260px card scrubber somewhere roomier to live, and a place
  // for a still-capture button.

  let player = null;          // the open overlay, or null
  let playerObjectUrl = null; // revoked on close; each open allocates a fresh blob

  function playerOpen() {
    return player !== null;
  }

  function closePlayer() {
    if (!player) return;
    const video = player.querySelector("video");
    if (video) { video.pause(); video.removeAttribute("src"); video.load(); }
    if (playerObjectUrl) { URL.revokeObjectURL(playerObjectUrl); playerObjectUrl = null; }
    player.remove();
    player = null;
    document.body.classList.remove("player-open");
  }

  /** "blue-jay-20260811-142233-4.20s.png" — mirrors the download route's naming. */
  function stillFilename(name, startTime, at) {
    const slug = (name || "bird").toLowerCase().normalize("NFKD")
      .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "bird";
    const d = new Date((Number(startTime) || Date.now() / 1000) * 1000);
    const p = (n) => String(n).padStart(2, "0");
    const stamp = d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" +
      p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
    return slug + "-" + stamp + "-" + at.toFixed(2) + "s.png";
  }

  function saveStill(video, name, startTime, button) {
    // videoWidth/Height is the clip's encoded resolution, not the on-screen size, so the
    // still is full quality regardless of how small the player is drawn.
    if (!video.videoWidth) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    // PNG: lossless for the decoded frame. The blob is same-origin, so the canvas is
    // never tainted and toBlob can't throw a security error.
    canvas.toBlob((blob) => {
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = stillFilename(name, startTime, video.currentTime);
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      if (button) {
        const was = button.textContent;
        button.textContent = "✓ saved";
        setTimeout(() => { button.textContent = was; }, 1500);
      }
    }, "image/png");
  }

  /** Advance exactly one frame where the browser can tell us, else a 30fps guess. */
  function stepFrame(video, dir) {
    video.pause();
    if (dir > 0 && typeof video.requestVideoFrameCallback === "function") {
      video.requestVideoFrameCallback(() => {});
    }
    video.currentTime = Math.max(0, Math.min(
      (video.duration || Infinity), video.currentTime + dir * (1 / 30)));
  }

  function openPlayer(ds) {
    closePlayer();
    const clipBase = BASE + "/media/frigate/" + encodeURIComponent(ds.event);

    player = document.createElement("div");
    player.className = "clip-player";
    player.innerHTML =
      '<div class="clip-backdrop"></div>' +
      '<div class="clip-panel" role="dialog" aria-modal="true" aria-label="Clip player">' +
        '<div class="clip-head"><span class="clip-title"></span>' +
          '<button type="button" class="clip-close" title="Close (Esc)">✕</button></div>' +
        '<div class="clip-stage"><video controls playsinline preload="auto"></video></div>' +
        '<div class="clip-controls">' +
          '<button type="button" data-seek="-1">⏪ 1s</button>' +
          '<button type="button" data-step="-1">◀ step</button>' +
          '<button type="button" data-step="1">step ▶</button>' +
          '<button type="button" data-seek="1">1s ⏩</button>' +
          '<button type="button" class="clip-still">⬇ save still</button>' +
        "</div>" +
      "</div>";
    document.body.appendChild(player);
    document.body.classList.add("player-open");

    const title = player.querySelector(".clip-title");
    title.textContent = ds.name || "Clip";
    const video = player.querySelector("video");

    // Fetch the remuxed clip whole, then play from memory. The stage stays black while
    // that happens and the browser's own buffering UI takes over once src is set — no
    // custom loading overlay, which is one less thing to sit on top of the video.
    fetch(clipBase + "/play.mp4")
      .then((res) => { if (!res.ok) throw new Error(res.status); return res.blob(); })
      .then((blob) => {
        playerObjectUrl = URL.createObjectURL(blob);
        video.src = playerObjectUrl;
      })
      .catch(() => {
        // Remux unavailable (no ffmpeg, Frigate unreachable): play the original so there
        // is still something to watch, and say in the title why it won't scrub.
        video.src = clipBase + "/clip.mp4";
        title.textContent = (ds.name || "Clip") + " · seeking unavailable";
      });

    player.addEventListener("click", (e) => {
      if (e.target.closest(".clip-backdrop") || e.target.closest(".clip-close")) {
        closePlayer();
        return;
      }
      const seek = e.target.closest("[data-seek]");
      if (seek) { video.currentTime += Number(seek.dataset.seek); return; }
      const step = e.target.closest("[data-step]");
      if (step) { stepFrame(video, Number(step.dataset.step)); return; }
      if (e.target.closest(".clip-still")) {
        saveStill(video, ds.name, ds.time, e.target.closest(".clip-still"));
      }
    });
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".clip-open");
    if (!btn) return;
    e.preventDefault();
    openPlayer({ event: btn.dataset.event, name: btn.dataset.name, time: btn.dataset.time });
  });

  document.addEventListener("keydown", (e) => {
    if (!playerOpen()) return;
    const video = player.querySelector("video");
    if (e.key === "Escape") { e.preventDefault(); closePlayer(); return; }
    // Arrows seek here rather than reaching the dex entry's prev/next navigation.
    if (e.key === "ArrowLeft") { e.preventDefault(); video.currentTime -= 1; }
    else if (e.key === "ArrowRight") { e.preventDefault(); video.currentTime += 1; }
    else if (e.key === "," ) { e.preventDefault(); stepFrame(video, -1); }
    else if (e.key === "." ) { e.preventDefault(); stepFrame(video, 1); }
  }, true);

  // --------------------------------------------------------- remove detections
  // Delete buttons open a small menu: remove from Aviary only, or also clear the
  // species label / delete the event at the source (Frigate / BirdNET-Go).
  // Delegated so cards injected by the live refresh keep working.

  // Source deletion leads: a misclassification you're removing is one you want gone from
  // Frigate/BirdNET-Go too, not just hidden from Aviary. Removing locally only is still
  // offered, second, and says explicitly that the source keeps its copy.
  function deleteMenuItems(ds) {
    if (ds.species) {
      return [
        ["Remove everywhere (Aviary + source)", "delete"],
        ["Remove from Aviary only (keeps source events)", "none"],
        ["Remove + clear Frigate labels (keeps video; deletes BirdNET-Go entries)", "clear"],
        ["Blacklist — remove everywhere, never record again", "blacklist"],
      ];
    }
    if (ds.source === "frigate") {
      return [
        ["Remove everywhere (deletes the Frigate event)", "delete"],
        ["Remove from Aviary only (keeps the Frigate event)", "none"],
        ["Remove + clear species label in Frigate (keeps video)", "clear"],
      ];
    }
    return [
      ["Remove everywhere (deletes from BirdNET-Go)", "delete"],
      ["Remove from Aviary only (keeps the BirdNET-Go entry)", "none"],
    ];
  }

  function closeDeleteMenu() {
    document.querySelectorAll(".del-menu").forEach((m) => m.remove());
  }

  function showDeleteMenu(btn) {
    closeDeleteMenu();
    const host = btn.closest(".det-card") || btn.closest(".species-hero") || btn.parentElement;
    const menu = document.createElement("div");
    menu.className = "del-menu";
    deleteMenuItems(btn.dataset).forEach(([label, action]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      if (action !== "none") b.classList.add("danger");
      b.addEventListener("click", () => { closeDeleteMenu(); doDelete(btn.dataset, action); });
      menu.appendChild(b);
    });
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", closeDeleteMenu);
    menu.appendChild(cancel);
    host.appendChild(menu);
  }

  async function doDelete(ds, action) {
    const isSpecies = !!ds.species;
    // Blacklisting purges the same way "remove species" does, but also refuses the
    // species at ingest from then on — irreversible for the history, so confirm.
    const blacklisting = action === "blacklist";
    if (blacklisting && !window.confirm(
      "Blacklist " + ds.species + "?\n\n" +
      "Every detection of it is deleted here AND at the source (Frigate events / " +
      "BirdNET-Go entries), and new ones are ignored from now on (no stats, no " +
      "notifications). You can allow it again from Settings, but the deleted " +
      "detections do not come back."
    )) return;

    // Blacklisting means "never record this again", so leaving the source's copies in
    // place would defeat the point — it deletes at the source like the menu's first entry.
    const path = blacklisting
      ? "/blacklist?species=" + encodeURIComponent(ds.species) + "&source_action=delete"
      : (isSpecies
        ? "/species/" + encodeURIComponent(ds.species) + "?source_action=" + action
        : "/detections/" + ds.id + "?source_action=" + action);
    try {
      const res = await fetch(API + path, { method: blacklisting ? "POST" : "DELETE" });
      const data = await res.json();
      if (!data.ok) {
        alert("Remove failed: " + (data.error || res.status));
        return;
      }
      const sr = data.source_result;
      if (sr && sr.ok === false) {
        alert("Removed from Aviary, but the source action failed: " + sr.error);
      } else if (data.source_error_count) {
        alert("Removed from Aviary; " + data.source_error_count + " source action(s) failed:\n" +
              data.source_errors.join("\n"));
      }
      if (isSpecies) {
        window.location = (BASE || "") + "/species";
      } else {
        const b = document.querySelector('.det-delete[data-id="' + ds.id + '"]');
        const card = b && b.closest(".det-card");
        if (card) card.remove();
      }
    } catch (e) {
      alert("Remove failed: " + e);
    }
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".det-delete, .species-delete");
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      showDeleteMenu(btn);
      return;
    }
    if (!e.target.closest(".del-menu")) closeDeleteMenu();
  });

  // Confirming a species into the registry. Rejecting deliberately has no handler here —
  // it reuses .species-delete above, so a misclassification is disposed of exactly one way.
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".species-confirm");
    if (!btn) return;
    e.preventDefault();
    const species = btn.dataset.species;
    btn.disabled = true;
    try {
      const res = await fetch(API + "/species-confirm?species=" + encodeURIComponent(species),
        { method: "POST" });
      const data = await res.json();
      if (!data.ok) {
        alert("Confirm failed: " + (data.error || res.status));
        btn.disabled = false;
        return;
      }
      window.location.reload();  // dex number, stats and banner all change at once
    } catch (err) {
      alert("Confirm failed: " + err);
      btn.disabled = false;
    }
  });

  // Naming a detection by hand — either by picking one of the model's own candidates or
  // by typing it. The species list is fetched once and cached, so the free-text prompt can
  // offer autocomplete over the identifier's actual vocabulary rather than accepting any
  // string that happens to be typed.
  let speciesListPromise = null;

  function knownSpecies() {
    if (!speciesListPromise) {
      speciesListPromise = getJson("/identify-species")
        .then((d) => d.species || [])
        .catch(() => []);
    }
    return speciesListPromise;
  }

  async function setSpecies(id, species, sci) {
    let url = API + "/detections/" + encodeURIComponent(id) + "/species" +
      "?species=" + encodeURIComponent(species);
    if (sci) url += "&scientific=" + encodeURIComponent(sci);
    const res = await fetch(url, { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      alert("Couldn't set the species: " + (data.error || res.status));
      return false;
    }
    return true;
  }

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".guess");
    if (!btn) return;
    e.preventDefault();

    let species = btn.dataset.species;
    let sci = btn.dataset.sci || "";
    if (btn.classList.contains("guess-other")) {
      const list = await knownSpecies();
      const hint = list.length
        ? "\n\n(" + list.length + " species in the identifier's regional list)"
        : "";
      species = window.prompt("What is this bird?" + hint, "");
      if (!species) return;
      species = species.trim();
      if (!species) return;
      // A name the identifier doesn't know is allowed — its list is regional and you may
      // genuinely have a vagrant — but it's worth one confirmation, since a typo here
      // creates a new species in the registry.
      if (list.length && !list.some((s) => s.toLowerCase() === species.toLowerCase())) {
        if (!window.confirm(
          '"' + species + '" is not in the identifier\'s species list.\n\n' +
          "Add it anyway? Check the spelling first — this creates a new species.")) {
          return;
        }
      }
      sci = "";
    }
    btn.disabled = true;
    if (await setSpecies(btn.dataset.id, species, sci)) window.location.reload();
    else btn.disabled = false;
  });

  // Re-run identification for one detection. Synchronous by design: the request holds
  // until the GPU answers (a few seconds), because the whole point is comparing the new
  // answer against the old one — a fire-and-forget that quietly changed the card later
  // would be useless for tuning.
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".reidentify");
    if (!btn) return;
    e.preventDefault();
    const rejecting = btn.classList.contains("reject");
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = rejecting ? "✗ trying again…" : "↻ identifying…";
    try {
      const res = await fetch(
        API + "/detections/" + encodeURIComponent(btn.dataset.id) + "/identify" +
          (rejecting ? "?reject=1" : ""),
        { method: "POST" });
      const data = await res.json();
      if (!data.ok) {
        alert("Re-identify failed: " + (data.error || res.status));
        btn.disabled = false;
        btn.textContent = original;
        return;
      }
      // Say what happened before the reload wipes the page. Rejections accumulate, so
      // after a few presses it is genuinely unclear what is still in the running.
      if (rejecting && !data.common_name) {
        alert("Ruled out " + btn.dataset.name +
              ". Nothing else cleared the confidence threshold — this detection is now in " +
              "the review queue.\n\nRuled out so far: " + (data.rejected || []).join(", "));
      }
      // Reload rather than patching the card: the species name, confidence bar, badge and
      // the review-queue count all move together, and the row may have left the queue.
      window.location.reload();
    } catch (err) {
      alert("Re-identify failed: " + err);
      btn.disabled = false;
      btn.textContent = original;
    }
  });
})();
