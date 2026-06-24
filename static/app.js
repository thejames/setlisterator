// Manual override: search the Plex library for a missing song and pick a track
// to add to the playlist. Progressive enhancement — without JS, missing rows
// simply stay missing.
(function () {
  function makeButton(label, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "result";
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  async function runSearch(row) {
    const query = row.querySelector(".q").value.trim();
    const results = row.querySelector(".results");
    results.textContent = "";
    if (!query) return;
    results.textContent = "searching…";
    let data;
    try {
      const resp = await fetch("/search?q=" + encodeURIComponent(query));
      data = await resp.json();
    } catch (err) {
      results.textContent = "search failed";
      return;
    }
    if (data.error) { results.textContent = data.error; return; }
    if (!data.results || !data.results.length) {
      results.textContent = "no matches in your library";
      return;
    }
    results.textContent = "";
    data.results.forEach(function (t) {
      if (t.rating_key == null) return;
      const label = t.artist + " — " + t.title + (t.album ? " · " + t.album : "");
      results.appendChild(makeButton(label, function () { choose(row, t, label); }));
    });
  }

  function choose(row, track, label) {
    const pick = row.querySelector(".pick");
    const inc = row.querySelector(".inc");
    pick.value = track.rating_key;
    pick.disabled = false;
    inc.checked = true;
    inc.disabled = false;
    inc.hidden = false;
    row.querySelector(".chosen").textContent = "✓ " + label;
    row.querySelector(".searcher").hidden = true;
    row.querySelector(".results").textContent = "";
    const matchcell = row.querySelector(".matchcell");
    if (matchcell) matchcell.textContent = "manual";
    row.classList.remove("missing");
    row.classList.add("resolved");
  }

  // Loading feedback: a form with data-loading shows that label on its submit
  // button while the (slow, Plex-bound) request is in flight. Disabling after
  // submit also guards against double-clicks.
  document.querySelectorAll("form[data-loading]").forEach(function (form) {
    form.addEventListener("submit", function () {
      const btn = form.querySelector(
        "button[type=submit], button:not([type])");
      if (btn && !btn.disabled) {
        btn.dataset.label = btn.textContent;
        btn.textContent = form.dataset.loading;
        btn.disabled = true;
      }
    });
  });

  document.querySelectorAll("tr.missing").forEach(function (row) {
    const go = row.querySelector(".go");
    const q = row.querySelector(".q");
    if (go) go.addEventListener("click", function () { runSearch(row); });
    if (q) q.addEventListener("keydown", function (e) {
      // Enter inside the form would submit it — search instead.
      if (e.key === "Enter") { e.preventDefault(); runSearch(row); }
    });
  });
})();
