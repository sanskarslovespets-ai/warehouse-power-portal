(function () {
  "use strict";

  // The sign-in page (static/login.html). /api/login checks the username and password against
  // scm.warehouse_users and sets the session cookie; then the portal opens.

  const form = document.getElementById("login-form");
  const username = document.getElementById("login-username");
  const password = document.getElementById("login-password");
  const error = document.getElementById("login-error");
  const btn = document.getElementById("btn-login");
  const btnLabel = document.getElementById("btn-login-label");
  const toggle = document.getElementById("btn-toggle-password");
  const caps = document.getElementById("login-caps");

  function showError(message) {
    error.textContent = message;
    error.classList.add("hidden");
    void error.offsetWidth; // restart the shake, so a second wrong try is noticed too
    error.classList.remove("hidden");
  }

  function setBusy(busy) {
    btn.disabled = busy;
    btn.classList.toggle("busy", busy);
    btnLabel.textContent = busy ? "Signing in…" : "Sign in";
  }

  // The eye button: shows the password as typed, so a long one can be checked before signing in.
  toggle.addEventListener("click", () => {
    const show = password.type === "password";
    password.type = show ? "text" : "password";
    const label = show ? "Hide password" : "Show password";
    toggle.setAttribute("aria-pressed", String(show));
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
    password.focus();
  });

  // Passwords are case-sensitive, so say so while Caps Lock is on.
  function checkCaps(e) {
    const on = e.getModifierState && e.getModifierState("CapsLock");
    caps.textContent = on ? "Caps Lock is on. Passwords are case-sensitive." : "";
  }
  password.addEventListener("keydown", checkCaps);
  password.addEventListener("keyup", checkCaps);
  password.addEventListener("blur", () => {
    caps.textContent = "";
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!username.value.trim() || !password.value) {
      showError("Enter your username and password.");
      (username.value.trim() ? password : username).focus();
      return;
    }

    setBusy(true);
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.value, password: password.value }),
      });
      if (res.ok) {
        location.replace("/");
        return;
      }
      let data = {};
      try {
        data = await res.json();
      } catch (err) {
        /* no body */
      }
      showError(data.error || `Sign-in failed (${res.status})`);
      password.value = "";
      password.focus();
    } catch (err) {
      showError("Could not reach the server. Please try again.");
    }
    setBusy(false);
  });
})();
