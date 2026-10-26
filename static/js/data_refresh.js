(function () {
  "use strict";

  // Top bar: when the dashboards' data was last pulled from the databases, when the next daily
  // auto-refresh is, and a Refresh from DB button for all of them at once.
  //
  // Whenever a cache gets new data - the morning run, this button, or a panel's own Refresh from DB -
  // the page fires a "panel:data-changed" event with the ids of the caches that changed
  // (rto_delivered, rto_mdnd, rto_recon), and each panel already opened reloads itself. So a page
  // left open overnight shows the 9:00 data without anyone reloading it.

  const IDLE_POLL_MS = 5 * 60 * 1000;
  const BUSY_POLL_MS = 4000;
  const NOTICE_MS = 6000;

  const label = document.getElementById("data-freshness");
  const btn = document.getElementById("btn-refresh-all");
  const btnLabel = btn.querySelector(".topbar-btn-label");

  let seen = null; // cache id -> fetched_at at the previous poll
  let pollTimer = null;

  function when(epoch) {
    return new Date(epoch * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  }

  function announce(sources) {
    if (sources.length) window.dispatchEvent(new CustomEvent("panel:data-changed", { detail: { sources } }));
  }

  function render(data) {
    const sources = data.sources;
    const busy = sources.some((s) => s.refreshing);
    const fetched = sources.map((s) => s.fetched_at).filter(Boolean);
    const next = "Next auto-refresh " + when(data.schedule.next_run);

    if (busy) {
      label.textContent = "Refreshing from the database… · " + next;
    } else if (fetched.length < sources.length) {
      label.textContent = "Loading data from the database for the first time…";
    } else {
      label.textContent = "Data as of " + when(Math.min(...fetched)) + " · " + next;
    }
    label.classList.remove("notice");
    label.classList.toggle("warn", sources.some((s) => s.error));
    label.title = sources
      .map((s) => `${s.label}: ${s.fetched_at ? when(s.fetched_at) : "not loaded yet"}${s.refreshing ? " (refreshing)" : ""}${s.error ? " - last refresh had a problem: " + s.error : ""}`)
      .concat(`Refreshes from the database every day at ${data.schedule.daily_at} ${data.schedule.timezone}.`)
      .join("\n");

    btn.disabled = busy;
    btn.classList.toggle("busy", busy);
    btnLabel.textContent = busy ? "Refreshing…" : "Refresh from DB";
    return busy || fetched.length < sources.length;
  }

  async function poll() {
    clearTimeout(pollTimer);
    let data;
    try {
      const res = await fetch("/api/data-status");
      data = await res.json();
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    } catch (e) {
      pollTimer = setTimeout(poll, IDLE_POLL_MS);
      return;
    }
    const busy = render(data);
    if (seen) announce(data.sources.filter((s) => s.fetched_at && s.fetched_at !== seen[s.id]).map((s) => s.id));
    seen = Object.fromEntries(data.sources.map((s) => [s.id, s.fetched_at]));
    pollTimer = setTimeout(poll, busy ? BUSY_POLL_MS : IDLE_POLL_MS);
  }

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    let data = {};
    let ok = false;
    try {
      const res = await fetch("/api/refresh-all", { method: "POST" });
      data = await res.json();
      ok = res.ok;
    } catch (e) {
      data = { error: "Could not reach the server." };
    }
    if (!ok) {
      // e.g. everything was refreshed a few minutes ago: say so for a moment, then show the state again
      label.textContent = data.error || "Could not start the refresh.";
      label.classList.add("notice");
      btn.disabled = false;
      clearTimeout(pollTimer);
      pollTimer = setTimeout(poll, NOTICE_MS);
      return;
    }
    announce(data.refreshing); // the open panels reload, show "Refreshing…" and follow it to the end
    poll();
  });

  poll();
})();
