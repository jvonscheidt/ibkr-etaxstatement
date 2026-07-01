"""Shared fixtures for the eCH-196 converter test suite."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.parse_ibkr import AccountInfo, CashTransaction, IBKRData, OpenPosition

REPO_ROOT = Path(__file__).resolve().parent.parent
TAX_XML = REPO_ROOT / "data" / "Tax.xml"
XSD_PATH = REPO_ROOT / "documentation" / "eCH-0196-2-2.xsd"

YEAR_END = date(2025, 12, 31)


@pytest.fixture
def fx_rates() -> dict:
    """Minimal year-end FX table (currency -> EUR), matching data/Tax.xml."""
    return {
        (YEAR_END, "CHF", "EUR"): 1.074,
        (YEAR_END, "USD", "EUR"): 0.85135,
        # Early-January rate so cash-interest payments dated 06/01 can convert.
        (date(2025, 1, 6), "CHF", "EUR"): 1.064,
    }


@pytest.fixture
def account() -> AccountInfo:
    return AccountInfo(
        account_id="U1234567",
        name="Max Mustermann",
        first_name="Max",
        last_name="Mustermann",
        canton="ZH",
        base_currency="EUR",
        ib_entity="IBKR",
    )


@pytest.fixture
def eur_position() -> OpenPosition:
    return OpenPosition(
        isin="IE00BKM4GZ66",
        symbol="EIMI",
        description="ISHARES CORE MSCI EM IMI ACC",
        currency="EUR",
        fx_rate_to_base=1.0,
        quantity=100.0,
        mark_price=30.0,
        position_value=3000.0,
        issuer_country_code="IE",
        report_date=YEAR_END,
        sub_category="ETF",
    )


@pytest.fixture
def data(account, eur_position, fx_rates) -> IBKRData:
    """A minimal but complete IBKRData: one EUR ETF, one cash interest receipt."""
    interest = CashTransaction(
        settle_date=date(2025, 1, 6),
        currency="CHF",
        fx_rate_to_base=1.074,
        amount=2.85,
        tx_type="Broker Interest Received",
        description="CHF Credit Interest",
        isin="",
        symbol="",
    )
    return IBKRData(
        account=account,
        positions=[eur_position],
        cash_transactions=[interest],
        fx_rates=dict(fx_rates),
    )
