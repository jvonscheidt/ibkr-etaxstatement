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
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from xml.dom import minidom
from xml.parsers.expat import ExpatError

from src.generate_ech196 import build, serialize
from src.parse_ibkr import parse

__version__ = "0.3.0"

XSD_URL = "https://www.ech.ch/xmlns/eCH-0196/2.2/eCH-0196-2-2.xsd"
XSD_NAMESPACE = "http://www.w3.org/2001/XMLSchema"


def _schema_references(document: minidom.Document) -> Iterator[minidom.Element]:
    for tag in ("import", "include", "redefine"):
        yield from document.getElementsByTagNameNS(XSD_NAMESPACE, tag)


def _download_xsd() -> None:
    print("Downloading latest eCH-0196 XSD...")
    xsd_path = _find_xsd() or (
        _application_dir() / "documentation" / "eCH-0196-2-2.xsd"
    )
    try:
        sources = {xsd_path.name: XSD_URL}
        pending = [xsd_path.name]
        schemas: dict[str, bytes] = {}
        while pending:
            name = pending.pop()
            if name in schemas:
                continue
            with urllib.request.urlopen(sources[name], timeout=30) as response:
                content = response.read()
            document = minidom.parseString(content)
            root = document.documentElement
            if root.namespaceURI != XSD_NAMESPACE or root.localName != "schema":
                raise ValueError("download is not an XML Schema")
            changed = False
            for reference in _schema_references(document):
                location = reference.getAttribute("schemaLocation")
                if not location:
                    continue
                url = urllib.parse.urlsplit(
                    urllib.parse.urljoin(sources[name], location)
                )
                if url.scheme not in {"http", "https"} or url.netloc not in {
                    "www.ech.ch",
                    "ech.ch",
                }:
                    raise ValueError("schema dependency is not an official eCH URL")
                url = url._replace(scheme="https")
                dependency = url.path.rsplit("/", 1)[-1]
                if not dependency.endswith(".xsd"):
                    raise ValueError("schema dependency has no XSD filename")
                source = url.geturl()
                if dependency in sources and sources[dependency] != source:
                    raise ValueError("schema dependencies have conflicting filenames")
                sources[dependency] = source
                if dependency not in schemas:
                    pending.append(dependency)
                reference.setAttribute("schemaLocation", dependency)
                changed = True
            # DOM serialization retains namespace declarations used in QName values.
            schemas[name] = document.toxml(encoding="utf-8") if changed else content

        xsd_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=xsd_path.parent) as directory:
            staging = Path(directory)
            generation = xsd_path.parent / f"ech-schemas-{uuid.uuid4().hex}"
            for name, content in schemas.items():
                if len(schemas) > 1:
                    document = minidom.parseString(content)
                    for reference in _schema_references(document):
                        location = reference.getAttribute("schemaLocation")
                        if not location:
                            continue
                        if name == xsd_path.name and location != xsd_path.name:
                            reference.setAttribute(
                                "schemaLocation", f"{generation.name}/{location}"
                            )
                        elif name != xsd_path.name and location == xsd_path.name:
                            reference.setAttribute("schemaLocation", f"../{location}")
                    content = document.toxml(encoding="utf-8")
                (staging / name).write_bytes(content)
            if len(schemas) == 1:
                os.replace(staging / xsd_path.name, xsd_path)
            else:
                # Keep dependencies isolated; replacing the root switches the cache.
                os.replace(staging, generation)
                try:
                    os.replace(generation / xsd_path.name, xsd_path)
                except OSError:
                    shutil.rmtree(generation)
                    raise
        print(f"XSD updated: {xsd_path}")
    except (OSError, urllib.error.URLError, ExpatError, ValueError) as exc:
        if xsd_path.exists():
            print(f"Warning: XSD download failed ({exc}); using cached copy.")
        else:
            print(f"Warning: XSD download failed ({exc}); validation will be skipped.")


def _application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _find_xsd() -> Path | None:
    for directory in (_application_dir(), Path.cwd()):
        path = directory / "documentation" / "eCH-0196-2-2.xsd"
        if path.is_file():
            return path
    return None


def _validate(root: ET.Element) -> bool:
    try:
        from lxml import etree as lxml_et
    except ImportError:
        print("lxml not installed — skipping XSD validation (pip install lxml)")
        return True

    xsd_path = _find_xsd()
    if xsd_path is None:
        print(
            "XSD not found in documentation beside the application or in the "
            "working directory — skipping validation"
        )
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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=__version__)
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

    _download_xsd()

    print(f"Parsing {input_path}...")
    try:
        data = parse(str(input_path))
    except (ValueError, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Account:   {data.account.name} ({data.account.account_id})")
    print(f"Canton:    {data.account.canton}")
    print(f"Positions: {len(data.positions)}")
    print(f"Cash txns: {len(data.cash_transactions)}")

    print("Generating eCH-196 XML...")
    try:
        root = build(data, eur_chf_override=args.eur_chf_rate)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not _validate(root):
        print(
            "Error: generated XML is invalid; no output was written.", file=sys.stderr
        )
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
