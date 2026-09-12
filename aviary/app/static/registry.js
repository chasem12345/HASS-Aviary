// Aviary — species index (filter/sort) and the dex registry/entry keyboard handling.
(function () {
  // ---------------------------------------------------------- species index
  // Filter and sort the tiles already on the page. Progressive: with JS off the page is
  // the server's count-ordered grid, and the dex registry (its own template, no
  // #speciesFilter) is untouched because we bail before touching anything.
  window.aviaryInitSpeciesIndex = function () {
    const input = document.getElementById("speciesFilter");
    const select = document.getElementById("speciesSort");
    const grid = document.getElementById("speciesGrid");
    if (!input || !select || !grid) return;
    const tiles = Array.prototype.slice.call(grid.querySelectorAll(".species-tile"));
    const noMatch = document.getElementById("speciesNoMatch");
    const count = document.getElementById("speciesCount");
    const KEY = "aviary.speciesSort";
    try { const saved = sessionStorage.getItem(KEY); if (saved) select.value = saved; } catch (e) { /* private mode */ }

    const num = (t, k) => parseFloat(t.dataset[k]) || 0;
    const sorters = {
      count: (a, b) => (num(b, "count") - num(a, "count")) || (num(b, "last") - num(a, "last")),
      recent: (a, b) => num(b, "last") - num(a, "last"),
      az: (a, b) => a.dataset.name.localeCompare(b.dataset.name),
      first: (a, b) => num(a, "first") - num(b, "first"),
    };

    function apply() {
      const needle = input.value.trim().toLowerCase();
      let shown = 0;
      tiles.forEach((t) => {
        const hit = !needle || t.dataset.name.indexOf(needle) !== -1 ||
          (t.dataset.sci || "").indexOf(needle) !== -1;
        t.hidden = !hit;
        if (hit) shown++;
      });
      // appendChild on an existing child moves it: one pass reorders the grid in place.
      tiles.slice().sort(sorters[select.value] || sorters.count).forEach((t) => grid.appendChild(t));
      if (noMatch) noMatch.hidden = shown > 0 || !tiles.length;
      if (count) count.textContent = String(shown);
    }

    let timer = null;
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(apply, 80); });
    // The input sits inside the filter form; Enter must not reload the page.
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
    select.addEventListener("change", () => {
      try { sessionStorage.setItem(KEY, select.value); } catch (e) { /* ignore */ }
      apply();
    });
    // The server already ordered by count; only reorder on load for a remembered sort.
    if (select.value !== "count") apply();
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
})();
