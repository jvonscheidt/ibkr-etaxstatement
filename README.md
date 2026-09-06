# ibkr-etaxstatement (eSteuerauszug / eRelevé fiscal)

Version: **0.3.1**.

Convert an **Interactive Brokers (IBKR) FlexQuery XML export** into a Swiss
**eCH-0196 E-Steuerauszug** — both as validated XML and as an **eCH-0270 barcode
PDF** that imports directly into cantonal tax software (e.g. ZHPrivateTax /
TaxMe) via *"Steuerausweis importieren"*.

Swiss residents holding securities at IBKR get no Swiss tax statement from the
broker. This tool builds a standards-compliant Wertschriftenverzeichnis from the
data IBKR already exports, including foreign-withholding amounts for **DA-1**
review and deductible debit interest, so the positions and income can be imported
instead of typed in by hand.

> **Disclaimer.** This is an independent, unofficial tool. It is **not**
> affiliated with or endorsed by Interactive Brokers, the Verein eCH, or any
> Swiss tax authority. It generates a self-issued tax statement — you are
> responsible for checking every figure against your own records before filing.
> No warranty of any kind (see [LICENSE](LICENSE)).

## Features

- Parses IBKR FlexQuery XML (open positions, dividend accruals, dividends,
  interest, withholding tax, margin interest, FX rates).
- Preserves fractional share quantities and uses historical dividend entitlement
  quantities, rather than year-end holdings, for security payments.
- Emits **eCH-0196 v2.2.0** XML, validated against the official XSD.
- FX conversion of EUR/USD positions and income to **CHF**, with an optional
  override for the official ESTV year-end rate (Jahresendkurs).
- Separate Swiss withholding claims (`grossRevenueA` / `withHoldingTaxClaim`)
  from foreign dividend income and withholding (`grossRevenueB` /
  `lumpSumTaxCreditAmount`), plus deductible debit interest under `listOfLiabilities`.
- **eCH-0270 barcode PDF**: a human-readable Wertschriftenverzeichnis page plus
  PDF417 Structured Append barcode sheet(s), verified to import into
  ZHPrivateTax.

Swiss private **capital gains are tax-exempt** and have no eCH-0196 element, so
realised trade gains/losses are intentionally **not** parsed or reported.

## Requirements

- Python 3.11+
- Dependencies in [`requirements.txt`](requirements.txt): `lxml` (XSD
  validation), and for the barcode PDF `pdf417gen` (vroonhof fork, installed
  from git), `reportlab`, `Pillow`, `python-barcode`, `pypdf`.

```bash
pip install -r requirements.txt
```

### Windows executable

Download the standalone `ibkr-etaxstatement.exe` from the
[latest release](https://github.com/jvonscheidt/ibkr-etaxstatement/releases/latest).
After the package is accepted into the Windows Package Manager repository,
install it with:

```powershell
winget install --id jvonscheidt.ibkr-etaxstatement --exact
```

The executable uses the same command line as `python convert.py`.

The eCH-0196 XSD is not redistributed here. At startup, the converter refreshes
the latest official v2.2 schema and its eCH dependencies, using local
`schemaLocation` references. It searches for `documentation/eCH-0196-2-2.xsd`
beside `convert.py` or the Windows executable, then in the working directory.
An existing cache is refreshed in place; otherwise the application directory is
used. If downloading fails, the cached copy is retained. Validation is explicitly
skipped if the main XSD or `lxml` is absent.

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

The year-end rate override affects holdings only. Dividends, withholding,
cash interest and debt interest retain their payment-date FX rates. CHF amounts
need no conversion-rate rows.

### Getting the input from IBKR

In IBKR Client Portal, create a **FlexQuery** covering the tax year with Open
Positions, Cash Transactions, **Change in Dividend Accruals**, and Conversion
Rates, run it, and save the XML as `data/Tax.xml`. Trades may be included but are
not used to infer dividend entitlement. Dates in the export are `DD/MM/YYYY`; the base currency is
assumed to be EUR. The converter derives the tax period from the FlexStatement
dates and rejects partial-year exports. Export **one account in one
FlexStatement per file**; multi-statement exports are rejected rather than
silently omitting accounts.

### Historical dividend quantities

Enable **ISIN, Currency, Quantity, Ex Date, Pay Date, Gross Amount**, and the
account/contract/model/action identifiers in Change in Dividend Accruals.
Include the corresponding identifiers and Ex Date in Cash Transactions when
available. IBKR defines the accrual quantity as the quantity held before ex-date;
that quantity and ex-date are preserved for payouts and linked adjustments,
including securities sold before year-end.

Accrual postings and reversals are metadata, not additional income. Consistent
records are used once; conflicting records, mismatched gross amounts, missing
entitlements and unlinked adjustments stop conversion with an error, before
writing output. Refunds and dividend reversals need an action ID or ex-date
link to their entitlement. Separate same-day events remain separate when their
identifiers differ. A split dividend/payment-in-lieu event is accepted only
when its combined gross amount matches one unambiguous accrual.

No closing-quantity, zero-quantity or trade-history fallback is used. Older
exports must be regenerated with the required metadata; non-positive accrual
quantities and unresolved corrections require manual reconciliation.
The anonymized `data/Tax.xml` sample has no security dividend payments and
therefore needs no accrual rows. To exercise historical dividend quantities,
use the synthetic example `tests/fixtures/dividend_accruals.xml`:

```bash
python convert.py tests/fixtures/dividend_accruals.xml output.xml
```

### Withholding tax and DA-1

Foreign withholding is recorded, but the FlexQuery does not establish the
treaty-limited, non-recoverable amount or your DA-1 eligibility. The converter
warns when manual confirmation is needed: it does not set the DA-1 eligibility
flag or per-payment non-recoverable amount, and the required
`totalNonRecoverableTax` subtotal remains zero (no credit claimed). Confirm and
enter the eligible amount in your tax application before filing. Foreign tax
must not be interpreted as a Swiss `withHoldingTaxClaim`.

Bank-account payments have no foreign-tax amount field in eCH-0196. Foreign tax
on cash interest is therefore preserved in XML payment annotations, with a
warning, rather than included in Swiss claims or securities-tax totals.

Withholding refunds are signed adjustments, converted on their own booking
dates; they reduce the corresponding tax totals. Payments in different
currencies remain separate, but Swiss income classification uses same-day
withholding debits regardless of currency; refunds alone do not reclassify new
positive income. Unmatched Swiss withholding produces a
warning to confirm the income's A/B classification manually.
Multiple holdings with the same ISIN are
consolidated in the first listing's currency so their combined value is retained
and dividend income is reported only once.

## How it works

```
src/parse_ibkr.py           # FlexQuery XML → dataclasses
src/dividend_entitlements.py # Match cash payments to historical accrual metadata
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
their optional decode dependencies are absent). CI exercises Python 3.11 and 3.12.
Release-script tests mock GitHub APIs and require Node.js 24; they skip locally
when Node is absent, while CI installs it explicitly.

## Release build

```powershell
pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean ibkr-etaxstatement.spec
```

The portable x64 executable is written to `dist\ibkr-etaxstatement.exe`.
Version 0.3.1 includes the automatic official XSD refresh and cache handling
missing from the v0.3.0 tagged source.

### Tag-driven publishing

`.github/workflows/release.yml` runs when a stable `vMAJOR.MINOR.PATCH` tag is
pushed. The tagged commit must belong to `main`, and its version must match
`convert.py`, this README, and both string and numeric versions in
`packaging/windows-version-info.txt`. Before tagging, merge the release changes
to `main` with green CI. Do not move an existing published tag.

The workflow runs the reusable CI workflow against the tagged source, builds a
Windows x64 executable, checks its CLI version, generates manifests from
`packaging/winget/templates`, and validates them with WinGet. The executable's
SHA-256 is calculated from that exact build, not copied from an older release.
The runner provisions the current stable WinGet client before validation;
preinstalled runner versions may not recognize the manifest schema headers.
It uploads the executable, `SHA256SUMS`, and three manifest files to a draft
GitHub release, publishes it only after every asset upload succeeds, then opens
the `microsoft/winget-pkgs` pull request in the same workflow. Building and
uploading an executable manually is no longer required.

To prepare and validate assets locally without publishing (use a fresh output
directory):

```powershell
python .github\scripts\prepare_release.py --tag v0.3.1
python -m PyInstaller --noconfirm --clean ibkr-etaxstatement.spec
python .github\scripts\prepare_release.py --tag v0.3.1 --installer dist\ibkr-etaxstatement.exe --output dist\release-0.3.1
winget validate --manifest dist\release-0.3.1\winget
```

Once ready, push a new version tag to trigger publication. No release is created
by the local preparation commands above.

### WinGet credentials and recovery

WinGet submission requires:

1. A `jvonscheidt/winget-pkgs` fork.
2. A `WINGET_TOKEN` repository secret containing a classic GitHub PAT with the
   `public_repo` scope.

The submission branches from the fork's existing `master`; it does not sync or
modify the fork's default branch or any workflow files. The PAT therefore does
not need the `workflow` scope. Initial packages and subsequent versions use the
same generated-manifest submission path.

If WinGet submission fails after GitHub publication, rerun **only failed jobs**
in that tag's Actions run. An existing open/merged PR or accepted version is
not submitted twice. Published release assets are never overwritten; rerunning
the entire workflow stops at an already-published release. A failed upload
leaves a draft that can be completed by retrying.

After submission, complete the upstream checklist and CLA, and resolve any
installer/Defender findings before merge. Successful GitHub publication or PR
creation does not mean the package is available in WinGet. The existing v0.3.0
submission's installation block is not bypassed by this workflow.
