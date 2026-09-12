// Aviary — Recent page live refresh.
(function () {
  const { BASE, withParams, getJson, playerOpen } = window.Aviary;

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
              zone: opts.zone,
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
