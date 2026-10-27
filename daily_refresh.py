"""The daily refresh: every database-backed cache is re-pulled once a day, at DAILY_REFRESH_AT (IST).

Two things follow from that one time of day:
- A cache counts as stale once it was fetched before the most recent scheduled time (is_due). So
  data fetched at 9:04 stays as it is until 9:00 the next morning, and a server that was off or
  asleep at 9:00 catches up on its own: the first page view (or the server starting) after 9:00
  finds the caches due and refreshes them in the background, as it always has for a stale cache.
- start() runs a background thread that calls the given refresh at the scheduled time, so on an
  always-on server the new data is there at 9:00 whether or not anyone opens the panel.

"Refresh from DB" (main.py) still re-pulls any time in between.

DAILY_REFRESH_AT is HH:MM in India time (default 09:00). IST has no daylight saving, so a fixed
+05:30 offset is exact and needs no time zone database on the server.
"""

import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")
DEFAULT_TIME = "09:00"
CHECK_EVERY_SECONDS = 60  # short sleeps, so a clock change or a laptop waking up can't skip a day


def scheduled_time():
    """(hour, minute) from DAILY_REFRESH_AT; a malformed value fails loudly rather than never running."""
    value = os.environ.get("DAILY_REFRESH_AT", "").strip() or DEFAULT_TIME
    try:
        hour, minute = (int(part) for part in value.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        raise ValueError(f"DAILY_REFRESH_AT must be HH:MM (24-hour, IST), got {value!r}") from None
    return hour, minute


def last_run(now=None):
    """The most recent scheduled time at or before `now` (an aware datetime; default: now)."""
    now = (now or datetime.now(IST)).astimezone(IST)
    hour, minute = scheduled_time()
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if now >= today else today - timedelta(days=1)


def next_run(now=None):
    return last_run(now) + timedelta(days=1)


def is_due(fetched_at):
    """True when data fetched at `fetched_at` (epoch seconds; None = never fetched) predates the last scheduled time."""
    return fetched_at is None or fetched_at < last_run().timestamp()


def schedule_info():
    """What the page shows about the schedule."""
    hour, minute = scheduled_time()
    return {
        "daily_at": f"{hour:02d}:{minute:02d}",
        "timezone": "IST",
        "last_run": last_run().timestamp(),
        "next_run": next_run().timestamp(),
    }


def _loop(refresh):
    done = last_run()
    while True:
        time.sleep(CHECK_EVERY_SECONDS)
        due = last_run()
        if due > done:  # a scheduled time has passed since the last run
            done = due
            try:
                refresh()
            except Exception:
                traceback.print_exc()  # keep the schedule alive for tomorrow


def start(refresh):
    """Calls refresh() at every scheduled time from now on, on a background thread."""
    scheduled_time()  # check DAILY_REFRESH_AT now, at startup, not first at 9:00
    threading.Thread(target=_loop, args=(refresh,), daemon=True, name="daily-refresh").start()
