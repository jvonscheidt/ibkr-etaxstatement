"""Tax-bucket, reversal, ISIN consolidation and currency regressions."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from src.generate_ech196 import NS, serialize
from src.generate_ech196 import build as build_statement
from src.parse_ibkr import CashTransaction, IBKRData, OpenPosition

from .conftest import XSD_PATH, with_dividend_accruals

FIRST = date(2025, 3, 1)
SECOND = date(2025, 9, 1)
YEAR_END = date(2025, 12, 31)
US_ISIN = "US0378331005"
CH_ISIN = "CH0038863350"


def build(data):
    return build_statement(with_dividend_accruals(data))


def _q(name):
    return f"{{{NS}}}{name}"


def _tx(amount, kind="Dividends", day=FIRST, currency="EUR", isin=US_ISIN):
    return CashTransaction(day, currency, 1.0, amount, kind, "", isin, "TEST")


def _payments(root):
    return root.findall(f".//{_q('security')}/{_q('payment')}")


@pytest.fixture
def tax_data(account):
    position = OpenPosition(
        US_ISIN, "TEST", "TEST SHARE", "EUR", 1.0, 10, 100, 1000, "US", YEAR_END, "STK"
    )
    rates = {}
    for day in (FIRST, SECOND, YEAR_END):
        rates[(day, "CHF", "EUR")] = 1.0
        rates[(day, "USD", "EUR")] = 0.5
    return IBKRData(account, [position], [], rates, date(2025, 1, 1), YEAR_END)


def test_foreign_tax_is_not_a_swiss_claim_or_assumed_da1_entitlement(tax_data):
    tax_data.cash_transactions = [
        _tx(100, currency="CHF"),
        _tx(-15, "Withholding Tax", currency="CHF"),
    ]

    with pytest.warns(UserWarning, match="DA-1 eligibility"):
        root = build(tax_data)

    payment = _payments(root)[0]
    assert payment.get("grossRevenueA") == "0.00"
    assert payment.get("grossRevenueB") == "100.00"
    assert payment.get("withHoldingTaxClaim") == "0.00"
    assert payment.get("lumpSumTaxCreditAmount") == "15.00"
    assert payment.get("lumpSumTaxCredit") is None
    assert payment.get("nonRecoverableTaxAmount") is None
    assert root.get("totalWithHoldingTaxClaim") == "0.00"
    securities = root.find(_q("listOfSecurities"))
    assert securities.get("totalLumpSumTaxCredit") == "15.00"
    assert securities.get("totalNonRecoverableTax") == "0.00"


@pytest.mark.parametrize("issuer_country", ["CH", ""])
def test_swiss_tax_and_income_feed_section_and_root_a_totals(tax_data, issuer_country):
    tax_data.positions = [
        replace(tax_data.positions[0], isin=CH_ISIN, issuer_country_code=issuer_country)
    ]
    tax_data.cash_transactions = [
        _tx(100, isin=CH_ISIN),
        _tx(-35, "Withholding Tax", isin=CH_ISIN),
        _tx(50, day=SECOND, isin=CH_ISIN),
    ]

    root = build(tax_data)

    for element in (root, root.find(_q("listOfSecurities"))):
        assert element.get("totalGrossRevenueA") == "100.00"
        assert element.get("totalGrossRevenueB") == "50.00"
        assert element.get("totalWithHoldingTaxClaim") == "35.00"
    assert root.find(_q("listOfSecurities")).get("totalLumpSumTaxCredit") == "0.00"
    assert all(p.get("lumpSumTaxCreditAmount") is None for p in _payments(root))


@pytest.mark.parametrize(
    "income_currency,tax_currency,tax_amount",
    [("EUR", "CHF", -35), ("CHF", "USD", -70)],
)
def test_swiss_tax_classification_does_not_depend_on_payment_currency(
    tax_data, income_currency, tax_currency, tax_amount
):
    tax_data.positions = [
        replace(tax_data.positions[0], isin=CH_ISIN, issuer_country_code="CH")
    ]
    tax_data.cash_transactions = [
        _tx(100, currency=income_currency, isin=CH_ISIN),
        _tx(tax_amount, "Withholding Tax", currency=tax_currency, isin=CH_ISIN),
    ]

    root = build(tax_data)

    assert root.get("totalGrossRevenueA") == "100.00"
    assert root.get("totalGrossRevenueB") == "0.00"
    assert root.get("totalWithHoldingTaxClaim") == "35.00"
    payments = {p.get("amountCurrency"): p for p in _payments(root)}
    assert payments[income_currency].get("grossRevenueA") == "100.00"
    assert payments[tax_currency].get("withHoldingTaxClaim") == "35.00"


def test_unmatched_swiss_withholding_warns_about_income_classification(tax_data):
    tax_data.positions = [
        replace(tax_data.positions[0], isin=CH_ISIN, issuer_country_code="CH")
    ]
    tax_data.cash_transactions = [
        _tx(100, isin=CH_ISIN),
        _tx(-35, "Withholding Tax", day=SECOND, isin=CH_ISIN),
    ]

    with pytest.warns(UserWarning, match="no same-day income"):
        root = build(tax_data)

    assert root.get("totalWithHoldingTaxClaim") == "35.00"
    assert root.get("totalGrossRevenueB") == "100.00"


@pytest.mark.parametrize("refund_currency", ["EUR", "CHF"])
def test_swiss_refund_does_not_reclassify_new_income(tax_data, refund_currency):
    tax_data.positions = [
        replace(tax_data.positions[0], isin=CH_ISIN, issuer_country_code="CH")
    ]
    tax_data.cash_transactions = [
        _tx(100, isin=CH_ISIN),
        _tx(-35, "Withholding Tax", isin=CH_ISIN),
        _tx(50, day=SECOND, isin=CH_ISIN),
        _tx(5, "Withholding Tax", day=SECOND, currency=refund_currency, isin=CH_ISIN),
    ]

    root = build(tax_data)

    assert root.get("totalGrossRevenueA") == "100.00"
    assert root.get("totalGrossRevenueB") == "50.00"
    assert root.get("totalWithHoldingTaxClaim") == "30.00"


def test_swiss_income_reversal_with_tax_refund_reverses_a_income(tax_data):
    tax_data.positions = [
        replace(tax_data.positions[0], isin=CH_ISIN, issuer_country_code="CH")
    ]
    tax_data.cash_transactions = [
        _tx(100, isin=CH_ISIN),
        _tx(-35, "Withholding Tax", isin=CH_ISIN),
        _tx(-100, day=SECOND, isin=CH_ISIN),
        _tx(35, "Withholding Tax", day=SECOND, currency="CHF", isin=CH_ISIN),
    ]

    root = build(tax_data)

    assert root.get("totalGrossRevenueA") == "0.00"
    assert root.get("totalGrossRevenueB") == "0.00"
    assert root.get("totalWithHoldingTaxClaim") == "0.00"


@pytest.mark.parametrize("swiss", [False, True])
@pytest.mark.parametrize("refund", [5, 15])
def test_later_refunds_reduce_tax_totals_even_without_same_day_income(
    tax_data, swiss, refund
):
    isin = CH_ISIN if swiss else US_ISIN
    tax_data.positions = [
        replace(
            tax_data.positions[0],
            isin=isin,
            issuer_country_code="CH" if swiss else "US",
        )
    ]
    tax_data.cash_transactions = [
        _tx(100, isin=isin),
        _tx(-15, "Withholding Tax", isin=isin),
        _tx(refund, "Withholding Tax", day=SECOND, isin=isin),
    ]

    root = build(tax_data)

    attribute = "withHoldingTaxClaim" if swiss else "lumpSumTaxCreditAmount"
    payments = _payments(root)
    assert payments[1].get("paymentDate") == SECOND.isoformat()
    assert Decimal(payments[1].get(attribute)) == -refund
    assert sum(Decimal(p.get(attribute, "0")) for p in payments) == 15 - refund
    subtotal = "totalWithHoldingTaxClaim" if swiss else "totalLumpSumTaxCredit"
    assert Decimal(root.find(_q("listOfSecurities")).get(subtotal)) == 15 - refund


def test_refund_with_income_on_its_date_still_reduces_foreign_tax(tax_data):
    tax_data.cash_transactions = [
        _tx(100),
        _tx(-15, "Withholding Tax"),
        _tx(100, day=SECOND),
        _tx(15, "Withholding Tax", day=SECOND),
    ]

    root = build(tax_data)

    assert root.get("totalGrossRevenueB") == "200.00"
    assert root.find(_q("listOfSecurities")).get("totalLumpSumTaxCredit") == "0.00"
    assert _payments(root)[1].get("lumpSumTaxCreditAmount") == "-15.00"


def test_withholding_adjustments_use_their_own_date_and_currency(tax_data):
    tax_data.fx_rates[(SECOND, "CHF", "EUR")] = 2.0
    tax_data.cash_transactions = [
        _tx(-20, "Withholding Tax", currency="USD"),
        _tx(4, "Withholding Tax", day=SECOND, currency="USD"),
    ]

    root = build(tax_data)

    payments = _payments(root)
    assert payments[0].get("lumpSumTaxCreditAmount") == "10.00"
    assert payments[1].get("lumpSumTaxCreditAmount") == "-1.00"
    assert root.find(_q("listOfSecurities")).get("totalLumpSumTaxCredit") == "9.00"
    assert root.get("totalGrossRevenueB") == "0.00"


@pytest.mark.parametrize(
    "second_currency,expected_value", [("EUR", "2000.00"), ("USD", "1500.00")]
)
def test_duplicate_isin_holdings_keep_value_without_duplicating_income(
    tax_data, second_currency, expected_value
):
    tax_data.positions.append(
        replace(
            tax_data.positions[0], currency=second_currency, symbol="SECOND LISTING"
        )
    )
    tax_data.cash_transactions = [_tx(100), _tx(-15, "Withholding Tax")]

    root = build(tax_data)

    securities = root.findall(f".//{_q('security')}")
    assert len(securities) == 1
    assert len(_payments(root)) == 1
    valuation = securities[0].find(_q("taxValue"))
    assert Decimal(valuation.get("quantity")) == 20
    assert valuation.get("value") == expected_value
    assert valuation.get("balance") == expected_value
    assert root.get("totalTaxValue") == expected_value
    assert root.get("totalGrossRevenueB") == "100.00"
    assert root.find(_q("listOfSecurities")).get("totalLumpSumTaxCredit") == "15.00"


def test_orphan_positions_keep_unique_ids_after_consolidation(tax_data):
    tax_data.positions.append(replace(tax_data.positions[0]))
    tax_data.cash_transactions = [
        _tx(100),
        _tx(50, isin=CH_ISIN),
        _tx(-17.5, "Withholding Tax", isin=CH_ISIN),
    ]

    root = build(tax_data)

    securities = root.findall(f".//{_q('security')}")
    assert [s.get("positionId") for s in securities] == ["1", "2"]
    assert root.get("totalGrossRevenueA") == "50.00"
    assert root.get("totalGrossRevenueB") == "100.00"
    assert root.get("totalWithHoldingTaxClaim") == "17.50"


@pytest.mark.parametrize("reverse_input", [False, True])
def test_same_day_income_and_tax_are_grouped_by_currency(tax_data, reverse_input):
    tax_data.cash_transactions = [
        _tx(100),
        _tx(100, currency="USD"),
        _tx(-15, "Withholding Tax"),
        _tx(-15, "Withholding Tax", currency="USD"),
    ]
    if reverse_input:
        tax_data.cash_transactions.reverse()

    root = build(tax_data)

    payments = {p.get("amountCurrency"): p for p in _payments(root)}
    assert set(payments) == {"EUR", "USD"}
    assert payments["EUR"].get("grossRevenueB") == "100.00"
    assert payments["USD"].get("grossRevenueB") == "50.00"
    assert payments["EUR"].get("lumpSumTaxCreditAmount") == "15.00"
    assert payments["USD"].get("lumpSumTaxCreditAmount") == "7.50"
    assert root.get("totalGrossRevenueB") == "150.00"
    assert root.find(_q("listOfSecurities")).get("totalLumpSumTaxCredit") == "22.50"


def test_withholding_in_another_currency_does_not_use_the_dividend_rate(tax_data):
    tax_data.cash_transactions = [
        _tx(100),
        _tx(-20, "Withholding Tax", currency="USD"),
    ]

    root = build(tax_data)

    payments = {p.get("amountCurrency"): p for p in _payments(root)}
    assert payments["EUR"].get("lumpSumTaxCreditAmount") is None
    assert payments["USD"].get("amount") == "0.00"
    assert payments["USD"].get("lumpSumTaxCreditAmount") == "10.00"
    assert root.get("totalGrossRevenueB") == "100.00"


def test_cash_tax_refunds_are_preserved_without_swiss_claims(tax_data):
    tax_data.cash_transactions = [
        _tx(100, "Broker Interest Received", isin=""),
        _tx(-15, "Withholding Tax", isin=""),
        _tx(15, "Withholding Tax", day=SECOND, isin=""),
    ]

    with pytest.warns(UserWarning, match="annotations only"):
        root = build(tax_data)

    payments = root.findall(
        f"{_q('listOfBankAccounts')}/{_q('bankAccount')}/{_q('payment')}"
    )
    assert "15.00 EUR (15.00 CHF)" in payments[1].get("name")
    assert "-15.00 EUR (-15.00 CHF)" in payments[2].get("name")
    assert root.get("totalGrossRevenueB") == "100.00"
    assert root.get("totalWithHoldingTaxClaim") == "0.00"
    assert root.find(_q("listOfSecurities")).get("totalLumpSumTaxCredit") == "0.00"


def test_corrected_tax_buckets_and_negative_adjustments_validate(tax_data):
    if not XSD_PATH.exists():
        pytest.skip("Official eCH-0196 XSD not downloaded")
    etree = pytest.importorskip("lxml.etree")
    tax_data.positions.extend(
        [
            replace(tax_data.positions[0], currency="USD"),
            replace(tax_data.positions[0], isin=CH_ISIN, issuer_country_code="CH"),
        ]
    )
    tax_data.cash_transactions = [
        _tx(100),
        _tx(-15, "Withholding Tax"),
        _tx(15, "Withholding Tax", day=SECOND),
        _tx(100, currency="USD"),
        _tx(-15, "Withholding Tax", currency="USD"),
        _tx(100, isin=CH_ISIN),
        _tx(-35, "Withholding Tax", isin=CH_ISIN),
        _tx(35, "Withholding Tax", day=SECOND, isin=CH_ISIN),
        _tx(-10, "Withholding Tax", isin=""),
        _tx(10, "Withholding Tax", day=SECOND, isin=""),
    ]

    root = build(tax_data)

    schema = etree.XMLSchema(etree.parse(str(XSD_PATH)))
    document = etree.fromstring(serialize(root).encode())
    assert schema.validate(document), str(schema.error_log)


def test_pdf_distinguishes_swiss_claims_from_unconfirmed_foreign_tax(
    tax_data, tmp_path
):
    pytest.importorskip("pdf417gen")
    pytest.importorskip("reportlab")
    pypdf = pytest.importorskip("pypdf")
    from src.generate_barcode_pdf import generate_barcode_pdf

    tax_data.cash_transactions = [_tx(100), _tx(-15, "Withholding Tax")]
    xml_path = tmp_path / "statement.xml"
    pdf_path = tmp_path / "statement.pdf"
    xml_path.write_text(serialize(build(tax_data)), encoding="utf-8")

    generate_barcode_pdf(xml_path, pdf_path)

    text = pypdf.PdfReader(pdf_path).pages[0].extract_text()
    assert "Total Bruttoertrag A CHF" in text
    assert "Total Bruttoertrag B CHF" in text
    assert "Total Verrechnungssteueranspruch CHF" in text
    assert "Quellensteuer Wertschriften CHF" in text
    assert "DA-1-Anspruch nicht ermittelt" in text
    assert "15.00" in text
    assert "Bruttoertrag B (DA-1)" not in text
