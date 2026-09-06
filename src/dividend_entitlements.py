"""Resolve cash payments to explicitly reported, unambiguous entitlements."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .parse_ibkr import CashTransaction, DividendAccrual

DIVIDEND_TYPES = {"Dividends", "Payment In Lieu Of Dividends"}
PaymentKey = tuple[date, str, str, str, str, str]


def payment_key(tx: CashTransaction) -> PaymentKey:
    return (
        tx.settle_date,
        tx.currency,
        tx.action_id,
        tx.conid,
        tx.model,
        tx.ex_date.isoformat() if tx.ex_date else "",
    )


def match_entitlement(
    transactions: list[CashTransaction], accruals: list[DividendAccrual]
) -> DividendAccrual:
    tx = transactions[0]
    adjustment = any(
        (t.tx_type == "Withholding Tax" and t.amount > 0)
        or (t.tx_type in DIVIDEND_TYPES and t.amount < 0)
        for t in transactions
    )
    candidates = []
    for accrual in accruals:
        if accrual.isin != tx.isin or accrual.model != tx.model:
            continue
        if tx.conid and accrual.conid != tx.conid:
            continue
        if tx.action_id and accrual.action_id and tx.action_id != accrual.action_id:
            continue
        if tx.ex_date and accrual.ex_date != tx.ex_date:
            continue
        shared_action = bool(tx.action_id and tx.action_id == accrual.action_id)
        if adjustment and not (shared_action or tx.ex_date):
            continue
        if not (shared_action or tx.ex_date or accrual.pay_date == tx.settle_date):
            continue
        candidates.append(accrual)

    income = [t for t in transactions if t.tx_type in DIVIDEND_TYPES]
    same_currency = [a for a in candidates if a.currency == tx.currency]
    # Tax can be booked in another currency, but only an unambiguous entitlement
    # can supply its quantity. Never use currency to alter the entitlement itself.
    if income or same_currency:
        candidates = same_currency

    # Po and Re are accrual lifecycle records, not extra shares or cash income.
    # Accept corroborating records only; conflicting corrections must be resolved.
    entitlements = {
        (
            a.quantity,
            a.ex_date,
            a.pay_date,
            abs(a.gross_amount),
            a.currency,
            a.conid,
            a.model,
            a.action_id,
        )
        for a in candidates
    }
    context = f"{tx.isin} on {tx.settle_date.isoformat()} ({tx.currency})"
    if len(entitlements) != 1:
        reason = "Missing" if not entitlements else "Ambiguous"
        raise ValueError(
            f"{reason} dividend entitlement for {context}. Include Change in "
            "Dividend Accruals with quantity, exDate, payDate, grossAmount, "
            "ISIN, currency and matching identifiers; reconcile corrections "
            "before generating the statement."
        )
    entitlement = candidates[0]
    if income:
        gross = sum((Decimal(str(t.amount)) for t in income), Decimal(0))
        cents = Decimal("0.01")
        if abs(gross).quantize(cents, rounding=ROUND_HALF_UP) != abs(
            entitlement.gross_amount
        ).quantize(cents, rounding=ROUND_HALF_UP):
            raise ValueError(
                f"Dividend gross amount does not match the accrual for {context}; "
                "reconcile split payments, cancellations or corrections."
            )
    return entitlement
