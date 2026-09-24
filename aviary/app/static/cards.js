// Aviary — detection-card actions: delete menu, confirm, guess chips, subject reject,
// re-identify, keep toggle, iNaturalist post/forget, bulk select.
(function () {
  const { API, BASE, getJson } = window.Aviary;

  // --------------------------------------------------------- remove detections
  // Delete buttons open a small menu: remove from Aviary only, or also clear the
  // species label / delete the event at the source (Frigate / BirdNET-Go).
  // Delegated so cards injected by the live refresh keep working.

  // Source deletion leads: a misclassification you're removing is one you want gone from
  // Frigate/BirdNET-Go too, not just hidden from Aviary. Removing locally only is still
  // offered, second, and says explicitly that the source keeps its copy.
  function deleteMenuItems(ds) {
    // "Not a bird": the same removal choices, each also teaching the identifier.
    if (ds.notbird) {
      return [
        ["Not a bird — learn it, remove everywhere (deletes the Frigate event)", "notbird:delete"],
        ["Not a bird — learn it, remove from Aviary only", "notbird:none"],
      ];
    }
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

  // On the Awaiting Review page a decided species leaves the queue in place — tile gone,
  // counts down — instead of navigating away. False anywhere else.
  function dropReviewTile(el) {
    const tile = el && el.closest(".review-tile");
    if (!tile) return false;
    tile.remove();
    const left = document.querySelectorAll(".review-tile").length;
    const count = document.getElementById("reviewCount");
    if (count) count.textContent = String(left);
    const badge = document.getElementById("reviewNavBadge");
    if (badge) {
      if (left) badge.textContent = String(left);
      else badge.remove();
    }
    const empty = document.getElementById("reviewEmpty");
    if (empty && !left) empty.hidden = false;
    return true;
  }

  function closeDeleteMenu() {
    document.querySelectorAll(".del-menu").forEach((m) => m.remove());
  }

  function showDeleteMenu(btn) {
    closeDeleteMenu();
    const host = btn.closest(".det-card") || btn.closest(".species-hero") ||
      btn.closest(".review-tile") || btn.parentElement;
    const menu = document.createElement("div");
    menu.className = "del-menu";
    deleteMenuItems(btn.dataset).forEach(([label, action]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      if (action !== "none") b.classList.add("danger");
      b.addEventListener("click", () => { closeDeleteMenu(); doDelete(btn.dataset, action, btn); });
      menu.appendChild(b);
    });
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", closeDeleteMenu);
    menu.appendChild(cancel);
    host.appendChild(menu);
  }

  async function doDelete(ds, action, btn) {
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
    const notBird = action.startsWith("notbird:");
    const path = blacklisting
      ? "/blacklist?species=" + encodeURIComponent(ds.species) + "&source_action=delete"
      : notBird
        ? "/detections/" + ds.id + "/not-bird?source_action=" + action.slice(8)
        : (isSpecies
          ? "/species/" + encodeURIComponent(ds.species) + "?source_action=" + action
          : "/detections/" + ds.id + "?source_action=" + action);
    try {
      const res = await fetch(API + path, { method: blacklisting || notBird ? "POST" : "DELETE" });
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
      if (notBird && !data.learned) {
        alert("Removed, but there was no stored embedding to learn from — re-identify a " +
              "detection first if you want it learned.");
      }
      if (isSpecies) {
        if (!dropReviewTile(btn)) window.location = (BASE || "") + "/species";
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
    const btn = e.target.closest(".det-delete, .species-delete, .notbird-open");
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
      // Species page: dex number, stats and banner all change at once.
      if (!dropReviewTile(btn)) window.location.reload();
    } catch (err) {
      alert("Confirm failed: " + err);
      btn.disabled = false;
    }
  });

  // Life list on iNaturalist. Post shows exactly what will be sent (from /species-inat)
  // and asks first — the observation goes out under the user's name. Forget only drops
  // the local link; the note in the response says so.
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".inat-post");
    if (!btn) return;
    e.preventDefault();
    const species = btn.dataset.species;
    btn.disabled = true;
    try {
      const p = await getJson("/species-inat", { species });
      if (p.error) { alert("Can't post to iNaturalist: " + p.error); btn.disabled = false; return; }
      const media = (p.media && p.media.length) ? p.media.join(", ") : "none — it will be a casual-grade record";
      if (!confirm("Post " + species + " to iNaturalist?\n\nObserved: " + p.observed +
                   "\nLocation: " + p.location + "\nMedia: " + media + "\nTaxon: " + p.taxon +
                   "\n\nThis creates an observation on your account.")) {
        btn.disabled = false;
        return;
      }
      const res = await fetch(API + "/species-inat?species=" + encodeURIComponent(species), { method: "POST" });
      const data = await res.json();
      if (!data.ok) { alert("Post failed: " + (data.error || res.status)); btn.disabled = false; return; }
      window.location.reload();
    } catch (err) {
      alert("Post failed: " + err);
      btn.disabled = false;
    }
  });

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".inat-forget");
    if (!btn) return;
    e.preventDefault();
    const species = btn.dataset.species;
    if (!confirm("Forget the iNaturalist link for " + species + "?\n\nThe observation on iNaturalist is not deleted — do that there if you want it gone.")) return;
    btn.disabled = true;
    try {
      const res = await fetch(API + "/species-inat/" + encodeURIComponent(species), { method: "DELETE" });
      const data = await res.json();
      if (!data.ok) { alert("Couldn't forget: " + (data.error || res.status)); btn.disabled = false; return; }
      window.location.reload();
    } catch (err) {
      alert("Couldn't forget: " + err);
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

  // `subject` (when present and non-zero) names one of the OTHER birds the identifier
  // found in the event — its own row, crop and embedding — rather than the detection.
  async function setSpecies(id, species, sci, subject) {
    let url = API + "/detections/" + encodeURIComponent(id) +
      (subject ? "/subjects/" + encodeURIComponent(subject) : "") + "/species" +
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

  // Forget one "Not a bird" example (Unidentified → Not a bird).
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".notbird-forget");
    if (!btn) return;
    e.preventDefault();
    btn.disabled = true;
    try {
      const res = await fetch(API + "/not-bird/" + encodeURIComponent(btn.dataset.example),
        { method: "DELETE" });
      const data = await res.json();
      if (!data.ok) { alert("Couldn't forget it: " + (data.error || res.status)); btn.disabled = false; return; }
      const row = btn.closest(".notbird-example");
      if (row) row.remove();
    } catch (err) { alert("Couldn't forget it: " + err); btn.disabled = false; }
  });

  // "That other bird is not a bird at all": learned as a negative, dropped from the event.
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".subject-notbird");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    btn.disabled = true;
    try {
      const res = await fetch(API + "/detections/" + encodeURIComponent(btn.dataset.id) +
        "/subjects/" + encodeURIComponent(btn.dataset.subject) + "/not-bird", { method: "POST" });
      const data = await res.json();
      if (!data.ok) { alert("Couldn't mark it: " + (data.error || res.status)); btn.disabled = false; return; }
      const chip = btn.closest(".subject-chip");
      if (chip) chip.remove();
    } catch (err) { alert("Couldn't mark it: " + err); btn.disabled = false; }
  });

  // "That other bird is not a X": recorded for that bird only, no GPU call.
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".subject-reject");
    if (!btn) return;
    e.preventDefault();
    btn.disabled = true;
    try {
      const res = await fetch(API + "/detections/" + encodeURIComponent(btn.dataset.id) +
        "/subjects/" + encodeURIComponent(btn.dataset.subject) + "/reject?species=" +
        encodeURIComponent(btn.dataset.name), { method: "POST" });
      const data = await res.json();
      if (!data.ok) { alert("Couldn't reject: " + (data.error || res.status)); btn.disabled = false; return; }
      window.location.reload();
    } catch (err) { alert("Couldn't reject: " + err); btn.disabled = false; }
  });

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".guess");
    if (!btn || btn.classList.contains("subject-reject") ||
        btn.classList.contains("subject-notbird")) return;
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
    if (await setSpecies(btn.dataset.id, species, sci, btn.dataset.subject)) window.location.reload();
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
      // A plain re-identify that stays uncertain while answers are banned is almost
      // always a stale rejection vetoing the right species — invisible otherwise, and
      // pressing re-identify harder can never fix it. Offer the way out.
      const uncertain = !data.common_name || data.common_name === "bird" ||
        data.id_status === "low_confidence";
      if (!rejecting && uncertain && (data.rejected || []).length) {
        const clear = confirm(
          "Still uncertain — but earlier rejections have ruled these out for this " +
          "detection:\n\n  " + data.rejected.join(", ") + "\n\n" +
          "Clear the rejections and identify again from scratch?");
        if (clear) {
          btn.textContent = "↻ identifying…";
          await fetch(
            API + "/detections/" + encodeURIComponent(btn.dataset.id) +
              "/identify?reset=1",
            { method: "POST" });
        }
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

  // Keep-forever toggle: flips Frigate's retain_indefinitely for the event. Patched in
  // place rather than reloading — nothing else on the page depends on the flag.
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".retain-toggle");
    if (!btn) return;
    e.preventDefault();
    const keep = btn.dataset.retained === "1" ? 0 : 1;
    btn.disabled = true;
    try {
      const res = await fetch(
        API + "/detections/" + encodeURIComponent(btn.dataset.id) + "/retain?keep=" + keep,
        { method: "POST" });
      const data = await res.json();
      if (!data.ok) {
        alert("Couldn't update retention: " + (data.error || res.status));
        return;
      }
      btn.dataset.retained = String(keep);
      btn.textContent = keep ? "📌 kept" : "📌 keep";
      btn.classList.toggle("retained", !!keep);
      btn.title = keep
        ? "Kept forever at Frigate — click to release it back to normal retention"
        : "Keep this clip forever: tells Frigate to never expire this event";
    } catch (err) {
      alert("Couldn't update retention: " + err);
    } finally {
      btn.disabled = false;
    }
  });

  // ------------------------------------------------------------------- bulk select
  // Only the Unidentified page renders #bulk-bar, so this binds nowhere else. The
  // checkboxes are injected when select mode is entered rather than baked into the
  // shared card macro — no other page's cards change, and cards loaded by pagination
  // are full page loads anyway.
  (function () {
    const bar = document.getElementById("bulk-bar");
    if (!bar) return;
    const controls = bar.querySelector(".bulk-controls");
    const toggle = document.getElementById("bulk-toggle");
    const allBtn = document.getElementById("bulk-all");
    const count = document.getElementById("bulk-count");
    const reidBtn = document.getElementById("bulk-reidentify");
    const delBtn = document.getElementById("bulk-delete");

    function boxes() {
      return Array.from(document.querySelectorAll(".bulk-pick input"));
    }
    function selectedIds() {
      return boxes().filter((b) => b.checked).map((b) => Number(b.dataset.id));
    }
    function refresh() {
      const n = selectedIds().length;
      count.textContent = n + " selected";
      if (reidBtn) reidBtn.disabled = !n;
      delBtn.disabled = !n;
    }

    function enter() {
      document.querySelectorAll(".det-card").forEach((card) => {
        // The delete button already carries the detection id on every card; reuse it
        // rather than teaching the template a second id attribute.
        const del = card.querySelector(".det-delete");
        if (!del || card.querySelector(".bulk-pick")) return;
        const label = document.createElement("label");
        label.className = "bulk-pick";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.dataset.id = del.dataset.id;
        label.appendChild(box);
        card.appendChild(label);
      });
      controls.hidden = false;
      toggle.textContent = "Cancel";
      refresh();
    }
    function exit() {
      document.querySelectorAll(".bulk-pick").forEach((el) => el.remove());
      document.querySelectorAll(".det-card.bulk-selected").forEach((el) =>
        el.classList.remove("bulk-selected"));
      controls.hidden = true;
      toggle.textContent = "☑ Select";
    }

    toggle.addEventListener("click", () => {
      if (controls.hidden) enter(); else exit();
    });

    allBtn.addEventListener("click", () => {
      // Toggles: everything on — or, if everything already was, everything off.
      const all = boxes();
      const everyOn = all.length > 0 && all.every((b) => b.checked);
      all.forEach((b) => {
        b.checked = !everyOn;
        b.closest(".det-card").classList.toggle("bulk-selected", b.checked);
      });
      refresh();
    });

    document.addEventListener("change", (e) => {
      const box = e.target.closest(".bulk-pick input");
      if (!box) return;
      box.closest(".det-card").classList.toggle("bulk-selected", box.checked);
      refresh();
    });

    async function bulkPost(path, ids) {
      const res = await fetch(API + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: ids }),
      });
      return res.json();
    }

    if (reidBtn) reidBtn.addEventListener("click", async () => {
      const ids = selectedIds();
      if (!ids.length) return;
      reidBtn.disabled = true;
      reidBtn.textContent = "↻ queueing…";
      try {
        const data = await bulkPost("/detections/bulk-identify", ids);
        if (!data.ok) {
          alert("Bulk re-identify failed: " + (data.error || "error"));
          return;
        }
        // Fire-and-forget by design (unlike the single button): the workers drain the
        // queue in the background, so say what was queued before reloading.
        alert("Queued " + data.queued + " detection(s) for identification" +
              (data.skipped ? "; " + data.skipped + " skipped (audio rows, already " +
               "running, or the queue is full — select those again in a bit)" : "") +
              ".\n\nThey resolve in the background as the GPU works through them.");
        window.location.reload();
      } catch (err) {
        alert("Bulk re-identify failed: " + err);
      } finally {
        reidBtn.textContent = "↻ Re-identify selected";
        refresh();
      }
    });

    delBtn.addEventListener("click", async () => {
      const ids = selectedIds();
      if (!ids.length) return;
      if (!window.confirm(
        "Delete " + ids.length + " detection(s) from Aviary?\n\n" +
        "Their Frigate events and clips are left alone. The rows are tombstoned so a " +
        "backfill won't re-import them. This cannot be undone.")) return;
      delBtn.disabled = true;
      delBtn.textContent = "× deleting…";
      try {
        const data = await bulkPost("/detections/bulk-delete", ids);
        if (!data.ok) {
          alert("Bulk delete failed: " + (data.error || "error"));
          return;
        }
        window.location.reload();
      } catch (err) {
        alert("Bulk delete failed: " + err);
      } finally {
        delBtn.textContent = "× Delete selected";
        refresh();
      }
    });
  })();
})();
