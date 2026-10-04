"""Tests for IBKR FlexQuery parsing (src/parse_ibkr.py)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from src.parse_ibkr import _date, _float, _parse_account, parse

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
        acct = _parse_account(
            self._elem(
                accountId="U1",
                name="Max Mustermann",
                state="CH-ZH",
                currency="EUR",
            )
        )
        assert acct.first_name == "Max"
        assert acct.last_name == "Mustermann"
        assert acct.canton == "ZH"

    def test_nobiliary_particle_kept_in_last_name(self):
        acct = _parse_account(
            self._elem(
                accountId="U1",
                name="Johannes von Scheidt",
                state="CH-ZG",
            )
        )
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
        income_types = {
            "Withholding Tax",
            "Broker Interest Received",
            "Broker Interest Paid",
            "Dividends",
            "Payment In Lieu Of Dividends",
        }
        source = ET.parse(TAX_XML)
        expected = [
            tx
            for tx in source.findall(
                "FlexStatements/FlexStatement/CashTransactions/CashTransaction"
            )
            if tx.get("type") in income_types
        ]
        assert types == {tx.get("type") for tx in expected}
        assert len(parsed.cash_transactions) == len(expected)
        assert "Deposits/Withdrawals" not in types

    def test_fx_rates_present(self, parsed):
        assert parsed.fx_rates[(date(2025, 12, 31), "CHF", "EUR")] == pytest.approx(
            1.074
        )
        assert parsed.fx_rates[(date(2025, 12, 31), "USD", "EUR")] == pytest.approx(
            0.85135
        )

    def test_statement_period_is_parsed(self, parsed):
        assert parsed.period_from == date(2025, 1, 1)
        assert parsed.period_to == date(2025, 12, 31)


def _parse_statement(tmp_path, body: str):
    xml = f"""<FlexQueryResponse><FlexStatements>
      <FlexStatement fromDate="01/01/2025" toDate="31/12/2025">
      <AccountInformation accountId="U1" name="A B" state="CH-ZH" currency="EUR"/>
      {body}
    </FlexStatement></FlexStatements></FlexQueryResponse>"""
    f = tmp_path / "mini.xml"
    f.write_text(xml, encoding="utf-8")
    return parse(str(f))


_X4 = """<OpenPosition levelOfDetail="SUMMARY" isin="X4" reportDate="31/12/2025"
    position="1" currency="EUR" markPrice="10" positionValue="10"/>"""


def test_positions_exclude_lot_rows_and_warn_on_other_dates(tmp_path):
    body = f"""<OpenPositions>
      <OpenPosition levelOfDetail="LOT" isin="X1" reportDate="31/12/2025" position="1"/>
      <OpenPosition levelOfDetail="SUMMARY" isin="X2" reportDate="30/06/2025" position="1"/>
      {_X4}
    </OpenPositions>"""
    with pytest.warns(UserWarning, match="2025-06-30"):
        parsed = _parse_statement(tmp_path, body)
    assert [p.isin for p in parsed.positions] == ["X4"]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            '<OpenPosition levelOfDetail="LOT" isin="X1" reportDate="31/12/2025"/>',
            "no SUMMARY rows",
        ),
        (
            '<OpenPosition levelOfDetail="SUMMARY" isin="X2" reportDate="30/12/2025"/>',
            "No open positions are reported at the statement period end",
        ),
        (
            '<OpenPosition levelOfDetail="SUMMARY" isin="" symbol="OPT" '
            'assetCategory="OPT" reportDate="31/12/2025"/>' + _X4,
            "'OPT'.*has no ISIN",
        ),
    ],
)
def test_positions_that_would_be_dropped_are_rejected(tmp_path, rows, message):
    with pytest.raises(ValueError, match=message):
        _parse_statement(tmp_path, f"<OpenPositions>{rows}</OpenPositions>")


def _cash(tx_type: str, amount: str = "10", isin: str = "", **attrs) -> str:
    element = ET.Element(
        "CashTransaction",
        {
            "type": tx_type,
            "currency": "CHF",
            "amount": amount,
            "settleDate": "15/06/2025",
            "isin": isin,
            **attrs,
        },
    )
    return ET.tostring(element, encoding="unicode")


def test_summary_cash_rows_are_not_counted_twice(tmp_path):
    rows = _cash("Broker Interest Received", levelOfDetail="DETAIL") + _cash(
        "Broker Interest Received", levelOfDetail="SUMMARY"
    )
    parsed = _parse_statement(tmp_path, f"<CashTransactions>{rows}</CashTransactions>")
    assert [tx.amount for tx in parsed.cash_transactions] == [10.0]


def test_summary_only_cash_rows_are_rejected(tmp_path):
    rows = _cash("Broker Interest Received", levelOfDetail="SUMMARY")
    with pytest.raises(ValueError, match="only SUMMARY rows"):
        _parse_statement(tmp_path, f"<CashTransactions>{rows}</CashTransactions>")


@pytest.mark.parametrize(
    "tx_type", ["Bond Interest Received", "Bond Interest Paid", "871(m) Withholding"]
)
def test_unsupported_tax_relevant_cash_types_are_rejected(tmp_path, tx_type):
    rows = _cash(tx_type, isin="US0000000001")
    with pytest.raises(ValueError, match="not supported"):
        _parse_statement(tmp_path, f"<CashTransactions>{rows}</CashTransactions>")


@pytest.mark.parametrize("tx_type", ["Dividends", "Payment In Lieu Of Dividends"])
def test_dividends_without_isin_are_rejected(tmp_path, tx_type):
    rows = _cash(tx_type, symbol="ABC")
    with pytest.raises(ValueError, match="'ABC' has no ISIN"):
        _parse_statement(tmp_path, f"<CashTransactions>{rows}</CashTransactions>")


def test_unrecognised_cash_types_warn_and_known_non_income_is_silent(tmp_path, recwarn):
    rows = _cash("Deposits/Withdrawals", "1000") + _cash("Mystery Credit")
    parsed = _parse_statement(tmp_path, f"<CashTransactions>{rows}</CashTransactions>")
    assert parsed.cash_transactions == []
    assert [str(w.message) for w in recwarn] == [
        "Ignoring cash transactions of unrecognised type: 'Mystery Credit'"
    ]


@pytest.mark.parametrize(
    ("attribute", "value", "field"),
    [
        ("amount", "N/A", "CashTransaction.amount"),
        ("settleDate", "2025-06-15", "CashTransaction.settleDate"),
    ],
)
def test_invalid_cash_transaction_fields_are_rejected(
    tmp_path, attribute, value, field
):
    attrs = {
        "type": "Dividends",
        "isin": "X1",
        "currency": "USD",
        "amount": "10",
        "settleDate": "15/06/2025",
    }
    attrs[attribute] = value
    cash_transaction = ET.Element("CashTransaction", attrs)
    cash_xml = ET.tostring(cash_transaction, encoding="unicode")
    xml = f"""<FlexQueryResponse><FlexStatements><FlexStatement>
      <AccountInformation accountId="U1" name="A B" state="CH-ZH" currency="EUR"/>
      <CashTransactions>{cash_xml}</CashTransactions>
    </FlexStatement></FlexStatements></FlexQueryResponse>"""
    path = tmp_path / "invalid.xml"
    path.write_text(xml, encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        parse(str(path))


@pytest.mark.parametrize("second_account", ["U1", "U2"])
def test_multiple_statements_are_rejected(tmp_path, second_account):
    xml = f"""<FlexQueryResponse><FlexStatements>
      <FlexStatement>
        <AccountInformation accountId="U1" name="A B" currency="EUR"/>
      </FlexStatement>
      <FlexStatement>
        <AccountInformation accountId="{second_account}" name="A B" currency="EUR"/>
      </FlexStatement>
    </FlexStatements></FlexQueryResponse>"""
    path = tmp_path / "multiple.xml"
    path.write_text(xml, encoding="utf-8")

    with pytest.raises(ValueError, match="Multiple FlexStatements"):
        parse(str(path))
