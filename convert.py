#!/usr/bin/env python3
"""Convert an IBKR FlexQuery XML export to an eCH-0196 v2.2.0 tax statement.

Usage:
    python convert.py data/Tax.xml output.xml [--eur-chf-rate 0.9311]

Options:
    --eur-chf-rate RATE   Override the EUR→CHF rate for year-end valuations.
                          Use the official ESTV Jahresendkurs if required.
                          Defaults to the rate embedded in the IBKR file.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from src.parse_ibkr import parse
from src.generate_ech196 import build, serialize


def _validate(root: ET.Element) -> bool:
    try:
        from lxml import etree as lxml_et
    except ImportError:
        print("lxml not installed — skipping XSD validation (pip install lxml)")
        return True

    xsd_path = Path(__file__).resolve().parent / "documentation" / "eCH-0196-2-2.xsd"
    if not xsd_path.exists():
        print("XSD not found at documentation/eCH-0196-2-2.xsd — skipping validation")
        print("Download from: https://www.ech.ch/de/ech/ech-0196/2.2.0")
        return True

    schema = lxml_et.XMLSchema(lxml_et.parse(str(xsd_path)))
    xml_str = serialize(root)
    doc = lxml_et.fromstring(xml_str.encode())
    if schema.validate(doc):
        print("XSD validation passed.")
        return True
    else:
        print("XSD validation FAILED:")
        for err in schema.error_log:
            print(f"  Line {err.line}: {err.message}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="Path to IBKR Tax.xml FlexQuery export")
    parser.add_argument("output", help="Path for the generated eCH-196 XML file")
    parser.add_argument(
        "--eur-chf-rate",
        type=float,
        default=None,
        metavar="RATE",
        help="Override EUR→CHF exchange rate for year-end valuations",
    )
    parser.add_argument(
        "--barcode-pdf",
        metavar="PATH",
        default=None,
        help="Also generate eCH-0270 barcode PDF at this path",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        return 1

    print(f"Parsing {input_path}...")
    data = parse(str(input_path))

    print(f"Account:   {data.account.name} ({data.account.account_id})")
    print(f"Canton:    {data.account.canton}")
    print(f"Positions: {len(data.positions)}")
    print(f"Cash txns: {len(data.cash_transactions)}")

    print("Generating eCH-196 XML...")
    root = build(data, eur_chf_override=args.eur_chf_rate)

    if not _validate(root):
        print("Error: generated XML is invalid; no output was written.", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n' + serialize(root)
    output_path.write_text(xml_content, encoding="utf-8")
    print(f"Written:   {output_path}")

    if args.barcode_pdf:
        try:
            from src.generate_barcode_pdf import generate_barcode_pdf
        except ImportError as exc:
            print(
                f"Error: barcode PDF dependencies are not installed ({exc}). "
                "Run: pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 1
        pdf_path = Path(args.barcode_pdf)
        print("Generating barcode PDF...")
        generate_barcode_pdf(output_path, pdf_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
