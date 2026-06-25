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

  // --- manual library search (missing cards) -------------------------------
  async function runSearch(card) {
    const query = card.querySelector(".q").value.trim();
    const results = card.querySelector(".results");
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
      b.addEventListener("click", function () { chooseMissing(card, t, label); });
      results.appendChild(b);
    });
  }
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
    if (go) go.addEventListener("click", function () { runSearch(card); });
    if (q) q.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); runSearch(card); }
    });
    if (skip) skip.addEventListener("click", function () {
      const inc = card.querySelector(".inc");
      if (inc) inc.checked = false;
      card.classList.add("skipped");
      updateCount();
    });
  });

  document.querySelectorAll("[data-dd-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      const dd = btn.closest("[data-dd]");
      const menu = dd.querySelector(".dd-menu");
      const opening = menu.hidden;
      closeDropdowns(dd);
      menu.hidden = !opening;
      btn.classList.toggle("open", opening);
    });
  });
  document.querySelectorAll(".dd-opt").forEach(function (opt) {
    opt.addEventListener("click", function () {
      const dd = opt.closest("[data-dd]");
      dd.querySelector("input[type=hidden]").value = opt.dataset.key;
      dd.querySelector("[data-dd-title]").textContent = opt.dataset.title;
      dd.querySelector("[data-dd-sub]").textContent = opt.dataset.sub;
      dd.querySelectorAll(".dd-opt").forEach(function (o) { o.classList.remove("sel"); });
      opt.classList.add("sel");
      dd.querySelector(".dd-menu").hidden = true;
      dd.querySelector(".dd-btn").classList.remove("open");
    });
  });
  document.addEventListener("click", function () { closeDropdowns(null); });

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
