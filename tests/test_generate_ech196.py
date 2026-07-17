"""Tests for eCH-196 XML generation (src/generate_ech196.py)."""

from __future__ import annotations

from datetime import date

import pytest

from src.generate_ech196 import (
    NS,
    _chf,
    _fx_to_chf,
    _security_category,
    _security_type,
    build,
    serialize,
)

YEAR_END = date(2025, 12, 31)


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


class TestChfFormatting:
    def test_two_decimal_places(self):
        assert _chf(3000) == "3000.00"

    def test_half_up_rounding(self):
        assert _chf(1.005) == "1.01"
        assert _chf(2.675) == "2.68"


class TestFxToChf:
    def test_chf_is_identity(self, fx_rates):
        assert _fx_to_chf("CHF", YEAR_END, fx_rates) == 1.0

    def test_eur_to_chf_is_reciprocal(self, fx_rates):
        # EUR->CHF = 1 / (CHF->EUR) = 1 / 1.074
        assert _fx_to_chf("EUR", YEAR_END, fx_rates) == pytest.approx(1 / 1.074)

    def test_usd_to_chf_via_eur(self, fx_rates):
        # USD->CHF = (USD->EUR) / (CHF->EUR)
        assert _fx_to_chf("USD", YEAR_END, fx_rates) == pytest.approx(0.85135 / 1.074)

    def test_walks_back_to_earlier_rate(self):
        rates = {(date(2025, 12, 29), "CHF", "EUR"): 1.07}
        # Query on the 31st; nearest earlier rate within 5 days is the 29th.
        assert _fx_to_chf("EUR", date(2025, 12, 31), rates) == pytest.approx(1 / 1.07)

    def test_missing_chf_rate_raises(self):
        with pytest.raises(ValueError):
            _fx_to_chf("EUR", YEAR_END, {})

    def test_missing_foreign_rate_raises(self, fx_rates):
        with pytest.raises(ValueError):
            _fx_to_chf("GBP", YEAR_END, fx_rates)


class TestSecurityClassification:
    def _pos(self, sub_category="ETF", description="FUND"):
        from src.parse_ibkr import OpenPosition
        return OpenPosition(
            isin="X", symbol="S", description=description, currency="EUR",
            fx_rate_to_base=1.0, quantity=1, mark_price=1, position_value=1,
            issuer_country_code="IE", report_date=YEAR_END, sub_category=sub_category,
        )

    def test_etf_maps_to_fund(self):
        assert _security_category(self._pos(sub_category="ETF")) == "FUND"

    def test_stock_maps_to_share(self):
        assert _security_category(self._pos(sub_category="STK")) == "SHARE"

    def test_accumulation_detected(self):
        assert _security_type(self._pos(description="MSCI WORLD ACC")) == "FUND.ACCUMULATION"

    def test_distribution_detected(self):
        assert _security_type(self._pos(description="MSCI WORLD DIST")) == "FUND.DISTRIBUTION"

    def test_unknown_defaults_to_accumulation(self):
        with pytest.warns(UserWarning):
            assert _security_type(self._pos(description="SOME FUND")) == "FUND.ACCUMULATION"

    def test_share_category_has_no_fund_security_type(self):
        # Stocks use the SHARE.* enumeration, not FUND.ACCUMULATION/DISTRIBUTION.
        # "APPLE INC" would previously match the " INC" substring heuristic and
        # be mislabeled as a distributing fund despite being a plain stock.
        pos = self._pos(sub_category="STK", description="APPLE INC")
        assert _security_type(pos) is None

    def test_word_boundary_avoids_false_positive(self):
        # "INCORPORATED" contains "INC" as a substring but not as a whole word;
        # the old `" INC" in text` heuristic misclassified this as DIST.
        pos = self._pos(description="GLOBAL INCORPORATED FUND")
        with pytest.warns(UserWarning):
            assert _security_type(pos) == "FUND.ACCUMULATION"


class TestBuild:
    def test_root_metadata(self, data):
        root = build(data)
        assert root.tag == _q("taxStatement")
        assert root.get("canton") == "ZH"
        assert root.get("taxPeriod") == "2025"
        assert root.get("periodFrom") == "2025-01-01"
        assert root.get("periodTo") == "2025-12-31"

    def test_security_value_uses_year_end_rate(self, data):
        root = build(data)
        tax_value = root.find(f"{_q('listOfSecurities')}/{_q('depot')}/{_q('security')}/{_q('taxValue')}")
        assert tax_value is not None
        # 3000 EUR * (1/1.074) CHF/EUR
        assert float(tax_value.get("value")) == pytest.approx(3000 / 1.074, abs=0.01)

    def test_eur_chf_override_changes_valuation(self, data):
        root = build(data, eur_chf_override=0.90)
        tax_value = root.find(f"{_q('listOfSecurities')}/{_q('depot')}/{_q('security')}/{_q('taxValue')}")
        # 3000 EUR * 0.90 = 2700.00 CHF
        assert float(tax_value.get("value")) == pytest.approx(2700.00, abs=0.01)

    def test_bank_accounts_precede_securities(self, data):
        root = build(data)
        children = [c.tag for c in root]
        assert _q("listOfBankAccounts") in children, "cash interest should produce a bank account"
        assert children.index(_q("listOfBankAccounts")) < children.index(_q("listOfSecurities"))

    def test_broker_interest_paid_becomes_liability(self, account, eur_position, fx_rates):
        from src.parse_ibkr import CashTransaction, IBKRData

        debit_interest = CashTransaction(
            settle_date=date(2025, 3, 5), currency="USD", fx_rate_to_base=0.85,
            amount=-0.15, tx_type="Broker Interest Paid",
            description="USD DEBIT INT FOR FEB-2025", isin="", symbol="",
        )
        fx = dict(fx_rates)
        fx[(date(2025, 3, 5), "USD", "EUR")] = 0.86
        fx[(date(2025, 3, 5), "CHF", "EUR")] = 1.07
        data = IBKRData(
            account=account, positions=[eur_position],
            cash_transactions=[debit_interest], fx_rates=fx,
        )
        root = build(data)

        liabilities = root.find(_q("listOfLiabilities"))
        assert liabilities is not None, "debit interest should produce a listOfLiabilities section"
        payment = liabilities.find(f"{_q('liabilityAccount')}/{_q('payment')}")
        assert payment is not None
        assert float(payment.get("grossRevenueB")) > 0

        # Debt interest is an expense, not income — it must NOT be folded into
        # the document's totalGrossRevenueB (which is income-only per XSD).
        assert root.get("totalGrossRevenueB") == "0.00"

        children = [c.tag for c in root]
        assert children.index(_q("listOfLiabilities")) < children.index(_q("listOfSecurities"))

    def test_no_liabilities_section_when_no_debit_interest(self, data):
        root = build(data)
        assert root.find(_q("listOfLiabilities")) is None

    def test_doc_id_embeds_date_and_seq(self, data):
        root = build(data)
        doc_id = root.get("id")
        # BEIL2 §2.1 layout: CH + clearing(5) + docpage(2) + account(14) + date(8) + seq(2)
        assert doc_id.startswith("CH")
        assert "20251231" in doc_id
        assert doc_id.endswith("01")

    def test_period_is_derived_from_export(self, account):
        from src.parse_ibkr import IBKRData, OpenPosition

        year_end = date(2024, 12, 31)
        position = OpenPosition(
            isin="IE00BKM4GZ66", symbol="EIMI",
            description="ISHARES CORE MSCI EM IMI ACC", currency="EUR",
            fx_rate_to_base=1.0, quantity=1, mark_price=10, position_value=10,
            issuer_country_code="IE", report_date=year_end, sub_category="ETF",
        )
        data = IBKRData(
            account, [position], [],
            {(year_end, "CHF", "EUR"): 1.05},
            period_from=date(2024, 1, 1), period_to=year_end,
        )

        root = build(data)

        assert root.get("taxPeriod") == "2024"
        assert root.get("periodFrom") == "2024-01-01"
        assert root.get("periodTo") == "2024-12-31"
        assert "20241231" in root.get("id")
        tax_value = root.find(
            f"{_q('listOfSecurities')}/{_q('depot')}/"
            f"{_q('security')}/{_q('taxValue')}"
        )
        assert tax_value.get("referenceDate") == "2024-12-31"

    def test_partial_year_export_is_rejected(self, data):
        data.period_from = date(2025, 2, 1)
        with pytest.raises(ValueError, match="full calendar year"):
            build(data)

    def test_serialize_roundtrips(self, data):
        xml = serialize(build(data))
        assert xml.startswith("<taxStatement") or "taxStatement" in xml
        assert NS in xml
