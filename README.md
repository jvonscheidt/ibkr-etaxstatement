# ibkr-etaxstatement (eSteuerauszug / eRelevé fiscal)

Convert an **Interactive Brokers (IBKR) FlexQuery XML export** into a Swiss
**eCH-0196 E-Steuerauszug** — both as validated XML and as an **eCH-0270 barcode
PDF** that imports directly into cantonal tax software (e.g. ZHPrivateTax /
TaxMe) via *"Steuerausweis importieren"*.

Swiss residents holding securities at IBKR get no Swiss tax statement from the
broker. This tool builds a standards-compliant Wertschriftenverzeichnis from the
data IBKR already exports, including the **DA-1** foreign-withholding-tax reclaim
and deductible debit interest, so the positions and income can be imported
instead of typed in by hand.

> **Disclaimer.** This is an independent, unofficial tool. It is **not**
> affiliated with or endorsed by Interactive Brokers, the Verein eCH, or any
> Swiss tax authority. It generates a self-issued tax statement — you are
> responsible for checking every figure against your own records before filing.
> No warranty of any kind (see [LICENSE](LICENSE)).

## Features

- Parses IBKR FlexQuery XML (open positions, dividends, interest, withholding
  tax, margin interest, FX rates).
- Emits **eCH-0196 v2.2.0** XML, validated against the official XSD.
- FX conversion of EUR/USD positions and income to **CHF**, with an optional
  override for the official ESTV year-end rate (Jahresendkurs).
- **DA-1** reclaim data (`grossRevenueB` / `withHoldingTaxClaim`) for foreign
  dividends, and deductible debit interest under `listOfLiabilities`.
- **eCH-0270 barcode PDF**: a human-readable Wertschriftenverzeichnis page plus
  PDF417 Structured Append barcode sheet(s), verified to import into
  ZHPrivateTax.

Swiss private **capital gains are tax-exempt** and have no eCH-0196 element, so
realised trade gains/losses are intentionally **not** parsed or reported.

## Requirements

- Python 3.10+
- Dependencies in [`requirements.txt`](requirements.txt): `lxml` (XSD
  validation), and for the barcode PDF `pdf417gen` (vroonhof fork, installed
  from git), `reportlab`, `Pillow`, `python-barcode`, `pypdf`.

```bash
pip install -r requirements.txt
```

The eCH-0196 XSD is not redistributed here. For XSD validation, download it from
<https://www.ech.ch/de/ech/ech-0196/2.2.0> and place it at
`documentation/eCH-0196-2-2.xsd` (validation is skipped if it or `lxml` is
absent).

## Usage

```bash
# eCH-0196 XML only:
python convert.py data/Tax.xml output.xml

# Override the year-end EUR→CHF rate with the official ESTV Jahresendkurs:
python convert.py data/Tax.xml output.xml --eur-chf-rate 0.9311

# Also produce the eCH-0270 barcode PDF for import into ZHPrivateTax:
python convert.py data/Tax.xml output.xml --eur-chf-rate 0.9311 \
    --barcode-pdf output_barcode.pdf
```

Import `output_barcode.pdf` into your tax application via *"Steuerausweis
importieren"*.

### Getting the input from IBKR

In IBKR Client Portal, create a **FlexQuery** covering the tax year with Open
Positions, Trades, Cash Transactions and Conversion Rates, run it, and save the
XML as `data/Tax.xml`. Dates in the export are `DD/MM/YYYY`; the base currency is
assumed to be EUR.

## How it works

```
src/parse_ibkr.py           # FlexQuery XML → dataclasses
src/generate_ech196.py      # dataclasses → eCH-0196 XML tree (+ XSD validation)
src/generate_barcode_pdf.py # eCH-0196 XML → eCH-0270 barcode PDF
convert.py                  # CLI: parse → build → validate → write [→ PDF]
tests/                      # pytest suite
```

The barcode PDF compresses the eCH-0196 XML with ZLIB, encodes it as PDF417
Structured Append (13×35, EC level 4, native 290×35 px, 4-integer Macro file id
per BEIL2 §2.2), and lays out a portrait statement page followed by rotated
barcode sheet(s). These details were confirmed by decoding real accepted
reference statements; see [`CLAUDE.md`](CLAUDE.md) for the full technical notes
and links to the eCH-0196 / eCH-0270 specifications.

## Tests

```bash
pip install pytest
python -m pytest
```

The suite covers parsing, FX→CHF conversion, eCH-0196 generation, an end-to-end
XSD validation, and barcode round-trip/structure (the barcode tests self-skip if
their optional decode dependencies are absent).

## License

This program is free software: you can redistribute it and/or modify it under the
terms of the **GNU General Public License v3.0** as published by the Free
Software Foundation. It is distributed WITHOUT ANY WARRANTY. See
[LICENSE](LICENSE) for the full text.
