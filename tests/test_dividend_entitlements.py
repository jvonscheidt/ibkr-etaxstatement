"""Historical quantities must come from the matched dividend event."""

import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from src.generate_ech196 import NS, build, serialize
from src.parse_ibkr import parse

from .conftest import INCOME_XML, XSD_PATH


def _q(name):
    return f"{{{NS}}}{name}"


def _payments(root):
    return root.findall(f".//{_q('security')}/{_q('payment')}")


@pytest.fixture
def entitlement_data():
    return parse(str(INCOME_XML))


def test_payment_quantity_and_ex_date_are_not_year_end_holdings(entitlement_data):
    root = build(entitlement_data)

    valuation = root.find(f".//{_q('taxValue')}")
    assert valuation.get("quantity") == "20.004"
    payments = _payments(root)
    assert all(p.get("quantity") == "10.002" for p in payments)
    assert all(p.get("exDate") == "2025-06-01" for p in payments)
    assert payments[0].get("amount") == "40.00"
    assert len(payments) == 2  # payout plus later tax refund, not Po plus Re


def test_sold_security_keeps_original_entitlement(entitlement_data):
    entitlement_data.positions = []

    root = build(entitlement_data)

    security = root.find(f".//{_q('security')}")
    assert security.find(_q("taxValue")) is None
    assert all(p.get("quantity") == "10.002" for p in _payments(root))


def test_refund_uses_original_quantity_not_new_entitlement(entitlement_data):
    original = entitlement_data.dividend_accruals[0]
    entitlement_data.dividend_accruals.append(
        replace(
            original,
            quantity=Decimal(100),
            action_id="2002",
            ex_date=date(2025, 8, 20),
            pay_date=date(2025, 9, 1),
        )
    )

    root = build(entitlement_data)

    refund = _payments(root)[1]
    assert refund.get("paymentDate") == "2025-09-01"
    assert refund.get("quantity") == "10.002"
    assert refund.get("exDate") == "2025-06-01"


def test_distinct_same_day_events_do_not_merge_quantities(entitlement_data):
    original = entitlement_data.dividend_accruals[0]
    entitlement_data.dividend_accruals.append(
        replace(
            original,
            action_id="2002",
            quantity=Decimal("2.5"),
            gross_amount=Decimal(10),
        )
    )
    entitlement_data.cash_transactions.append(
        replace(entitlement_data.cash_transactions[0], action_id="2002", amount=10)
    )

    root = build(entitlement_data)

    income = [p for p in _payments(root) if Decimal(p.get("amount")) > 0]
    assert [(p.get("amount"), p.get("quantity")) for p in income] == [
        ("40.00", "10.002"),
        ("10.00", "2.5"),
    ]


def test_split_dividend_and_pil_use_one_entitlement(entitlement_data):
    dividend = entitlement_data.cash_transactions[0]
    entitlement_data.cash_transactions[0] = replace(dividend, amount=25)
    entitlement_data.cash_transactions.append(
        replace(dividend, amount=15, tx_type="Payment In Lieu Of Dividends")
    )

    root = build(entitlement_data)

    income = [p for p in _payments(root) if Decimal(p.get("amount")) > 0]
    assert len(income) == 1
    assert income[0].get("quantity") == "10.002"
    assert income[0].get("amount") == "40.00"


def test_full_income_reversal_retains_original_entitlement(entitlement_data):
    entitlement_data.cash_transactions.append(
        replace(
            entitlement_data.cash_transactions[0],
            amount=-40,
            settle_date=date(2025, 9, 1),
        )
    )

    root = build(entitlement_data)

    reversal = _payments(root)[1]
    assert reversal.get("amount") == "-40.00"
    assert reversal.get("quantity") == "10.002"
    assert reversal.get("exDate") == "2025-06-01"


def test_unique_ordinary_payment_can_match_without_optional_identifiers(
    entitlement_data,
):
    entitlement_data.cash_transactions = [
        replace(
            entitlement_data.cash_transactions[0], action_id="", conid="", ex_date=None
        )
    ]
    entitlement_data.dividend_accruals = [
        replace(entitlement_data.dividend_accruals[0], action_id="", conid="")
    ]

    root = build(entitlement_data)

    assert _payments(root)[0].get("quantity") == "10.002"


@pytest.mark.parametrize("closed", [False, True])
def test_missing_metadata_never_falls_back_to_closing_quantity(
    entitlement_data, closed
):
    entitlement_data.dividend_accruals = []
    if closed:
        entitlement_data.positions = []

    with pytest.raises(ValueError, match="Missing dividend entitlement"):
        build(entitlement_data)


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("quantity", Decimal("20.004")),
        ("gross_amount", Decimal(80)),
        ("pay_date", date(2025, 6, 16)),
    ],
)
def test_conflicting_accrual_records_are_rejected(entitlement_data, attribute, value):
    entitlement_data.dividend_accruals.append(
        replace(entitlement_data.dividend_accruals[0], **{attribute: value})
    )

    with pytest.raises(ValueError, match="Ambiguous dividend entitlement"):
        build(entitlement_data)


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("action_id", "unrelated"),
        ("conid", "another-listing"),
        ("model", "another-model"),
        ("currency", "EUR"),
        ("ex_date", date(2025, 5, 1)),
    ],
)
def test_conflicting_cash_metadata_does_not_match(entitlement_data, attribute, value):
    entitlement_data.cash_transactions[0] = replace(
        entitlement_data.cash_transactions[0], **{attribute: value}
    )

    with pytest.raises(ValueError, match="Missing dividend entitlement"):
        build(entitlement_data)


def test_gross_amount_mismatch_is_rejected(entitlement_data):
    entitlement_data.cash_transactions[0] = replace(
        entitlement_data.cash_transactions[0], amount=30
    )

    with pytest.raises(ValueError, match="gross amount does not match"):
        build(entitlement_data)


def test_unlinked_refund_does_not_guess_from_its_booking_date(entitlement_data):
    refund = replace(
        entitlement_data.cash_transactions[2], action_id="", conid="", ex_date=None
    )
    entitlement_data.cash_transactions = [refund]
    entitlement_data.dividend_accruals = [
        replace(entitlement_data.dividend_accruals[0], pay_date=refund.settle_date)
    ]

    with pytest.raises(ValueError, match="Missing dividend entitlement"):
        build(entitlement_data)


@pytest.mark.parametrize(
    "income_amount,adjustment_amount,adjustment_type",
    [(50, 1, "Withholding Tax"), (60, -10, "Dividends")],
)
def test_grouped_adjustments_cannot_borrow_an_ordinary_pay_date_match(
    entitlement_data, income_amount, adjustment_amount, adjustment_type
):
    dividend = replace(
        entitlement_data.cash_transactions[0],
        amount=income_amount,
        settle_date=date(2025, 9, 1),
        action_id="",
        conid="",
        ex_date=None,
    )
    adjustment = replace(dividend, amount=adjustment_amount, tx_type=adjustment_type)
    entitlement_data.cash_transactions = [dividend, adjustment]
    entitlement_data.dividend_accruals.append(
        replace(
            entitlement_data.dividend_accruals[0],
            quantity=Decimal(100),
            gross_amount=Decimal(50),
            pay_date=date(2025, 9, 1),
            ex_date=date(2025, 8, 20),
            action_id="",
            conid="",
        )
    )

    with pytest.raises(ValueError, match="Missing dividend entitlement"):
        build(entitlement_data)


@pytest.mark.parametrize("quantity", ["0.004", "0.00000001", "0.123456789012345678"])
def test_quantities_keep_decimal_precision_through_xml_parse_and_build(
    tmp_path, quantity
):
    tree = ET.parse(INCOME_XML)
    statement = tree.find("FlexStatements/FlexStatement")
    statement.find("OpenPositions/OpenPosition").set("position", quantity)
    for accrual in statement.findall(
        "ChangeInDividendAccruals/ChangeInDividendAccrual"
    ):
        accrual.set("quantity", quantity)
    path = tmp_path / "fractional.xml"
    tree.write(path, encoding="utf-8")

    data = parse(str(path))
    root = build(data)

    assert data.positions[0].quantity == Decimal(quantity)
    assert root.find(f".//{_q('taxValue')}").get("quantity") == quantity
    assert all(p.get("quantity") == quantity for p in _payments(root))
    if XSD_PATH.exists():
        etree = pytest.importorskip("lxml.etree")
        schema = etree.XMLSchema(etree.parse(str(XSD_PATH)))
        doc = etree.fromstring(serialize(root).encode())
        assert schema.validate(doc), str(schema.error_log)


def test_consolidated_quantities_do_not_gain_float_rounding_error(entitlement_data):
    position = entitlement_data.positions[0]
    entitlement_data.positions = [
        replace(position, quantity=Decimal("0.1")),
        replace(position, quantity=Decimal("0.2")),
        replace(position, quantity=Decimal("0.00000001")),
    ]

    root = build(entitlement_data)

    assert root.find(f".//{_q('taxValue')}").get("quantity") == "0.30000001"


@pytest.mark.parametrize(
    "attribute,value,error",
    [
        ("accountId", "U9999999", "account"),
        ("isin", "", "ISIN"),
        ("quantity", "", "quantity"),
        ("quantity", "NaN", "quantity"),
        ("quantity", "-10", "positive"),
        ("exDate", "", "exDate"),
        ("exDate", "01/07/2025", "after payDate"),
        ("payDate", "", "payDate"),
        ("grossAmount", "", "grossAmount"),
    ],
)
def test_invalid_accrual_metadata_is_rejected(tmp_path, attribute, value, error):
    tree = ET.parse(INCOME_XML)
    accrual = tree.find(
        "FlexStatements/FlexStatement/ChangeInDividendAccruals/ChangeInDividendAccrual"
    )
    accrual.set(attribute, value)
    path = tmp_path / "invalid.xml"
    tree.write(path, encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        parse(str(path))


def test_cli_does_not_write_files_for_missing_entitlements(
    tmp_path, monkeypatch, capsys
):
    import convert

    monkeypatch.setattr(convert, "_download_xsd", lambda: None)
    tree = ET.parse(INCOME_XML)
    statement = tree.find("FlexStatements/FlexStatement")
    statement.remove(statement.find("ChangeInDividendAccruals"))
    path = tmp_path / "missing.xml"
    tree.write(path, encoding="utf-8")
    output, pdf = tmp_path / "output.xml", tmp_path / "output.pdf"
    monkeypatch.setattr(
        sys, "argv", ["convert.py", str(path), str(output), "--barcode-pdf", str(pdf)]
    )

    assert convert.main() == 1
    assert "Missing dividend entitlement" in capsys.readouterr().err
    assert not output.exists()
    assert not pdf.exists()
