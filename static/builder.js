// Drag-to-reorder for the playlist builder. SortableJS handles the drag; the
// new order is POSTed via htmx, which swaps a freshly-numbered tracklist back
// (so data-index stays consistent for the next drag and for remove buttons).
(function () {
  function initSortable() {
    var list = document.getElementById("tracklist");
    if (!list || !window.Sortable) return;
    // htmx replaces the whole <ul> on each swap; the fresh node lacks this flag,
    // so we re-bind exactly once per node and never double-bind an existing one.
    if (list.dataset.sortableBound) return;
    list.dataset.sortableBound = "1";
    Sortable.create(list, {
      handle: ".drag-handle",
      animation: 150,
      onEnd: function () {
        var order = Array.prototype.map
          .call(list.children, function (li) { return li.getAttribute("data-index"); })
          .filter(function (v) { return v !== null; })
          .join(",");
        window.htmx.ajax("POST", list.getAttribute("data-reorder-url"), {
          values: { order: order },
          target: "#tracklist",
          swap: "outerHTML",
        });
      },
    });
  }

  document.addEventListener("DOMContentLoaded", initSortable);
  // Any swap that replaces the tracklist yields a fresh, unbound node → re-bind.
  document.body.addEventListener("htmx:afterSwap", initSortable);
})();
