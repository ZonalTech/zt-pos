// Ajax pagination: clicking a pager link or changing "Rows per page" swaps only
// the table pane (#table-pane) instead of reloading the whole page (which would
// rebuild charts, refetch everything, and jump back to the top). The pane's
// contents — table + pager — are replaced from the same URL's response, scroll
// position is preserved, and the address bar is kept in sync.
(function () {
  const PANE = "table-pane";
  const pane = () => document.getElementById(PANE);
  if (!pane()) return;

  // The app scrolls inside .app-scroll, not the window — remember/restore it.
  const scroller = document.querySelector(".app-scroll") || document.scrollingElement;

  async function load(url, push) {
    const p = pane();
    const y = scroller ? scroller.scrollTop : 0;
    p.classList.add("pane-loading");
    let html;
    try {
      const res = await fetch(url, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" });
      html = await res.text();
    } catch (e) {
      location.href = url; // genuine network failure: navigate normally
      return;
    }
    const fresh = new DOMParser().parseFromString(html, "text/html").getElementById(PANE);
    const cur = pane();
    if (fresh && cur) {
      cur.innerHTML = fresh.innerHTML;
      cur.classList.remove("pane-loading");
      if (scroller) scroller.scrollTop = y; // stay where you were
    } else if (cur) {
      cur.classList.remove("pane-loading");
    }
    // History sync is best-effort — never let it trigger a reload.
    if (push) {
      try { history.pushState({ pane: true }, "", url); } catch (e) { /* ignore */ }
    }
  }

  // Pager Prev/Next (anchors with class .btn inside the pane).
  document.addEventListener("click", (e) => {
    const a = e.target.closest("#" + PANE + " a.btn");
    if (!a || a.classList.contains("disabled")) return;
    e.preventDefault();
    load(a.getAttribute("href"), true);
  });

  // Rows-per-page select inside the pane.
  document.addEventListener("change", (e) => {
    const sel = e.target.closest("#" + PANE + " select");
    if (!sel) return;
    const params = new URLSearchParams(location.search);
    params.set("per", sel.value);
    params.set("page", "1");
    load(location.pathname + "?" + params.toString(), true);
  });

  // Back/forward buttons.
  window.addEventListener("popstate", () => load(location.href, false));
})();
