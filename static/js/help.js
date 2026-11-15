(function () {
  "use strict";

  // Help & FAQs (#panel-help): the sections are written in static/index.html. This
  // - hides the sections for panels someone can't open (a section's data-panel: one panel, or several separated by
  //   spaces for a section shared by them; none means everyone's),
  // - builds the topics list from the sections' data-group and h3,
  // - searches them: a section shows if it has every word typed, and the answers that match open,
  // - opens the help for the page someone is on, from the top bar's Help button.

  const panel = document.getElementById("panel-help");
  const navItem = document.querySelector('.side-nav-item[data-panel="help"]');
  if (!panel || !navItem) return;
  const sections = [...panel.querySelectorAll(".help-section")];
  const tocList = document.getElementById("help-toc-list");
  const search = document.getElementById("help-search");
  const noResults = document.getElementById("help-no-results");
  let current = document.querySelector(".side-nav-item.active:not([data-always])")?.dataset.panel || ""; // the page before Help

  const visible = (s) => !s.hidden && !s.classList.contains("help-filtered");

  function buildToc() {
    const groups = new Map();
    sections.filter((s) => !s.hidden).forEach((s) => {
      const g = s.dataset.group || "Help";
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(s);
    });
    tocList.replaceChildren(...[...groups].map(([name, list]) => {
      const box = document.createElement("div");
      box.className = "help-toc-group";
      const h = document.createElement("div");
      h.className = "help-toc-title";
      h.textContent = name;
      box.append(h, ...list.map((s) => {
        const a = document.createElement("a");
        a.href = "#" + s.id;
        a.className = "help-toc-link";
        a.dataset.target = s.id;
        a.textContent = s.querySelector("h3").firstChild.textContent.trim();
        a.addEventListener("click", (e) => {
          e.preventDefault();
          s.scrollIntoView({ behavior: "smooth", block: "start" });
        });
        return a;
      }));
      return box;
    }));
  }

  function applySearch() {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    sections.forEach((s) => {
      const match = !words.length || words.every((w) => s.textContent.toLowerCase().includes(w));
      s.classList.toggle("help-filtered", !match);
      s.querySelectorAll("details").forEach((d) => {
        d.open = words.length > 0 && words.every((w) => d.textContent.toLowerCase().includes(w));
      });
    });
    tocList.querySelectorAll(".help-toc-link").forEach((a) => {
      a.hidden = !visible(document.getElementById(a.dataset.target));
    });
    tocList.querySelectorAll(".help-toc-group").forEach((g) => {
      g.hidden = !g.querySelector(".help-toc-link:not([hidden])");
    });
    noResults.hidden = sections.some(visible);
  }

  // The top bar's Help: open Help at the page someone was on (the start, if it has no section of its own).
  function openHelpFor(page) {
    if (search.value) {
      search.value = "";
      applySearch();
    }
    navItem.click();
    const target = document.getElementById("help-" + page);
    if (target && visible(target)) target.scrollIntoView({ block: "start" });
    else window.scrollTo(0, 0);
  }

  document.querySelectorAll(".side-nav-item:not([data-always])").forEach((item) => {
    item.addEventListener("click", () => { current = item.dataset.panel; });
  });
  document.getElementById("btn-help").addEventListener("click", () => openHelpFor(current));
  search.addEventListener("input", applySearch);

  buildToc();
  window.PortalAccess.ready.then((access) => {
    sections.forEach((s) => {
      if (s.dataset.panel) s.hidden = !s.dataset.panel.split(/\s+/).some((p) => access.canOpen(p));
    });
    if (access.role === "viewer") panel.querySelectorAll(".help-editors").forEach((n) => n.classList.add("help-viewer-note"));
    buildToc();
    applySearch();
  });
})();
