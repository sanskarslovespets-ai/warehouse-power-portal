(function () {
  "use strict";

  // Users & access (admins only): everyone in scm.warehouse_users with their role and tabs, and the
  // form to add someone or change what they can open. users.py decides what each role may do, and
  // refuses what an admin shouldn't do (change their own role, delete themselves); this page shows it.

  const $ = (id) => document.getElementById(id);
  const ROLE_NAMES = { admin: "Admin", editor: "Editor", viewer: "Viewer" };
  const JSON_HEADERS = { "Content-Type": "application/json" };

  const tbody = $("users-tbody");
  const banner = $("banner-users");
  const overlay = $("user-modal");
  const form = $("user-form");
  const formError = $("user-form-error");
  const usernameInput = $("u-username");
  const passwordInput = $("u-password");
  const tabList = $("u-tab-list");
  const roleHint = $("u-role-hint");
  const deleteBtn = $("btn-delete-user");
  const saveBtn = $("btn-save-user");

  let tabs = []; // [{id, name, group}] in sidebar order, from /api/users
  let you = "";
  let editing = null; // the user being changed, or null while adding one

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function request(url, options) {
    const res = await fetch(url, options);
    let data = {};
    try {
      data = await res.json();
    } catch (e) {
      /* no body */
    }
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  function showBanner(message, kind) {
    banner.textContent = message;
    banner.className = "banner " + kind;
  }

  // ---------- the list ----------
  async function load(notice) {
    try {
      const data = await request("/api/users");
      tabs = data.tabs;
      you = data.you;
      render(data.users);
      if (notice) showBanner(notice, "success");
      else banner.className = "banner hidden";
    } catch (e) {
      showBanner(e.message, "error");
    }
  }

  function render(list) {
    tbody.replaceChildren(...list.map(row));
    $("users-empty").classList.toggle("hidden", list.length > 0);
  }

  function tabInfo(id) {
    return tabs.find((t) => t.id === id) || { id, name: id, group: "" };
  }

  // [{group, tabs}] in sidebar order: the sidebar section (RTO, Breach Explorer) each tab sits under
  function byGroup(list) {
    const groups = [];
    list.forEach((t) => {
      if (!groups.length || groups[groups.length - 1].group !== t.group) groups.push({ group: t.group, tabs: [] });
      groups[groups.length - 1].tabs.push(t);
    });
    return groups;
  }

  function row(user) {
    const tr = document.createElement("tr");

    const name = el("td", "user-name");
    name.append(el("strong", "", user.username));
    if (user.username === you) name.append(el("span", "you-tag", "You"));
    tr.append(name);

    const role = el("td");
    role.append(el("span", "role-badge " + (user.role || "none"), user.role ? ROLE_NAMES[user.role] : "No access yet"));
    tr.append(role);

    const access = el("td", "wrap tab-chips");
    if (user.role === "admin") access.append(el("span", "tab-chip all", "Every tab + Users"));
    else if (!user.tabs.length) access.append(el("span", "muted", "No tabs"));
    else {
      byGroup(user.tabs.map(tabInfo)).forEach((g) => {
        access.append(el("span", "tab-chip-group", g.group));
        g.tabs.forEach((t) => access.append(el("span", "tab-chip", t.name)));
      });
    }
    tr.append(access);

    let changed = "-";
    if (user.updated_at) {
      changed = new Date(user.updated_at * 1000).toLocaleString(undefined, {
        day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit",
      });
      if (user.updated_by) changed += " by " + user.updated_by;
    }
    tr.append(el("td", "muted", changed));

    const actions = el("td", "row-actions");
    const edit = el("button", "btn btn-secondary btn-sm", "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => openForm(user));
    actions.append(edit);
    tr.append(actions);
    return tr;
  }

  // ---------- the add / change form ----------
  function selectedRole() {
    const checked = form.querySelector('input[name="role"]:checked');
    return checked ? checked.value : "viewer";
  }

  // Admins see every tab: their boxes are all ticked and fixed.
  function matchTabsToRole() {
    const admin = selectedRole() === "admin";
    tabList.querySelectorAll("input").forEach((box) => {
      if (admin) box.checked = true;
      box.disabled = admin;
    });
    $("u-tabs-hint").textContent = admin ? "Admins see every tab, and this Users page." : "Tick the tabs they can open.";
  }

  // The message sits at the top of the form, which scrolls on a short window: scroll back up to it, or
  // Save user seems to do nothing while the reason is out of sight.
  function showFormError(message) {
    formError.textContent = message;
    formError.classList.remove("hidden");
    form.scrollTop = 0;
  }

  function openForm(user) {
    editing = user;
    const self = !!user && user.username === you;
    const fixed = !!user && user.fixed_admin;

    formError.classList.add("hidden");
    $("user-modal-title").textContent = user ? "Change " + user.username : "Add user";
    usernameInput.value = user ? user.username : "";
    usernameInput.disabled = !!user;
    $("u-username-hint").textContent = user
      ? "Usernames can't be changed."
      : "3 to 64 letters, digits, dots, dashes, underscores or @. It can't be changed later.";
    passwordInput.value = "";
    $("u-password-label").textContent = user ? "New password" : "Password";
    passwordInput.placeholder = user ? "Leave blank to keep their password" : "";
    $("u-password-hint").textContent = user
      ? "Only fill this in to give them a new password. Anyone signed in with the old one is signed out."
      : "At least 8 characters. Pass it on to them yourself.";

    const role = user ? user.role || "viewer" : "viewer";
    form.querySelectorAll('input[name="role"]').forEach((radio) => {
      radio.checked = radio.value === role;
      radio.disabled = self || fixed;
    });
    roleHint.textContent = fixed
      ? "Always an admin: set by ADMIN_USERS on the server."
      : self
        ? "You can't change your own role. Another admin can."
        : "";
    roleHint.classList.toggle("hidden", !roleHint.textContent);

    tabList.replaceChildren(
      ...byGroup(tabs).flatMap((g) => [
        el("p", "tab-group-name", g.group),
        ...g.tabs.map((t) => {
          const box = document.createElement("input");
          box.type = "checkbox";
          box.value = t.id;
          box.checked = !!user && user.tabs.includes(t.id);
          const option = el("label", "tab-option");
          option.append(box, el("span", "", t.name));
          return option;
        }),
      ])
    );
    matchTabsToRole();

    deleteBtn.classList.toggle("hidden", !user || self || fixed);
    overlay.classList.remove("hidden");
    (user ? passwordInput : usernameInput).focus();
  }

  function closeForm() {
    overlay.classList.add("hidden");
    editing = null;
  }

  form.addEventListener("change", (e) => {
    if (e.target.name === "role") matchTabsToRole();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      username: usernameInput.value.trim(),
      password: passwordInput.value,
      role: selectedRole(),
      tabs: [...tabList.querySelectorAll("input:checked")].map((box) => box.value),
    };
    if (!editing && (!body.username || !body.password)) {
      showFormError("Enter a username and a password.");
      return;
    }
    formError.classList.add("hidden");
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
    const adding = !editing;
    const name = adding ? body.username : editing.username;
    try {
      if (adding) {
        await request("/api/users", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) });
      } else {
        await request("/api/users/" + encodeURIComponent(name), { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(body) });
      }
      closeForm();
      await load(adding ? `Added ${name}.` : `Saved ${name}. If they're signed in, the change reaches them within a minute.`);
    } catch (err) {
      showFormError(err.message);
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save user";
    }
  });

  deleteBtn.addEventListener("click", async () => {
    if (!editing) return;
    const name = editing.username;
    if (!confirm(`Delete ${name}? They won't be able to sign in any more.`)) return;
    deleteBtn.disabled = true;
    try {
      await request("/api/users/" + encodeURIComponent(name), { method: "DELETE" });
      closeForm();
      await load(`Deleted ${name}.`);
    } catch (err) {
      showFormError(err.message);
    } finally {
      deleteBtn.disabled = false;
    }
  });

  $("btn-add-user").addEventListener("click", () => openForm(null));
  $("btn-close-user-modal").addEventListener("click", closeForm);
  $("btn-cancel-user").addEventListener("click", closeForm);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeForm();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !overlay.classList.contains("hidden")) closeForm();
  });

  // Loaded each time the panel is opened, so it shows changes other admins made.
  document.querySelector('.side-nav-item[data-panel="users"]').addEventListener("click", () => load());
})();
