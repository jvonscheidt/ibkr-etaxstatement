"""Parse an IBKR FlexQuery XML export into clean Python dataclasses."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date


def _date(s: str) -> date | None:
    """Parse DD/MM/YYYY; strip time component if present."""
    if not s:
        return None
    s = s.split(";")[0]
    try:
        day, month, year = (int(part) for part in s.split("/"))
        return date(year, month, day)
    except ValueError:
        return None


def _float(s: str) -> float:
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _required_date(value: str | None, field: str) -> date:
    parsed = _date(value or "")
    if parsed is None:
        raise ValueError(f"Invalid or missing {field}: {value!r}")
    return parsed


def _required_float(value: str | None, field: str) -> float:
    if value is None or not value.strip():
        raise ValueError(f"Invalid or missing {field}: {value!r}")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid or missing {field}: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"Invalid or missing {field}: {value!r}")
    return parsed


def _optional_date(value: str | None, field: str) -> date | None:
    if not value:
        return None
    return _required_date(value, field)


@dataclass
class AccountInfo:
    account_id: str
    name: str
    first_name: str
    last_name: str
    canton: str  # e.g. "ZH" from state "CH-ZH"
    base_currency: str
    ib_entity: str


@dataclass
class OpenPosition:
    isin: str
    symbol: str
    description: str
    currency: str
    fx_rate_to_base: float  # currency → base (EUR)
    quantity: float
    mark_price: float
    position_value: float  # in position currency
    issuer_country_code: str
    report_date: date
    sub_category: str  # e.g. "ETF"


@dataclass
class CashTransaction:
    settle_date: date
    currency: str
    fx_rate_to_base: float  # currency → EUR
    amount: float
    tx_type: (
        str  # "Withholding Tax" | "Broker Interest Received" | "Broker Interest Paid"
    )
    # | "Dividends" | "Payment In Lieu Of Dividends"
    description: str
    isin: str  # empty for cash interest/WHT
    symbol: str
    asset_category: str = ""
    sub_category: str = ""
    issuer_country_code: str = ""


@dataclass
class IBKRData:
    account: AccountInfo
    positions: list[OpenPosition]
    cash_transactions: list[CashTransaction]
    # (report_date, from_currency, to_currency) → rate
    # All rates are X → EUR (base currency)
    fx_rates: dict[tuple[date, str, str], float]
    period_from: date | None = None
    period_to: date | None = None


def _parse_account(elem) -> AccountInfo:
    name = elem.get("name", "")
    parts = name.split()
    first_name = parts[0] if parts else ""
    # Handle "von", "de", "van" prefixes in last name
    if len(parts) >= 3 and parts[-2].lower() in ("von", "de", "van", "der", "den"):
        last_name = f"{parts[-2]} {parts[-1]}"
    elif len(parts) >= 2:
        last_name = parts[-1]
    else:
        last_name = name
    state = elem.get("state", "")
    canton = state.split("-")[1] if "-" in state else state
    return AccountInfo(
        account_id=elem.get("accountId", ""),
        name=name,
        first_name=first_name,
        last_name=last_name,
        canton=canton,
        base_currency=elem.get("currency", "EUR"),
        ib_entity=elem.get("ibEntity", ""),
    )


def _parse_positions(stmt, period_to: date | None = None) -> list[OpenPosition]:
    positions = []
    for op in stmt.findall("OpenPositions/OpenPosition"):
        if op.get("levelOfDetail") != "SUMMARY":
            continue
        report_date = _required_date(op.get("reportDate"), "OpenPosition.reportDate")
        if period_to is not None and report_date != period_to:
            continue
        if period_to is None and (report_date.month != 12 or report_date.day != 31):
            continue
        isin = op.get("isin", "")
        if not isin:
            continue
        positions.append(
            OpenPosition(
                isin=isin,
                symbol=op.get("symbol", ""),
                description=op.get("description", ""),
                currency=op.get("currency", ""),
                fx_rate_to_base=_required_float(
                    op.get("fxRateToBase", "1"), "OpenPosition.fxRateToBase"
                ),
                quantity=_required_float(op.get("position"), "OpenPosition.position"),
                mark_price=_required_float(
                    op.get("markPrice"), "OpenPosition.markPrice"
                ),
                position_value=_required_float(
                    op.get("positionValue"), "OpenPosition.positionValue"
                ),
                issuer_country_code=op.get("issuerCountryCode", ""),
                report_date=report_date,
                sub_category=op.get("subCategory", ""),
            )
        )
    return positions


_INCOME_TYPES = {
    "Withholding Tax",
    "Broker Interest Received",
    "Broker Interest Paid",
    "Dividends",
    "Payment In Lieu Of Dividends",
}


def _parse_cash_transactions(stmt) -> list[CashTransaction]:
    txs = []
    for ct in stmt.findall("CashTransactions/CashTransaction"):
        tx_type = ct.get("type", "")
        if tx_type not in _INCOME_TYPES:
            continue
        settle = ct.get("settleDate", "") or ct.get("dateTime", "")
        txs.append(
            CashTransaction(
                settle_date=_required_date(settle, "CashTransaction.settleDate"),
                currency=ct.get("currency", ""),
                fx_rate_to_base=_required_float(
                    ct.get("fxRateToBase", "1"), "CashTransaction.fxRateToBase"
                ),
                amount=_required_float(ct.get("amount"), "CashTransaction.amount"),
                tx_type=tx_type,
                description=ct.get("description", ""),
                isin=ct.get("isin", ""),
                symbol=ct.get("symbol", ""),
                asset_category=ct.get("assetCategory", ""),
                sub_category=ct.get("subCategory", ""),
                issuer_country_code=ct.get("issuerCountryCode", ""),
            )
        )
    return txs


def _parse_fx_rates(stmt) -> dict[tuple[date, str, str], float]:
    rates: dict[tuple[date, str, str], float] = {}
    for cr in stmt.findall("ConversionRates/ConversionRate"):
        rd = _required_date(cr.get("reportDate"), "ConversionRate.reportDate")
        from_c = cr.get("fromCurrency", "")
        to_c = cr.get("toCurrency", "")
        rate = _required_float(cr.get("rate"), "ConversionRate.rate")
        # IBKR emits -1 when no rate is available for a currency/date.
        if rate <= 0:
            continue
        if from_c and to_c:
            rates[(rd, from_c, to_c)] = rate
    return rates


def parse(xml_path: str) -> IBKRData:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    stmt = root.find("FlexStatements/FlexStatement")
    if stmt is None:
        raise ValueError("No FlexStatement found in XML")

    period_from = _optional_date(stmt.get("fromDate"), "FlexStatement.fromDate")
    period_to = _optional_date(stmt.get("toDate"), "FlexStatement.toDate")

    return IBKRData(
        account=_parse_account(stmt.find("AccountInformation")),
        positions=_parse_positions(stmt, period_to),
        cash_transactions=_parse_cash_transactions(stmt),
        fx_rates=_parse_fx_rates(stmt),
        period_from=period_from,
        period_to=period_to,
    )
