// Aviary — species page: about card, reference photos and audio, seasonality.
(function () {
  const { getJson, cssVar, axisColor, tint, perDayChart, hourlyChart } = window.Aviary;

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
      // The article's own sections, collapsed: the lead paragraph is often two sentences.
      const secs = el.querySelector(".about-sections");
      if (secs && Array.isArray(info.sections)) {
        info.sections.forEach((sec) => {
          if (!sec || !sec.title || !sec.text) return;
          const d = document.createElement("details");
          d.className = "about-section";
          const sum = document.createElement("summary");
          sum.textContent = sec.title;
          const p = document.createElement("p");
          p.textContent = sec.text;
          d.appendChild(sum); d.appendChild(p);
          secs.appendChild(d);
        });
      }

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

  // ------------------------------------------------------------- seasonality
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // Dashed line at the current month, so "is it here now?" is one glance.
  function nowMarker(month) {
    return [{
      id: "aviaryNow",
      afterDraw(chart) {
        const { ctx, chartArea: a, scales: { x } } = chart;
        if (!a || !x || !month) return;
        const X = x.getPixelForValue(month - 1);
        ctx.save();
        ctx.strokeStyle = cssVar("--warn", "#d9a441"); ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(X, a.top); ctx.lineTo(X, a.bottom); ctx.stroke();
        ctx.fillStyle = cssVar("--warn", "#d9a441"); ctx.font = "11px " + cssVar("--font-body", "sans-serif");
        ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillText("now", X, a.top + 2);
        ctx.restore();
      },
    }];
  }

  async function loadSeasonality(name, scientific) {
    const el = document.getElementById("season");
    if (!el) return;
    try {
      const d = await getJson("/seasonality", { name: name, sci: scientific });
      const region = d.region && Array.isArray(d.region.months) ? d.region : null;
      const yard = Array.isArray(d.yard) ? d.yard : [];
      const yardTotal = yard.reduce((a, b) => a + b, 0);
      if (!region && !yardTotal && !d.migration) return;

      const bits = [];
      if (region && region.label) bits.push(region.label);
      if (d.migration) bits.push(d.migration);
      if (d.mass_g) bits.push(Math.round(d.mass_g) + " g");
      el.querySelector(".season-label").textContent = bits.join(" · ");

      const credit = el.querySelector(".season-credit");
      if (region) {
        credit.textContent = "Regional presence: " + region.total.toLocaleString() +
          " iNaturalist research-grade observations within " + region.radius_km +
          " km of your Home Assistant location, by month observed" +
          (d.migration ? " · migration class & mass: AVONET (CC BY 4.0)" : "");
      } else {
        credit.textContent = "Set a location in Home Assistant to see the region's month-by-month presence" +
          (d.migration ? " · migration class & mass: AVONET (CC BY 4.0)" : "");
      }

      const datasets = [];
      const scales = {
        x: { ticks: { color: axisColor() }, grid: { display: false } },
      };
      if (region) {
        const peak = Math.max.apply(null, region.months) || 1;
        datasets.push({
          label: "in the region", yAxisID: "yRegion",
          data: region.months.map((v) => Math.round(1000 * v / peak) / 10),
          backgroundColor: tint(cssVar("--accent", "#2f7d5b"), 0.35), borderRadius: 3,
        });
        scales.yRegion = {
          position: "left", beginAtZero: true, max: 100,
          ticks: { color: axisColor(), callback: (v) => v + "%" },
          title: { display: true, text: "share of peak month", color: axisColor(), font: { size: 11 } },
        };
      }
      if (yardTotal) {
        datasets.push({
          label: "at your feeder", yAxisID: "yYard", data: yard,
          backgroundColor: cssVar("--frigate", "#3b6ea5"), borderRadius: 3,
          barPercentage: region ? 0.45 : 0.8,
        });
        scales.yYard = {
          position: region ? "right" : "left", beginAtZero: true,
          ticks: { color: axisColor(), precision: 0 }, grid: { drawOnChartArea: !region },
          title: { display: true, text: "your sightings", color: axisColor(), font: { size: 11 } },
        };
      }
      if (datasets.length) {
        new Chart(document.getElementById("seasonChart"), {
          type: "bar",
          data: { labels: MONTHS, datasets },
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false }, tooltip: { callbacks: {
              label: (c) => c.dataset.yAxisID === "yRegion"
                ? c.parsed.y + "% of peak (" + region.months[c.dataIndex].toLocaleString() + " obs.)"
                : c.parsed.y + " at your feeder",
            } } },
            scales,
          },
          plugins: nowMarker(d.month_now),
        });
      } else {
        el.querySelector(".chart-box").hidden = true;
      }
      el.hidden = false;
    } catch (e) { /* leave the Seasonality card hidden */ }
  }

  window.aviaryInitSpecies = function (opts) {
    perDayChart({ species: opts.species, days: 30 });
    hourlyChart({ species: opts.species, days: 3650 }, opts.sun);
    loadSpeciesInfo(opts.species, opts.scientific);
    loadSeasonality(opts.species, opts.scientific);
    loadReferencePhotos(opts.species, opts.scientific);
    loadReferenceAudio(opts.species, opts.scientific);
  };
})();
