# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-05-04

### Changed
- **CSV: multi-currency files without `currency` set now error.**
  Previously the parser silently picked the dominant currency and
  dropped the rest, which masked real activity on accounts that mix
  currencies (rare on Revolut — the typical export is one CSV per
  currency account — but still a data-loss path). Single-currency
  CSVs are unchanged. To export every currency from a mixed file,
  define one config section per currency and rerun the converter once
  per section. Mirrors the same behaviour adopted by
  `ofxstatement-paypal-2 3.3.0`.
- **CSV: configured `currency` now wins over auto-detection.** When
  `currency` is set in config it is honoured as the explicit filter;
  detection only fills in when `currency` is unset. Previously
  detection always overrode the configured value.
- **CSV: warns when the configured `currency` isn't present** in the
  file (instead of silently producing an empty OFX with no signal).
- **Plugin: `currency` no longer defaults to `"EUR"`.** Unset stays
  unset so the parser can distinguish "auto-detect" from "filter to
  EUR". The PDF parser still falls back to `"EUR"` internally if the
  PDF heading doesn't yield a currency, preserving today's PDF
  behaviour.

## [0.2.1] - 2026-04-13

### Fixed
- PDF column-threshold calibration is now robust to sub-point x0 jitter
  between the header row and body rows (e.g. `Description` header at
  x0=124.7600 vs description words at x0=124.7598). The threshold is
  placed at the midpoint between the `Date` and `Description` header
  x0 positions instead of at `Description`'s exact x0. Previously this
  caused whole transactions to be silently dropped — diagnosed on a
  65-page EUR statement whose running balance was off by -1,300.00 due
  to 8 dropped transactions on one page.
- Revolut's `Reverted` sub-table (appended after the main transaction
  table with a different layout and no `Balance` column) is now
  recognised and excluded. Previously its rows were parsed as normal
  transactions, which left the last real row's `end_balance` as `None`
  on statements that had reverted entries (observed on TRY).

### Changed
- Refactored the month-name lookup from a flat 165-entry table into a
  2D `Dict[lang_code, Tuple[12 aliases]]` structure, mirroring the
  layout used by ofxstatement-consorsbank. Build-time assertions verify
  every language row has 12 months and flag cross-language collisions.

## [0.2.0] - 2026-04-12

### Added
- Multi-currency PDF support: USD, GBP, JPY, INR, CHF, TRY and any ISO-coded
  currency are now detected and parsed. Prefix-style (`$100.00`) and
  suffix-style (`100.00 TRY`) amount formats are both handled.
- Multi-currency CSV support: the `Currency` column is auto-detected and
  `statement.currency` is set accordingly. Mixed-currency files keep only the
  dominant currency's rows.
- GBP statements: account ID is derived from Sort Code + Account Number
  (`GB-<sort>-<acct>`) instead of the shared EUR IBAN.
- PDF transaction-type mappings: `SWIFT Transfer to`, `SWIFT Transfer from`.
- Test fixtures for USD, GBP (Sort Code), and TRY (suffix-style) statements,
  plus a test that secondary-currency amounts are filtered.
- "Supported currencies" section in the README.
- Runtime calibration of PDF column x-thresholds from the
  `Date | Description | Money out | Money in | Balance` header row. Thresholds
  now adapt to layout shifts between Revolut PDF versions instead of relying
  on hardcoded point positions; the old constants remain as initial defaults.
- PDF header detection is fuzzy and alias-driven — renaming a column (e.g.
  `Money out` → `Withdrawals`, `Description` → `Details`) no longer produces
  a silent empty statement. New aliases can be added to `_HEADER_ALIASES`.
- PDF transaction dates accept multiple formats (`Jan 15, 2025`, `15 Jan 2025`,
  `2025-01-15`, `15/01/2025`); the section-header date range accepts the same
  set. Protects against future wording changes in Revolut's PDF generator.
- Calibration sanity check: thresholds that come out non-monotonic (e.g. when
  a header word is mislabelled) are rejected and the previous values are kept.
- New `RevolutPDFFormatError`: raised when a PDF has pages but no section
  headers and no table header rows are recognised, instead of silently
  emitting an empty OFX. Points the user at the issue tracker.
- CSV columns are now resolved by name from the header row (with a per-field
  alias list) instead of by hardcoded positional index. Reordered or newly
  inserted columns are tolerated, optional columns (`Fee`, `Balance`,
  `Currency`) can be absent, and missing *required* columns raise
  `RevolutCSVFormatError` instead of silently writing wrong data.
- CSV header lookups are case-insensitive and tolerate a UTF-8 BOM
  (`utf-8-sig`) on the first cell.
- CSV timestamps accept multiple formats (`2025-01-15 10:30:00`,
  `2025-01-15T10:30:00`, `2025-01-15T10:30:00.123456`, `2025-01-15`).
- CSV `State` filter now accepts `COMPLETED`, `COMPLETE`, `SETTLED`, and
  `POSTED` case-insensitively, instead of only the literal `COMPLETED`.
- When a non-empty CSV produces zero statement lines, the warning log now
  lists the distinct products, states, and currencies observed so users can
  tell a wrong account filter from a schema change.
- CSV amount parser is now locale-aware: both `.` (English) and `,`
  (European) decimal separators are accepted, with the other treated as a
  thousands grouping. A localized Revolut export (`-1.234,56`) no longer
  silently fails every row through the Decimal conversion fallback. Applies
  to the `Amount`, `Fee`, and `Balance` columns.
- Multi-language PDF/CSV resilience: column headers, section headers,
  currency heading, date parsing, and fee-word fallback now recognise
  German, French, Spanish, Italian, Portuguese, Dutch, and Polish
  translations in addition to English. Specifically:
  - Transaction-date parser replaces locale-sensitive `strptime("%b")`
    with an explicit month-name lookup covering standard + genitive forms
    (e.g. German `Januar`, French `janvier`, Polish `stycznia`).
  - PDF column-header detection is alias-driven instead of requiring
    literal `Date` / `Balance` at the line boundary, so `Datum … Saldo`
    or `Fecha … Saldo` is recognised.
  - PDF section regex accepts section-type words in every supported
    language (`Konto`, `Compte`, `Cuenta`, …) and range connectors
    (`bis`, `au`, `até`, `hasta`, …); matches are canonicalised back to
    `Account`/`Deposit` via `_canonical_section` so downstream filtering
    stays language-independent.
  - PDF currency detection accepts localized equivalents of "Statement"
    (`Kontoauszug`, `Relevé`, `Extracto`, `Estratto`, `Extrato`,
    `Afschrift`, `Wyciąg`). The whitelist prevents the old loose
    `<CUR> <any-word>` pattern from false-matching lines like
    `CEO JANE` or `BIC TESTDE2XXXX`.
  - Fee-word fallback in `_match_txn_type` triggers on `Gebühr`, `frais`,
    `tarifa`, `comisión`, `tassa`, `taxa`, `opłata`, `vergoeding`, etc.,
    not only the English `fee`.
  - CSV header aliases cover common translations of every field
    (`Betrag`, `Montant`, `Importe`, `Kwota`; `Währung`, `Devise`,
    `Valuta`; …).
  - Transaction-type prefix maps (PDF `PDF_TXN_TYPE_MAP`, CSV
    `CSV_TXN_TYPE_MAP`) remain English-only. Adding language variants
    requires a real non-English Revolut sample to verify wording —
    open an issue if you hit one.

### Fixed
- Currency detection: `_CURRENCY_RE` was missing `re.MULTILINE`, so the
  currency extracted from a PDF's `"<CUR> Statement"` header line never
  matched and every statement silently defaulted to EUR. Non-EUR statements
  therefore lost every transaction to the "no EUR amount" fallback.
- `statement.currency` is now assigned after header extraction, not before,
  so the reported currency matches the detected currency.

## [0.1.0] - 2026-04-11

### Added
- PDF parser using pdfplumber with x-coordinate column classification to
  handle Revolut's positional layout across multi-page statements, detail /
  continuation lines, and sub-account sections.
- CSV parser with fee subtraction and `State = COMPLETED` filtering.
- Account selection: `Current`, `Deposit`, or sub-account owner name
  (PDF only — CSV exports don't contain sub-account data).
- Transaction-type maps for PDF (description prefix) and CSV (`Type` column)
  covering transfers, card payments, interest, fees, currency exchange,
  pockets, ATM withdrawals, top-ups and refunds.
- Stable SHA-256-based transaction IDs so re-importing a statement is
  idempotent in GnuCash.
- Start / end balance extraction from the first and last transaction lines.
- 16 tests using synthetic PDF / CSV fixtures (no real-statement dependency).
- GPLv3 license headers on all source files.

[Unreleased]: https://github.com/eduralph/ofxstatement-revolut/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/eduralph/ofxstatement-revolut/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/eduralph/ofxstatement-revolut/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/eduralph/ofxstatement-revolut/releases/tag/v0.1.0
