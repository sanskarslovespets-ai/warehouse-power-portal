(function () {
  "use strict";

  // Who is signed in (top bar), and Sign out. Loaded before the other scripts so that, once the
  // session runs out (auth.SESSION_HOURS) with the page still open, the next /api/ call's 401
  // takes the page back to the sign-in page instead of every panel showing "Please sign in."

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async function (...args) {
    const res = await nativeFetch(...args);
    if (res.status === 401) location.replace("/login");
    return res;
  };

  const who = document.getElementById("signed-in-as");
  nativeFetch("/api/me")
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => {
      if (!data || !data.username) return;
      who.textContent = data.username;
      who.title = "Signed in as " + data.username;
      who.hidden = false;
    })
    .catch(() => {});

  document.getElementById("btn-sign-out").addEventListener("click", async () => {
    try {
      await nativeFetch("/api/logout", { method: "POST" });
    } finally {
      location.replace("/login");
    }
  });
})();
