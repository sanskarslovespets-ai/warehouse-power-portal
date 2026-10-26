"""The RTO MDND tab's input columns, joined onto the cached RTO-Delivered rows.

Used by the MDND table (app.py) and by its analytics (analytics_mdnd.py), so both read the
same rule: an AWB already logged in the RTO Process form counts as "Received and Damaged"
(with that entry's received date) and is locked; every other AWB shows whatever a person
has typed in so far.
"""

import entries_store

RECEIVED_AND_DAMAGED = "Received and Damaged"


def apply_inputs(rows):
    """Copies of `rows` with the input fields (and `locked`) added; the cached rows are never mutated."""
    logged = entries_store.entries_by_awb()
    saved = entries_store.get_mdnd_inputs()
    out = []
    for r in rows:
        row = dict(r)
        row.update({f: "" for f in entries_store.MDND_INPUT_FIELDS})
        row.update(saved.get(row["awb"], {}))
        entry = logged.get(row["awb"])
        row["locked"] = entry is not None
        if entry:
            row["received_at_wh"] = RECEIVED_AND_DAMAGED
            row["received_date"] = entry["received_date"]
        out.append(row)
    return out
