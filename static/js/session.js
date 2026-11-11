(function () {
  "use strict";

  // Who is signed in and what they may open (/api/me, see users.py): their name and Sign out in the
  // top bar, and only their tabs in the sidebar. Loaded before the other scripts so that:
  // - window.PortalAccess.ready is there for them to wait on before loading a tab's data;
  // - once the session ends (unused for auth.SESSION_HOURS, a password change) or their access is taken
  //   away, the next /api/ call's 401 takes the page back to the sign-in page, which says they were
  //   signed out (?expired=1), instead of every panel showing an error.
  // The server checks every request itself; this only makes the page fit the person using it.

  const ROLE_NAMES = { admin: "Admin", editor: "Editor", viewer: "Viewer" };

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async function (...args) {
    const res = await nativeFetch(...args);
    if (res.status === 401) location.replace("/login?expired=1");
    return res;
  };

  function showNoAccess(message) {
    document.querySelectorAll(".side-nav-item").forEach((i) => i.classList.remove("active"));
    document.querySelectorAll(".side-panel").forEach((p) => p.classList.toggle("active", p.id === "panel-no-access"));
    if (message) document.getElementById("no-access-text").textContent = message;
  }

  function apply(me) {
    const panels = new Set(me.panels);
    document.body.dataset.role = me.role; // shows the page (style.css) and hides .edit-only buttons from viewers

    document.querySelectorAll(".side-nav-item[data-panel]").forEach((item) => {
      item.hidden = !panels.has(item.dataset.panel);
    });
    document.querySelectorAll(".nav-group").forEach((group) => {
      group.hidden = !group.querySelector(".side-nav-item:not([hidden])");
    });
    // The page opens on Analytics; someone without it starts on their first tab instead. The click waits
    // for the page's other scripts: a tab loads its data when its sidebar item is clicked, so a click
    // before its script has run (/api/me can answer while they are still loading) would leave it blank.
    const active = document.querySelector(".side-nav-item.active");
    if (!active || active.hidden) {
      const first = document.querySelector(".side-nav-item:not([hidden])");
      if (!first) showNoAccess();
      else if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => first.click(), { once: true });
      else first.click();
    }

    const who = document.getElementById("signed-in-as");
    who.textContent = me.username;
    who.title = `Signed in as ${me.username} (${ROLE_NAMES[me.role]})`;
    who.hidden = false;

    return {
      username: me.username,
      role: me.role,
      canOpen: (panel) => panels.has(panel),
    };
  }

  window.PortalAccess = {
    ready: fetch("/api/me")
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`Request failed (${res.status})`))))
      .then(apply)
      .catch(() => {
        document.body.dataset.role = "none";
        showNoAccess("Could not load your access. Refresh the page to try again.");
        return { username: "", role: "none", canOpen: () => false };
      }),
  };

  document.getElementById("btn-sign-out").addEventListener("click", async () => {
    try {
      await nativeFetch("/api/logout", { method: "POST" });
    } finally {
      location.replace("/login");
    }
  });
})();
