"""Tests for IBKR FlexQuery parsing (src/parse_ibkr.py)."""

from __future__ import annotations

from datetime import date

import pytest

from src.parse_ibkr import _date, _float, _parse_account, parse
import xml.etree.ElementTree as ET

from .conftest import TAX_XML


class TestDateParsing:
    def test_valid_ddmmyyyy(self):
        assert _date("31/12/2025") == date(2025, 12, 31)

    def test_strips_time_component(self):
        # IBKR sometimes appends ";HHMMSS"
        assert _date("31/12/2025;235959") == date(2025, 12, 31)

    def test_empty_returns_none(self):
        assert _date("") is None

    def test_invalid_returns_none(self):
        assert _date("2025-12-31") is None  # ISO format is not accepted
        assert _date("garbage") is None


class TestFloatParsing:
    def test_valid(self):
        assert _float("3000.55") == pytest.approx(3000.55)

    def test_empty_is_zero(self):
        assert _float("") == 0.0

    def test_invalid_is_zero(self):
        assert _float("N/A") == 0.0


class TestAccountParsing:
    def _elem(self, **attrs) -> ET.Element:
        el = ET.Element("AccountInformation")
        for k, v in attrs.items():
            el.set(k, v)
        return el

    def test_simple_name_and_canton(self):
        acct = _parse_account(self._elem(
            accountId="U1", name="Max Mustermann", state="CH-ZH", currency="EUR",
        ))
        assert acct.first_name == "Max"
        assert acct.last_name == "Mustermann"
        assert acct.canton == "ZH"

    def test_nobiliary_particle_kept_in_last_name(self):
        acct = _parse_account(self._elem(
            accountId="U1", name="Johannes von Scheidt", state="CH-ZG",
        ))
        assert acct.first_name == "Johannes"
        assert acct.last_name == "von Scheidt"

    def test_canton_without_prefix(self):
        acct = _parse_account(self._elem(name="A B", state="ZH"))
        assert acct.canton == "ZH"

    def test_base_currency_defaults_to_eur(self):
        acct = _parse_account(self._elem(name="A B"))
        assert acct.base_currency == "EUR"


@pytest.fixture(scope="module")
def parsed():
    assert TAX_XML.exists(), f"sample data missing: {TAX_XML}"
    return parse(str(TAX_XML))


class TestParseRealFile:
    def test_account(self, parsed):
        assert parsed.account.account_id == "U00000000"
        assert parsed.account.canton == "ZH"

    def test_four_year_end_positions(self, parsed):
        assert len(parsed.positions) == 4
        assert all(p.report_date == date(2025, 12, 31) for p in parsed.positions)
        assert all(p.isin for p in parsed.positions)

    def test_cash_transactions_filtered_to_income_types(self, parsed):
        # Deposits/Withdrawals and "AF" must be excluded; only income types remain.
        types = {t.tx_type for t in parsed.cash_transactions}
        assert types <= {
            "Withholding Tax", "Broker Interest Received", "Broker Interest Paid",
            "Dividends", "Payment In Lieu Of Dividends",
        }
        assert "Deposits/Withdrawals" not in types
        # The sample holds distributing securities, so dividends must be captured.
        assert "Dividends" in types

    def test_fx_rates_present(self, parsed):
        assert parsed.fx_rates[(date(2025, 12, 31), "CHF", "EUR")] == pytest.approx(1.074)
        assert parsed.fx_rates[(date(2025, 12, 31), "USD", "EUR")] == pytest.approx(0.85135)

    def test_statement_period_is_parsed(self, parsed):
        assert parsed.period_from == date(2025, 1, 1)
        assert parsed.period_to == date(2025, 12, 31)


def test_positions_exclude_non_summary_and_non_year_end(tmp_path):
    xml = """<FlexQueryResponse><FlexStatements><FlexStatement>
      <AccountInformation accountId="U1" name="A B" state="CH-ZH" currency="EUR"/>
      <OpenPositions>
        <OpenPosition levelOfDetail="LOT" isin="X1" reportDate="31/12/2025" position="1"/>
        <OpenPosition levelOfDetail="SUMMARY" isin="X2" reportDate="30/06/2025" position="1"/>
        <OpenPosition levelOfDetail="SUMMARY" isin="" reportDate="31/12/2025" position="1"/>
        <OpenPosition levelOfDetail="SUMMARY" isin="X4" reportDate="31/12/2025" position="1"
                      currency="EUR" markPrice="10" positionValue="10"/>
      </OpenPositions>
    </FlexStatement></FlexStatements></FlexQueryResponse>"""
    f = tmp_path / "mini.xml"
    f.write_text(xml, encoding="utf-8")
    parsed = parse(str(f))
    # Only the LOT-excluded / mid-year / no-ISIN rows drop out; X4 remains.
    assert [p.isin for p in parsed.positions] == ["X4"]
