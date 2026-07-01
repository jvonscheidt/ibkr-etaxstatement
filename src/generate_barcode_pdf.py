"""
Generate an eCH-0270 compliant barcode PDF from an eCH-196 XML file.

Layout (per eCH-0270 v1.0.0):
  - Human-readable Wertschriftenverzeichnis page(s) first (portrait, form 197)
  - 2D-barcode sheet(s) after them (landscape via /Rotate 90, form 196)
  - 1D barcode (Code 128, 16-digit) on every page
  - 2D barcodes (PDF417 Structured Append / Macro) on the barcode sheets
    * 13 cols, 35 rows, EC level 4, ZLIB-compressed XML
    * Up to 6 segments per page, right-aligned (segment 0 = rightmost slot),
      rotated 90° CW
    * Extra gap between segments 3 and 4 (fold compensation)
"""

from __future__ import annotations

import hashlib
import io
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image
from pdf417gen import render_image, encode_macro
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import cm, mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode.code128 import Code128

from .generate_ech196 import IBKR_CLEARING_NUMBER, NS

# ── eCH-0270 constants ──────────────────────────────────────────────────────
COLS = 13
ROWS = 35
EC_LEVEL = 4
BARCODES_PER_PAGE = 6

# Print-scale correction: PDFs are printed at 97 %, scale up by 1/0.97
_SCALE = 1.0 / 0.97

# Element dimensions (PDF, after scale correction)
_EL_W_CM = 0.042 * _SCALE   # width per module column  ≈ 0.0433 cm
_EL_H_CM = 0.080 * _SCALE   # height per row            ≈ 0.0825 cm

# Natural barcode size (290 modules wide × 35 rows tall)
_BC_W_CM = 290 * _EL_W_CM   # ≈ 12.56 cm
_BC_H_CM = ROWS * _EL_H_CM  # ≈  2.89 cm

# After 90° CW rotation: width ↔ height
_BC_ROT_W_CM = _BC_H_CM     # ≈  2.89 cm
_BC_ROT_H_CM = _BC_W_CM     # ≈ 12.56 cm

# Page margins
_TOP_CM = 5.0
_SIDE_CM = 2.0
_BOT_CM = 2.0
_GAP_CM = 1.0          # minimum gap between segments
_FOLD_EXTRA_CM = 0.5   # extra gap between segments 3 and 4

# Code 128 (1D barcode)
# The reference eSteuerauszug (data/example_etax.pdf) distinguishes its page
# types by the leading 3-digit form number: 196 on the 2D-barcode sheet, 197 on
# the human-readable pages. eCH-0270 §2.1 fixes only the first 3 digits (196 for
# eCH-0196) and leaves the remaining 13 issuer-defined; we mirror the reference
# so ZHPrivateTax's scanner can tell barcode sheets from statement pages.
_CODE128_FORM_2D   = "196"  # 2D-barcode sheet (eCH-0196)
_CODE128_FORM_TEXT = "197"  # human-readable statement page (no 2D barcode)
_CODE128_VER  = "22"    # version 2.2
_CODE128_ORG  = IBKR_CLEARING_NUMBER  # Interactive Brokers clearing number
_CODE128_ORI  = "0"     # orientation: 0 = landscape (Querformat)
_CODE128_DIR  = "1"     # Pos ID: 1 = top/bottom, left-to-right


def _barcode_images(segments: list) -> list[Image.Image]:
    """Render PDF417 segment codes to native-resolution PIL images (no rotation).

    eCH-0270 §2.2.3: barcode images have "eine Auflösung von 290 x 35 Pixel"
    (one pixel per module/row) — scaling to the target 0.042cm/0.08cm element
    size happens via the PDF's own image placement (drawImage width/height),
    not by baking supersampling into the PNG. A real reference eSteuerauszug
    PDF (data/example_etax.pdf) was inspected directly and confirmed its
    embedded barcode images are exactly 290x35 pixels — a prior version of
    this code rendered at 3x that resolution (870x315), which likely broke
    decoders that read the image at a fixed 1-pixel-per-module assumption.
    The visual 90° CW rotation is applied via a PDF CTM transform when drawing.
    """
    images = []
    for codes in segments:
        img = render_image(codes, scale=1, ratio=1, padding=0)
        images.append(img)  # native 290×35 — no PIL rotation
    return images


def _code128_value(page_num: int, has_2d: bool, form: str) -> str:
    """Build the 16-digit Code 128 payload per BEIL2 §2.4.
    Format: Form(3) + Version(2) + Clearing(5) + Page(3) + 2D_flag(1) + Orientation(1) + PosID(1)
    """
    flag = "1" if has_2d else "0"
    value = form + _CODE128_VER + _CODE128_ORG + f"{page_num:03d}" + flag + _CODE128_ORI + _CODE128_DIR
    assert len(value) == 16, f"Code128 payload length != 16: {value!r}"
    return value


def _draw_code128(c: canvas.Canvas, page_w_cm: float, page_h_cm: float,
                  page_num: int, has_2d: bool, form: str) -> None:
    """Draw Code 128 at top-left, 10 mm from left edge, within 30 mm from top."""
    value = _code128_value(page_num, has_2d, form)
    bc = Code128(
        value,
        barWidth=0.3 * mm,
        barHeight=7 * mm,
        humanReadable=True,
        fontSize=7,
        textColor="black",
    )
    x = 10 * mm
    # Place top of barcode at 10 mm from page top
    barcode_total_h = 7 * mm + 2 * mm + 3 * mm  # bar + gap + text = 12 mm
    y = page_h_cm * cm - 10 * mm - barcode_total_h
    bc.drawOn(c, x, y)


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _statement_securities(root: ET.Element) -> list[dict]:
    """Extract the per-security holdings for the human-readable statement."""
    rows = []
    for sec in root.iter(_q("security")):
        tv = sec.find(_q("taxValue"))
        rows.append({
            "name": sec.get("securityName", ""),
            "isin": sec.get("isin", ""),
            "ccy": sec.get("currency", ""),
            "qty": (tv.get("quantity", "") if tv is not None else ""),
            "value": (tv.get("value", "") if tv is not None else ""),
        })
    return rows


# Portrait statement-page layout (points; reportlab origin = bottom-left)
_ST_ROWS_PER_PAGE = 30
_ST_COLS = (  # (label, x in cm from left, right-aligned?)
    ("Pos", 2.0, False),
    ("Bezeichnung", 3.0, False),
    ("ISIN", 10.5, False),
    ("Whrg", 14.0, False),
    ("Anzahl", 16.2, True),
    ("Steuerwert CHF", 19.0, True),
)


def _draw_statement_pages(c: canvas.Canvas, root: ET.Element,
                          page_num_start: int) -> int:
    """Draw human-readable Wertschriftenverzeichnis page(s), 1D barcode each.

    Returns the number of pages drawn. These are normal portrait pages (no
    /Rotate) placed before the 2D-barcode sheet(s), matching the reference
    eSteuerauszug's structure (text pages + a separate barcode sheet). Each
    page carries a Code 128 with form 197 and the 2D flag cleared, so the
    scanner treats it as a statement page rather than a barcode sheet.
    """
    w_pt, h_pt = A4  # portrait
    page_h_cm = h_pt / cm
    page_w_cm = w_pt / cm

    inst = root.find(_q("institution"))
    client = root.find(_q("client"))
    inst_name = inst.get("name", "") if inst is not None else ""
    client_name = ""
    client_no = ""
    if client is not None:
        client_name = f"{client.get('firstName', '')} {client.get('lastName', '')}".strip()
        client_no = client.get("clientNumber", "")

    secs = _statement_securities(root)
    n_pages = max(1, (len(secs) + _ST_ROWS_PER_PAGE - 1) // _ST_ROWS_PER_PAGE)

    page_num = page_num_start
    for pi in range(n_pages):
        _draw_code128(c, page_w_cm, page_h_cm, page_num,
                      has_2d=False, form=_CODE128_FORM_TEXT)

        y = h_pt - 3.8 * cm  # below the top 1D barcode
        c.setFont("Helvetica-Bold", 14)
        c.drawString(2 * cm, y, "E-Steuerauszug – Wertschriftenverzeichnis")
        y -= 0.9 * cm

        c.setFont("Helvetica", 9)
        for line in (
            f"Institut: {inst_name}",
            f"Kunde: {client_name}   Kundennummer: {client_no}",
            f"Steuerperiode: {root.get('taxPeriod', '')}   Kanton: {root.get('canton', '')}",
            f"Dokument-ID: {root.get('id', '')}",
        ):
            c.drawString(2 * cm, y, line)
            y -= 0.5 * cm

        y -= 0.3 * cm
        c.setFont("Helvetica-Bold", 8)
        for label, x_cm, right in _ST_COLS:
            if right:
                c.drawRightString(x_cm * cm, y, label)
            else:
                c.drawString(x_cm * cm, y, label)
        y -= 0.15 * cm
        c.line(2 * cm, y, page_w_cm * cm - 2 * cm, y)
        y -= 0.5 * cm

        chunk = secs[pi * _ST_ROWS_PER_PAGE : (pi + 1) * _ST_ROWS_PER_PAGE]
        c.setFont("Helvetica", 8)
        for i, s in enumerate(chunk, start=pi * _ST_ROWS_PER_PAGE + 1):
            cells = [str(i), s["name"][:48], s["isin"], s["ccy"], s["qty"], s["value"]]
            for (label, x_cm, right), text in zip(_ST_COLS, cells):
                if right:
                    c.drawRightString(x_cm * cm, y, text)
                else:
                    c.drawString(x_cm * cm, y, text)
            y -= 0.5 * cm

        # Totals on the last statement page
        if pi == n_pages - 1:
            y -= 0.2 * cm
            c.line(2 * cm, y, page_w_cm * cm - 2 * cm, y)
            y -= 0.55 * cm
            c.setFont("Helvetica-Bold", 8)
            c.drawString(3 * cm, y, "Total Steuerwert CHF")
            c.drawRightString(19.0 * cm, y, root.get("totalTaxValue", ""))
            y -= 0.5 * cm
            c.setFont("Helvetica", 8)
            c.drawString(3 * cm, y, "Total Bruttoertrag B (DA-1) CHF")
            c.drawRightString(19.0 * cm, y, root.get("totalGrossRevenueB", ""))
            y -= 0.45 * cm
            c.drawString(3 * cm, y, "Total anrechenbare Quellensteuer CHF")
            c.drawRightString(19.0 * cm, y, root.get("totalWithHoldingTaxClaim", ""))

        c.showPage()
        page_num += 1

    return n_pages


def _draw_barcode_page(c: canvas.Canvas, page_w_cm: float, page_h_cm: float,
                        portrait_w_pt: float, images: list[Image.Image], page_num: int) -> None:
    """Draw up to BARCODES_PER_PAGE rotated PDF417 images on a landscape page.

    Segment order runs right-to-left: the first (lowest-index) Macro PDF417
    segment goes in the rightmost slot, matching a real reference eSteuerauszug
    (data/example_etax.pdf) whose barcode payload only decompressed correctly
    when its rightmost-positioned segment was treated as segment 0.

    The underlying PDF page is portrait with /Rotate 90 set (see
    generate_barcode_pdf()), matching that same reference file's actual page
    structure — landscape MediaBox pages with no /Rotate flag (a prior version
    of this code) may not be found by decoders that locate barcode images via
    page/rotation metadata rather than full rendering. A single content-stream
    transform here maps our existing "logical landscape" coordinate math onto
    the portrait page, so everything below is otherwise unchanged.
    """
    n = len(images)

    # Right-align: if fewer than 6, treat as if the left positions are empty
    # Segment positions 0..5 left to right; use the last n positions
    slot_w_cm = _BC_ROT_W_CM + _GAP_CM

    # x-coordinates for all 6 slots
    def slot_x(slot: int) -> float:
        extra = _FOLD_EXTRA_CM if slot >= 3 else 0.0
        return _SIDE_CM + slot * slot_w_cm + extra

    # reportlab y=0 is bottom; segments start below top margin
    y_bottom_cm = page_h_cm - _TOP_CM - _BC_ROT_H_CM

    # Note: /Rotate is set afterwards via pypdf post-processing, not
    # c.setPageRotation() — reportlab's version swaps the MediaBox to
    # compensate (preserving the portrait canvas's own visual appearance),
    # which is the opposite of what's needed: MediaBox must stay portrait
    # while /Rotate alone changes the effective display shape to landscape.
    c.saveState()
    c.transform(0, 1, -1, 0, portrait_w_pt, 0)

    for k, img in enumerate(images):
        slot = (BARCODES_PER_PAGE - 1) - k   # rightmost slot = segment 0
        x_cm = slot_x(slot)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        ir = ImageReader(buf)

        # BEIL2 §2.3: apply CW rotation via CTM so the image XObject stays native.
        # CTM [0, 1, -1, 0, tx, ty] maps image (0,0)→(tx,ty) bottom-right, then
        # sweeps up — placing native 290×35 image in the _BC_ROT_W × _BC_ROT_H slot.
        x0 = x_cm * cm
        y0 = y_bottom_cm * cm
        c.saveState()
        c.transform(0, 1, -1, 0, x0 + _BC_ROT_W_CM * cm, y0)
        c.drawImage(ir, 0, 0, width=_BC_W_CM * cm, height=_BC_H_CM * cm)
        c.restoreState()

    _draw_code128(c, page_w_cm, page_h_cm, page_num, has_2d=True, form=_CODE128_FORM_2D)
    c.restoreState()
    c.showPage()


def generate_barcode_pdf(xml_path: Path, pdf_path: Path) -> None:
    """Generate eCH-0270 barcode PDF from an eCH-196 XML file."""
    # Normalise to LF so compressed output is platform-independent
    xml_bytes = xml_path.read_bytes().replace(b"\r\n", b"\n")

    root = ET.fromstring(xml_bytes)
    doc_id = root.get("id", "tax-statement")

    # Settled by direct evidence, not spec-reading: a real reference
    # eSteuerauszug (data/example_etax.pdf, from Hypothekarbank Lenzburg) was
    # decoded independently via zxing-cpp, and its barcode payload only
    # decompresses successfully as standard ZLIB (starts with the 78 da
    # magic header; raw DEFLATE fails with "invalid stored block lengths").
    # This matches BEIL2 §2.2's explicit "ZLIB Komprimierung z.B.
    # java.util.zip.Deflater.BEST_COMPRESSION" wording, not eCH-0270 §2.2.2's
    # vaguer "ZIP-File-Algorithmus" phrase (which was tried and is wrong here).
    compressed = zlib.compress(xml_bytes, 9)
    print(f"  XML: {len(xml_bytes):,} B  ->  compressed (ZLIB): {len(compressed):,} B")

    # BEIL2 §2.2: the Macro PDF417 PDFMacroFileId MUST be "eine zufällige Zahl
    # (4 Integer)" — four codewords, matching a real accepted eSteuerauszug
    # whose file id decodes to four codewords (e.g. 112 170 169 206). If left
    # unset, pdf417gen emits a single-codeword file id, which ZHPrivateTax's
    # J4L-based reader rejects as invalid Structured Append metadata ("Der
    # Barcode enthält keine gültigen Daten"). Derive four codewords (each a
    # valid PDF417 value in 0..899) deterministically from the document id so
    # the same statement always yields the same, document-unique file id.
    digest = hashlib.sha256(doc_id.encode("utf-8")).digest()
    file_id = [
        ((digest[2 * i] << 8) | digest[2 * i + 1]) % 900 for i in range(4)
    ]

    segments = encode_macro(
        compressed,
        columns=COLS,
        security_level=EC_LEVEL,
        force_rows=ROWS,
        segment_size=450,   # eCH-0270 max bytes per segment
        file_name=doc_id,
        file_id=file_id,    # BEIL2 §2.2: 4-integer PDFMacroFileId (see above)
        force_binary=True,  # compressed bytes must not be re-interpreted as text/numeric
    )
    print(f"  PDF417 segments: {len(segments)}  file_id: {file_id}")

    images = _barcode_images(segments)

    # Logical layout is landscape; the underlying PDF page is portrait with
    # /Rotate 90 set (matching data/example_etax.pdf's actual structure — see
    # _draw_barcode_page's docstring), so page_w/page_h here describe the
    # "logical landscape" space our positioning math targets, not the
    # portrait-shaped canvas itself.
    page_w, page_h = landscape(A4)
    page_w_cm = page_w / cm
    page_h_cm = page_h / cm
    portrait_w_pt, _ = A4

    c = canvas.Canvas(str(pdf_path), pagesize=A4)

    # Human-readable statement page(s) first (portrait, not rotated), then the
    # 2D-barcode sheet(s) — matching the reference eSteuerauszug's structure and
    # eCH-0270 §2.2.4 ("2D-Barcodes are usually on the last pages").
    page_num = 1
    n_text_pages = _draw_statement_pages(c, root, page_num)
    page_num += n_text_pages

    for start in range(0, len(images), BARCODES_PER_PAGE):
        batch = images[start : start + BARCODES_PER_PAGE]
        _draw_barcode_page(c, page_w_cm, page_h_cm, portrait_w_pt, batch, page_num)
        page_num += 1

    c.save()

    # Set /Rotate 90 on the barcode sheets only (not the portrait statement
    # pages), without touching MediaBox — reportlab's own setPageRotation()
    # swaps MediaBox to compensate instead (see _draw_barcode_page's docstring),
    # so this is done as a pypdf post-processing pass.
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for idx, page in enumerate(reader.pages):
        if idx >= n_text_pages:   # barcode sheet
            page.rotate(90)
        writer.add_page(page)
    with open(pdf_path, "wb") as f:
        writer.write(f)

    print(f"  Written: {pdf_path}  ({n_text_pages} statement page(s) + "
          f"{len(reader.pages) - n_text_pages} barcode sheet(s))")
