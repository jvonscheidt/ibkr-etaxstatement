# AGENTS.md

Repository guidance for coding agents.

## Purpose

Convert Interactive Brokers (IBKR) FlexQuery XML exports into:

1. eCH-0196 V2.2.0 XML for Swiss tax applications such as VSTax and TaxMe.
2. eCH-0270 V1.0.0 barcode PDF for applications such as ZHPrivateTax.

## Input: `data/Tax.xml`

IBKR FlexQuery export. Base currency EUR. Dates use `DD/MM/YYYY`.
`fxRateToBase` converts position or trade currency to EUR.

Key elements:

- `<FlexStatement>`: period and account
- `<OpenPositions>/<OpenPosition levelOfDetail="SUMMARY">`: year-end holdings
- `<Trades>/<Trade levelOfDetail="EXECUTION">`: executions
- `<CashTransactions>/<CashTransaction>`: dividends, interest, withholding tax
- `<ConversionRates>/<ConversionRate>`: daily currency-to-EUR rates

Sample contains ETF positions (MEUD, EIMI, MEUS, XFFE), trades, and STK/ETF
assets from SBF, LSEETF, LSE, and TRWBDE.

## Required Output

- Dec 31 security holdings and values
- Dividend and interest income per position
- EUR/USD conversion to CHF
- DA-1 reclaim data for foreign withholding tax
- "Broker Interest Paid" as deductible debt interest under
  `listOfLiabilities`

Do not report realised trade gains or losses. eCH-0196 has no field for them;
Swiss private capital gains are tax-exempt. Cost basis or realised P/L needs
separate output.

## Specifications

Download official files; do not redistribute them:

- eCH-0196 V2.2.0: <https://www.ech.ch/de/ech/ech-0196/2.2.0>
  - E-Steuerauszug standard
  - Technical guide: XML schema, fields, validation
  - Barcode guide: layout and Macro PDF417 encoding
  - `eCH-0196-2-2.xsd`
- eCH-0270 V1.0.0: <https://www.ech.ch/de/ech/ech-0270/1.0.0>

Check specs before changing output or barcode behavior.

## Commands

```bash
# XML
python convert.py data/Tax.xml output.xml

# XML with official ESTV year-end EUR-to-CHF rate
python convert.py data/Tax.xml output.xml --eur-chf-rate 0.9311

# XML and barcode PDF
python convert.py data/Tax.xml output.xml --eur-chf-rate 0.9311 --barcode-pdf output_barcode.pdf

# Dependencies
pip install -r requirements.txt

# Required before every commit
black .
ruff check .
python -m pytest
```

XSD validation runs when `lxml` and
`documentation/eCH-0196-2-2.xsd` exist. `documentation/` is git-ignored local
scratch space. Missing dependency or XSD causes explicit validation skip.

## Structure

```text
src/parse_ibkr.py           Parse Tax.xml into dataclasses
src/generate_ech196.py      Build eCH-0196 XML
src/generate_barcode_pdf.py Build eCH-0270 PDF
convert.py                  Parse, build, validate, write, optionally make PDF
tests/                      pytest suite
```

`pytest.ini` sets `pythonpath = .`. Tests cover parsing, FX conversion,
eCH-0196 generation, barcode generation, XSD integration, and known defects.
`tests/test_known_issues.py` protects against WHT double-counting and dropped
dividend income.

## Barcode PDF

`--barcode-pdf` creates human-readable portrait pages first, then landscape
2D-barcode sheets. eCH-0270 section 2.2.4 puts 2D barcodes last.

### 1D Code 128

Every page gets 16 digits:

`FORM(3) + 22 + clearing(5) + PPP(page) + 2D-flag(1) + 0(orient) + 1(posID)`

- `FORM=197` on human-readable pages
- `FORM=196` on 2D sheets
- Page numbers continue across both parts

### Macro PDF417

- Compress eCH-0196 XML with ZLIB level 9 using `zlib.compress()`
- Split into segments no larger than 450 bytes
- Encode 13 columns by 35 rows, EC level 4
- Render native 290 by 35 pixels; never supersample
- Place segments right-to-left; segment 0 occupies rightmost slot
- Put at most 6 segments per page
- Rotate sheets 90 degrees clockwise using portrait MediaBox plus `/Rotate 90`
- Right-align segments on A4
- `PDFMacroFileId` must contain exactly 4 integers, each 0 through 899
- Derive file ID deterministically from document ID
- Put document ID in `PDFMacroFileName`

Use only vroonhof `pdf417gen`, pinned in `requirements.txt`. Required APIs:
`encode_macro()` with `force_binary`, `segment_size`, `force_rows`, and
`file_name`; plus `render_image()`.

Reference `data/example_etax.pdf` was decoded independently with `zxing-cpp`
and J4L RPDF417Vision. Confirmed behavior:

- Payload starts with ZLIB header `78 da`, not raw DEFLATE
- Payload decompresses after rightmost-first concatenation
- File ID contains 4 codewords

Trust decoded reference behavior over ambiguous spec prose. Single-codeword
file IDs fail in ZHPrivateTax with `"keine gültigen Daten"`.

## FX Conversion

All conversion rates convert source currency to EUR.

- EUR to CHF: `1 / (CHF-to-EUR rate)`
- USD to CHF: `(USD-to-EUR rate) / (CHF-to-EUR rate)`
- Sample Dec 31, 2025 rates: CHF to EUR `1.074`; USD to EUR `0.85135`

## eCH-0196 Rules

- Namespace: `http://www.ech.ch/xmlns/eCH-0196/2`
- `securityTaxValueType`: `referenceDate`, `quantity`, `quotation`,
  `exchangeRate`, `value`
- `securityPaymentType`: `paymentDate`, `amountCurrency`, `amount`,
  `exchangeRate`, `grossRevenueA`, `grossRevenueB`, `withHoldingTaxClaim`
- `grossRevenueA`: Swiss-source income with 35% Swiss WHT
- `grossRevenueB`: foreign-source income for DA-1
- Account ETFs are accumulating: `securityCategory="FUND"` and
  `securityType="FUND.ACCUMULATION"`
- Set fund accumulation/distribution type only for `FUND`; `SHARE` uses its own
  enumeration
- Report margin interest under
  `listOfLiabilities/liabilityAccount/payment`
- Liability `totalGrossRevenueB` is expense-only; never add it to root
  income-only `totalGrossRevenueB`
- Omit optional liability `taxValue`; FlexQuery has no year-end debt balance

## Git and GitHub

- Default branch: `main`
- Minor changes: commit directly to `main`
- Major changes: `feat/<topic>`, `fix/<topic>`, or `chore/<topic>`
- Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
  `chore:`
- Imperative subject, maximum 72 characters
- Commit body explains why, not what
- One logical change per commit
- Before every commit: run `black .`, `ruff check .`, and `python -m pytest`
- Update branches with `git pull --rebase`
- Never force-push shared branches
- Use `gh` for GitHub work
- Keep PRs small and single-purpose
- Require green CI before merge
- Update README before version tag
