"""Build an eCH-0196 v2.2.0 XML tree from parsed IBKR data."""

from __future__ import annotations

import re
import warnings
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import xml.etree.ElementTree as ET

from .parse_ibkr import IBKRData, OpenPosition, CashTransaction

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

# IBKR cash-transaction types that represent per-security (dividend) income.
DIVIDEND_TYPES = {"Dividends", "Payment In Lieu Of Dividends"}


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _chf(value: float) -> str:
    """Round to 2 decimal places and format as string."""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _fx_to_chf(currency: str, ref_date: date, fx_rates: dict) -> float:
    """
    Return the exchange rate: 1 unit of `currency` = X CHF.

    All rates in fx_rates are currency→EUR. Base currency is EUR.
    CHF→EUR rate gives us EUR→CHF = 1/(CHF→EUR).
    For any other currency: currency→CHF = (currency→EUR) / (CHF→EUR).
    """
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

    if currency == "CHF":
        return 1.0
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
        period_to = date(max(tx.settle_date.year for tx in data.cash_transactions), 12, 31)
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
    data: IBKRData, year_end: date
) -> tuple[ET.Element, float, float, float]:
    """Build <listOfSecurities>.

    Returns (element, total_tax_value_chf, total_gross_revenue_b_chf,
    total_withholding_tax_claim_chf).
    """
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

    total_chf = 0.0
    total_rev_b = 0.0
    total_wht = 0.0
    for idx, pos in enumerate(data.positions, start=1):
        rate = _fx_to_chf(pos.currency, year_end, data.fx_rates)
        chf_value = round(pos.position_value * rate, 2)
        total_chf += chf_value

        sec_attrs = {
            "positionId": str(idx),
            "country": pos.issuer_country_code or "XX",
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

        ET.SubElement(sec_el, _q("taxValue"), **{
            "referenceDate": year_end.isoformat(),
            "quotationType": "PIECE",
            "quantity": _chf(pos.quantity),
            "balanceCurrency": pos.currency,
            "unitPrice": _chf(pos.mark_price),
            "balance": _chf(pos.position_value),
            "exchangeRate": str(round(rate, 6)),
            "value": _chf(chf_value),
        })

        # Payments linked to this security
        income_txs = income_by_isin.get(pos.isin, [])
        wht_txs = wht_by_isin.get(pos.isin, [])
        rev_b, wht = _build_security_payments(
            sec_el, income_txs, wht_txs, data.fx_rates, pos.quantity
        )
        total_rev_b += rev_b
        total_wht += wht

    # A security sold during the year is absent from OpenPositions but its
    # dividend and withholding-tax entries still belong in the annual return.
    open_isins = {pos.isin for pos in data.positions}
    orphan_isins = sorted((set(income_by_isin) | set(wht_by_isin)) - open_isins)
    for offset, isin in enumerate(orphan_isins, start=len(data.positions) + 1):
        income_txs = income_by_isin.get(isin, [])
        wht_txs = wht_by_isin.get(isin, [])
        txs = income_txs or wht_txs
        representative = txs[0]
        country = representative.issuer_country_code
        if not country:
            country = isin[:2] if len(isin) >= 2 and isin[:2].isalpha() else "XX"
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
        rev_b, wht = _build_security_payments(
            sec_el, income_txs, wht_txs, data.fx_rates, quantity=0.0
        )
        total_rev_b += rev_b
        total_wht += wht

    list_el.set("totalTaxValue", _chf(total_chf))
    list_el.set("totalGrossRevenueA", "0.00")
    list_el.set("totalGrossRevenueB", _chf(total_rev_b))
    list_el.set("totalWithHoldingTaxClaim", _chf(total_wht))
    list_el.set("totalLumpSumTaxCredit", "0.00")
    list_el.set("totalNonRecoverableTax", "0.00")
    list_el.set("totalAdditionalWithHoldingTaxUSA", "0.00")
    list_el.set("totalGrossRevenueIUP", "0.00")
    list_el.set("totalGrossRevenueConversion", "0.00")

    return list_el, total_chf, total_rev_b, total_wht


def _build_security_payments(
    sec_el: ET.Element,
    income_txs: list[CashTransaction],
    wht_txs: list[CashTransaction],
    fx_rates: dict,
    quantity: float,
) -> tuple[float, float]:
    """Emit <payment> elements for one security.

    Returns (total_gross_revenue_b_chf, total_withholding_tax_claim_chf).
    """
    if not income_txs and not wht_txs:
        return 0.0, 0.0

    total_rev_b = 0.0
    total_wht = 0.0

    # Group income and WHT by settle_date; emit one payment per income event.
    income_by_date: dict[date, list[CashTransaction]] = {}
    for tx in income_txs:
        income_by_date.setdefault(tx.settle_date, []).append(tx)

    # Net WHT per date (negative amount = tax withheld). WHT is matched to the
    # income booked on the *same* date — not summed across all dates — so a
    # security paying on multiple dates does not double-count its DA-1 claim.
    net_wht: dict[date, float] = {}
    wht_currency: dict[date, str] = {}
    for tx in wht_txs:
        net_wht[tx.settle_date] = net_wht.get(tx.settle_date, 0.0) + tx.amount
        wht_currency.setdefault(tx.settle_date, tx.currency)

    for pay_date, txs in sorted(income_by_date.items()):
        gross_b = sum(t.amount for t in txs)
        rate = _fx_to_chf(txs[0].currency, pay_date, fx_rates)
        gross_b_chf = round(gross_b * rate, 2)
        wht_chf = round(max(0.0, -net_wht.get(pay_date, 0.0)) * rate, 2)
        total_rev_b += gross_b_chf
        total_wht += wht_chf

        ET.SubElement(sec_el, _q("payment"), **{
            "paymentDate": pay_date.isoformat(),
            "quotationType": "PIECE",
            "quantity": _chf(quantity),
            "amountCurrency": txs[0].currency,
            "amount": _chf(gross_b),
            "exchangeRate": str(round(rate, 6)),
            "grossRevenueA": "0.00",
            "grossRevenueB": _chf(gross_b_chf),
            "withHoldingTaxClaim": _chf(wht_chf),
        })

    # WHT withheld on dates with no matching income (e.g. adjustments) would
    # otherwise be dropped — emit each as a standalone reclaim so no DA-1
    # credit is lost.
    for wht_date in sorted(net_wht):
        if wht_date in income_by_date or net_wht[wht_date] >= 0:
            continue
        ccy = wht_currency[wht_date]
        rate = _fx_to_chf(ccy, wht_date, fx_rates)
        wht_chf = round(-net_wht[wht_date] * rate, 2)
        total_wht += wht_chf
        ET.SubElement(sec_el, _q("payment"), **{
            "paymentDate": wht_date.isoformat(),
            "quotationType": "PIECE",
            "quantity": _chf(quantity),
            "amountCurrency": ccy,
            "amount": "0.00",
            "exchangeRate": str(round(rate, 6)),
            "grossRevenueA": "0.00",
            "grossRevenueB": "0.00",
            "withHoldingTaxClaim": _chf(wht_chf),
        })

    return total_rev_b, total_wht


def _build_bank_accounts(data: IBKRData) -> tuple[ET.Element, float, float, float]:
    """
    Build <listOfBankAccounts> from cash interest / WHT transactions.
    Returns (element, total_revenue_b, total_wht, total_tax_value).
    """
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
    total_rev_b = 0.0
    total_wht = 0.0

    for ccy in sorted(set(list(income_by_ccy) + list(wht_by_ccy))):
        income_txs = income_by_ccy.get(ccy, [])
        wht_txs = wht_by_ccy.get(ccy, [])

        # Net amounts in CHF
        acct_rev_b = 0.0
        acct_wht = 0.0

        ba_el = ET.SubElement(list_el, _q("bankAccount"), **{
            "bankAccountName": f"IBKR {ccy} Cash",
            "bankAccountCountry": "GB",   # IB-UK
            "bankAccountCurrency": ccy,
            "totalTaxValue": "0.00",       # closing balance not available
            "totalGrossRevenueA": "0.00",
            "totalGrossRevenueB": "0.00",  # filled in below
            "totalWithHoldingTaxClaim": "0.00",
        })

        # One payment per interest-received event
        for tx in sorted(income_txs, key=lambda t: t.settle_date):
            rate = _fx_to_chf(tx.currency, tx.settle_date, data.fx_rates)
            rev_b_chf = round(tx.amount * rate, 2)
            acct_rev_b += rev_b_chf
            ET.SubElement(ba_el, _q("payment"), **{
                "paymentDate": tx.settle_date.isoformat(),
                "amountCurrency": tx.currency,
                "amount": _chf(tx.amount),
                "exchangeRate": str(round(rate, 6)),
                "grossRevenueA": "0.00",
                "grossRevenueB": _chf(rev_b_chf),
                "withHoldingTaxClaim": "0.00",
            })

        # Net WHT for this currency: negative = still withheld (reclaimable)
        net_wht_ccy = sum(t.amount for t in wht_txs)
        if net_wht_ccy < -0.005:
            rate = _fx_to_chf(ccy, wht_txs[-1].settle_date, data.fx_rates)
            wht_chf = round(-net_wht_ccy * rate, 2)
            acct_wht += wht_chf
            # Attach WHT claim to the last income payment if present, else new entry
            if income_txs:
                last_pay = sorted(income_txs, key=lambda t: t.settle_date)[-1]
                pay_date = last_pay.settle_date
            else:
                pay_date = sorted(wht_txs, key=lambda t: t.settle_date)[-1].settle_date
            ET.SubElement(ba_el, _q("payment"), **{
                "paymentDate": pay_date.isoformat(),
                "amountCurrency": ccy,
                "amount": "0.00",
                "exchangeRate": str(round(rate, 6)),
                "grossRevenueA": "0.00",
                "grossRevenueB": "0.00",
                "withHoldingTaxClaim": _chf(wht_chf),
            })

        ba_el.set("totalGrossRevenueB", _chf(acct_rev_b))
        ba_el.set("totalWithHoldingTaxClaim", _chf(acct_wht))
        total_rev_b += acct_rev_b
        total_wht += acct_wht

    list_el.set("totalTaxValue", "0.00")
    list_el.set("totalGrossRevenueA", "0.00")
    list_el.set("totalGrossRevenueB", _chf(total_rev_b))
    list_el.set("totalWithHoldingTaxClaim", _chf(total_wht))

    return list_el, total_rev_b, total_wht, 0.0


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
        la_el = ET.SubElement(list_el, _q("liabilityAccount"), **{
            "bankAccountName": f"IBKR {ccy} Margin",
            "bankAccountCountry": "GB",   # IB-UK
            "bankAccountCurrency": ccy,
            "totalTaxValue": "0.00",        # closing debt balance not available
            "totalGrossRevenueB": "0.00",   # filled in below
        })

        acct_rev_b = 0.0
        for tx in sorted(txs, key=lambda t: t.settle_date):
            rate = _fx_to_chf(tx.currency, tx.settle_date, data.fx_rates)
            amount = -tx.amount  # "Broker Interest Paid" amounts are negative
            rev_b_chf = round(amount * rate, 2)
            acct_rev_b += rev_b_chf
            ET.SubElement(la_el, _q("payment"), **{
                "paymentDate": tx.settle_date.isoformat(),
                "amountCurrency": tx.currency,
                "amount": _chf(amount),
                "exchangeRate": str(round(rate, 6)),
                "grossRevenueB": _chf(rev_b_chf),
            })

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

    if eur_chf_override is not None:
        # Inject a synthetic CHF→EUR rate for the statement year-end.
        chf_eur = 1.0 / eur_chf_override
        data.fx_rates[(year_end, "CHF", "EUR")] = chf_eur

    sec_list, total_tax_value, sec_rev_b, sec_wht = _build_securities(data, year_end)
    ba_list, ba_rev_b, ba_wht, _ = _build_bank_accounts(data)
    li_list = _build_liabilities(data)

    total_rev_b = sec_rev_b + ba_rev_b
    total_wht = sec_wht + ba_wht

    canton = data.account.canton
    creation_dt = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

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
        "totalTaxValue": _chf(total_tax_value),
        "totalGrossRevenueA": "0.00",
        "totalGrossRevenueB": _chf(total_rev_b),
        "totalWithHoldingTaxClaim": _chf(total_wht),
        "minorVersion": MINOR_VERSION,
    }
    root = ET.Element(_q("taxStatement"), **root_attrs)
    root.set(f"{{{NS_XSI}}}schemaLocation", SCHEMA_LOCATION)

    # institution
    ET.SubElement(root, _q("institution"), name="Interactive Brokers")

    # client
    ET.SubElement(root, _q("client"), **{
        "clientNumber": data.account.account_id,
        "firstName": data.account.first_name,
        "lastName": data.account.last_name,
    })

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
