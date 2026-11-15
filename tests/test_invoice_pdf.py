"""Reading a box supplier's PDF tax invoice (invoice_pdf.py). The PDFs are built here (pdf_fixture.make_pdf) with
the text the supplier's real invoices give back: IGST and CGST + SGST, wrapped 8-digit HSN codes and wrapped
SKUs, all seen in their October 2026 invoices."""
from datetime import date
from decimal import Decimal

import pytest

import invoice_pdf
from pdf_fixture import make_pdf

HEAD = [
    "1", "DEALBERG TECHNOLOGIES PVT LTD", "Company ID :  U51909KA2016PTC093363", "Bangalore Karnataka 560102",
    "GSTIN 29AAFCD7015E1ZT", "TAX INVOICE", " Invoice# INV-20262642", "  Invoice Date : 01/10/2026",
    "  Terms : Net 30", "Bill To Ship To", "Supertails- Pets Centric Private Limited", "GSTIN 06AALCP9335K2ZU",
]


def invoice(items, header, totals, head=HEAD):
    return make_pdf([*head, *header, *items, "Total In Words", "Indian Rupee ... Only", *totals,
                     "Terms & Conditions", "Authorized Signature"])


IGST_HEADER = ["# Item & Description HSN/SAC Qty Rate", "IGST", "Amount% Amt"]
GST_HEADER = ["# Item & Description", "HSN", "/SAC Qty Rate", "CGST SGST", "Amount% Amt % Amt"]


def test_an_out_of_state_invoice_with_igst():
    found = invoice_pdf.read(invoice(
        ["1 DEL Supertails 15*15*15 Printed 5 Ply ", " carton box", " DBSUPTDEL006",
         "481910 916.00 11.00 5% 503.80 10,076.00"],
        IGST_HEADER, ["Sub Total 10,076.00", "IGST5 (5%) 503.80", "Total 10,579.80", "Balance Due 10,579.80"]))
    assert {k: v for k, v in found.items() if k != "lines"} == {
        "invoice_number": "INV-20262642", "invoice_date": date(2026, 10, 1), "supplier": "DEALBERG TECHNOLOGIES PVT LTD",
        "supplier_gstin": "29AAFCD7015E1ZT", "sub_total": Decimal("10076.00"), "total": Decimal("10579.80")}
    assert found["lines"] == [{
        "line": 1, "description": "DEL Supertails 15*15*15 Printed 5 Ply carton box", "sku": "DBSUPTDEL006",
        "hsn": "481910", "qty": Decimal("916.00"), "rate": Decimal("11.00"), "tax_pct": Decimal("5"),
        "tax_amount": Decimal("503.80"), "amount": Decimal("10076.00"), "total": Decimal("10579.80")}]


def test_a_karnataka_invoice_adds_cgst_and_sgst_and_reads_every_line():
    found = invoice_pdf.read(invoice(
        ["1 BLR Supertails 30*20*10 ", " Printed 5 Ply carton box", " DBSUPTBLR008",
         "481910 3,126.00 21.25 2.5% 1,660.69 2.5% 1,660.69 66,427.50",
         "2 BLR Supertails 15*15*15 ", " Printed 5 Ply carton box", " DBSUPTBLR010",
         "481910 100.00 10.00 2.5% 25.00 2.5% 25.00 1,000.00"],
        GST_HEADER, ["Sub Total 67,427.50", "CGST2.5 (2.5%) 1,685.69", "SGST2.5 (2.5%) 1,685.69", "Total 70,798.88"]))
    first, second = found["lines"]
    assert (first["sku"], first["qty"], first["tax_pct"], first["tax_amount"], first["total"]) == (
        "DBSUPTBLR008", Decimal("3126.00"), Decimal("5.0"), Decimal("3321.38"), Decimal("69748.88"))
    assert (second["line"], second["sku"], second["description"]) == (2, "DBSUPTBLR010", "BLR Supertails 15*15*15 Printed 5 Ply carton box")


def test_an_eight_digit_hsn_that_wrapped_onto_the_lines_above_is_put_back_together():
    found = invoice_pdf.read(invoice(
        ["1  White Shredded Papers", " DBSTAILS0048", "4806200", "0",
         "500.00 27.00 2.5% 337.50 2.5% 337.50 13,500.00"],
        GST_HEADER, ["Sub Total 13,500.00", "CGST2.5 (2.5%) 337.50", "SGST2.5 (2.5%) 337.50", "Total 14,175.00"]))
    assert [(ln["description"], ln["sku"], ln["hsn"], ln["qty"]) for ln in found["lines"]] == [
        ("White Shredded Papers", "DBSTAILS0048", "48062000", Decimal("500.00"))]


def test_a_long_sku_that_wrapped_is_put_back_together():
    found = invoice_pdf.read(invoice(
        ["1 MUM Supertails 75*50*20 Printed ", " 5 Ply carton box", "DBSUPTMU", "M012",
         "48191010 150.00 109.74 5% 823.05 16,461.00"],
        IGST_HEADER, ["Sub Total 16,461.00", "IGST5 (5%) 823.05", "Total 17,284.05"]))
    assert [(ln["description"], ln["sku"]) for ln in found["lines"]] == [
        ("MUM Supertails 75*50*20 Printed 5 Ply carton box", "DBSUPTMUM012")]


@pytest.mark.parametrize("pdf, says", [
    (invoice(["1 DEL box", " DBSUPTDEL006", "481910 916.00 11.00 5% 503.80 10,076.00"], IGST_HEADER,
             ["Sub Total 10,000.00", "Total 10,503.80"]), "don't add up to the Sub Total"),
    (invoice(["1 DEL box", " DBSUPTDEL006", "481910 916.00 11.00 5% 503.80 10,076.00"], IGST_HEADER,
             ["Sub Total 10,076.00", "Total 10,079.80"]), "don't add up to the Total"),
    (invoice([], IGST_HEADER, ["Sub Total 0.00", "Total 0.00"]), "no line items found"),
    (invoice(["1 DEL box", "481910 916.00 11.00 5% 503.80 10,076.00"], IGST_HEADER,
             ["Sub Total 10,076.00", "Total 10,579.80"], head=[ln for ln in HEAD if "Invoice#" not in ln]),
     "no invoice number"),
    (make_pdf(["Delivery challan", "Nothing to see here"]), "no text saying TAX INVOICE"),
    (make_pdf([]), "no text saying TAX INVOICE"),
    (b"not a pdf at all", "not a PDF that can be read"),
])
def test_anything_it_cant_read_with_confidence_is_refused_saying_why(pdf, says):
    with pytest.raises(invoice_pdf.NotReadable, match=says):
        invoice_pdf.read(pdf)
