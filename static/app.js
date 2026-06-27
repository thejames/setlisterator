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

  // --- live selected count (action bar + Create button) --------------------
  function updateCount() {
    const n = document.querySelectorAll("input.inc:checked").length;
    const sel = document.querySelector("[data-selected]");
    const btn = document.querySelector("[data-create-count]");
    if (sel) sel.textContent = n;
    if (btn) btn.textContent = n;
  }

  // --- manual library search (shared by missing cards and matched rows) ----
  // `scope` is any element containing a `.q` input and a `.results` box; each
  // result button invokes onPick(track, label).
  async function doSearch(scope, onPick) {
    const query = scope.querySelector(".q").value.trim();
    const results = scope.querySelector(".results");
    results.textContent = "";
    if (!query) return;
    results.textContent = "searching…";
    let data;
    try {
      const resp = await fetch("/search?q=" + encodeURIComponent(query));
      data = await resp.json();
    } catch (err) { results.textContent = "search failed"; return; }
    if (data.error) { results.textContent = data.error; return; }
    if (!data.results || !data.results.length) {
      results.textContent = "no matches in your library"; return;
    }
    results.textContent = "";
    data.results.forEach(function (t) {
      if (t.rating_key == null) return;
      const label = t.artist + " — " + t.title + (t.album ? " · " + t.album : "");
      const b = el("button", "result", label);
      b.type = "button";
      b.addEventListener("click", function () { onPick(t, label); });
      results.appendChild(b);
    });
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
    updateCount();
  }
  // Re-point a matched row to an arbitrary library track found via search.
  function applyPick(pos, track, label) {
    const row = document.querySelector('tr[data-rownum="' + pos + '"]');
    if (!row) return;
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

  // Prefer-album select: re-match on change. No-JS users use the Apply button.
  // requestSubmit() fires the submit event so the data-loading spinner runs.
  document.querySelectorAll("[data-album-select]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      if (sel.form.requestSubmit) sel.form.requestSubmit();
      else sel.form.submit();
    });
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

  document.querySelectorAll("[data-accept]").forEach(function (b) {
    b.addEventListener("click", function () {
      const inc = incFor(b.dataset.accept);
      if (inc) inc.checked = true;
      const card = b.closest(".fuzzycard");
      if (card) card.classList.remove("rejected");
      updateCount();
    });
  });
  document.querySelectorAll("[data-reject]").forEach(function (b) {
    b.addEventListener("click", function () {
      const inc = incFor(b.dataset.reject);
      if (inc) inc.checked = false;
      const card = b.closest(".fuzzycard");
      if (card) card.classList.add("rejected");
      updateCount();
    });
  });

  const jsonBtn = document.querySelector("[data-json-toggle]");
  const jsonView = document.querySelector("[data-jsonview]");
  if (jsonBtn && jsonView) {
    jsonBtn.addEventListener("click", function () {
      if (jsonView.hidden) { jsonView.textContent = buildJSON(); jsonView.hidden = false; }
      else { jsonView.hidden = true; }
    });
  }

  document.querySelectorAll("input.inc").forEach(function (cb) {
    cb.addEventListener("change", updateCount);
  });
  updateCount();
})();
