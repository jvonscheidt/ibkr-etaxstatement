"""End-to-end regression test for the eCH-0270 barcode PDF.

Guards the three non-obvious, evidence-derived decisions in
generate_barcode_pdf (ZLIB not raw DEFLATE, right-to-left segment order,
native 1-px-per-module rendering): generate the PDF, then decode it back with
an *independent* stack (PyMuPDF render + zxing-cpp) and assert the payload
round-trips to exactly the source XML. A regression that flips any of those
(e.g. reverts to raw DEFLATE or reorders segments) fails here even though the
XSD/parsing tests stay green.

Skipped unless the full render+decode toolchain is installed.
"""

from __future__ import annotations

import zlib

import pytest

fitz = pytest.importorskip("fitz", reason="PyMuPDF required to render the PDF")
zxingcpp = pytest.importorskip("zxingcpp", reason="zxing-cpp required to decode PDF417")
pytest.importorskip("pdf417gen", reason="pdf417gen required to encode barcodes")
pytest.importorskip("reportlab", reason="reportlab required to draw the PDF")
pytest.importorskip("pypdf", reason="pypdf required for page rotation")
Image = pytest.importorskip("PIL.Image", reason="Pillow required to buffer images")

from src.parse_ibkr import parse
from src.generate_ech196 import build, serialize
from src.generate_barcode_pdf import generate_barcode_pdf

from .conftest import TAX_XML


def _decode_segments(pdf_path) -> list[bytes]:
    """Render every page and return decoded PDF417 payloads, rightmost-first.

    zxing-cpp reports symbols left-to-right; the eCH-0270 layout places
    segment 0 in the rightmost slot, so we sort each page's symbols by
    descending x to recover encode order before concatenating.
    """
    ordered: list[dict] = []
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=300)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            symbols = [
                r for r in zxingcpp.read_barcodes(img)
                if str(r.format) == "PDF417" and r.bytes
            ]
            symbols.sort(key=lambda r: r.position.top_left.x, reverse=True)
            ordered.extend(
                {"bytes": bytes(s.bytes), "file_id": s.extra.get("FileId")}
                for s in symbols
            )
    return ordered


def test_barcode_pdf_roundtrips_to_source_xml(tmp_path):
    if not TAX_XML.exists():
        pytest.skip(f"sample data missing: {TAX_XML}")

    xml_path = tmp_path / "out.xml"
    pdf_path = tmp_path / "out_barcode.pdf"

    root = build(parse(str(TAX_XML)), eur_chf_override=0.9311)
    xml_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + serialize(root),
        encoding="utf-8",
    )

    generate_barcode_pdf(xml_path, pdf_path)

    segments = _decode_segments(pdf_path)
    assert segments, "no PDF417 segments decoded from the generated PDF"

    payload = b"".join(s["bytes"] for s in segments)
    restored = zlib.decompress(payload)  # standard ZLIB; raw DEFLATE would raise

    expected = xml_path.read_bytes().replace(b"\r\n", b"\n")
    assert restored == expected

    # BEIL2 §2.2: PDFMacroFileId must be 4 integers. zxing renders it as the
    # four 3-digit codewords concatenated (12 digits), identical across all
    # segments. A single-codeword id (pdf417gen's default) is what ZHPrivateTax
    # rejects as "keine gültigen Daten", so guard the width and consistency.
    file_ids = {s["file_id"] for s in segments}
    assert len(file_ids) == 1, f"segments disagree on Macro file id: {file_ids}"
    (file_id,) = file_ids
    assert file_id is not None and len(file_id) == 12, (
        f"Macro file id must be 4 codewords (12 digits), got {file_id!r}"
    )


def test_pdf_has_portrait_statement_then_rotated_barcode_sheet(tmp_path):
    """Structure must be human-readable page(s) first, then rotated barcode
    sheet(s) — mirroring the reference eSteuerauszug. Checks page rotation
    metadata only (no barcode decode), so it's deterministic everywhere."""
    if not TAX_XML.exists():
        pytest.skip(f"sample data missing: {TAX_XML}")

    xml_path = tmp_path / "out.xml"
    pdf_path = tmp_path / "out_barcode.pdf"
    root = build(parse(str(TAX_XML)), eur_chf_override=0.9311)
    xml_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + serialize(root),
        encoding="utf-8",
    )
    generate_barcode_pdf(xml_path, pdf_path)

    with fitz.open(str(pdf_path)) as doc:
        rotations = [page.rotation for page in doc]

    assert len(rotations) >= 2, "expected at least one statement page and one barcode sheet"
    # Leading statement page(s) are portrait/unrotated; trailing barcode sheet(s)
    # carry /Rotate 90. Order matters: no rotated page may precede an unrotated one.
    assert rotations[0] == 0, f"first page must be an unrotated statement page, got {rotations}"
    assert rotations[-1] == 90, f"last page must be a rotated barcode sheet, got {rotations}"
    first_rotated = rotations.index(90)
    assert all(r == 0 for r in rotations[:first_rotated]), rotations
    assert all(r == 90 for r in rotations[first_rotated:]), rotations
