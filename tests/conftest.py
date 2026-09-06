"""Shared fixtures for the eCH-196 converter test suite."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.parse_ibkr import (
    AccountInfo,
    CashTransaction,
    DividendAccrual,
    IBKRData,
    OpenPosition,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TAX_XML = REPO_ROOT / "data" / "Tax.xml"
XSD_PATH = REPO_ROOT / "documentation" / "eCH-0196-2-2.xsd"
INCOME_XML = REPO_ROOT / "tests" / "fixtures" / "dividend_accruals.xml"

YEAR_END = date(2025, 12, 31)


def with_dividend_accruals(data: IBKRData) -> IBKRData:
    """Supply explicit synthetic ten-share entitlements for accounting-only tests."""
    groups = {}
    for tx in data.cash_transactions:
        if tx.isin:
            groups.setdefault((tx.isin, tx.currency, tx.settle_date), []).append(tx)
    accruals = []
    transactions = []
    for tx in data.cash_transactions:
        transactions.append(
            replace(tx, ex_date=tx.settle_date - timedelta(days=1)) if tx.isin else tx
        )
    for (isin, currency, day), txs in groups.items():
        income = [
            t for t in txs if t.tx_type in {"Dividends", "Payment In Lieu Of Dividends"}
        ]
        if not income and any(
            t.isin == isin
            and t.settle_date == day
            and t.tx_type in {"Dividends", "Payment In Lieu Of Dividends"}
            for t in data.cash_transactions
        ):
            continue
        gross = (
            sum((Decimal(str(t.amount)) for t in income), Decimal(0))
            if income
            else Decimal(100)
        )
        accruals.append(
            DividendAccrual(
                isin, currency, day - timedelta(days=1), day, Decimal(10), abs(gross)
            )
        )
    return replace(data, cash_transactions=transactions, dividend_accruals=accruals)


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
