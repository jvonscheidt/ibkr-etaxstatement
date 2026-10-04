"""Parse an IBKR FlexQuery XML export into clean Python dataclasses."""

from __future__ import annotations

import math
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation


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


def _required_decimal(value: str | None, field: str) -> Decimal:
    if value is None or not value.strip():
        raise ValueError(f"Invalid or missing {field}: {value!r}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid or missing {field}: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"Invalid or missing {field}: {value!r}")
    return parsed


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
    quantity: Decimal | float
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
    action_id: str = ""
    conid: str = ""
    model: str = ""
    ex_date: date | None = None


@dataclass(frozen=True)
class DividendAccrual:
    isin: str
    currency: str
    ex_date: date
    pay_date: date
    quantity: Decimal
    gross_amount: Decimal
    action_id: str = ""
    conid: str = ""
    model: str = ""


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
    dividend_accruals: list[DividendAccrual] = field(default_factory=list)


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
    rows = stmt.findall("OpenPositions/OpenPosition")
    # LOT rows repeat their SUMMARY row; without SUMMARY rows holdings would vanish.
    summaries = [op for op in rows if op.get("levelOfDetail") == "SUMMARY"]
    if rows and not summaries:
        raise ValueError(
            "OpenPositions contain no SUMMARY rows; enable the Summary level of "
            "detail for Open Positions in the FlexQuery."
        )
    positions = []
    skipped_dates = set()
    for op in summaries:
        report_date = _required_date(op.get("reportDate"), "OpenPosition.reportDate")
        if period_to is not None and report_date != period_to:
            skipped_dates.add(report_date)
            continue
        if period_to is None and (report_date.month != 12 or report_date.day != 31):
            skipped_dates.add(report_date)
            continue
        isin = op.get("isin", "")
        if not isin:
            raise ValueError(
                f"Open position {op.get('symbol', '')!r} "
                f"(assetCategory {op.get('assetCategory', '')!r}) has no ISIN; "
                "positions without an ISIN are not supported."
            )
        positions.append(
            OpenPosition(
                isin=isin,
                symbol=op.get("symbol", ""),
                description=op.get("description", ""),
                currency=op.get("currency", ""),
                fx_rate_to_base=_required_float(
                    op.get("fxRateToBase", "1"), "OpenPosition.fxRateToBase"
                ),
                quantity=_required_decimal(op.get("position"), "OpenPosition.position"),
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
    if skipped_dates and not positions:
        raise ValueError(
            "No open positions are reported at the statement period end; found "
            "only " + ", ".join(sorted(d.isoformat() for d in skipped_dates))
        )
    if skipped_dates:
        warnings.warn(
            "Ignoring open positions not reported at the statement period end: "
            + ", ".join(sorted(d.isoformat() for d in skipped_dates)),
            stacklevel=2,
        )
    return positions


_INCOME_TYPES = {
    "Withholding Tax",
    "Broker Interest Received",
    "Broker Interest Paid",
    "Dividends",
    "Payment In Lieu Of Dividends",
}
# Tax-relevant cash flows this converter cannot report; failing beats omitting.
_UNSUPPORTED_TYPES = {
    "Bond Interest Received",
    "Bond Interest Paid",
    "871(m) Withholding",
}
# Capital movements and fees with no eCH-0196 income or withholding field.
_IGNORED_TYPES = {
    "Deposits/Withdrawals",
    "Deposits & Withdrawals",
    "Other Fees",
    "Broker Fees",
    "Advisor Fees",
    "Commission Adjustments",
}


def _parse_cash_transactions(stmt) -> list[CashTransaction]:
    rows = stmt.findall("CashTransactions/CashTransaction")
    # SUMMARY rows aggregate DETAIL rows; counting both would double the income.
    details = [ct for ct in rows if ct.get("levelOfDetail") != "SUMMARY"]
    if rows and not details:
        raise ValueError(
            "CashTransactions contain only SUMMARY rows; enable the Detail level "
            "of detail for Cash Transactions in the FlexQuery."
        )
    txs = []
    unknown_types = set()
    for ct in details:
        tx_type = ct.get("type", "")
        if tx_type in _UNSUPPORTED_TYPES:
            raise ValueError(
                f"Cash transaction type {tx_type!r} is not supported; it cannot "
                "be reported in the eCH-0196 statement."
            )
        if tx_type in _IGNORED_TYPES:
            continue
        if tx_type not in _INCOME_TYPES:
            unknown_types.add(tx_type)
            continue
        if tx_type in ("Dividends", "Payment In Lieu Of Dividends") and not ct.get(
            "isin"
        ):
            raise ValueError(
                f"{tx_type} for {ct.get('symbol', '')!r} has no ISIN and cannot be "
                "attributed to a security."
            )
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
                action_id=ct.get("actionID", ""),
                conid=ct.get("conid", ""),
                model=ct.get("model", ""),
                ex_date=_optional_date(ct.get("exDate"), "CashTransaction.exDate"),
            )
        )
    if unknown_types:
        warnings.warn(
            "Ignoring cash transactions of unrecognised type: "
            + ", ".join(sorted(repr(t) for t in unknown_types)),
            stacklevel=2,
        )
    return txs


def _parse_dividend_accruals(stmt, account_id: str) -> list[DividendAccrual]:
    accruals = []
    for elem in stmt.findall("ChangeInDividendAccruals/ChangeInDividendAccrual"):
        if elem.get("accountId", account_id) != account_id:
            raise ValueError("Dividend accrual account does not match the statement")
        isin = elem.get("isin", "")
        currency = elem.get("currency", "")
        if not isin or not currency:
            raise ValueError("Dividend accruals require ISIN and currency")
        quantity = _required_decimal(elem.get("quantity"), "DividendAccrual.quantity")
        if quantity <= 0:
            raise ValueError(
                "Dividend accrual quantity must be positive; non-positive "
                "entitlements require manual reconciliation"
            )
        ex_date = _required_date(elem.get("exDate"), "DividendAccrual.exDate")
        pay_date = _required_date(elem.get("payDate"), "DividendAccrual.payDate")
        if ex_date > pay_date:
            raise ValueError("Dividend accrual exDate must not be after payDate")
        accruals.append(
            DividendAccrual(
                isin=isin,
                currency=currency,
                ex_date=ex_date,
                pay_date=pay_date,
                quantity=quantity,
                gross_amount=_required_decimal(
                    elem.get("grossAmount"), "DividendAccrual.grossAmount"
                ),
                action_id=elem.get("actionID", ""),
                conid=elem.get("conid", ""),
                model=elem.get("model", ""),
            )
        )
    return accruals


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
    statements = root.findall("FlexStatements/FlexStatement")
    if not statements:
        raise ValueError("No FlexStatement found in XML")
    if len(statements) != 1:
        raise ValueError(
            "Multiple FlexStatements are not supported; export one account "
            "and one full calendar year per input file."
        )
    stmt = statements[0]

    period_from = _optional_date(stmt.get("fromDate"), "FlexStatement.fromDate")
    period_to = _optional_date(stmt.get("toDate"), "FlexStatement.toDate")
    account = _parse_account(stmt.find("AccountInformation"))

    return IBKRData(
        account=account,
        positions=_parse_positions(stmt, period_to),
        cash_transactions=_parse_cash_transactions(stmt),
        fx_rates=_parse_fx_rates(stmt),
        period_from=period_from,
        period_to=period_to,
        dividend_accruals=_parse_dividend_accruals(stmt, account.account_id),
    )
