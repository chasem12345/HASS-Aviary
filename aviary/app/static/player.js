// Aviary — the full-width clip player (HLS for recordings windows, blob for clips).
(function () {
  const { BASE } = window.Aviary;

  // ------------------------------------------------------------------ clip player
  // Two kinds of source, one player.
  //
  // Recordings windows (▶ play visit, ⤢ with padding, ⇄ other camera) play as HLS from
  // Frigate's own recordings service, proxied same-origin by /media/frigate/vod/…: the
  // first frame is one 10 s segment away however long the window, the playlist carries
  // the duration so seeking works from the start, and segments load on demand. hls.js is
  // fetched lazily on the first such play (Safari on iPhone plays HLS natively).
  //
  // Event clips, exports and keepsakes are short MP4s. Frigate's event clip is a
  // fragmented MP4 with a zero-duration header that a browser can't seek, so /play.mp4
  // serves an ffmpeg faststart remux and the player pulls it ONCE into a blob: every seek
  // after that is against bytes already in memory.
  //
  // Either way the player gives the cramped 260px card scrubber somewhere roomier to
  // live, and a place for a still-capture button.

  let player = null;          // the open overlay, or null
  let playerObjectUrl = null; // revoked on close; each open allocates a fresh blob
  let playerHls = null;       // the hls.js instance for an HLS source; destroyed on close
  let hlsLoading = null;      // memoised script-load promise for hls.js

  /** Load the vendored hls.js once, on demand. Resolves when window.Hls exists. */
  function loadHls() {
    if (window.Hls) return Promise.resolve();
    if (!hlsLoading) {
      hlsLoading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = window.AVIARY_HLS_SRC || (BASE + "/static/hls.light.min.js");
        script.onload = () => (window.Hls ? resolve() : reject(new Error("hls.js did not define Hls")));
        script.onerror = () => { hlsLoading = null; reject(new Error("hls.js failed to load")); };
        document.head.appendChild(script);
      });
    }
    return hlsLoading;
  }

  /** The master playlist of a camera's recordings window, via the Aviary proxy. */
  function vodPlaylistUrl(camera, start, end) {
    return BASE + "/media/frigate/vod/" + encodeURIComponent(camera) +
      "/start/" + encodeURIComponent(start) + "/end/" + encodeURIComponent(end) + "/master.m3u8";
  }

  function playerOpen() {
    return player !== null;
  }

  function closePlayer() {
    if (!player) return;
    const video = player.querySelector("video");
    if (playerHls) { playerHls.destroy(); playerHls = null; }
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
    // PNG: lossless for the decoded frame. The source is same-origin either way — a blob,
    // or HLS segments proxied by Aviary — so the canvas is never tainted and toBlob can't
    // throw a security error.
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

  /** Attach an HLS playlist to the player's <video>; fall back to the fMP4 passthrough. */
  function startHls(ds, video, title) {
    const name = ds.name || "Clip";
    title.textContent = name + " · loading…";
    let started = false;
    video.addEventListener("loadedmetadata", () => { started = true; title.textContent = name; }, { once: true });
    const fallback = () => {
      if (playerHls) { playerHls.destroy(); playerHls = null; }
      // Once frames have played there is nothing sensible to fall back to mid-stream.
      if (started) { title.textContent = name + " · stream lost"; return; }
      video.src = ds.fallback;
      title.textContent = name + " · seeking unavailable";
    };
    loadHls().then(() => {
      if (window.Hls && Hls.isSupported()) {
        playerHls = new Hls({ maxBufferLength: 30, maxBufferSize: 20 * 1000 * 1000 });
        let recovered = false;
        playerHls.on(Hls.Events.ERROR, (_, data) => {
          if (!data.fatal) return;
          if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !recovered) {
            recovered = true;
            playerHls.recoverMediaError();
            return;
          }
          fallback();
        });
        playerHls.loadSource(ds.hls);
        playerHls.attachMedia(video);
      } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        // No MSE (iPhone Safari): the browser plays the playlist itself.
        video.addEventListener("error", fallback, { once: true });
        video.src = ds.hls;
      } else {
        fallback();
      }
    }).catch(fallback);
  }

  function openPlayer(ds) {
    closePlayer();
    // Sources default to the event's own media. ds.hls selects HLS playback of a
    // recordings window (with ds.fallback as the passthrough); ds.src/ds.fallback override
    // the MP4 URLs for other footage (exports, keepsakes). Everything downstream —
    // stepping, still capture — is source-agnostic.
    const clipBase = BASE + "/media/frigate/" + encodeURIComponent(ds.event || "");
    const src = ds.src || clipBase + "/play.mp4";
    const fallback = ds.fallback || clipBase + "/clip.mp4";

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

    if (ds.hls) {
      startHls({ ...ds, fallback }, video, title);
    } else {
      // Fetch the remuxed clip whole, then play from memory. The stage stays black while
      // that happens and the browser's own buffering UI takes over once src is set — no
      // custom loading overlay, which is one less thing to sit on top of the video.
      fetch(src)
        .then((res) => { if (!res.ok) throw new Error(res.status); return res.blob(); })
        .then((blob) => {
          playerObjectUrl = URL.createObjectURL(blob);
          video.src = playerObjectUrl;
        })
        .catch(() => {
          // Remux unavailable (no ffmpeg, Frigate unreachable): play the original so there
          // is still something to watch, and say in the title why it won't scrub.
          video.src = fallback;
          title.textContent = (ds.name || "Clip") + " · seeking unavailable";
        });
    }

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
    const ds = { event: btn.dataset.event, name: btn.dataset.name, time: btn.dataset.time };
    // When the card carries a padded window, stream the camera's own recordings for it
    // (±clip_pad_seconds around the event) as HLS. The bare event clip stays the fallback —
    // recordings older than the camera's retention are gone while the event clip
    // survives under alert/detection retention.
    if (btn.dataset.camera && btn.dataset.start && btn.dataset.end) {
      ds.hls = vodPlaylistUrl(btn.dataset.camera, btn.dataset.start, btn.dataset.end);
      ds.fallback = BASE + "/media/frigate/" + encodeURIComponent(btn.dataset.event) + "/clip.mp4";
    }
    openPlayer(ds);
  });

  // The kept zoomed footage: a Frigate export made when the event was pinned. Already
  // a seekable MP4, so src and fallback are the same URL; while the export is still
  // processing the route answers with an error and the player reports it in the title.
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".export-open");
    if (!btn) return;
    e.preventDefault();
    const url = BASE + "/media/frigate/export/" + encodeURIComponent(btn.dataset.det) + "/video.mp4";
    openPlayer({
      name: (btn.dataset.name || "Clip") + " · zoomed (kept)",
      time: btn.dataset.time,
      src: url,
      fallback: url,
    });
  });

  // A species keepsake's exported clip (Kept page). Same shape as the kept export above.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".keepsake-open");
    if (!btn) return;
    e.preventDefault();
    const url = BASE + "/media/keepsake/video.mp4?species=" + encodeURIComponent(btn.dataset.species) +
      "&role=" + encodeURIComponent(btn.dataset.role) + "&zoom=" + (btn.dataset.zoom === "1" ? "1" : "0");
    openPlayer({
      name: btn.dataset.species + " · " + btn.dataset.role + " sighting" +
        (btn.dataset.zoom === "1" ? " · zoomed" : ""),
      time: btn.dataset.time,
      src: url,
      fallback: url,
    });
  });

  // "View on the other camera": the same time window, from the paired camera's
  // continuous recordings. Same player, HLS like ▶ play visit; the fMP4 passthrough of
  // the same window is the fallback.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".other-cam-open");
    if (!btn) return;
    e.preventDefault();
    openPlayer({
      name: (btn.dataset.name || "Clip") + " · " + btn.dataset.camera,
      time: btn.dataset.start,
      hls: vodPlaylistUrl(btn.dataset.camera, btn.dataset.start, btn.dataset.end),
      fallback: BASE + "/media/frigate/recordings/" + encodeURIComponent(btn.dataset.camera) +
        "/clip.mp4?start=" + encodeURIComponent(btn.dataset.start) +
        "&end=" + encodeURIComponent(btn.dataset.end),
    });
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

  Object.assign(window.Aviary, { playerOpen });
})();
