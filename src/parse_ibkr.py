"""Parse an IBKR FlexQuery XML export into clean Python dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
import xml.etree.ElementTree as ET


def _date(s: str) -> Optional[date]:
    """Parse DD/MM/YYYY; strip time component if present."""
    if not s:
        return None
    s = s.split(";")[0]
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        return None


def _float(s: str) -> float:
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


@dataclass
class AccountInfo:
    account_id: str
    name: str
    first_name: str
    last_name: str
    canton: str       # e.g. "ZH" from state "CH-ZH"
    base_currency: str
    ib_entity: str


@dataclass
class OpenPosition:
    isin: str
    symbol: str
    description: str
    currency: str
    fx_rate_to_base: float   # currency → base (EUR)
    quantity: float
    mark_price: float
    position_value: float    # in position currency
    issuer_country_code: str
    report_date: date
    sub_category: str        # e.g. "ETF"


@dataclass
class CashTransaction:
    settle_date: date
    currency: str
    fx_rate_to_base: float   # currency → EUR
    amount: float
    tx_type: str             # "Withholding Tax" | "Broker Interest Received" | "Broker Interest Paid"
                             # | "Dividends" | "Payment In Lieu Of Dividends"
    description: str
    isin: str                # empty for cash interest/WHT
    symbol: str


@dataclass
class IBKRData:
    account: AccountInfo
    positions: list[OpenPosition]
    cash_transactions: list[CashTransaction]
    # (report_date, from_currency, to_currency) → rate
    # All rates are X → EUR (base currency)
    fx_rates: dict[tuple[date, str, str], float]


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


def _parse_positions(stmt) -> list[OpenPosition]:
    positions = []
    for op in stmt.findall("OpenPositions/OpenPosition"):
        if op.get("levelOfDetail") != "SUMMARY":
            continue
        report_date = _date(op.get("reportDate", ""))
        if report_date is None or report_date.month != 12 or report_date.day != 31:
            continue
        isin = op.get("isin", "")
        if not isin:
            continue
        positions.append(OpenPosition(
            isin=isin,
            symbol=op.get("symbol", ""),
            description=op.get("description", ""),
            currency=op.get("currency", ""),
            fx_rate_to_base=_float(op.get("fxRateToBase", "1")) or 1.0,
            quantity=_float(op.get("position", "0")),
            mark_price=_float(op.get("markPrice", "0")),
            position_value=_float(op.get("positionValue", "0")),
            issuer_country_code=op.get("issuerCountryCode", ""),
            report_date=report_date,
            sub_category=op.get("subCategory", ""),
        ))
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
        txs.append(CashTransaction(
            settle_date=_date(settle),
            currency=ct.get("currency", ""),
            fx_rate_to_base=_float(ct.get("fxRateToBase", "1")) or 1.0,
            amount=_float(ct.get("amount", "0")),
            tx_type=tx_type,
            description=ct.get("description", ""),
            isin=ct.get("isin", ""),
            symbol=ct.get("symbol", ""),
        ))
    return txs


def _parse_fx_rates(stmt) -> dict[tuple[date, str, str], float]:
    rates: dict[tuple[date, str, str], float] = {}
    for cr in stmt.findall("ConversionRates/ConversionRate"):
        rd = _date(cr.get("reportDate", ""))
        from_c = cr.get("fromCurrency", "")
        to_c = cr.get("toCurrency", "")
        rate = _float(cr.get("rate", ""))
        if rd and from_c and to_c and rate:
            rates[(rd, from_c, to_c)] = rate
    return rates


def parse(xml_path: str) -> IBKRData:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    stmt = root.find("FlexStatements/FlexStatement")
    if stmt is None:
        raise ValueError("No FlexStatement found in XML")

    return IBKRData(
        account=_parse_account(stmt.find("AccountInformation")),
        positions=_parse_positions(stmt),
        cash_transactions=_parse_cash_transactions(stmt),
        fx_rates=_parse_fx_rates(stmt),
    )
