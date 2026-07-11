# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Purpose

This repository holds Interactive Brokers (IBKR) FlexQuery XML exports for tax calculation purposes. 

## Data File: Tax.xml

The file is a FlexQuery XML export containing:

- **Open positions**: ETF holdings (MEUD, EIMI, MEUS, XFFE) across European exchanges
- **Trade history**: Detailed execution records with FX conversion rates (EUR/USD)
- **Asset categories**: STK and ETF, traded on SBF, LSEETF, LSE, TRWBDE exchanges

The data file is at `data/Tax.xml`. Key XML elements:

- `<FlexStatement>` — root, holds period metadata and account info
- `<OpenPositions>/<OpenPosition levelOfDetail="SUMMARY">` — year-end holdings
- `<Trades>/<Trade levelOfDetail="EXECUTION">` — individual executions
- `<CashTransactions>/<CashTransaction>` — interest income and withholding tax
- `<ConversionRates>/<ConversionRate>` — daily FX rates, all in `fromCurrency→EUR` format

All dates in the IBKR file are `DD/MM/YYYY`. The base currency is EUR. The `fxRateToBase` attribute on positions/trades converts position currency → EUR.

## Project Goal

Parse `Tax.xml` and produce two artifacts:

1. An **eCH-196 compliant XML export** suitable for importing into a Swiss tax application (e.g., VSTax / TaxMe). eCH-196 is the Swiss e-government standard for the Wertschriftenverzeichnis (securities register) used in cantonal tax declarations.
2. An **eCH-0270 compliant barcode PDF** that embeds the eCH-196 XML as Macro PDF417 barcodes, for import into tax applications that ingest a scanned/uploaded "Steuerausweis" (e.g., ZHPrivateTax) rather than raw XML.

The full technical specifications are published by Verein eCH (not redistributed in this repo). Download them from the standard pages:
- **eCH-0196 V2.2.0** — <https://www.ech.ch/de/ech/ech-0196/2.2.0> — hosts:
  - `STAN…eCH-0196_V2.2.0_E-Steuerauszug_.pdf` — the E-Steuerauszug standard.
  - `BEIL1…eCH-0196_V2.2.0_Technische Wegleitung.pdf` — XML schema, field definitions, validation rules.
  - `BEIL2…eCH-0196_V2.0.0_Barcode Generierung…pdf` — barcode layout and Macro PDF417 encoding (the source of the `BEIL2 §2.x` citations in the code).
  - `eCH-0196-2-2.xsd` — the XSD (see "Running the Converter" for where to place it).
- **eCH-0270 V1.0.0** — <https://www.ech.ch/de/ech/ech-0270/1.0.0> — hosts `STAN…eCH-0270_V1.0.0_Barcode Generierung.pdf`, the barcode standard.

Consult them for schema details, field definitions, and validation rules before generating output.

The output must cover:
- Security holdings (open positions) with valuation as of Dec 31st
- Dividend and interest income per position
- FX conversion to CHF where positions are denominated in EUR or USD
- **DA-1 reclaim data**: foreign withholding tax (Verrechnungssteuer) deducted at source on dividends from non-Swiss securities, which Swiss residents can reclaim via the DA-1 annex to the tax declaration
- Deductible debt interest ("Broker Interest Paid" / margin interest), reported under `listOfLiabilities`

Note: `Tax.xml` contains a `<Trades>` block with individual executions, but realised gains/losses from trades are **not** parsed or reported. The eCH-0196 schema has no element for capital gains/losses — this is consistent with Swiss law, under which private capital gains are tax-exempt and therefore don't belong in the Wertschriftenverzeichnis. If a future need arises to track cost basis or realised P/L for other purposes (e.g. US tax obligations), that would need a separate output, not an eCH-196 field.

## Running the Converter

```bash
# Generate eCH-196 XML only:
python convert.py data/Tax.xml output.xml

# Override year-end EUR→CHF rate with official ESTV Jahresendkurs:
python convert.py data/Tax.xml output.xml --eur-chf-rate 0.9311

# Also generate the eCH-0270 barcode PDF for import into ZHPrivateTax:
python convert.py data/Tax.xml output.xml --eur-chf-rate 0.9311 --barcode-pdf output_barcode.pdf
```

XSD validation runs automatically if `lxml` is installed (`pip install lxml`). Download `eCH-0196-2-2.xsd` from <https://www.ech.ch/de/ech/ech-0196/2.2.0> and place it at `documentation/eCH-0196-2-2.xsd` (the `documentation/` directory is git-ignored — a local scratch area for the downloaded specs/XSD). Validation is skipped with a message if the XSD or `lxml` is absent.

## Running the Tests

```bash
pip install pytest
python -m pytest
```

Tests live in `tests/` (`pytest.ini` sets `pythonpath = .` so `import src.*` resolves). The suite covers parsing, FX→CHF conversion, eCH-196 generation, and an end-to-end XSD validation of the sample file (skipped if `lxml`/the XSD is absent). `tests/test_known_issues.py` holds regression tests for previously-fixed defects (WHT double-counting and dropped dividend income).

Dependencies for barcode PDF generation:
```bash
pip install -r requirements.txt
```
Barcode generation uses only the vroonhof fork of `pdf417gen` (installed from git). It provides both `encode_macro()` — Macro PDF417 with the `force_binary`/`segment_size`/`force_rows`/`file_name` support eCH-0270 needs — and `render_image()` to rasterize the encoded segments. It is pinned in `requirements.txt`.

## Code Structure

```
src/parse_ibkr.py           # Parse Tax.xml → dataclasses (AccountInfo, OpenPosition, CashTransaction, FxRate)
src/generate_ech196.py      # Dataclasses → eCH-196 XML element tree
src/generate_barcode_pdf.py # eCH-0270 barcode PDF: 1D Code 128 + 2D PDF417 (pdf417gen.encode_macro) segments on landscape A4
convert.py                  # CLI entry point: parse → build → validate → write [→ barcode PDF]
tests/                       # pytest suite (parsing, FX, generation, XSD integration, known-issue xfails)
```

### Barcode PDF Format (eCH-0270)

The `--barcode-pdf` flag generates an eCH-0270 compliant PDF. It has two parts, mirroring the reference eSteuerauszug's structure: **human-readable Wertschriftenverzeichnis page(s) first** (portrait), then the **2D-barcode sheet(s)** (landscape). eCH-0270 §2.2.4: the 2D barcodes are usually on the last pages.
- **1D barcode** (Code 128, 16 digits) on every page: `FORM(3)` + `22` + clearing(5) + `PPP`(page) + `2D-flag(1)` + `0`(orient) + `1`(posID). `FORM` is `197` on human-readable pages and `196` on the 2D-barcode sheet — the reference file distinguishes page types this way, and eCH-0270 §2.1 fixes only the first 3 digits (leaving the other 13 issuer-defined). Page numbering is continuous across both parts.
- **2D barcodes** (PDF417 Structured Append / Macro): eCH-196 XML compressed with ZLIB level 9 (`zlib.compress()`), split into segments of ≤450 bytes, encoded with 13 columns × 35 rows, EC level 4, at native 290×35 pixel resolution (1 pixel per module/row — matches eCH-0270 §2.2.3's stated resolution; do not supersample). Segments are placed **right-to-left** (segment 0 = rightmost slot, then increasing index moving left) when fewer than 6 are on a page.
  - **Macro `PDFMacroFileId` must be 4 integers** (BEIL2 §2.2: "eine zufällige Zahl (4 Integer)"). `pdf417gen` defaults to a single codeword if `file_id` is unset; a single-codeword id is rejected by ZHPrivateTax's J4L-based reader as "keine gültigen Daten". We derive four codewords (each 0..899) deterministically from the document id. `PDFMacroFileName` carries the document id.
  - These specifics (ZLIB not raw DEFLATE, native resolution, right-to-left segment order, 4-integer file id) are confirmed by directly decoding a real reference eSteuerauszug at `data/example_etax.pdf` with independent decoders (`zxing-cpp` and the J4L RPDF417Vision reference reader — an external evaluation tool from <http://www.java4less.com>, not redistributed here — neither this project's own code): its payload starts with the `78 da` ZLIB header, decompressed only once its segments were concatenated rightmost-first, and its file id decodes to 4 codewords. eCH-0270 §2.2.2's "ZIP-File-Algorithmus" wording reads like raw DEFLATE but isn't what real-world decoders implement — trust the reference file's decoded bytes over the spec's prose if they ever conflict again. See [[zhprivatetax-j4l-debug]] for the debugging method.
- Barcode-sheet segments are placed rotated 90° CW (portrait MediaBox + `/Rotate 90` applied to those pages only) on A4, right-aligned, up to 6 per page
- This PDF can be imported into ZHPrivateTax / TaxMe via "Steuerausweis importieren"

### FX Conversion to CHF

All ConversionRates are `fromCurrency→EUR`. To get currency→CHF:
- EUR→CHF: `1 / (CHF→EUR rate)`
- USD→CHF: `(USD→EUR rate) / (CHF→EUR rate)`

Year-end (31.12.2025) rates from the file: CHF→EUR = 1.074, USD→EUR = 0.85135.

### eCH-196 Schema Notes

- Namespace: `http://www.ech.ch/xmlns/eCH-0196/2`
- `securityTaxValueType` fields: `referenceDate`, `quantity`, `quotation`, `exchangeRate`, `value` (CHF)
- `securityPaymentType` fields: `paymentDate`, `amountCurrency`, `amount`, `exchangeRate`, `grossRevenueA`, `grossRevenueB`, `withHoldingTaxClaim`
- `grossRevenueA` = Swiss-source income (35% Swiss WHT); `grossRevenueB` = foreign-source income (DA-1)
- All ETFs in this account are accumulating (ACC) — mapped to `securityCategory="FUND"`, `securityType="FUND.ACCUMULATION"`. `securityType` (FUND.ACCUMULATION/DISTRIBUTION) is only set for `FUND`-category positions; it's omitted for `SHARE` positions since eCH-196 uses a separate `SHARE.*` enumeration for those.
- `listOfLiabilities`/`liabilityAccount`/`payment`: "Broker Interest Paid" (margin/debit interest) is reported here as deductible debt interest (Schuldzinsen), with its own `totalGrossRevenueB` (expense-side) that is **not** folded into the document's root `totalGrossRevenueB` (income-only). No year-end debt balance is available from the FlexQuery export, so `taxValue` is omitted (optional per XSD).

## Git & GitHub

- Default branch is `main`; minor changes commit to it directly. Major changes get branched:
  `feat/<topic>`, `fix/<topic>`, `chore/<topic>`.
- Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
  `chore:`. Imperative subject ≤ 72 chars; body explains *why*, not *what*.
- One logical change per commit; run the lint/format/test before.
- Update branches with `git pull --rebase`; never force-push a shared branch.
- Use the `gh` CLI for GitHub work (`gh pr create`, `gh issue view`, …). Keep
  PRs small and single-purpose; CI must be green before merge.
- Update the README before tagging a new version.