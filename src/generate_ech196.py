"""Build an eCH-0196 v2.2.0 XML tree from parsed IBKR data."""

from __future__ import annotations

import math
import re
import warnings
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from .dividend_entitlements import (
    DIVIDEND_TYPES,
    PaymentKey,
    match_entitlement,
    payment_key,
)
from .parse_ibkr import CashTransaction, DividendAccrual, IBKRData, OpenPosition

NS = "http://www.ech.ch/xmlns/eCH-0196/2"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", NS)
ET.register_namespace("xsi", NS_XSI)

# Real accepted eSteuerauszüge declare the XML-Schema-instance namespace and a
# schemaLocation pointing at the eCH-0196 2.2 XSD. ZHPrivateTax rejects our
# document ("keine gültigen Daten") without them, even though they don't affect
# XSD validation. minorVersion is likewise "22" for a v2.2 document (a real 2025
# reference uses 22); we previously emitted "0", which labels the payload v2.0.
SCHEMA_LOCATION = (
    "http://www.ech.ch/xmlns/eCH-0196/2 "
    "http://www.ech.ch/xmlns/eCH-0196/2.2/eCH-0196-2-2.xsd"
)
MINOR_VERSION = "22"

# Interactive Brokers' clearing number, used in the eCH-196 document id and
# the eCH-0270 barcode's Code 128 payload (imported by generate_barcode_pdf).
IBKR_CLEARING_NUMBER = "89095"


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _chf(value: float | Decimal) -> str:
    """Round to 2 decimal places and format as string."""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _quantity(value: float | Decimal) -> str:
    """Preserve fractional units and avoid exponents, which XSD decimals forbid."""
    return format(Decimal(str(value)), "f")


def _total(elements: Iterable[ET.Element], attribute: str) -> str:
    return _chf(sum((Decimal(el.get(attribute, "0")) for el in elements), Decimal(0)))


def _security_country(isin: str, issuer_country: str) -> str:
    if issuer_country:
        return issuer_country.upper()
    return isin[:2].upper() if len(isin) >= 2 and isin[:2].isalpha() else "XX"


def _fx_to_chf(currency: str, ref_date: date, fx_rates: dict) -> float:
    """
    Return the exchange rate: 1 unit of `currency` = X CHF.

    All rates in fx_rates are currency→EUR. Base currency is EUR.
    CHF→EUR rate gives us EUR→CHF = 1/(CHF→EUR).
    For any other currency: currency→CHF = (currency→EUR) / (CHF→EUR).
    """
    if currency == "CHF":
        return 1.0

    # Find the closest available rate on or before ref_date
    def _rate(from_c: str, to_c: str) -> float | None:
        # Try exact date first, then walk back up to 5 trading days
        from datetime import timedelta

        for delta in range(6):
            d = ref_date - timedelta(days=delta)
            r = fx_rates.get((d, from_c, to_c))
            if r is not None:
                return r
        return None

    chf_eur = _rate("CHF", "EUR")
    if chf_eur is None or chf_eur == 0:
        raise ValueError(f"No CHF→EUR rate found near {ref_date}")

    if currency == "EUR":
        return 1.0 / chf_eur

    cur_eur = _rate(currency, "EUR")
    if cur_eur is None:
        raise ValueError(f"No {currency}→EUR rate found near {ref_date}")
    return cur_eur / chf_eur


def _security_category(pos: OpenPosition) -> str:
    """Map IBKR subCategory to eCH-196 securityCategoryType."""
    if pos.sub_category == "ETF":
        return "FUND"
    return "SHARE"


def _transaction_security_category(tx: CashTransaction) -> str:
    """Map the limited metadata available for a closed security."""
    if tx.sub_category == "ETF":
        return "FUND"
    if tx.asset_category == "STK":
        return "SHARE"
    return "OTHER"


_ACC_TOKENS = re.compile(r"\b(?:ACC|1C|ACCUM\w*)\b")
_DIST_TOKENS = re.compile(r"\b(?:DIST\w*|INC|INCOME)\b")


def _security_type(pos: OpenPosition) -> str | None:
    """Guess accumulation vs distribution from ETF name.

    Only applicable to FUND-category securities (eCH-196 has a separate
    SHARE.* enumeration for stocks); returns None otherwise so the optional
    `securityType` attribute is simply omitted.
    """
    if _security_category(pos) != "FUND":
        return None
    name_upper = pos.description.upper()
    if _ACC_TOKENS.search(name_upper):
        return "FUND.ACCUMULATION"
    if _DIST_TOKENS.search(name_upper):
        return "FUND.DISTRIBUTION"
    warnings.warn(
        f"Could not determine accumulating/distributing status for "
        f"{pos.description!r} (ISIN {pos.isin}); defaulting to FUND.ACCUMULATION.",
        stacklevel=2,
    )
    return "FUND.ACCUMULATION"


def _statement_period(data: IBKRData) -> tuple[date, date]:
    """Return the full calendar-year period represented by the export."""
    period_to = data.period_to
    if period_to is None and data.positions:
        period_to = max(pos.report_date for pos in data.positions)
    if period_to is None and data.cash_transactions:
        period_to = date(
            max(tx.settle_date.year for tx in data.cash_transactions), 12, 31
        )
    if period_to is None:
        raise ValueError("Cannot determine tax period from the IBKR export")

    period_from = data.period_from or date(period_to.year, 1, 1)
    expected_from = date(period_to.year, 1, 1)
    expected_to = date(period_to.year, 12, 31)
    if period_from != expected_from or period_to != expected_to:
        raise ValueError(
            "IBKR export must cover one full calendar year "
            f"({expected_from.isoformat()} through {expected_to.isoformat()})"
        )
    return period_from, period_to


def _build_securities(
    data: IBKRData, year_end: date, valuation_fx_rates: dict
) -> ET.Element:
    """Build holdings and income once per ISIN, including closed securities."""
    list_el = ET.Element(_q("listOfSecurities"))
    depot_el = ET.SubElement(list_el, _q("depot"), depotNumber=data.account.account_id)

    # Group security-linked income/WHT by isin. Dividends (and payments in lieu)
    # are the per-security income; broker interest is cash income handled elsewhere.
    income_by_isin: dict[str, list[CashTransaction]] = {}
    wht_by_isin: dict[str, list[CashTransaction]] = {}
    for tx in data.cash_transactions:
        if not tx.isin:
            continue
        if tx.tx_type in DIVIDEND_TYPES:
            income_by_isin.setdefault(tx.isin, []).append(tx)
        elif tx.tx_type == "Withholding Tax":
            wht_by_isin.setdefault(tx.isin, []).append(tx)

    positions_by_isin: dict[str, list[OpenPosition]] = {}
    for pos in data.positions:
        positions_by_isin.setdefault(pos.isin, []).append(pos)

    for idx, positions in enumerate(positions_by_isin.values(), start=1):
        pos = positions[0]
        rate = _fx_to_chf(pos.currency, year_end, valuation_fx_rates)
        quantity = sum((Decimal(str(p.quantity)) for p in positions), Decimal(0))
        # Express multiple listings in the first listing's currency, preserving
        # their combined CHF value rather than duplicating their cash flows.
        balance = sum(
            (
                p.position_value
                if p.currency == pos.currency
                else p.position_value
                * _fx_to_chf(p.currency, year_end, valuation_fx_rates)
                / rate
            )
            for p in positions
        )
        chf_value = round(balance * rate, 2)

        sec_attrs = {
            "positionId": str(idx),
            "country": _security_country(pos.isin, pos.issuer_country_code),
            "currency": pos.currency,
            "quotationType": "PIECE",
            "securityCategory": _security_category(pos),
            "securityName": pos.description[:60],
        }
        if pos.isin:
            sec_attrs["isin"] = pos.isin
        sec_type = _security_type(pos)
        if sec_type is not None:
            sec_attrs["securityType"] = sec_type
        sec_el = ET.SubElement(depot_el, _q("security"), **sec_attrs)

        tax_value = ET.SubElement(
            sec_el,
            _q("taxValue"),
            referenceDate=year_end.isoformat(),
            quotationType="PIECE",
            quantity=_quantity(quantity),
            balanceCurrency=pos.currency,
            balance=_chf(balance),
            exchangeRate=str(round(rate, 6)),
            value=_chf(chf_value),
        )
        if len(positions) == 1:
            tax_value.set("unitPrice", _chf(pos.mark_price))
        elif quantity:
            tax_value.set("unitPrice", _chf(Decimal(str(balance)) / quantity))

        # Payments linked to this security
        income_txs = income_by_isin.get(pos.isin, [])
        wht_txs = wht_by_isin.get(pos.isin, [])
        _build_security_payments(
            sec_el, income_txs, wht_txs, data.fx_rates, data.dividend_accruals
        )

    # A security sold during the year is absent from OpenPositions but its
    # dividend and withholding-tax entries still belong in the annual return.
    open_isins = set(positions_by_isin)
    orphan_isins = sorted((set(income_by_isin) | set(wht_by_isin)) - open_isins)
    for offset, isin in enumerate(orphan_isins, start=len(positions_by_isin) + 1):
        income_txs = income_by_isin.get(isin, [])
        wht_txs = wht_by_isin.get(isin, [])
        txs = income_txs or wht_txs
        representative = txs[0]
        country = _security_country(isin, representative.issuer_country_code)
        category = _transaction_security_category(representative)
        sec_attrs = {
            "positionId": str(offset),
            "country": country.upper(),
            "currency": representative.currency,
            "quotationType": "PIECE",
            "securityCategory": category,
            "securityName": (representative.symbol or isin)[:60],
            "isin": isin,
        }
        if category == "FUND":
            sec_attrs["securityType"] = "FUND.DISTRIBUTION"
        sec_el = ET.SubElement(depot_el, _q("security"), **sec_attrs)
        _build_security_payments(
            sec_el, income_txs, wht_txs, data.fx_rates, data.dividend_accruals
        )

    list_el.set("totalTaxValue", _total(list_el.iter(_q("taxValue")), "value"))
    for total, payment_attribute in (
        ("totalGrossRevenueA", "grossRevenueA"),
        ("totalGrossRevenueB", "grossRevenueB"),
        ("totalWithHoldingTaxClaim", "withHoldingTaxClaim"),
        ("totalLumpSumTaxCredit", "lumpSumTaxCreditAmount"),
    ):
        list_el.set(total, _total(list_el.iter(_q("payment")), payment_attribute))
    # Required subtotal: no treaty-limited DA-1 credit is inferred from cash data.
    list_el.set("totalNonRecoverableTax", "0.00")
    list_el.set("totalAdditionalWithHoldingTaxUSA", "0.00")
    list_el.set("totalGrossRevenueIUP", "0.00")
    list_el.set("totalGrossRevenueConversion", "0.00")

    return list_el


def _build_security_payments(
    sec_el: ET.Element,
    income_txs: list[CashTransaction],
    wht_txs: list[CashTransaction],
    fx_rates: dict,
    accruals: list[DividendAccrual],
) -> None:
    """Emit currency-specific income and signed withholding adjustments."""
    groups: dict[PaymentKey, list[CashTransaction]] = {}
    for tx in income_txs + wht_txs:
        groups.setdefault(payment_key(tx), []).append(tx)
    entitlements = {
        key: match_entitlement(txs, accruals) for key, txs in groups.items()
    }
    income: dict[PaymentKey, Decimal] = {}
    for tx in income_txs:
        key = payment_key(tx)
        income[key] = income.get(key, Decimal(0)) + Decimal(str(tx.amount))

    withholding: dict[PaymentKey, Decimal] = {}
    for tx in wht_txs:
        key = payment_key(tx)
        withholding[key] = withholding.get(key, Decimal(0)) - Decimal(str(tx.amount))

    swiss = sec_el.get("country") == "CH"
    tax_dates = {key[0] for key, tax in withholding.items() if tax > 0}
    refund_dates = {key[0] for key, tax in withholding.items() if tax < 0}
    if swiss:
        income_dates = {key[0] for key in income}
        unmatched_dates = {
            key[0]
            for key, tax in withholding.items()
            if tax > 0 and key[0] not in income_dates
        }
        if unmatched_dates:
            warnings.warn(
                f"Swiss withholding for {sec_el.get('isin', 'unknown security')} "
                "has no same-day income; confirm the income's A/B classification "
                "manually.",
                stacklevel=2,
            )
    if not swiss and any(withholding.values()):
        warnings.warn(
            f"Foreign withholding for {sec_el.get('isin', 'unknown security')} "
            "is recorded, but DA-1 eligibility and the non-recoverable amount "
            "must be confirmed manually; no DA-1 entitlement is assumed.",
            stacklevel=2,
        )

    for key in sorted(income.keys() | withholding.keys()):
        pay_date, currency = key[:2]
        entitlement = entitlements[key]
        gross = income.get(key, Decimal(0))
        tax = withholding.get(key, Decimal(0))
        rate = _fx_to_chf(currency, pay_date, fx_rates)
        gross_chf = _chf(gross * Decimal(str(rate)))
        tax_chf = _chf(tax * Decimal(str(rate)))
        # Refunds can reverse A income, but do not move new positive income to A.
        revenue_a = swiss and (
            pay_date in tax_dates or (gross < 0 and pay_date in refund_dates)
        )

        payment = ET.SubElement(
            sec_el,
            _q("payment"),
            paymentDate=pay_date.isoformat(),
            exDate=entitlement.ex_date.isoformat(),
            quotationType="PIECE",
            quantity=_quantity(entitlement.quantity),
            amountCurrency=currency,
            amount=_chf(gross),
            exchangeRate=str(round(rate, 6)),
            grossRevenueA=gross_chf if revenue_a else "0.00",
            grossRevenueB="0.00" if revenue_a else gross_chf,
            withHoldingTaxClaim=tax_chf if swiss else "0.00",
        )
        if not swiss and tax:
            payment.set("lumpSumTaxCreditAmount", tax_chf)
        if key not in income:
            payment.set("name", "Withholding tax adjustment")


def _build_bank_accounts(data: IBKRData) -> ET.Element:
    """Build cash income, retaining foreign withholding as payment annotations."""
    # Group by currency
    income_by_ccy: dict[str, list[CashTransaction]] = {}
    wht_by_ccy: dict[str, list[CashTransaction]] = {}

    for tx in data.cash_transactions:
        if tx.isin:  # security-linked → handled in securities section
            continue
        if tx.tx_type == "Broker Interest Received":
            income_by_ccy.setdefault(tx.currency, []).append(tx)
        elif tx.tx_type == "Withholding Tax":
            wht_by_ccy.setdefault(tx.currency, []).append(tx)

    list_el = ET.Element(_q("listOfBankAccounts"))

    for ccy in sorted(set(list(income_by_ccy) + list(wht_by_ccy))):
        income_txs = income_by_ccy.get(ccy, [])
        wht_txs = wht_by_ccy.get(ccy, [])

        # Net amounts in CHF
        acct_rev_b = 0.0

        ba_el = ET.SubElement(
            list_el,
            _q("bankAccount"),
            bankAccountName=f"IBKR {ccy} Cash",
            bankAccountCountry="GB",  # IB-UK
            bankAccountCurrency=ccy,
            totalTaxValue="0.00",  # closing balance not available
            totalGrossRevenueA="0.00",
            totalGrossRevenueB="0.00",  # filled in below
            totalWithHoldingTaxClaim="0.00",
        )

        # One payment per interest-received event
        for tx in sorted(income_txs, key=lambda t: t.settle_date):
            rate = _fx_to_chf(tx.currency, tx.settle_date, data.fx_rates)
            rev_b_chf = round(tx.amount * rate, 2)
            acct_rev_b += rev_b_chf
            ET.SubElement(
                ba_el,
                _q("payment"),
                paymentDate=tx.settle_date.isoformat(),
                amountCurrency=tx.currency,
                amount=_chf(tx.amount),
                exchangeRate=str(round(rate, 6)),
                grossRevenueA="0.00",
                grossRevenueB=_chf(rev_b_chf),
                withHoldingTaxClaim="0.00",
            )

        # Bank-account payments have no foreign-tax amount field in eCH-0196.
        # Keep the signed amount in the supported name field, not a Swiss claim.
        if wht_txs:
            warnings.warn(
                f"Foreign withholding on IBKR {ccy} cash is recorded in payment "
                "annotations only; bank accounts have no foreign-tax field. "
                "DA-1 eligibility must be confirmed manually.",
                stacklevel=2,
            )
        for tx in sorted(wht_txs, key=lambda t: t.settle_date):
            rate = _fx_to_chf(ccy, tx.settle_date, data.fx_rates)
            tax = -Decimal(str(tx.amount))
            tax_chf = _chf(tax * Decimal(str(rate)))
            ET.SubElement(
                ba_el,
                _q("payment"),
                paymentDate=tx.settle_date.isoformat(),
                name=f"Foreign withholding tax: {_chf(tax)} {ccy} ({tax_chf} CHF); "
                "DA-1 eligibility not determined",
                amountCurrency=ccy,
                amount="0.00",
                exchangeRate=str(round(rate, 6)),
                grossRevenueA="0.00",
                grossRevenueB="0.00",
                withHoldingTaxClaim="0.00",
            )

        ba_el.set("totalGrossRevenueB", _chf(acct_rev_b))

    list_el.set("totalTaxValue", "0.00")
    list_el.set("totalGrossRevenueA", "0.00")
    list_el.set("totalGrossRevenueB", _total(list_el, "totalGrossRevenueB"))
    list_el.set("totalWithHoldingTaxClaim", "0.00")

    return list_el


def _build_liabilities(data: IBKRData) -> ET.Element:
    """
    Build <listOfLiabilities> from margin/debit interest ("Broker Interest Paid").

    This is deductible debt interest (Schuldzinsen), distinct from the income
    reported under <listOfBankAccounts> — eCH-196 keeps them in a separate
    section with its own (expense-side) totalGrossRevenueB. No year-end debt
    balance is available from the FlexQuery export, so <taxValue> (optional
    per XSD) is omitted; only the interest payments are reported.
    """
    interest_by_ccy: dict[str, list[CashTransaction]] = {}
    for tx in data.cash_transactions:
        if tx.tx_type == "Broker Interest Paid":
            interest_by_ccy.setdefault(tx.currency, []).append(tx)

    list_el = ET.Element(_q("listOfLiabilities"))
    total_rev_b = 0.0

    for ccy in sorted(interest_by_ccy):
        txs = interest_by_ccy[ccy]
        la_el = ET.SubElement(
            list_el,
            _q("liabilityAccount"),
            bankAccountName=f"IBKR {ccy} Margin",
            bankAccountCountry="GB",  # IB-UK
            bankAccountCurrency=ccy,
            totalTaxValue="0.00",  # closing debt balance not available
            totalGrossRevenueB="0.00",  # filled in below
        )

        acct_rev_b = 0.0
        for tx in sorted(txs, key=lambda t: t.settle_date):
            rate = _fx_to_chf(tx.currency, tx.settle_date, data.fx_rates)
            amount = -tx.amount  # "Broker Interest Paid" amounts are negative
            rev_b_chf = round(amount * rate, 2)
            acct_rev_b += rev_b_chf
            ET.SubElement(
                la_el,
                _q("payment"),
                paymentDate=tx.settle_date.isoformat(),
                amountCurrency=tx.currency,
                amount=_chf(amount),
                exchangeRate=str(round(rate, 6)),
                grossRevenueB=_chf(rev_b_chf),
            )

        la_el.set("totalGrossRevenueB", _chf(acct_rev_b))
        total_rev_b += acct_rev_b

    list_el.set("totalTaxValue", "0.00")
    list_el.set("totalGrossRevenueB", _chf(total_rev_b))

    return list_el


def build(data: IBKRData, eur_chf_override: float | None = None) -> ET.Element:
    """
    Build the complete eCH-196 XML element tree.

    Args:
        data: Parsed IBKR data.
        eur_chf_override: If provided, overrides the IBKR embedded EUR→CHF rate
                          for all year-end valuations (e.g. ESTV official rate).
    """
    period_from, year_end = _statement_period(data)
    tax_period = str(year_end.year)

    valuation_fx_rates = data.fx_rates
    if eur_chf_override is not None:
        if not math.isfinite(eur_chf_override) or eur_chf_override <= 0:
            raise ValueError("EUR-to-CHF valuation rate must be finite and positive")
        # Inject a synthetic CHF→EUR rate for the statement year-end.
        chf_eur = 1.0 / eur_chf_override
        valuation_fx_rates = dict(data.fx_rates)
        valuation_fx_rates[(year_end, "CHF", "EUR")] = chf_eur

    sec_list = _build_securities(data, year_end, valuation_fx_rates)
    ba_list = _build_bank_accounts(data)
    li_list = _build_liabilities(data)

    canton = data.account.canton
    creation_dt = datetime.now(UTC).astimezone().strftime("%Y-%m-%dT%H:%M:%S")

    # BEIL2 §2.1 ID format: CH + clearing(5) + docpage(2) + account(14) + date(8) + seq(2).
    # IBKR_CLEARING_NUMBER is Interactive Brokers' clearing number. The date+seq
    # are always the last 10 characters regardless of the account-number field's
    # actual length — confirmed against a real reference eSteuerauszug's own id
    # (data/example_etax.pdf), whose customer-number field is only 12 digits,
    # not the 14 implied by a literal reading of the BEIL2 spec text.
    acct_padded = data.account.account_id.rjust(14, "0")
    doc_id = f"CH{IBKR_CLEARING_NUMBER}01{acct_padded}{year_end.strftime('%Y%m%d')}01"

    root_attrs = {
        "id": doc_id,
        "creationDate": creation_dt,
        "taxPeriod": tax_period,
        "periodFrom": period_from.isoformat(),
        "periodTo": year_end.isoformat(),
        "country": "CH",
        "canton": canton,
        "minorVersion": MINOR_VERSION,
    }
    for attribute in (
        "totalTaxValue",
        "totalGrossRevenueA",
        "totalGrossRevenueB",
        "totalWithHoldingTaxClaim",
    ):
        root_attrs[attribute] = _total((sec_list, ba_list), attribute)
    root = ET.Element(_q("taxStatement"), **root_attrs)
    root.set(f"{{{NS_XSI}}}schemaLocation", SCHEMA_LOCATION)

    # institution
    ET.SubElement(root, _q("institution"), name="Interactive Brokers")

    # client
    ET.SubElement(
        root,
        _q("client"),
        clientNumber=data.account.account_id,
        firstName=data.account.first_name,
        lastName=data.account.last_name,
    )

    # XSD sequence: listOfBankAccounts, listOfLiabilities, listOfExpenses, listOfSecurities
    if list(ba_list):
        root.append(ba_list)
    if list(li_list):
        root.append(li_list)
    root.append(sec_list)

    return root


def serialize(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=False)
