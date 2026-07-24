// Progressive-enhancement interactions for the preview page. Without JS the
// form still submits (matched rows are checked; missing rows just stay missing).
(function () {
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function incFor(pos) {
    return document.querySelector('input.inc[value="' + pos + '"]');
  }
  // Plex star rating (0–10, 2 per star) → "⭐️⭐️⭐️", rounded to whole stars
  // (half-star ratings round up); "" when unrated.
  function stars(rating) {
    if (rating == null || rating <= 0) return "";
    return "⭐️".repeat(Math.round(rating / 2));
  }

  // --- live selected count (action bar + Create button) --------------------
  function updateCount() {
    const n = document.querySelectorAll("input.inc:checked").length;
    const sel = document.querySelector("[data-selected]");
    const btn = document.querySelector("[data-create-count]");
    if (sel) sel.textContent = n;
    if (btn) btn.textContent = n;
  }

  // --- sticky (touched) selections -----------------------------------------
  // A selection becomes sticky the moment the user acts on it; a preferred-album
  // switch then re-matches only untouched rows (docs/adr/0001). Intent-based —
  // any interaction freezes the row. Positions are tracked as strings; the flag
  // lives only in the page, so leaving/reloading resets it.
  const touched = new Set();
  function markTouched(pos) {
    pos = String(pos);
    touched.add(pos);
    const row = document.querySelector('tr[data-rownum="' + pos + '"]');
    if (!row) return;
    const num = row.querySelector(".num");
    if (num && !num.querySelector(".sticky")) {
      const p = el("span", "sticky", "✎");
      p.title = "Edited — kept when you change the preferred album";
      num.appendChild(p);
    }
  }

  // --- manual library search (shared by missing cards and matched rows) ----
  // `scope` is any element containing a `.q` input and a `.results` box; each
  // result button invokes onPick(track, label).
  async function doSearch(scope, onPick, opts) {
    opts = opts || {};
    const query = scope.querySelector(".q").value.trim();
    const results = scope.querySelector(".results");
    results.textContent = "";
    if (!query) return;
    results.textContent = "searching…";
    let data;
    try {
      const url = "/search?q=" + encodeURIComponent(query) +
        (opts.albums ? "&albums=1" : "");
      const resp = await fetch(url);
      data = await resp.json();
    } catch (err) { results.textContent = "search failed"; return; }
    if (data.error) { results.textContent = data.error; return; }
    if (!data.results || !data.results.length) {
      results.textContent = "no matches in your library"; return;
    }
    results.textContent = "";
    data.results.forEach(function (t) {
      if (t.rating_key == null) return;
      if (t.type === "album") { results.appendChild(albumItem(t, onPick)); return; }
      const label = t.artist + " — " + t.title + (t.album ? " · " + t.album : "");
      const b = el("button", "result", label);
      b.type = "button";
      const st = stars(t.rating);
      if (st) b.appendChild(el("span", "result-stars", st));
      b.addEventListener("click", function () { onPick(t, label); });
      results.appendChild(b);
    });
  }

  // An album search result: a whole-album "add" button (click the badge/label)
  // plus a disclosure toggle that lazily reveals the album's tracks so a single
  // track can be added — handy when you recall the album but not the song.
  function albumItem(album, onPick) {
    const wrap = el("div", "albumitem");
    const head = el("div", "albumhead");

    const toggle = el("button", "album-toggle", "▸");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "Show album tracks");

    const add = el("button", "result result-album");
    add.type = "button";
    add.appendChild(el("span", "reslabel", "ALBUM"));
    add.appendChild(el("span", null, album.title + " — " + album.artist +
      (album.count ? " · " + album.count + " tracks" : "")));
    add.addEventListener("click", function () { onPick(album, album.title); });

    const sub = el("div", "album-tracks");
    sub.hidden = true;
    let loaded = false;
    toggle.addEventListener("click", async function () {
      if (sub.hidden && !loaded) {                 // fetch tracks on first open
        loaded = true;
        sub.textContent = "loading…";
        try {
          const resp = await fetch("/album/" + encodeURIComponent(album.rating_key));
          const data = await resp.json();
          sub.textContent = "";
          (data.tracks || []).forEach(function (tr) {
            if (tr.rating_key == null) return;
            const tb = el("button", "result result-track", tr.title || "");
            tb.type = "button";
            const st = stars(tr.rating);
            if (st) tb.appendChild(el("span", "result-stars", st));
            tb.addEventListener("click", function () { onPick(tr, tr.title); });
            sub.appendChild(tb);
          });
          if (!sub.children.length) sub.textContent = "no tracks in this album";
        } catch (e) { sub.textContent = "couldn't load album"; loaded = false; }
      }
      sub.hidden = !sub.hidden;
      toggle.textContent = sub.hidden ? "▸" : "▾";
      toggle.setAttribute("aria-expanded", String(!sub.hidden));
    });

    head.appendChild(toggle);
    head.appendChild(add);
    wrap.appendChild(head);
    wrap.appendChild(sub);
    return wrap;
  }
  // Choose a library track for a (previously) missing card.
  function chooseMissing(card, track, label) {
    const pick = card.querySelector(".pick");
    const inc = card.querySelector(".inc");
    pick.value = track.rating_key; pick.disabled = false;
    inc.checked = true; inc.disabled = false; inc.hidden = false;
    card.querySelector(".chosen").textContent = "✓ " + label;
    card.querySelector(".searcher").hidden = true;
    card.querySelector(".results").textContent = "";
    card.classList.remove("skipped");
    markTouched(card.dataset.pos);
    updateCount();
  }
  // Re-point a matched row to an arbitrary library track found via search.
  function applyPick(pos, track, label) {
    const row = document.querySelector('tr[data-rownum="' + pos + '"]');
    if (!row) return;
    markTouched(pos);
    const pick = row.querySelector('[name="pick_' + pos + '"]');
    if (pick) { pick.value = track.rating_key; pick.disabled = false; }
    const inc = row.querySelector("input.inc");
    if (inc) { inc.checked = true; inc.disabled = false; inc.hidden = false; }
    const title = track.artist + " — " + track.title;
    const sub = track.album || "searched";
    const menu = row.querySelector(".dd-menu");
    if (menu) {                                     // multi-match dropdown row
      // Represent the searched track as a (selected) option so reopening the
      // dropdown stays honest and re-selecting it works like any other.
      let opt = menu.querySelector('.dd-opt[data-key="' + track.rating_key + '"]');
      if (!opt) {
        opt = el("button", "dd-opt");
        opt.type = "button";
        opt.dataset.key = track.rating_key;
        opt.dataset.title = title;
        opt.dataset.sub = sub;
        opt.appendChild(el("span", "dd-title trunc", title));
        opt.appendChild(el("span", "dd-sub trunc", sub));
        opt.addEventListener("click", function () { selectDdOpt(opt); });
        menu.appendChild(opt);
      }
      selectDdOpt(opt);                             // sets pick + display + sel
    } else {                                        // single-candidate row
      const t = row.querySelector("[data-rowtitle]");
      if (t) t.textContent = title;
      const s = row.querySelector("[data-rowsub]");
      if (s) s.textContent = track.album || "";
    }
    const box = row.querySelector("[data-rowsearch-box]");
    if (box) box.hidden = true;
    const tgl = row.querySelector('.rowsearch-btn[data-rowsearch="' + pos + '"]');
    if (tgl) tgl.classList.remove("open");          // clear the magnifier highlight
    // The auto-match explanation no longer describes a hand-picked track.
    const pop = row.querySelector("[data-matchpop]");
    if (pop) {
      const lines = pop.querySelectorAll(".matchpop-line");
      if (lines[0]) lines[0].textContent = "Chosen manually from library search";
      if (lines[1]) lines[1].hidden = true;
      const code = pop.querySelector("[data-matchpop-code]");
      if (code) code.hidden = true;
    }
    row.querySelectorAll(".results").forEach(function (r) { r.textContent = ""; });
    updateCount();
  }

  // --- custom multi-match dropdown -----------------------------------------
  function closeDropdowns(except) {
    document.querySelectorAll("[data-dd]").forEach(function (dd) {
      if (dd === except) return;
      const menu = dd.querySelector(".dd-menu");
      const btn = dd.querySelector(".dd-btn");
      if (menu) menu.hidden = true;
      if (btn) btn.classList.remove("open");
    });
  }
  // Close every match-explanation popover except the one in `except` (a
  // [data-matchinfo] button's parent), resetting each trigger's aria state.
  function closeMatchPops(except) {
    document.querySelectorAll("[data-matchinfo]").forEach(function (btn) {
      if (btn.parentElement === except) return;
      const pop = btn.parentElement.querySelector("[data-matchpop]");
      if (pop) pop.hidden = true;
      btn.setAttribute("aria-expanded", "false");
    });
  }
  // Apply a dropdown option's choice to its row (hidden pick + button display).
  function selectDdOpt(opt) {
    const dd = opt.closest("[data-dd]");
    dd.querySelector("input[type=hidden]").value = opt.dataset.key;
    dd.querySelector("[data-dd-title]").textContent = opt.dataset.title;
    dd.querySelector("[data-dd-sub]").textContent = opt.dataset.sub;
    dd.querySelectorAll(".dd-opt").forEach(function (o) { o.classList.remove("sel"); });
    opt.classList.add("sel");
    dd.querySelector(".dd-menu").hidden = true;
    dd.querySelector(".dd-btn").classList.remove("open");
    const row = dd.closest("tr[data-rownum]");
    if (row) markTouched(row.dataset.rownum);
  }

  // --- preview JSON of the current selection -------------------------------
  function buildJSON() {
    const nameEl = document.querySelector("input[name=name]");
    const tracks = [];
    document.querySelectorAll("input.inc:checked").forEach(function (inc) {
      const pos = inc.value;
      const pick = document.querySelector('[name="pick_' + pos + '"]');
      tracks.push({ position: Number(pos),
                    rating_key: pick ? pick.value : null });
    });
    return JSON.stringify(
      { playlist: nameEl ? nameEl.value : "", tracks: tracks }, null, 2);
  }

  // --- fuzzy-match confirm cards -------------------------------------------
  function onAccept(b) {
    const inc = incFor(b.dataset.accept);
    if (inc) inc.checked = true;
    const card = b.closest(".fuzzycard");
    if (card) card.classList.remove("rejected");
    markTouched(b.dataset.accept);
    updateCount();
  }
  function onReject(b) {
    const inc = incFor(b.dataset.reject);
    if (inc) inc.checked = false;
    const card = b.closest(".fuzzycard");
    if (card) card.classList.add("rejected");
    markTouched(b.dataset.reject);
    updateCount();
  }
  function wireFuzzyCard(card) {
    const a = card.querySelector("[data-accept]");
    const r = card.querySelector("[data-reject]");
    if (a) a.addEventListener("click", function () { onAccept(a); });
    if (r) r.addEventListener("click", function () { onReject(r); });
  }
  // The candidate sub-line shared by the dropdown and the fuzzy card (mirrors
  // the Jinja form in preview.html): "Album · tier/source".
  function candSub(c) {
    return (c.album ? c.album + " · " : "") + c.tier + "/" + c.source;
  }
  // Build a fuzzy confirm card mirroring the server-rendered markup so an album
  // switch can add one for a row whose new leading match is only a fuzzy hit.
  function makeFuzzyCard(song) {
    const card = el("div", "card card-fuzzy fuzzycard");
    card.dataset.fcard = song.position;
    const left = el("div"); left.style.minWidth = "0";
    left.appendChild(el("div", "caplabel", "From setlist"));
    left.appendChild(el("div", "trunc", song.title));
    const right = el("div"); right.style.minWidth = "0";
    right.appendChild(el("div", "caplabel", "Plex suggests"));
    right.appendChild(el("div", "trunc",
      song.track_artist + " — " + song.track_title));
    right.appendChild(el("div", "sub trunc", candSub(song)));
    const btns = el("div");
    btns.style.cssText = "display:flex;gap:8px;justify-content:flex-end";
    const acc = el("button", "btn-sm btn-amber", "Accept");
    acc.type = "button"; acc.dataset.accept = song.position;
    const rej = el("button", "btn-sm", "Reject");
    rej.type = "button"; rej.dataset.reject = song.position;
    btns.appendChild(acc); btns.appendChild(rej);
    card.appendChild(left);
    card.appendChild(el("div", "arrow-mid", "→"));
    card.appendChild(right); card.appendChild(btns);
    return card;
  }
  function insertFuzzyInOrder(container, card, position) {
    const after = Array.prototype.find.call(container.children, function (c) {
      return Number(c.dataset.fcard) > position;
    });
    container.insertBefore(card, after || null);
  }
  // Add/remove/refresh the fuzzy card for an untouched position to match its new
  // leading candidate. Only ever called for untouched rows, so a present card is
  // never in an accepted/rejected (touched) state — safe to rebuild wholesale.
  function syncFuzzyCard(song) {
    const container = document.querySelector("[data-fuzzy-cards]");
    const section = document.querySelector("[data-fuzzy-section]");
    if (!container) return;
    const pos = String(song.position);
    const existing = container.querySelector('[data-fcard="' + pos + '"]');
    if (existing) existing.remove();
    if (song.matched && song.quality === "fuzzy") {
      const card = makeFuzzyCard(song);
      insertFuzzyInOrder(container, card, song.position);
      wireFuzzyCard(card);
    }
    if (section) section.hidden = container.children.length === 0;
  }

  // --- preferred-album re-match (in place, keeps touched rows sticky) -------
  // Re-order an untouched multi-match row's dropdown to the server's new
  // candidate order and adopt its new default pick + display. Deliberately does
  // NOT call selectDdOpt() — a re-match is not a user touch, so the row stays
  // untouched and will re-match again on the next album switch.
  function patchMultiRow(row, song) {
    const dd = row.querySelector("[data-dd]");
    const menu = dd && dd.querySelector(".dd-menu");
    const cands = song.candidates || [];
    if (!menu || !cands.length) return;
    cands.forEach(function (c) {                    // move options into new order
      const opt = menu.querySelector('.dd-opt[data-key="' + c.rating_key + '"]');
      if (opt) menu.appendChild(opt);
    });
    menu.querySelectorAll(".dd-opt").forEach(function (o) { o.classList.remove("sel"); });
    const lead = cands[0];
    const first = menu.querySelector('.dd-opt[data-key="' + lead.rating_key + '"]');
    if (first) first.classList.add("sel");
    dd.querySelector("input[type=hidden]").value = lead.rating_key;
    dd.querySelector("[data-dd-title]").textContent =
      lead.track_artist + " — " + lead.track_title;
    dd.querySelector("[data-dd-sub]").textContent = candSub(lead);
  }
  // Inline status shown beside the album select. On failure we keep the page's
  // selections intact rather than resubmitting, so the message must be visible.
  function setRematchMsg(form, text) {
    let n = form.querySelector("[data-rematch-msg]");
    if (!n) {
      n = el("span", "sub");
      n.dataset.rematchMsg = "1";
      n.style.color = "var(--red)";
      form.appendChild(n);
    }
    n.textContent = text;
  }
  // The in-place "Prefer album" switch: re-match via JSON, patch only untouched
  // rows. On failure it keeps every selection and reverts the album choice —
  // never a full resubmit, which would wipe the user's in-page edits.
  async function rematchAlbum(sel) {
    const form = sel.form;
    // Snapshot the form BEFORE disabling the select — a disabled control is
    // omitted from FormData, which would drop prefer_album from the request.
    const body = new FormData(form);
    const prevAlbum = sel.dataset.prev != null ? sel.dataset.prev : sel.value;
    const apply = form.querySelector('button[type="submit"]');
    const prev = apply ? apply.textContent : "";
    if (apply) { apply.textContent = "Re-matching…"; apply.disabled = true; }
    sel.disabled = true;
    let data;
    try {
      const resp = await fetch("/rematch", { method: "POST", body: body });
      data = await resp.json();
    } catch (e) { data = { error: "Re-match failed" }; }
    sel.disabled = false;
    if (apply) { apply.textContent = prev; apply.disabled = false; }
    if (!data || data.error) {
      sel.value = prevAlbum;                          // undo the choice; keep rows
      setRematchMsg(form, "Couldn't re-match — selections left unchanged.");
      return;
    }
    setRematchMsg(form, "");
    (data.songs || []).forEach(function (song) {
      if (touched.has(String(song.position))) return;   // sticky: leave it alone
      const row = document.querySelector(
        'tr[data-rownum="' + song.position + '"]');
      if (row && row.querySelector("[data-dd]")) patchMultiRow(row, song);
      syncFuzzyCard(song);
    });
    sel.dataset.prev = sel.value;                     // remember for next revert
    updateCount();
  }

  // --- wiring --------------------------------------------------------------
  document.querySelectorAll("form[data-loading]").forEach(function (form) {
    form.addEventListener("submit", function () {
      const btn = form.querySelector("button[type=submit], button:not([type])");
      if (btn && !btn.disabled) {
        btn.textContent = form.dataset.loading;
        btn.disabled = true;
      }
    });
  });

  // Nav dropdowns ("New", "History"): click to toggle, click-away closes.
  // Opening one closes any other (stopPropagation skips the click-away).
  document.querySelectorAll("[data-navdrop-toggle]").forEach(function (btn) {
    const menu = btn.parentElement.querySelector(".navdrop-menu");
    if (!menu) return;
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      const opening = menu.hidden;
      document.querySelectorAll(".navdrop-menu").forEach(function (m) { m.hidden = true; });
      document.querySelectorAll("[data-navdrop-toggle]").forEach(function (b) {
        b.setAttribute("aria-expanded", "false");
      });
      menu.hidden = !opening;
      btn.setAttribute("aria-expanded", String(opening));
    });
  });
  document.addEventListener("click", function () {
    document.querySelectorAll(".navdrop-menu").forEach(function (m) { m.hidden = true; });
    document.querySelectorAll("[data-navdrop-toggle]").forEach(function (b) {
      b.setAttribute("aria-expanded", "false");
    });
  });

  // Prefer-album select: re-match in place, keeping touched rows sticky.
  // No-JS users fall back to the Apply button (a full submit that resets).
  document.querySelectorAll("[data-album-select]").forEach(function (sel) {
    sel.dataset.prev = sel.value;   // last album that matched, for error revert
    sel.addEventListener("change", function () { rematchAlbum(sel); });
  });

  document.querySelectorAll("[data-missing]").forEach(function (card) {
    const go = card.querySelector(".go");
    const q = card.querySelector(".q");
    const skip = card.querySelector("[data-skip]");
    function run() { doSearch(card, function (t, l) { chooseMissing(card, t, l); }); }
    if (go) go.addEventListener("click", run);
    if (q) q.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); run(); }
    });
    if (skip) skip.addEventListener("click", function () {
      const inc = card.querySelector(".inc");
      if (inc) inc.checked = false;
      card.classList.add("skipped");
      markTouched(card.dataset.pos);
      updateCount();
    });
  });

  // Matched rows: a "search…" toggle reveals an inline searcher to re-point the
  // row at any library track when none of the auto-matches are right.
  document.querySelectorAll("[data-rowsearch]").forEach(function (btn) {
    const pos = btn.dataset.rowsearch;
    const box = document.querySelector('[data-rowsearch-box="' + pos + '"]');
    if (!box) return;
    const go = box.querySelector(".go");
    const q = box.querySelector(".q");
    function run() { doSearch(box, function (t, l) { applyPick(pos, t, l); }); }
    btn.addEventListener("click", function () {
      box.hidden = !box.hidden;
      btn.classList.toggle("open", !box.hidden);
      if (!box.hidden && q) q.focus();
    });
    if (go) go.addEventListener("click", run);
    if (q) q.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); run(); }
    });
  });

  document.querySelectorAll("[data-dd-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      const dd = btn.closest("[data-dd]");
      const menu = dd.querySelector(".dd-menu");
      const opening = menu.hidden;
      closeDropdowns(dd);
      closeMatchPops(null);
      menu.hidden = !opening;
      btn.classList.toggle("open", opening);
    });
  });
  document.querySelectorAll(".dd-opt").forEach(function (opt) {
    opt.addEventListener("click", function () { selectDdOpt(opt); });
  });

  // --- match-explanation popover (clicking the Exact/Fuzzy pill) ------------
  document.querySelectorAll("[data-matchinfo]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      const pop = btn.parentElement.querySelector("[data-matchpop]");
      if (!pop) return;
      const opening = pop.hidden;
      closeMatchPops(null);
      closeDropdowns(null);
      pop.hidden = !opening;
      btn.setAttribute("aria-expanded", String(opening));
    });
  });

  document.addEventListener("click", function () {
    closeDropdowns(null);
    closeMatchPops(null);
  });

  // --- chip-to-row highlighting --------------------------------------------
  // Clicking a category chip flashes its matching rows, then leaves a coloured
  // left bar + faint tint until another chip (or the same one) is clicked.
  function clearRowMarks() {
    document.querySelectorAll("tr.row-marked, tr.row-flash").forEach(function (tr) {
      tr.classList.remove("row-marked", "row-flash");
    });
    document.querySelectorAll(".chip.active").forEach(function (c) {
      c.classList.remove("active");
    });
  }
  function markRows(chip) {
    if (chip.classList.contains("active")) { clearRowMarks(); return; }  // toggle off
    const rows = document.querySelectorAll(
      'tr[data-status="' + chip.dataset.chipFilter + '"]');
    if (!rows.length) return;                       // 0-count chip: leave marks as-is
    clearRowMarks();
    // row-flash plays once and settles into the resting row-marked tint; it is
    // stripped again by the next clearRowMarks(), so no animationend cleanup is
    // needed (and chasing it leaks listeners when a flash is cut short).
    rows.forEach(function (tr) { tr.classList.add("row-marked", "row-flash"); });
    chip.classList.add("active");
    rows[0].scrollIntoView({ block: "center", behavior: "smooth" });
  }
  document.querySelectorAll("[data-chip-filter]").forEach(function (chip) {
    chip.addEventListener("click", function () { markRows(chip); });
    chip.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); markRows(chip); }
    });
  });
  // The "songs" total chip clears any active highlight.
  document.querySelectorAll("[data-chip-clear]").forEach(function (chip) {
    chip.addEventListener("click", clearRowMarks);
    chip.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); clearRowMarks(); }
    });
  });

  document.querySelectorAll(".fuzzycard").forEach(wireFuzzyCard);

  const jsonBtn = document.querySelector("[data-json-toggle]");
  const jsonView = document.querySelector("[data-jsonview]");
  if (jsonBtn && jsonView) {
    jsonBtn.addEventListener("click", function () {
      if (jsonView.hidden) { jsonView.textContent = buildJSON(); jsonView.hidden = false; }
      else { jsonView.hidden = true; }
    });
  }

  document.querySelectorAll("input.inc").forEach(function (cb) {
    cb.addEventListener("change", function () {
      markTouched(cb.value);   // toggling include/skip is an explicit touch
      updateCount();
    });
  });
  updateCount();

  // --- build page + playlist editor: numbered, draggable track table --------
  // Shared by /build and /playlists/<id>/edit (setlist-style layout). No-ops
  // when the table scope is absent. Submission order is the DOM order of the
  // hidden rating_keys inputs, so reordering the rows reorders the playlist.
  (function wireBuild() {
    const scope = document.querySelector("[data-build]");
    if (!scope) return;                         // only on build/editor pages
    const table = scope.querySelector(".tracktable");
    const body = table && table.tBodies[0];     // header <tr> + data rows
    if (!body) return;
    const empty = scope.querySelector("[data-build-empty]");
    const submit = document.querySelector("[data-build-submit]");
    const counts = document.querySelectorAll("[data-count]");
    const picked = new Set();
    let dragEl = null;

    function refresh() {
      if (submit) submit.disabled = picked.size === 0;
      if (empty) empty.hidden = picked.size !== 0;
      counts.forEach(function (c) { c.textContent = picked.size; });
    }

    function renumber() {
      body.querySelectorAll(".chosen-row").forEach(function (tr, i) {
        const numTd = tr.querySelector(".num");
        if (numTd) numTd.textContent = (i + 1 < 10 ? "0" : "") + (i + 1);
      });
    }

    function addTrack(t) {
      const key = String(t.rating_key);
      if (picked.has(key)) return;              // dedupe: skip already-chosen
      picked.add(key);

      const tr = el("tr", "chosen-row");
      tr.draggable = true;                      // drag the row to reorder

      const gripTd = el("td", "grip", "⠿");
      gripTd.setAttribute("aria-hidden", "true");

      const numTd = el("td", "num");            // filled by renumber()

      // Artist / Album / Track broken out into their own columns.
      const trackTd = el("td", "trunc", t.title || "");
      const hidden = el("input");
      hidden.type = "hidden"; hidden.name = "rating_keys"; hidden.value = key;
      trackTd.appendChild(hidden);
      const artistTd = el("td", "trunc", t.artist || "");
      const albumTd = el("td", "trunc sub", t.album || "");
      const starsTd = el("td", "sub stars", stars(t.rating));

      const rmTd = el("td");
      rmTd.style.textAlign = "right";
      const rm = el("button", "trackrm", "✕");
      rm.type = "button";
      rm.setAttribute("aria-label", "Remove track");
      rm.addEventListener("click", function () {
        picked.delete(key); tr.remove(); renumber(); refresh();
      });
      rmTd.appendChild(rm);

      tr.addEventListener("dragstart", function () {
        dragEl = tr; tr.classList.add("dragging");
      });
      tr.addEventListener("dragend", function () {
        tr.classList.remove("dragging"); dragEl = null; renumber();
      });

      tr.appendChild(gripTd);
      tr.appendChild(numTd);
      tr.appendChild(trackTd);
      tr.appendChild(artistTd);
      tr.appendChild(albumTd);
      tr.appendChild(starsTd);
      tr.appendChild(rmTd);
      body.appendChild(tr);
      renumber(); refresh();
    }

    function afterElement(y) {
      const rows = Array.prototype.slice.call(
        body.querySelectorAll(".chosen-row:not(.dragging)"));
      let best = null, bestOffset = Number.NEGATIVE_INFINITY;
      rows.forEach(function (row) {
        const box = row.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > bestOffset) { bestOffset = offset; best = row; }
      });
      return best;                              // null => append at end
    }
    body.addEventListener("dragover", function (e) {
      if (!dragEl) return;
      e.preventDefault();
      const ref = afterElement(e.clientY);
      if (ref == null) body.appendChild(dragEl);
      else body.insertBefore(dragEl, ref);
    });

    // Add-tracks search box (its own scope, outside the table panel). Results
    // include whole albums; clicking one inserts all its tracks in order.
    async function onAdd(t) {
      if (t.type === "album") {
        try {
          const resp = await fetch("/album/" + encodeURIComponent(t.rating_key));
          const data = await resp.json();
          if (data.tracks) data.tracks.forEach(addTrack);   // in album order, deduped
        } catch (e) { /* leave the list unchanged on failure */ }
      } else {
        addTrack(t);
      }
    }
    const addScope = document.querySelector("[data-add]");
    if (addScope) {
      const q = addScope.querySelector(".q");
      const go = addScope.querySelector(".go");
      const run = function () { doSearch(addScope, onAdd, {albums: true}); };
      if (go) go.addEventListener("click", run);
      if (q) q.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); run(); }
      });
    }

    // Seed any preloaded tracks (the editor renders them as JSON).
    const seedEl = document.querySelector("[data-build-initial]");
    if (seedEl) {
      let seed = [];
      try { seed = JSON.parse(seedEl.textContent || "[]"); } catch (e) { seed = []; }
      seed.forEach(function (t) {
        if (t && t.rating_key != null) addTrack(t);
      });
    }
    refresh();
  })();
})();
