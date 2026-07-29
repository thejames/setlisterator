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

  // Pair a pickable control with its own audition button, side by side. The
  // button is a sibling, not a child: nested interactive content is invalid
  // HTML, and a <span> stand-in can be clicked but never focused or reached by
  // keyboard. The player opens beneath the pair.
  function auditionRow(pickEl, ratingKey) {
    const row = el("div", "auditionrow-pair");
    const aud = el("button", "aud-btn", "▶");
    aud.type = "button";
    aud.title = "Audition this track";
    aud.setAttribute("aria-label", "Audition this track");
    aud.addEventListener("click", function () {
      if (aud.classList.contains("playing")) { stopAudition(); return; }
      mountAudition(row, ratingKey, false, aud);
    });
    row.appendChild(pickEl);
    row.appendChild(aud);
    return row;
  }

  // --- manual library search (shared by missing cards and matched rows) ----
  // `scope` is any element containing a `.q` input and a `.results` box; each
  // result button invokes onPick(track, label).
  async function doSearch(scope, onPick, opts) {
    opts = opts || {};
    const query = scope.querySelector(".q").value.trim();
    const results = scope.querySelector(".results");
    stopAuditionIn(results);
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
    stopAuditionIn(results);
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
      results.appendChild(auditionRow(b, t.rating_key));
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
            sub.appendChild(auditionRow(tb, tr.rating_key));
          });
          if (!sub.children.length) sub.textContent = "no tracks in this album";
        } catch (e) { sub.textContent = "couldn't load album"; loaded = false; }
      }
      if (!sub.hidden) stopAuditionIn(sub);      // collapsing hides the player
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
    stopAuditionIn(card);
    card.querySelector(".results").textContent = "";
    card.classList.remove("skipped");
    markTouched(card.dataset.pos);
    updateCount();
  }
  // Re-point a matched row to an arbitrary library track found via search.
  function applyPick(pos, track, label) {
    const row = document.querySelector('tr[data-rownum="' + pos + '"]');
    if (!row) return;
    stopAudition();          // the row no longer points at what's playing
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
        menu.appendChild(ddRow(opt, track.rating_key));
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
  // Auditioning a candidate must never select it — merely listening would
  // touch the row and freeze it against re-match. Being a sibling of the
  // option rather than a child is what guarantees that: the click cannot
  // reach the option's handler at all.
  function wireDdAudition(aud) {
    aud.addEventListener("click", function (e) {
      // Not to protect the option — being its sibling already does that — but
      // to keep the document-level handler from closing the menu. Comparing
      // two versions means auditioning both without it shutting between them.
      e.stopPropagation();
      const row = aud.closest("tr[data-rownum]");
      if (!row) return;
      if (aud.classList.contains("playing")) { stopAudition(); return; }
      mountAudition(row, aud.dataset.audKey, true, aud);
    });
  }

  // Pair a dropdown option with its own audition button. Same reasoning as
  // auditionRow(): a sibling button, so it can be tabbed to.
  function ddRow(opt, ratingKey) {
    const row = el("div", "dd-row");
    const aud = el("button", "aud-btn dd-aud", "▶");
    aud.type = "button";
    aud.dataset.audKey = ratingKey;
    aud.title = "Audition this version";
    aud.setAttribute("aria-label", "Audition this version");
    wireDdAudition(aud);
    row.appendChild(opt);
    row.appendChild(aud);
    return row;
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
    // Hear the proposed track before accepting it into the setlist — the whole
    // question a fuzzy card asks. The track is whatever the row currently
    // picks, so there is no key to carry on the card itself.
    const p = card.querySelector("[data-aud-fuzzy]");
    if (p) p.addEventListener("click", function () {
      if (p.classList.contains("playing")) { stopAudition(); return; }
      const row = document.querySelector(
        'tr[data-rownum="' + p.dataset.audFuzzy + '"]');
      const key = row && rowPick(row);
      if (key) mountAudition(card, key, false, p);
    });
    const a = card.querySelector("[data-accept]");
    const r = card.querySelector("[data-reject]");
    if (a) a.addEventListener("click", function () { stopAudition(); onAccept(a); });
    if (r) r.addEventListener("click", function () { stopAudition(); onReject(r); });
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
    const aud = el("button", "aud-btn", "▶");
    aud.type = "button"; aud.dataset.audFuzzy = song.position;
    aud.title = "Audition this match"; aud.setAttribute("aria-label", "Audition");
    btns.appendChild(aud);
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
      if (opt) menu.appendChild(opt.closest(".dd-row") || opt);
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

  // --- optional poster upload (create + build + editor) --------------------
  // Progressive enhancement over a bare <input type=file>: a thumbnail preview
  // plus a client-side type/size pre-check so an obviously-bad file is flagged
  // before submit. The server is still the real safety net (best-effort, never
  // blocks the save) — this just gives faster feedback. The accepted types and
  // size cap are read from the field's data-attrs (rendered from core's single
  // source), so this pre-check can't drift from the server's own validation.
  function posterPolicy(field) {
    const types = (field.getAttribute("data-poster-accept") || "").split(",")
      .map(function (s) { return s.trim(); }).filter(Boolean);
    return { types: types, max: Number(field.getAttribute("data-poster-max")) || 0 };
  }
  function mbLabel(bytes) { return Math.round(bytes / (1024 * 1024)) + " MB"; }
  function posterNote(field, text, bad) {
    const note = field.querySelector("[data-poster-note]");
    if (!note) return;
    note.textContent = text || "";
    note.hidden = !text;
    note.classList.toggle("bad", !!bad);
  }
  // The file wins server-side when both are filled (ADR 0005), so once a file
  // is picked the link box goes away rather than sitting there looking like it
  // still counts. Clearing the file brings it back with whatever was typed.
  function posterLinkVisible(field, visible) {
    const row = field.querySelector("[data-poster-linkrow]");
    if (row) row.hidden = !visible;
  }
  function onPosterPick(input) {
    const field = input.closest("[data-poster-field]");
    const prev = field && field.querySelector("[data-poster-preview]");
    const file = input.files && input.files[0];
    if (prev) { prev.hidden = true; prev.removeAttribute("src"); }
    if (!field) return;
    posterLinkVisible(field, !file);
    if (!file) { posterNote(field, ""); return; }
    const pol = posterPolicy(field);
    // A file we've just flagged as unusable isn't beating anything, so the link
    // box comes back — the server falls through to it for exactly this case,
    // and hiding it would misreport which image is about to be used.
    if (pol.types.length && pol.types.indexOf(file.type) === -1) {
      posterNote(field, "That’s not a JPEG, PNG or WebP — it won’t be used.", true);
      posterLinkVisible(field, true);
      return;
    }
    if (pol.max && file.size > pol.max) {
      posterNote(field, "That image is over " + mbLabel(pol.max) + " — it won’t be used.", true);
      posterLinkVisible(field, true);
      return;
    }
    posterNote(field, "");
    if (prev) {
      const reader = new FileReader();
      reader.onload = function () { prev.src = reader.result; prev.hidden = false; };
      reader.readAsDataURL(file);
    }
  }

  // --- auditioning (ADR-0004) ----------------------------------------------
  // Listening to a candidate before selecting it. Deliberately never calls
  // markTouched(): auditioning is not a touch, so a row auditioned but not
  // chosen still re-matches on the next preferred-album switch. Do not "fix"
  // that by marking the row — it would silently freeze rows the user only
  // listened to.
  //
  // There is exactly one player at a time; it is created next to whatever is
  // being auditioned and destroyed when anything invalidates it. Stopping
  // clears the element's src and calls load(), which is what actually closes
  // the connection — pausing alone leaves the socket open, and on the proxy
  // path that holds one of the server's four threads.

  const PLEX_BASE = document.body.dataset.plexBase || "";
  let auditionEl = null;      // the roaming host node (tr or div)
  let auditionAudio = null;

  // Reachability is a property of the browser's network, not the server's, so
  // only the browser can answer it — cached per tab because the answer can't
  // change without a reload. ?audition=direct|proxy forces either path, which
  // is the only way to exercise the one the probe didn't pick.
  async function canReachPlex() {
    // The override sticks for the tab: the pages worth testing are reached by
    // POST (/preview, /create), where a query string can't follow. ?audition=
    // auto clears it again.
    let forced = new URLSearchParams(location.search).get("audition");
    if (forced) sessionStorage.setItem("auditionForce", forced);
    else forced = sessionStorage.getItem("auditionForce");
    if (forced === "auto") sessionStorage.removeItem("auditionForce");
    if (forced === "direct") return true;
    if (forced === "proxy") return false;
    const cached = sessionStorage.getItem("auditionDirect");
    if (cached !== null) return cached === "1";
    let ok = false;
    if (PLEX_BASE) {
      try {
        // no-cors: we only need "did the connection succeed", not the body.
        // 4s, not 2: a Tailscale-routed LAN measured 1.16s for this request, so
        // a tighter budget would misreport a working direct path as unreachable.
        // The cost of a longer wait is absorbed because this runs at page load.
        await fetch(PLEX_BASE + "/identity",
                    { mode: "no-cors", signal: AbortSignal.timeout(4000) });
        ok = true;
      } catch (err) { ok = false; }
    }
    sessionStorage.setItem("auditionDirect", ok ? "1" : "0");
    return ok;
  }

  // The audition quality setting (gear menu). Stored per browser, so the
  // server's configured default only applies until you touch the toggle.
  function transcodePref() {
    const saved = localStorage.getItem("auditionTranscode");
    if (saved !== null) return saved === "1";
    return document.body.dataset.transcodeDefault !== "0";
  }
  function setTranscodePref(on) {
    localStorage.setItem("auditionTranscode", on ? "1" : "0");
  }

  function clockText(secs) {
    if (!isFinite(secs) || secs < 0) return "–:––";
    const m = Math.floor(secs / 60);
    return m + ":" + String(Math.floor(secs % 60)).padStart(2, "0");
  }

  function stopAudition() {
    if (auditionAudio) {
      auditionAudio.pause();
      auditionAudio.removeAttribute("src");
      auditionAudio.load();                 // closes the connection
      auditionAudio = null;
    }
    if (auditionEl) { auditionEl.remove(); auditionEl = null; }
    document.querySelectorAll(".aud-btn.playing").forEach(function (b) {
      b.classList.remove("playing");
    });
  }

  // Stop only if the player is the one attached to `host`. Lets a caller tear
  // down its own row without silencing an audition running elsewhere.
  function stopAuditionFor(host) {
    if (auditionEl && host && host.nextElementSibling === auditionEl) stopAudition();
  }

  // Stop if the player lives inside `container`. Search results mount the
  // player among themselves, and emptying or hiding the list would otherwise
  // detach it while the audio plays on — no controls, and on the proxy path a
  // server thread held open.
  function stopAuditionIn(container) {
    if (auditionEl && container && container.contains(auditionEl)) stopAudition();
  }

  // `after` is the node to open beneath; `asRow` wraps in a <tr> for the
  // preview table, otherwise a plain div (search results).
  async function mountAudition(after, ratingKey, asRow, trigger) {
    stopAudition();

    const box = el("div", "audition");
    const toggle = el("button", "aud-toggle", "▶"); toggle.type = "button";
    toggle.disabled = true;
    const seek = document.createElement("input");
    seek.type = "range"; seek.className = "seek";
    seek.min = 0; seek.max = 1; seek.step = 0.1; seek.value = 0;
    seek.disabled = true;
    const time = el("span", "time", "0:00 / –:––");
    const what = el("span", "what", "loading…");
    const note = el("span", "note", "");
    box.append(toggle, seek, time, what, note, el("span", "esc", "ESC to stop"));

    let host;
    if (asRow) {
      host = document.createElement("tr");
      host.className = "auditionrow";
      const td = document.createElement("td");
      // Span whatever the host row spans — the preview table and the picker
      // have different column counts, and neither should have to say so.
      td.colSpan = after.children.length || 1;
      td.appendChild(box);
      host.appendChild(td);
    } else {
      host = el("div", "auditionbox");
      host.appendChild(box);
    }
    after.parentNode.insertBefore(host, after.nextSibling);
    auditionEl = host;
    if (trigger) trigger.classList.add("playing");

    // Pinned for the life of this audition: re-reading it on a seek would let
    // a mid-audition toggle change the stream's mode while the offset
    // arithmetic still assumed the old one.
    const pref = transcodePref();
    let data;
    try {
      const resp = await fetch("/audition/" + encodeURIComponent(ratingKey) +
                               "?transcode=" + (pref ? "1" : "0"));
      data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || "unavailable");
    } catch (err) {
      if (auditionEl !== host) return;      // superseded while loading
      what.textContent = "can't audition this track";
      note.textContent = String(err.message || err);
      note.classList.add("warn");
      return;
    }
    const direct = await canReachPlex();
    if (auditionEl !== host) return;

    const audio = new Audio();
    audio.preload = "auto";
    audio.src = direct ? data.direct_url : data.stream_path;
    auditionAudio = audio;

    what.textContent = (data.artist ? data.artist + " — " : "") + data.title;
    // Surfaced deliberately: which path is in use and whether the scrubber can
    // be trusted. A transcode is chunked with no byte ranges, so it cannot be
    // seeked at all — saying so beats a seek bar that silently does nothing.
    const transcoded = data.mode === "transcode";
    const deliveryNote = (direct ? "direct" : "proxied") +
      (transcoded ? " · 256k" : "");
    note.textContent = deliveryNote;

    // Plex knows the real duration even when the stream won't declare one.
    const known = data.duration ? data.duration / 1000 : NaN;

    // Where in the track this stream begins. Always 0 for direct audio, which
    // seeks natively by byte range. For a transcode there are no byte ranges,
    // so seeking forward means restarting the stream at a new offset — and
    // then element time is relative to it.
    let baseOffset = 0;
    let seeking = false;

    function total() {
      return isFinite(audio.duration) && audio.duration > 0 && !transcoded
        ? audio.duration : known;
    }
    function position() { return baseOffset + audio.currentTime; }

    // Whether `t` (absolute) is already downloaded, and so free to jump to.
    // Chrome keeps everything behind the playhead but only ~2.4s ahead, so in
    // practice this is true for every backward seek and false for a jump
    // forward — which is exactly the split we want.
    function isBuffered(t) {
      const rel = t - baseOffset;
      for (let i = 0; i < audio.buffered.length; i++) {
        if (rel >= audio.buffered.start(i) && rel <= audio.buffered.end(i)) return true;
      }
      return false;
    }

    function paint() {
      if (seeking) return;                  // don't fight the user's drag
      const dur = total();
      time.textContent = clockText(position()) + " / " + clockText(dur);
      if (isFinite(dur) && dur > 0) {
        seek.max = dur;
        seek.value = position();
        seek.disabled = false;
      }
    }

    // Restart a transcode at `t` seconds in. Costs a new Plex transcode
    // session and about a second of rebuffer, so it is the fallback, not the
    // mechanism — buffered seeks never come through here.
    async function restartAt(t) {
      const url = new URL(audio.src, location.origin);
      url.searchParams.set("offset", Math.floor(t));
      // Only our own route takes the preference; a direct Plex URL already
      // encodes the choice in which endpoint it points at.
      if (url.origin === location.origin) {
        url.searchParams.set("transcode", pref ? "1" : "0");
      }
      baseOffset = t;
      const wasPlaying = !audio.paused;
      audio.src = url.toString();
      // Signal with a class, not with text: the labels are flex: none, so any
      // extra word rewraps the whole control while you are looking at it.
      box.classList.add("seeking");
      try { if (wasPlaying) await audio.play(); } catch (err) { /* ignore */ }
      box.classList.remove("seeking");
      paint();
    }

    audio.addEventListener("loadedmetadata", paint);
    audio.addEventListener("timeupdate", paint);
    audio.addEventListener("ended", function () {
      toggle.textContent = "▶";
      if (trigger) trigger.classList.remove("playing");
    });
    audio.addEventListener("error", function () {
      what.textContent = "audition failed";
      note.textContent = direct
        ? "couldn't reach Plex from the browser" : "stream error";
      note.classList.add("warn");
      toggle.disabled = true;
    });
    // Dragging updates the readout only; the seek itself waits for release,
    // so scrubbing across a transcode doesn't spawn a Plex session per pixel.
    seek.addEventListener("input", function () {
      seeking = true;
      time.textContent = clockText(Number(seek.value)) + " / " + clockText(total());
    });
    seek.addEventListener("change", function () {
      seeking = false;
      const t = Number(seek.value);
      if (!transcoded || isBuffered(t)) {
        audio.currentTime = t - baseOffset;  // free: bytes are already here
        paint();
      } else {
        restartAt(t);                        // forward past the buffer
      }
    });
    toggle.addEventListener("click", function () {
      if (audio.paused) { audio.play(); toggle.textContent = "❚❚"; }
      else { audio.pause(); toggle.textContent = "▶"; }
    });

    toggle.disabled = false;
    toggle.textContent = "❚❚";
    paint();
    try { await audio.play(); } catch (err) { toggle.textContent = "▶"; }
  }

  // The row's current pick — the hidden input both single- and multi-candidate
  // rows carry, so this always follows the selection rather than the match.
  function rowPick(row) {
    const inp = row.querySelector('input[type=hidden][name^="pick_"]');
    return inp && inp.value;
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
    sel.addEventListener("change", function () {
      stopAudition();        // a re-match can rewrite the candidate playing
      rematchAlbum(sel);
    });
  });

  document.querySelectorAll("[data-poster-input]").forEach(function (input) {
    input.addEventListener("change", function () { onPosterPick(input); });
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

  // --- audition wiring -----------------------------------------------------
  // Audition the row's currently selected track.
  document.querySelectorAll("[data-aud-row]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const row = btn.closest("tr[data-rownum]");
      const key = row && rowPick(row);
      if (!key) return;
      if (btn.classList.contains("playing")) { stopAudition(); return; }
      mountAudition(row, key, true, btn);
    });
  });

  document.querySelectorAll("[data-aud-key]").forEach(wireDdAudition);

  // Settings: the audition quality toggle in the gear menu. Applies to the
  // next audition — an in-flight one is left alone rather than restarted
  // under the user.
  document.querySelectorAll("[data-setting-transcode]").forEach(function (cb) {
    cb.checked = transcodePref();
    cb.addEventListener("change", function () { setTranscodePref(cb.checked); });
  });

  // Warm the reachability verdict now rather than on the first click: an
  // unroutable Plex takes the full abort timeout to fail, and finding that out
  // mid-click would stall the first audition. Also the point at which an
  // ?audition= override is captured for the tab, so it survives the POSTs to
  // /preview and /create. Fire-and-forget — nothing waits on it.
  canReachPlex();

  // Escape stops. Not space — the preview page is full of checkboxes and
  // inputs where space already means something, so a global binding would
  // fight the page. The control says "ESC to stop" because nobody guesses it.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && auditionEl) stopAudition();
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
      // Both controls on one line; stacked, they make every row taller.
      const actions = el("div", "trackactions");
      // Audition a track already in the playlist — same roaming player the
      // preview rows and search results use, mounted under this row.
      const aud = el("button", "aud-btn", "▶");
      aud.type = "button";
      aud.title = "Audition this track";
      aud.setAttribute("aria-label", "Audition");
      aud.addEventListener("click", function () {
        if (aud.classList.contains("playing")) { stopAudition(); return; }
        mountAudition(tr, key, true, aud);
      });
      actions.appendChild(aud);
      const rm = el("button", "trackrm", "✕");
      rm.type = "button";
      rm.setAttribute("aria-label", "Remove track");
      rm.addEventListener("click", function () {
        stopAuditionFor(tr);   // the row is going away; so is anything under it
        picked.delete(key); tr.remove(); renumber(); refresh();
      });
      actions.appendChild(rm);
      rmTd.appendChild(actions);

      tr.addEventListener("dragstart", function () {
        stopAudition();        // reordering would strand the player mid-table
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
