"""Reads a box supplier's tax invoice PDF (Dealberg's, made in Zoho) into its header and line items.

The PDF has a text layer, so this reads that text; no OCR. Each line item comes out as

    1 DEL Supertails 15*15*15 Printed 5 Ply      <- the item number, then its description
     carton box                                   <- more of the description
     DBSUPTDEL006                                 <- the SKU
    481910 916.00 11.00 5% 503.80 10,076.00       <- HSN, qty, rate, then a % and an amount per tax, then the amount

with one tax (IGST, out of state) or two (CGST and SGST, in Karnataka, where Dealberg is). An invoice is only
returned if its lines add up to its Sub Total and, with tax, to its Total; otherwise it can't be trusted and
NotReadable says why, so nothing half-read is ever stored.
"""
import io
import re
from datetime import datetime
from decimal import Decimal

from pypdf import PdfReader

NUMBER = r"\d[\d,]*\.\d+"
# The HSN is missing from the row when it's 8 digits: it wraps in its narrow column ("4806200", "0") onto the
# lines above, so those digits-only lines at the end of the description are put back together.
ROW = re.compile(rf"^(?:(?P<hsn>\d{{4,8}})\s+)?(?P<qty>{NUMBER})\s+(?P<rate>{NUMBER})\s+"
                 rf"(?P<taxes>(?:\d+(?:\.\d+)?%\s+{NUMBER}\s+){{1,2}})(?P<amount>{NUMBER})$")
DIGITS = re.compile(r"^\d+$")
TAX = re.compile(rf"(\d+(?:\.\d+)?)%\s+({NUMBER})")
SKU = re.compile(r"^[A-Z][A-Z0-9_-]{5,}$")  # DBSUPTDEL006: capitals and digits, no spaces
PART = re.compile(r"^[A-Z0-9_-]+$")
ITEM_START = re.compile(r"^\d{1,3}\s+(?=\S)")
TABLE_START = re.compile(r"^Amount\s*%")  # the item table's header ends "Amount% Amt" (or "Amount% Amt % Amt")
TABLE_END = re.compile(r"^(Total In Words|Sub Total)\b")
ROUNDING = Decimal("0.05")  # how far the stated totals may be from the lines' sums


class NotReadable(ValueError):
    """Not an invoice this can read with confidence; the message says what was wrong."""


def _amount(text):
    return Decimal(text.replace(",", ""))


def _field(pattern, text, what):
    found = re.search(pattern, text, re.MULTILINE)
    if not found:
        raise NotReadable(f"no {what} found")
    return found.group(1).strip()


def _lines(text):
    """The line items: [{line, description, sku, hsn, qty, rate, tax_pct, tax_amount, amount, total}]."""
    items, block, in_table = [], [], False
    for raw in text.splitlines():
        line = raw.strip()
        if TABLE_START.match(line):
            in_table, block = True, []
            continue
        if not in_table or not line:
            continue
        if TABLE_END.match(line):
            in_table = False
            continue
        row = ROW.match(line)
        if not row:
            block.append(line)
            continue
        hsn = row["hsn"]
        if not hsn:
            wrapped = []
            while len(block) > 1 and DIGITS.match(block[-1]):
                wrapped.insert(0, block.pop())
            hsn = "".join(wrapped)
        if not block or not ITEM_START.match(block[0]):
            raise NotReadable(f"a line item's description is missing (before '{line}')")
        number, first = block[0].split(None, 1)
        words = [first, *block[1:]]
        sku = ""
        for k in (1, 2, 3):  # a long SKU can wrap too: "DBSUPTMU", "M012"
            tail = words[-k:]
            if len(words) > k and all(PART.match(w) for w in tail) and SKU.match("".join(tail)):
                sku, words = "".join(tail), words[:-k]
                break
        taxes = [(Decimal(pct), _amount(amt)) for pct, amt in TAX.findall(row["taxes"])]
        amount = _amount(row["amount"])
        tax_amount = sum((amt for _pct, amt in taxes), Decimal(0))
        items.append({
            "line": int(number),
            "description": re.sub(r"\s+", " ", " ".join(words)).strip(),
            "sku": sku,
            "hsn": hsn,
            "qty": _amount(row["qty"]),
            "rate": _amount(row["rate"]),
            "tax_pct": sum((pct for pct, _amt in taxes), Decimal(0)),
            "tax_amount": tax_amount,
            "amount": amount,
            "total": amount + tax_amount,
        })
        block = []
    return items


def read(data):
    """The invoice in `data` (a PDF's bytes): {invoice_number, invoice_date, supplier, supplier_gstin, sub_total,
    total, lines}. Raises NotReadable for anything else (a scan, another layout, totals that don't add up)."""
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        raise NotReadable("not a PDF that can be read") from e
    if "TAX INVOICE" not in text.upper():
        raise NotReadable("no text saying TAX INVOICE (a scan or photo can't be read)")

    number = _field(r"Invoice#\s*(\S+)", text, "invoice number")
    try:
        date = datetime.strptime(_field(r"Invoice Date\s*:\s*(\d{2}/\d{2}/\d{4})", text, "invoice date"), "%d/%m/%Y").date()
    except ValueError:
        raise NotReadable("the invoice date isn't a real date") from None
    supplier_gstin = _field(r"GSTIN\s+([0-9A-Z]{15})", text, "supplier GSTIN")  # the first is the supplier's
    supplier = next((ln.strip() for ln in text.splitlines() if re.search(r"[A-Za-z]{3}", ln)), "")
    sub_total = _amount(_field(rf"^Sub Total\s+({NUMBER})", text, "Sub Total"))
    total = _amount(_field(rf"^Total\s*₹?\s*({NUMBER})", text, "Total"))

    lines = _lines(text)
    if not lines:
        raise NotReadable("no line items found")
    if abs(sum(ln["amount"] for ln in lines) - sub_total) > ROUNDING:
        raise NotReadable(f"the line items don't add up to the Sub Total ({sub_total})")
    if abs(sum(ln["total"] for ln in lines) - total) > ROUNDING:
        raise NotReadable(f"the line items and tax don't add up to the Total ({total})")
    return {"invoice_number": number, "invoice_date": date, "supplier": supplier, "supplier_gstin": supplier_gstin,
            "sub_total": sub_total, "total": total, "lines": lines}
