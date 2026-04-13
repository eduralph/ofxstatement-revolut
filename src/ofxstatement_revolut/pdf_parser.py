# Copyright (C) 2026 Eduard Ralph
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import hashlib
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

import pdfplumber

from ofxstatement.parser import AbstractStatementParser
from ofxstatement.statement import Statement, StatementLine

logger = logging.getLogger(__name__)

# ── Default x-coordinate thresholds for column detection ─────────────────────
# Used until the parser sees a "Date Description Money out Money in Balance"
# header row, at which point the thresholds are recalibrated from the actual
# header word positions. This makes the parser tolerant of minor layout shifts
# between Revolut PDF versions.
_DEFAULT_DESC_X = 120.0
_DEFAULT_MONEY_OUT_X = 300.0
_DEFAULT_MONEY_IN_X = 400.0
_DEFAULT_BALANCE_X = 500.0

# ── Multi-language month names ───────────────────────────────────────────────
#
# strptime's %b/%B are locale-sensitive and fail silently on non-English month
# names (a German "15. Januar 2025" wouldn't parse in a C-locale process).
# Explicit, locale-independent lookup instead.
#
# Two-dimensional table: one row per language, 12 columns per row (one per
# month, Jan..Dec). Each cell is a tuple of aliases — full name, common
# abbreviation(s), and inflected forms (e.g. Polish genitive "stycznia" used
# in dates like "15 stycznia"). Add a new language by appending a row.
#
# Cross-language collisions are fine as long as both sides map to the same
# month (validated at module load) — e.g. "mai" → 5 in DE, NL, and FR.
_MONTHS_BY_LANGUAGE: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    # ISO 639-1 code → 12 cells, one per month.
    "en": (
        ("jan", "january"),
        ("feb", "february"),
        ("mar", "march"),
        ("apr", "april"),
        ("may",),
        ("jun", "june"),
        ("jul", "july"),
        ("aug", "august"),
        ("sep", "sept", "september"),
        ("oct", "october"),
        ("nov", "november"),
        ("dec", "december"),
    ),
    "de": (
        ("januar", "jän", "jänner"),
        ("februar",),
        ("mär", "märz"),
        ("april",),
        ("mai",),
        ("juni",),
        ("juli",),
        ("august",),
        ("september",),
        ("okt", "oktober"),
        ("november",),
        ("dez", "dezember"),
    ),
    "fr": (
        ("janv", "janvier"),
        ("févr", "fevr", "février", "fevrier"),
        ("mars",),
        ("avr", "avril"),
        ("mai",),
        ("juin",),
        ("juil", "juillet"),
        ("août", "aout"),
        ("septembre",),
        ("octobre",),
        ("novembre",),
        ("déc", "décembre", "decembre"),
    ),
    "es": (
        ("ene", "enero"),
        ("febrero",),
        ("marzo",),
        ("abr", "abril"),
        ("mayo",),
        ("junio",),
        ("julio",),
        ("ago", "agosto"),
        ("septiembre",),
        ("octubre",),
        ("noviembre",),
        ("dic", "diciembre"),
    ),
    "it": (
        ("gen", "gennaio"),
        ("febbraio",),
        ("marzo",),
        ("aprile",),
        ("mag", "maggio"),
        ("giu", "giugno"),
        ("lug", "luglio"),
        ("agosto",),
        ("set", "sett", "settembre"),
        ("ott", "ottobre"),
        ("novembre",),
        ("dicembre",),
    ),
    "pt": (
        ("janeiro",),
        ("fev", "fevereiro"),
        ("março", "marco"),
        ("abril",),
        ("maio",),
        ("junho",),
        ("julho",),
        ("agosto",),
        ("set", "setembro"),
        ("out", "outubro"),
        ("novembro",),
        ("dez", "dezembro"),
    ),
    "nl": (
        ("januari",),
        ("februari",),
        ("mrt", "maart"),
        ("april",),
        ("mei",),
        ("juni",),
        ("juli",),
        ("augustus",),
        ("september",),
        ("okt",),
        ("november",),
        ("december",),
    ),
    # Polish dates use the genitive case: "15 stycznia 2025" (not "styczeń").
    # Both nominative and genitive forms listed so either parses.
    "pl": (
        ("sty", "styczeń", "styczen", "stycznia"),
        ("lut", "luty", "lutego"),
        ("marzec", "marca"),
        ("kwi", "kwiecień", "kwiecien", "kwietnia"),
        ("maj", "maja"),
        ("cze", "czerwiec", "czerwca"),
        ("lip", "lipiec", "lipca"),
        ("sie", "sierpień", "sierpien", "sierpnia"),
        ("wrz", "wrzesień", "wrzesien", "września", "wrzesnia"),
        (
            "paź",
            "paz",
            "październik",
            "pazdziernik",
            "października",
            "pazdziernika",
        ),
        ("lis", "listopad", "listopada"),
        ("gru", "grudzień", "grudzien", "grudnia"),
    ),
}

# Flattened lookup: alias → month number (1..12). Collisions across languages
# are allowed as long as they map to the same month (checked at load time).
_MONTH_NAMES: Dict[str, int] = {}
for _lang, _months in _MONTHS_BY_LANGUAGE.items():
    assert len(_months) == 12, f"{_lang!r} row must have 12 months"
    for _idx, _aliases in enumerate(_months):
        _num = _idx + 1
        for _alias in _aliases:
            _key = _alias.casefold()
            if _MONTH_NAMES.setdefault(_key, _num) != _num:
                raise AssertionError(
                    f"Month name collision: {_alias!r} maps to both "
                    f"{_MONTH_NAMES[_key]} and {_num} (seen in {_lang!r})"
                )


# ── Regex patterns ───────────────────────────────────────────────────────────

# Letter-like character (Unicode-aware, excludes digits and underscore) — used
# in date / header patterns so they match non-ASCII month names like "Januar",
# "févr", "março", "październik".
_L = r"[^\W\d_]"

# Transaction-date formats, language-neutral. Month-name variants use the
# _MONTH_NAMES lookup below (locale-independent); numeric variants go through
# strptime. The regex serves as a gatekeeper — "is this line a transaction
# date?" — and accepts any letter sequence in the month-name slot. Actual
# validation happens in _parse_date.
_DATE_RE = re.compile(
    r"^(?:"
    rf"{_L}+\.?\s\d{{1,2}},\s\d{{4}}"  # Jan 15, 2025 / enero 15, 2025
    rf"|\d{{1,2}}\.?\s{_L}+\.?\s\d{{4}}"  # 15 Jan 2025 / 15. Januar 2025
    r"|\d{4}-\d{2}-\d{2}"  # 2025-01-15
    r"|\d{1,2}/\d{1,2}/\d{4}"  # 15/01/2025
    r")$"
)

# Numeric-only strptime formats. Month-name formats bypass strptime entirely
# (see _parse_date) so we avoid %b's locale dependency.
_NUMERIC_DATE_FORMATS: Tuple[str, ...] = (
    "%Y-%m-%d",
    "%d/%m/%Y",
)

# Captures the three components of a month-name date in either order.
_MONTH_NAME_DATE_RE = re.compile(
    rf"^(?:"
    rf"({_L}+)\.?\s(\d{{1,2}}),\s(\d{{4}})"
    rf"|(\d{{1,2}})\.?\s({_L}+)\.?\s(\d{{4}})"
    rf")$"
)

_SECTION_DATE_PATTERN = (
    rf"(?:"
    rf"{_L}+\.?\s\d{{1,2}},\s\d{{4}}"
    rf"|\d{{1,2}}\.?\s{_L}+\.?\s\d{{4}}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r")"
)

# Connectors used in "<date> to <date>" ranges across supported languages.
# Broadened from the English "to" alone. Matched case-insensitively.
_SECTION_CONNECTOR = r"(?:to|bis|au|à|até|hasta|al|a|tot|do|-)"

# Words that introduce a section (equivalent of English "Account" / "Deposit").
# Only the language-specific labels; the structural regex below does not
# require the exact English phrase "transactions from".
# Section-type word lists, one per canonical English label. _SECTION_RE
# matches either set; _canonical_section() maps the matched word back to
# "Account" or "Deposit" so downstream filtering stays language-independent.
_SECTION_ACCOUNT_ALIASES: Tuple[str, ...] = (
    "Account",
    "Konto",
    "Compte",
    "Cuenta",
    "Conto",
    "Conta",
    "Rekening",
    "Rachunek",
)
_SECTION_DEPOSIT_ALIASES: Tuple[str, ...] = (
    "Deposit",
    "Savings",
    "Einlage",
    "Depot",
    "Sparkonto",
    "Dépôt",
    "Depot",
    "Epargne",
    "Épargne",
    "Depósito",
    "Deposito",
    "Ahorro",
    "Risparmi",
    "Poupança",
    "Poupanca",
    "Spaargeld",
    "Lokata",
    "Oszczędności",
    "Oszczednosci",
)

_SECTION_ACCOUNT_WORDS = (
    r"(?:" + "|".join(re.escape(a) for a in _SECTION_ACCOUNT_ALIASES) + r")"
)
_SECTION_DEPOSIT_WORDS = (
    r"(?:" + "|".join(re.escape(a) for a in _SECTION_DEPOSIT_ALIASES) + r")"
)

_SECTION_ACCOUNT_SET = {a.casefold() for a in _SECTION_ACCOUNT_ALIASES}
_SECTION_DEPOSIT_SET = {a.casefold() for a in _SECTION_DEPOSIT_ALIASES}


def _canonical_section(word: str) -> str:
    """Map a matched section-type word to its canonical English label."""
    w = word.casefold()
    if w in _SECTION_ACCOUNT_SET:
        return "Account"
    if w in _SECTION_DEPOSIT_SET:
        return "Deposit"
    return word  # unknown — preserve as-is so it surfaces in logs


# Final section-header regex. Format:
#   [<Owner>'s ] <section-word> <anything> <date> <connector> <date>
# The "<anything>" middle absorbs language-specific filler like "transactions
# from" / "Kontotransaktionen" / "transactions du" without needing a per-
# language alias. Matching is case-insensitive for the section words and
# connectors so we don't have to enumerate capitalization variants.
_SECTION_RE = re.compile(
    r"^(?:(\w+)(?:'s|'s)\s+)?"
    rf"({_SECTION_ACCOUNT_WORDS}|{_SECTION_DEPOSIT_WORDS})"
    r"\b[^\n]*?\s"
    rf"({_SECTION_DATE_PATTERN})"
    rf"\s{_SECTION_CONNECTOR}\s"
    rf"({_SECTION_DATE_PATTERN})$",
    re.IGNORECASE,
)

_IBAN_RE = re.compile(r"IBAN\s+([A-Z]{2}\d{2}[A-Z0-9]+)")

# Currency detection. Revolut writes "<CUR> Statement" as a page heading.
# The word "Statement" is translated on non-English exports, so we accept
# any of the known localized equivalents. Using an explicit list instead
# of "<CUR> <any-word>" avoids false matches on lines like "CEO JANE".
_STATEMENT_WORDS: Tuple[str, ...] = (
    "Statement",  # EN
    "Kontoauszug",
    "Kontoauszüge",  # DE
    "Relevé",
    "Releve",  # FR
    "Extracto",  # ES
    "Estratto",  # IT
    "Extrato",  # PT
    "Afschrift",
    "Rekeningafschrift",  # NL
    "Wyciąg",
    "Wyciag",  # PL
)
_CURRENCY_RE = re.compile(
    r"^([A-Z]{3})\s+(?:" + "|".join(re.escape(w) for w in _STATEMENT_WORDS) + r")\b.*$",
    re.MULTILINE,
)
_SORT_CODE_RE = re.compile(r"Sort Code\s+(\d{6})")
_ACCOUNT_NUMBER_RE = re.compile(r"Account Number\s+(\d{6,})")

# End-of-table marker for the "Reverted" sub-section that Revolut appends
# at the end of some statements. It lists transactions that were charged
# and then reversed (net impact zero), uses a 4-column `Start date |
# Description | Money out | Money in` header (no Balance), and must NOT
# be treated as part of the regular transaction table.
#
# English-only: add localizations when real non-English samples appear.
_REVERTED_RE = re.compile(r"^Reverted\b", re.IGNORECASE)

# ── Column header aliases ────────────────────────────────────────────────────
#
# Used both to detect the transaction-table header row AND to calibrate
# column x-positions from its word coordinates. Aliases are grouped by
# logical column, with English first followed by translations for every
# language Revolut is known to localize into. Matching is case-insensitive.
#
# Add a new language by appending its column labels to each list. The header
# detector matches on the *first* token of a multi-word alias (so "Money out"
# and "Paid out" are distinguished at x-classification time, not at the
# alias level).
_HEADER_ALIASES: Dict[str, List[str]] = {
    "date": [
        "Date",  # EN / FR
        "Datum",  # DE / NL
        "Fecha",  # ES
        "Data",  # IT / PT / PL
    ],
    "description": [
        "Description",  # EN / FR
        "Details",  # EN
        "Beschreibung",  # DE
        "Descripción",
        "Descripcion",  # ES
        "Descrizione",  # IT
        "Descrição",
        "Descricao",  # PT
        "Omschrijving",  # NL
        "Opis",  # PL
        "Libellé",
        "Libelle",  # FR (alt.)
    ],
    "money_out": [
        "Money out",
        "Withdrawals",
        "Paid out",
        "Debit",  # EN
        "Ausgehend",
        "Ausgänge",
        "Ausgaenge",
        "Abhebung",
        "Soll",  # DE
        "Débit",
        "Debit",
        "Sortie",
        "Retrait",  # FR
        "Débito",
        "Debito",
        "Retiros",
        "Cargo",  # ES
        "Uscite",
        "Prelievi",
        "Addebito",  # IT
        "Saída",
        "Saida",
        "Débito",
        "Retirada",  # PT
        "Uit",
        "Opnames",  # NL
        "Obciążenia",
        "Obciazenia",
        "Wypłaty",
        "Wyplaty",  # PL
    ],
    "money_in": [
        "Money in",
        "Deposits",
        "Paid in",
        "Credit",  # EN
        "Eingehend",
        "Eingänge",
        "Eingaenge",
        "Einzahlung",
        "Haben",  # DE
        "Crédit",
        "Credit",
        "Entrée",
        "Entree",  # FR
        "Crédito",
        "Credito",
        "Depósitos",
        "Depositos",
        "Abono",  # ES
        "Entrate",
        "Versamenti",
        "Accredito",  # IT
        "Entrada",
        "Depósito",
        "Deposito",  # PT
        "In",
        "Stortingen",  # NL
        "Uznania",
        "Wpłaty",
        "Wplaty",  # PL
    ],
    "balance": [
        "Balance",  # EN / FR
        "Saldo",  # DE / ES / IT / PT / NL / PL
        "Solde",  # FR
    ],
}

# Keywords used by the generic fee fallback when no transaction-type prefix
# matches. Multilingual so "Gebühr" / "frais" / "tassa" / "tarifa" / "taxa"
# / "opłata" trigger FEE classification.
_FEE_WORDS: Tuple[str, ...] = (
    "fee",  # EN
    "gebühr",
    "gebuhr",  # DE
    "frais",  # FR
    "tarifa",
    "comisión",
    "comision",  # ES
    "tassa",
    "commissione",  # IT
    "taxa",
    "tarifa",  # PT
    "kosten",
    "vergoeding",  # NL
    "opłata",
    "oplata",  # PL
)
_FEE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _FEE_WORDS) + r")\b",
    re.IGNORECASE,
)


def _first_tokens(aliases: List[str]) -> Set[str]:
    """Lowercased first tokens of each alias, for position-based matching."""
    return {a.split(" ", 1)[0].casefold() for a in aliases}


_HEADER_FIRST_TOKENS: Dict[str, Set[str]] = {
    col: _first_tokens(_HEADER_ALIASES[col]) for col in _HEADER_ALIASES
}


def _looks_like_header_row(line_text: str) -> bool:
    """True if `line_text` looks like the transaction-table header row.

    Language-agnostic: requires one token-start alias from each of the five
    logical columns (date, description, money_out, money_in, balance). Does
    NOT require a specific word at the line boundary — Revolut's localized
    PDFs can reorder or split column labels in ways that break a strict
    startswith/endswith match.
    """
    tokens = [t.casefold() for t in line_text.split()]
    if not tokens:
        return False

    found: Dict[str, bool] = {col: False for col in _HEADER_ALIASES}
    # money_out/money_in share first tokens in some languages ("Paid out" /
    # "Paid in"); count matches separately so "Paid out Paid in" counts as
    # both columns rather than one.
    money_matches = 0
    for tok in tokens:
        for col in ("date", "description", "balance"):
            if tok in _HEADER_FIRST_TOKENS[col]:
                found[col] = True
        if (
            tok in _HEADER_FIRST_TOKENS["money_out"]
            or tok in _HEADER_FIRST_TOKENS["money_in"]
        ):
            money_matches += 1
    return (
        found["date"]
        and found["description"]
        and found["balance"]
        and money_matches >= 2
    )


def _parse_date(date_str: str) -> datetime:
    """Parse a transaction date, locale-independently.

    Numeric formats (ISO, DD/MM/YYYY) go through strptime. Month-name
    formats (both "Jan 15, 2025" and "15 Jan 2025" orders) are matched
    against the multilingual _MONTH_NAMES table, so a German "15. Januar
    2025" or French "15 janvier 2025" parses even under a C locale.
    """
    date_str = date_str.strip()

    for fmt in _NUMERIC_DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    m = _MONTH_NAME_DATE_RE.match(date_str)
    if m:
        if m.group(1) is not None:  # "Month D, YYYY"
            mon_raw, day, year = m.group(1), m.group(2), m.group(3)
        else:  # "D Month YYYY"
            day, mon_raw, year = m.group(4), m.group(5), m.group(6)
        mon = _MONTH_NAMES.get(mon_raw.casefold().rstrip("."))
        if mon is not None:
            return datetime(int(year), mon, int(day))

    raise ValueError(f"Unrecognized date format: {date_str!r}")


class RevolutPDFFormatError(ValueError):
    """Raised when the PDF has content but nothing the parser recognises."""


# Currencies Revolut writes with a leading symbol (e.g. "$19.61", "€50.00").
# Anything not in this map is written with a trailing ISO code
# (e.g. "29,155.00 TRY", "120.50 CHF").
_PREFIX_SYMBOLS: Dict[str, str] = {
    "EUR": "€",
    "USD": "$",
    "GBP": "£",
    "JPY": "¥",
    "INR": "₹",
    "AUD": "A$",
    "CAD": "C$",
    "NZD": "NZ$",
}

# All known prefixes, longest first so "A$" matches before "$".
_ALL_PREFIXES = sorted(set(_PREFIX_SYMBOLS.values()), key=len, reverse=True)

_LEADING_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in _ALL_PREFIXES) + r")\s*"
)
# Trailing three-letter ISO code, optionally preceded by whitespace.
_TRAILING_CODE_RE = re.compile(r"\s*[A-Z]{3}$")

# ── OFX transaction type mapping ─────────────────────────────────────────────
#
# Maps description keywords to OFX ttype strings.  The first match wins, so
# more specific entries come before generic ones.
#
# Entries marked ★ are confirmed against real Revolut EUR statements (2026).
# Entries marked ○ are best-effort for keywords not yet observed in the wild.
# If you encounter a misclassified transaction, please open an issue.
PDF_TXN_TYPE_MAP: List[Tuple[str, str]] = [
    # ── Transfers ─────────────────────────────────────────────────────────
    ("Transfer to", "XFER"),  # ★ outgoing transfer to person/account
    ("Transfer from", "XFER"),  # ★ incoming transfer from person/account
    ("SWIFT Transfer to", "XFER"),  # ★ outgoing SWIFT / international transfer
    ("SWIFT Transfer from", "XFER"),  # ○ incoming SWIFT / international transfer
    # ── Incoming payments ─────────────────────────────────────────────────
    ("Payment from", "DEP"),  # ★ incoming payment (SEPA credit transfer)
    # ── Interest ──────────────────────────────────────────────────────────
    ("Net Interest Paid", "INT"),  # ★ daily interest on savings/deposit
    ("Withheld Tax Refund", "INT"),  # ★ tax refund on interest (Freistellungsauftrag)
    ("Interest earned", "INT"),  # ○ alternate interest label
    # ── Fees and charges ──────────────────────────────────────────────────
    ("Premium plan fee", "FEE"),  # ★ monthly subscription fee
    ("Plus plan fee", "FEE"),  # ★ monthly subscription fee (Plus tier)
    # ── Currency exchange ─────────────────────────────────────────────────
    ("Exchanged to", "XFER"),  # ★ currency exchange (outgoing leg)
    ("Exchanged from", "XFER"),  # ○ currency exchange (incoming leg)
    # ── Internal savings moves ────────────────────────────────────────────
    ("To EUR", "XFER"),  # ★ move to savings vault / pocket
    ("From EUR", "XFER"),  # ★ move from savings vault / pocket
    # ── Pockets ───────────────────────────────────────────────────────────
    ("To pocket", "XFER"),  # ★ move money into a pocket
    ("Pocket Withdrawal", "XFER"),  # ★ withdraw money from a pocket
    # ── ATM ───────────────────────────────────────────────────────────────
    ("Cash withdrawal at", "ATM"),  # ★ ATM cash withdrawal
    # ── Top-ups ───────────────────────────────────────────────────────────
    ("Top-up by", "DEP"),  # ★ incoming top-up
    # ── Plan refunds ─────────────────────────────────────────────────────
    ("Plan termination refund", "FEE"),  # ★ refund of a cancelled plan
]


def _parse_amount(text: str) -> Decimal:
    """Strip leading symbol / trailing ISO code / thousands separators."""
    cleaned = _LEADING_PREFIX_RE.sub("", text)
    cleaned = _TRAILING_CODE_RE.sub("", cleaned)
    cleaned = cleaned.replace(",", "").strip()
    return Decimal(cleaned)


def _is_primary_amount(text: str, currency: str) -> bool:
    """True if `text` is an amount in the given primary currency.

    Prefix currencies match with `startswith(symbol)`;
    suffix currencies match with `endswith(code)`.
    """
    if currency in _PREFIX_SYMBOLS:
        return text.startswith(_PREFIX_SYMBOLS[currency])
    return text.rstrip().endswith(currency)


def _is_foreign_amount(text: str, currency: str) -> bool:
    """True if `text` is an amount in a *different* (non-primary) currency.

    Used to discard secondary-currency continuation lines and columns.
    """
    if _is_primary_amount(text, currency):
        return False
    if _LEADING_PREFIX_RE.match(text):
        return True
    # Trailing three-letter code that isn't our currency
    m = _TRAILING_CODE_RE.search(text)
    if m and text.rstrip()[-3:] != currency:
        return True
    return False


def _make_id(date: datetime, amount: Decimal, memo: str) -> str:
    """Stable 16-hex-char transaction ID derived from key fields."""
    raw = f"{date.isoformat()}|{amount}|{memo}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _match_txn_type(description: str) -> str:
    """Match a PDF transaction description to an OFX transaction type.

    Uses the PDF_TXN_TYPE_MAP entries first (prefix match), then falls back
    to a generic word-boundary check for "fee", and finally to amount-sign
    heuristics.
    """
    for prefix, ttype in PDF_TXN_TYPE_MAP:
        if description.lower().startswith(prefix.lower()):
            return ttype
    # Multilingual fee-word fallback (EN, DE, FR, ES, IT, PT, NL, PL). Word-
    # boundary-matched so "Coffee" doesn't spuriously match "fee".
    if _FEE_RE.search(description):
        return "FEE"
    return ""  # empty = caller should use amount-sign fallback


class _RawTransaction:
    def __init__(
        self,
        date_str: str,
        description: str,
        money_out: Optional[str],
        money_in: Optional[str],
        balance: Optional[str],
        section: str,
        owner: Optional[str],
    ):
        self.date_str = date_str
        self.description = description
        self.detail_lines: List[str] = []
        self.money_out = money_out
        self.money_in = money_in
        self.balance = balance
        self.section = section
        self.owner = owner


class RevolutPDFParser(AbstractStatementParser):
    def __init__(
        self,
        filename: str,
        account: str = "Current",
        currency: str = "EUR",
        account_id: str = "",
    ):
        self.filename = filename
        self.account_filter = account
        self.currency = currency
        self.account_id = account_id
        # Column x-thresholds — recalibrated when the header row is parsed.
        self._desc_x = _DEFAULT_DESC_X
        self._money_out_x = _DEFAULT_MONEY_OUT_X
        self._money_in_x = _DEFAULT_MONEY_IN_X
        self._balance_x = _DEFAULT_BALANCE_X
        # Diagnostic counters populated by _extract_all_transactions.
        self._n_pages = 0
        self._sections_seen = 0
        self._header_rows_seen = 0

    def parse(self) -> Statement:
        logger.info("Parsing PDF %s", self.filename)
        statement = Statement()
        statement.bank_id = "Revolut"

        # Header extraction (inside _extract_all_transactions) may update
        # self.currency and self.account_id, so set them on the statement
        # afterwards.
        raw_transactions = self._extract_all_transactions()

        # Loud-fail when the PDF has content but we recognised nothing — a
        # silent empty OFX would make the user think the statement was empty.
        # Zero transactions WITH section headers is legitimate (empty account).
        if (
            not raw_transactions
            and self._n_pages > 0
            and self._sections_seen == 0
            and self._header_rows_seen == 0
        ):
            raise RevolutPDFFormatError(
                f"Could not recognise any transaction table in "
                f"{self.filename!r}. Pages={self._n_pages}, "
                f"section_headers=0, header_rows=0. "
                f"The PDF layout may have changed — please open an issue "
                f"at https://github.com/eduralph/ofxstatement-revolut/issues."
            )

        filtered = self._filter_transactions(raw_transactions)
        statement.currency = self.currency
        logger.info(
            "Extracted %d total transactions, %d match account=%r",
            len(raw_transactions),
            len(filtered),
            self.account_filter,
        )

        for raw in filtered:
            sl = self._to_statement_line(raw)
            statement.lines.append(sl)

        statement.account_id = self.account_id

        if statement.lines:
            statement.start_date = statement.lines[0].date
            statement.end_date = statement.lines[-1].date
            # Derive balances from first and last transaction
            cur = self.currency
            if filtered[0].balance:
                first_amount = (
                    -_parse_amount(filtered[0].money_out)
                    if filtered[0].money_out
                    and _is_primary_amount(filtered[0].money_out, cur)
                    else (
                        _parse_amount(filtered[0].money_in)
                        if filtered[0].money_in
                        and _is_primary_amount(filtered[0].money_in, cur)
                        else Decimal("0")
                    )
                )
                first_balance = _parse_amount(filtered[0].balance)
                statement.start_balance = first_balance - first_amount
            if filtered[-1].balance:
                statement.end_balance = _parse_amount(filtered[-1].balance)
            logger.info(
                "Statement: %s to %s, %d lines, start_balance=%s, end_balance=%s",
                (
                    statement.start_date.strftime("%Y-%m-%d")
                    if statement.start_date
                    else "?"
                ),
                statement.end_date.strftime("%Y-%m-%d") if statement.end_date else "?",
                len(statement.lines),
                statement.start_balance,
                statement.end_balance,
            )
        else:
            logger.warning("No transactions found for account=%r", self.account_filter)

        return statement

    def _filter_transactions(
        self, raw_transactions: List[_RawTransaction]
    ) -> List[_RawTransaction]:
        """Filter raw transactions based on the account setting.

        The account setting can be:
        - "Current" (default): main account holder's current account transactions
        - "Deposit": main account holder's deposit/savings transactions
        - An owner name (e.g. "Andrew", "MATTHEW"): all transactions belonging
          to that sub-account (current account, pockets, and savings combined)
        """
        acct = self.account_filter.lower()

        if acct in ("current", "deposit"):
            section_type = "Deposit" if acct == "deposit" else "Account"
            return [
                t
                for t in raw_transactions
                if t.section.lower() == section_type.lower() and t.owner is None
            ]

        # Sub-account: match owner name case-insensitively, include all sections
        return [
            t
            for t in raw_transactions
            if t.owner is not None and t.owner.lower() == acct
        ]

    def _extract_all_transactions(self) -> list:
        """Parse every transaction from the PDF across all pages and sections."""
        transactions: list = []
        current_section: Optional[str] = None
        current_owner: Optional[str] = None
        current_txn: Optional[_RawTransaction] = None
        in_table = False
        self._sections_seen = 0
        self._header_rows_seen = 0

        with pdfplumber.open(self.filename) as pdf:
            self._n_pages = len(pdf.pages)
            first_page_text = pdf.pages[0].extract_text() or ""
            self._extract_header_info(first_page_text)
            logger.info(
                "PDF: %d page(s), currency=%s, account_id=%s",
                self._n_pages,
                self.currency,
                self.account_id or "(not set)",
            )

            for page_num, page in enumerate(pdf.pages, 1):
                words = page.extract_words(keep_blank_chars=True)
                word_lines = self._group_words_by_line(words)
                logger.debug(
                    "  Page %d/%d: %d word-lines",
                    page_num,
                    self._n_pages,
                    len(word_lines),
                )

                for y, line_words in sorted(word_lines.items()):
                    line_text = " ".join(
                        w["text"] for w in sorted(line_words, key=lambda w: w["x0"])
                    )

                    # Check for "Reverted" sub-table — these transactions
                    # were charged and then reversed (net zero) and must
                    # not be included in the statement. Close any pending
                    # transaction and stop capturing until the next proper
                    # section header or balanced table header appears.
                    if _REVERTED_RE.match(line_text.strip()):
                        if current_txn:
                            transactions.append(current_txn)
                            current_txn = None
                        in_table = False
                        continue

                    # Check for section header
                    m = _SECTION_RE.match(line_text.strip())
                    if m:
                        if current_txn:
                            transactions.append(current_txn)
                            current_txn = None
                        current_owner = m.group(1)
                        current_section = _canonical_section(m.group(2))
                        in_table = False
                        self._sections_seen += 1
                        logger.debug(
                            "  Page %d: section=%r owner=%r (%s to %s)",
                            page_num,
                            current_section,
                            current_owner,
                            m.group(3),
                            m.group(4),
                        )
                        continue

                    # Check for table header
                    if _looks_like_header_row(line_text):
                        in_table = True
                        self._header_rows_seen += 1
                        self._calibrate_from_header(line_words)
                        continue

                    if not in_table or current_section is None:
                        continue

                    # Skip footer
                    if "Report lost or stolen card" in line_text:
                        if current_txn:
                            transactions.append(current_txn)
                            current_txn = None
                        in_table = False
                        continue

                    # Classify words by x position
                    date_words = []
                    desc_words = []
                    money_out_words = []
                    money_in_words = []
                    balance_words = []

                    for w in sorted(line_words, key=lambda w: w["x0"]):
                        x = w["x0"]
                        if x < self._desc_x:
                            date_words.append(w["text"])
                        elif x < self._money_out_x:
                            desc_words.append(w["text"])
                        elif x < self._money_in_x:
                            money_out_words.append(w["text"])
                        elif x < self._balance_x:
                            money_in_words.append(w["text"])
                        else:
                            balance_words.append(w["text"])

                    date_text = " ".join(date_words).strip()
                    desc_text = " ".join(desc_words).strip()
                    money_out_text = " ".join(money_out_words).strip() or None
                    money_in_text = " ".join(money_in_words).strip() or None
                    balance_text = " ".join(balance_words).strip() or None

                    # New transaction line?
                    if _DATE_RE.match(date_text) and desc_text:
                        if current_txn:
                            transactions.append(current_txn)

                        # Filter secondary-currency amounts (keep only primary)
                        cur = self.currency
                        if money_out_text and not _is_primary_amount(
                            money_out_text, cur
                        ):
                            logger.debug(
                                "  Skipping non-%s money_out %r on %s %s",
                                cur,
                                money_out_text,
                                date_text,
                                desc_text,
                            )
                            money_out_text = None
                        if money_in_text and not _is_primary_amount(money_in_text, cur):
                            logger.debug(
                                "  Skipping non-%s money_in %r on %s %s",
                                cur,
                                money_in_text,
                                date_text,
                                desc_text,
                            )
                            money_in_text = None

                        current_txn = _RawTransaction(
                            date_str=date_text,
                            description=desc_text,
                            money_out=money_out_text,
                            money_in=money_in_text,
                            balance=balance_text,
                            section=current_section,
                            owner=current_owner,
                        )
                    elif current_txn and desc_text:
                        # Detail/continuation line - skip secondary currency lines
                        if not _is_foreign_amount(desc_text, self.currency):
                            current_txn.detail_lines.append(desc_text)

        if current_txn:
            transactions.append(current_txn)

        return transactions

    def _calibrate_from_header(self, line_words: list) -> None:
        """Update column x-thresholds from the observed header row.

        Amount columns are right-aligned, so a number's x0 can be *less* than
        its header word's x0. To keep all numbers in the right bucket we use
        the midpoint between adjacent header x0's as each threshold. The
        description threshold uses the midpoint between Date and Description
        for the same reason applied to left-aligned columns: description
        words share the Description column's x0 to within PDF rendering
        jitter, so a threshold set exactly at that x0 will misclassify any
        description word whose rasterised x0 drifts even 0.001 below it.

        Falls back to keeping existing thresholds if the header words can't be
        resolved or if the resulting thresholds aren't strictly increasing
        (which would indicate we mis-identified one of the columns).
        """
        date_x: Optional[float] = None
        desc_x: Optional[float] = None
        balance_x: Optional[float] = None
        money_positions: List[float] = []

        date_first = _HEADER_FIRST_TOKENS["date"]
        desc_first = _HEADER_FIRST_TOKENS["description"]
        balance_first = _HEADER_FIRST_TOKENS["balance"]
        money_first = (
            _HEADER_FIRST_TOKENS["money_out"] | _HEADER_FIRST_TOKENS["money_in"]
        )

        for w in sorted(line_words, key=lambda w: w["x0"]):
            # pdfplumber with keep_blank_chars=True returns multi-word labels
            # like "Money out" as a single word. Match on the first token so
            # both single-word ("Withdrawals") and multi-word ("Money out")
            # aliases calibrate identically.
            first = w["text"].strip().split(" ", 1)[0].casefold()
            x0 = w["x0"]
            if date_x is None and first in date_first:
                date_x = x0
            elif desc_x is None and first in desc_first:
                desc_x = x0
            elif first in money_first:
                money_positions.append(x0)
            elif balance_x is None and first in balance_first:
                balance_x = x0

        if desc_x is None or len(money_positions) < 2 or balance_x is None:
            logger.debug(
                "Header row incomplete (date=%s, desc=%s, money=%s, balance=%s) "
                "— keeping existing thresholds",
                date_x,
                desc_x,
                money_positions,
                balance_x,
            )
            return

        money_out_x, money_in_x = money_positions[0], money_positions[1]
        # If Date was matched, use the midpoint. Otherwise fall back to the
        # Description x0 itself — still wrong at sub-point precision, but the
        # best we can do without both endpoints.
        new_desc_x = (date_x + desc_x) / 2 if date_x is not None else desc_x
        new_money_out_x = (desc_x + money_out_x) / 2
        new_money_in_x = (money_out_x + money_in_x) / 2
        new_balance_x = (money_in_x + balance_x) / 2

        # Sanity check: thresholds must be strictly left-to-right. Anything
        # else means we mislabelled a word (e.g. "Balance" appearing before
        # "Description") and committing these values would misclassify every
        # subsequent transaction row.
        if not (new_desc_x < new_money_out_x < new_money_in_x < new_balance_x):
            logger.warning(
                "Calibration produced non-monotonic thresholds "
                "(desc=%.1f money_out=%.1f money_in=%.1f balance=%.1f) — "
                "keeping previous values",
                new_desc_x,
                new_money_out_x,
                new_money_in_x,
                new_balance_x,
            )
            return

        self._desc_x = new_desc_x
        self._money_out_x = new_money_out_x
        self._money_in_x = new_money_in_x
        self._balance_x = new_balance_x

        logger.debug(
            "Calibrated columns: desc=%.1f money_out=%.1f money_in=%.1f balance=%.1f",
            self._desc_x,
            self._money_out_x,
            self._money_in_x,
            self._balance_x,
        )

    def _extract_header_info(self, first_page_text: str) -> None:
        m = _CURRENCY_RE.search(first_page_text)
        if m:
            self.currency = m.group(1)
            logger.debug("Currency extracted from PDF: %s", self.currency)

        if not self.account_id:
            self.account_id = self._extract_account_id(first_page_text)
            if self.account_id:
                logger.debug("Account ID from PDF: %s", self.account_id)

    def _extract_account_id(self, text: str) -> str:
        """Extract a stable per-currency account identifier.

        GBP statements use Sort Code + Account Number (UK bank coordinates)
        and the IBAN field contains the main EUR account — so for GBP we
        prefer the UK coordinates. For other currencies, the first IBAN
        wins. Returns empty string if nothing is found.
        """
        if self.currency == "GBP":
            sc = _SORT_CODE_RE.search(text)
            an = _ACCOUNT_NUMBER_RE.search(text)
            if sc and an:
                return f"GB-{sc.group(1)}-{an.group(1)}"

        m = _IBAN_RE.search(text)
        if m:
            return m.group(1)
        return ""

    def _group_words_by_line(self, words: list) -> Dict[int, list]:
        lines: Dict[int, list] = {}
        for w in words:
            y = round(w["top"] / 3) * 3
            if y not in lines:
                lines[y] = []
            lines[y].append(w)
        return lines

    def _to_statement_line(self, raw: _RawTransaction) -> StatementLine:
        sl = StatementLine()
        sl.date = _parse_date(raw.date_str)

        cur = self.currency
        if raw.money_out and _is_primary_amount(raw.money_out, cur):
            sl.amount = -_parse_amount(raw.money_out)
        elif raw.money_in and _is_primary_amount(raw.money_in, cur):
            sl.amount = _parse_amount(raw.money_in)
        else:
            sl.amount = Decimal("0")
            logger.warning(
                "Transaction on %s has no %s amount: %r (out=%r, in=%r)",
                raw.date_str,
                cur,
                raw.description,
                raw.money_out,
                raw.money_in,
            )

        sl.memo = raw.description
        if raw.detail_lines:
            sl.memo = raw.description + " | " + " | ".join(raw.detail_lines)

        sl.id = _make_id(sl.date, sl.amount, sl.memo)

        ttype = _match_txn_type(raw.description)
        if ttype:
            sl.trntype = ttype
        elif sl.amount > 0:
            sl.trntype = "CREDIT"
            logger.debug(
                "No type-map match for %r — falling back to CREDIT (amount=%s)",
                raw.description,
                sl.amount,
            )
        else:
            sl.trntype = "DEBIT"
            logger.debug(
                "No type-map match for %r — falling back to DEBIT (amount=%s)",
                raw.description,
                sl.amount,
            )

        return sl
