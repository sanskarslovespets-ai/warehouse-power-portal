"""The Warehouse Exceptions page's data: runs its three exports and bundles what they write into one file.

Not a copy from upstream (see ../README.md): it does the job of upstream's refresh_warehouse_dashboard.py,
minus building the HTML page. Like that script, it runs the three exports one after another, each in its own
process, and stops at the first that fails, so the page is never half new and half old: breach_explorer.py
then keeps serving the previous bundle. The promise-engine cutoff behind packing pendency is not fatal here
either: export_warehouse_pendency.py falls back to 14:00 itself when that host can't be reached.

Writes data/warehouse_data.json: {"notfound": ..., "pendency": ..., "complaints": ...}, each exactly the
payload upstream injects into its page (static/js/wh_exceptions.js reads them the same way).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
OUT = os.path.join(DATA, "warehouse_data.json")

# (part of the bundle, export script, the file it writes in data/), in upstream's order
STEPS = [
    ("notfound", "export_warehouse_notfound.py", "notfound_payload.json"),
    ("pendency", "export_warehouse_pendency.py", "pendency_payload.json"),
    ("complaints", "export_warehouse_complaints.py", "complaints_payload.json"),
]


def main():
    bundle = {}
    for part, script, output in STEPS:
        path = os.path.join(DATA, output)
        if os.path.exists(path):
            os.remove(path)  # so an export that writes nothing can't pass off the last run's file as new
        print("Exporting %s..." % part, flush=True)
        run = subprocess.run([sys.executable, os.path.join(HERE, script)], capture_output=True, text=True)
        sys.stdout.write(run.stdout or "")
        if run.returncode != 0:
            sys.stderr.write(run.stderr or "")
            last = (run.stderr or "").strip().splitlines()[-1:] or ["exit code %d" % run.returncode]
            sys.exit("%s failed: %s" % (script, last[0]))  # breach_explorer.py shows this last line
        with open(path, encoding="utf-8") as f:
            bundle[part] = json.load(f)

    os.makedirs(DATA, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(bundle, f, separators=(",", ":"), ensure_ascii=True)
    print("wrote %s  %.0f KB" % (OUT, os.path.getsize(OUT) / 1024))


if __name__ == "__main__":
    main()
