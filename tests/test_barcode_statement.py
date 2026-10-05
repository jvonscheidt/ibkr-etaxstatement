"""Human-readable statement page of the eCH-0270 barcode PDF."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

pytest.importorskip("pdf417gen")
pytest.importorskip("reportlab")
pypdf = pytest.importorskip("pypdf")

from reportlab.lib.units import cm
from reportlab.pdfbase.pdfmetrics import stringWidth

from src.generate_barcode_pdf import (
    _printable,
    _statement_chunks,
    _truncated,
    generate_barcode_pdf,
)
from src.generate_ech196 import build, serialize


def _statement_text(data, tmp_path) -> str:
    xml_path = tmp_path / "statement.xml"
    pdf_path = tmp_path / "statement.pdf"
    xml_path.write_text(serialize(build(data)), encoding="utf-8")
    generate_barcode_pdf(xml_path, pdf_path)
    return pypdf.PdfReader(pdf_path).pages[0].extract_text()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Zürich Genève", "Zürich Genève"),  # cp1252 letters stay as they are
        ("Dvořák Łódź", "Dvorák Lódz"),  # ř, Ł, ź keep their base letter
        ("王", "?"),
    ],
)
def test_text_outside_helvetica_encoding_is_folded(text, expected):
    assert _printable(text) == expected


def test_long_names_are_truncated_to_their_column():
    name = "ISHARES CORE MSCI EMERGING MARKETS IMI UCITS ETF USD ACC"
    width = 7.2 * cm

    text = _truncated(name, "Helvetica", 8, width)

    assert text.endswith("…")
    assert stringWidth(text, "Helvetica", 8) <= width
    assert _truncated("EIMI", "Helvetica", 8, width) == "EIMI"


@pytest.mark.parametrize(
    ("rows", "pages"), [(0, [0]), (26, [26]), (27, [27, 0]), (31, [30, 1])]
)
def test_totals_get_room_on_the_last_page(rows, pages):
    chunks = _statement_chunks([{}] * rows)
    assert [len(chunk) for chunk in chunks] == pages


def test_statement_page_shows_folded_names_and_interest_totals(data, tmp_path):
    data.account = replace(data.account, first_name="Antonín", last_name="Dvořák")
    data.positions = [
        replace(
            data.positions[0],
            description="ISHARES CORE MSCI EMERGING MARKETS IMI UCITS ETF USD ACC",
        )
    ]
    interest = data.cash_transactions[0]
    data.cash_transactions.append(
        replace(interest, amount=-1.5, tx_type="Broker Interest Paid")
    )

    text = _statement_text(data, tmp_path)

    assert "Antonín Dvorák" in text
    assert "ISHARES CORE MSCI EMERGING" in text and "…" in text
    # Root B income includes the 2.85 account interest, now shown separately.
    assert "Bruttoertrag B Wertschriften CHF" in text
    for label, value in (
        ("Bruttoertrag B Kontozinsen CHF", "2.85"),
        ("Total Bruttoertrag B CHF", "2.85"),
        ("Schuldzinsen CHF", "1.50"),
    ):
        assert re.search(rf"{label}\s+{re.escape(value)}", text), label
