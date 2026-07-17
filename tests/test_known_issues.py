"""Regression tests for defects flagged in code review and since fixed.

Both bugs are now fixed; these tests guard against regressions.
"""

from __future__ import annotations

from datetime import date

import xml.etree.ElementTree as ET
import pytest

from src.parse_ibkr import CashTransaction, parse
from src.generate_ech196 import NS, _build_security_payments, serialize

from .conftest import XSD_PATH


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def test_withholding_tax_not_double_counted():
    # Bug #1 (fixed): WHT must be matched to the income on its own date, not
    # summed across all dates and re-emitted on every payment.
    sec_el = ET.Element(_q("security"))
    income = [
        CashTransaction(date(2025, 3, 1), "CHF", 1.0, 100.0, "Dividends", "", "X", "S"),
        CashTransaction(date(2025, 9, 1), "CHF", 1.0, 100.0, "Dividends", "", "X", "S"),
    ]
    wht = [
        CashTransaction(
            date(2025, 3, 1), "CHF", 1.0, -35.0, "Withholding Tax", "", "X", "S"
        )
    ]
    # CHF payments convert 1:1, but _fx_to_chf still needs a CHF->EUR rate row.
    fx = {
        (date(2025, 3, 1), "CHF", "EUR"): 1.07,
        (date(2025, 9, 1), "CHF", "EUR"): 1.07,
    }

    rev_b, wht_total = _build_security_payments(
        sec_el, income, wht, fx_rates=fx, quantity=10.0
    )

    claim_in_xml = sum(
        float(p.get("withHoldingTaxClaim", "0")) for p in sec_el.findall(_q("payment"))
    )
    # Only 35.00 CHF was actually withheld; it must not appear twice.
    assert claim_in_xml == pytest.approx(35.0)
    # The returned totals feed the section/root aggregates and must match.
    assert wht_total == pytest.approx(35.0)
    assert rev_b == pytest.approx(200.0)


def test_dividend_wht_aggregates_into_root_totals(tmp_path):
    # Bug #2 (fixed), end to end: a distributing security's dividend and its
    # foreign WHT must appear in the security payment AND in the root totals.
    from datetime import date as _date
    from src.parse_ibkr import (
        AccountInfo,
        CashTransaction as CT,
        IBKRData,
        OpenPosition,
    )
    from src.generate_ech196 import build

    isin = "IE00DIST0001"
    pos = OpenPosition(
        isin=isin,
        symbol="VWRD",
        description="VANGUARD FTSE ALL-WORLD DIST",
        currency="USD",
        fx_rate_to_base=0.85,
        quantity=10,
        mark_price=100,
        position_value=1000,
        issuer_country_code="IE",
        report_date=_date(2025, 12, 31),
        sub_category="ETF",
    )
    div = CT(
        _date(2025, 6, 15), "USD", 0.85, 40.0, "Dividends", "Q2 dividend", isin, "VWRD"
    )
    wht = CT(
        _date(2025, 6, 15), "USD", 0.85, -6.0, "Withholding Tax", "US WHT", isin, "VWRD"
    )
    fx = {
        (_date(2025, 12, 31), "CHF", "EUR"): 1.074,
        (_date(2025, 12, 31), "USD", "EUR"): 0.85135,
        (_date(2025, 6, 15), "CHF", "EUR"): 1.07,
        (_date(2025, 6, 15), "USD", "EUR"): 0.86,
    }
    acct = AccountInfo("U1", "A B", "A", "B", "ZH", "EUR", "IBKR")
    root = build(IBKRData(acct, [pos], [div, wht], fx))

    # Root totals must be non-zero and equal the securities-section subtotals.
    assert float(root.get("totalGrossRevenueB")) > 0
    assert float(root.get("totalWithHoldingTaxClaim")) > 0
    sec = root.find(f"{_q('listOfSecurities')}")
    assert root.get("totalGrossRevenueB") == sec.get("totalGrossRevenueB")
    assert root.get("totalWithHoldingTaxClaim") == sec.get("totalWithHoldingTaxClaim")


def test_dividends_are_parsed(tmp_path):
    # Bug #2 (fixed): 'Dividends' must be captured so dividend income and its
    # DA-1 foreign WHT flow through for distributing securities.
    xml = """<FlexQueryResponse><FlexStatements><FlexStatement>
      <AccountInformation accountId="U1" name="A B" state="CH-ZH" currency="EUR"/>
      <CashTransactions>
        <CashTransaction type="Dividends" isin="X4" symbol="S" currency="USD"
                         amount="50" settleDate="15/06/2025"/>
      </CashTransactions>
    </FlexStatement></FlexStatements></FlexQueryResponse>"""
    f = tmp_path / "div.xml"
    f.write_text(xml, encoding="utf-8")
    parsed = parse(str(f))
    assert any(t.tx_type == "Dividends" for t in parsed.cash_transactions)


def test_income_from_security_sold_before_year_end_is_reported():
    from src.generate_ech196 import build
    from src.parse_ibkr import AccountInfo, IBKRData

    isin = "US0378331005"
    pay_date = date(2025, 5, 15)
    dividend = CashTransaction(
        pay_date,
        "USD",
        0.85,
        10.0,
        "Dividends",
        "AAPL CASH DIVIDEND",
        isin,
        "AAPL",
    )
    withholding = CashTransaction(
        pay_date,
        "USD",
        0.85,
        -1.5,
        "Withholding Tax",
        "AAPL US TAX",
        isin,
        "AAPL",
    )
    data = IBKRData(
        AccountInfo("U1", "A B", "A", "B", "ZH", "EUR", "IB-UK"),
        [],
        [dividend, withholding],
        {
            (pay_date, "CHF", "EUR"): 1.07,
            (pay_date, "USD", "EUR"): 0.85,
        },
        period_from=date(2025, 1, 1),
        period_to=date(2025, 12, 31),
    )

    root = build(data)

    security = root.find(f"{_q('listOfSecurities')}/{_q('depot')}/{_q('security')}")
    assert security is not None
    assert security.get("isin") == isin
    assert security.find(_q("taxValue")) is None
    payment = security.find(_q("payment"))
    assert float(payment.get("grossRevenueB")) > 0
    assert float(payment.get("withHoldingTaxClaim")) > 0
    securities = root.find(_q("listOfSecurities"))
    assert root.get("totalGrossRevenueB") == securities.get("totalGrossRevenueB")
    assert root.get("totalWithHoldingTaxClaim") == securities.get(
        "totalWithHoldingTaxClaim"
    )

    if XSD_PATH.exists():
        lxml_etree = pytest.importorskip("lxml.etree")
        schema = lxml_etree.XMLSchema(lxml_etree.parse(str(XSD_PATH)))
        document = lxml_etree.fromstring(serialize(root).encode())
        assert schema.validate(document), str(schema.error_log)
