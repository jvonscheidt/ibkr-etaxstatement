"""End-to-end: parse the real sample file, build XML, validate against the XSD."""

from __future__ import annotations

import pytest

from src.parse_ibkr import parse
from src.generate_ech196 import build, serialize

from .conftest import TAX_XML, XSD_PATH

lxml_etree = pytest.importorskip(
    "lxml.etree", reason="lxml required for XSD validation"
)


@pytest.fixture(scope="module")
def built_root():
    assert TAX_XML.exists(), f"sample data missing: {TAX_XML}"
    return build(parse(str(TAX_XML)), eur_chf_override=0.9311)


def test_output_validates_against_xsd(built_root):
    if not XSD_PATH.exists():
        pytest.skip(f"XSD not present at {XSD_PATH}")
    schema = lxml_etree.XMLSchema(lxml_etree.parse(str(XSD_PATH)))
    doc = lxml_etree.fromstring(serialize(built_root).encode())
    is_valid = schema.validate(doc)
    errors = "\n".join(f"  line {e.line}: {e.message}" for e in schema.error_log)
    assert is_valid, f"XSD validation failed:\n{errors}"


def test_totals_are_wellformed_numbers(built_root):
    for attr in ("totalTaxValue", "totalGrossRevenueB", "totalWithHoldingTaxClaim"):
        val = built_root.get(attr)
        assert val is not None
        float(val)  # must parse
