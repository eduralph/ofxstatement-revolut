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

import csv
import hashlib
import logging
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

from ofxstatement.parser import AbstractStatementParser
from ofxstatement.statement import Statement, StatementLine

logger = logging.getLogger(__name__)

# Timestamp formats accepted in the Completed Date / Started Date columns.
# First match wins. Hedges against a future Revolut format change so one
# wording tweak doesn't silently drop every row.
_DATE_FORMATS: Tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",  # 2025-01-15 10:30:00 (current Revolut format)
    "%Y-%m-%dT%H:%M:%S",  # 2025-01-15T10:30:00 (ISO 8601)
    "%Y-%m-%dT%H:%M:%S.%f",  # 2025-01-15T10:30:00.123456
    "%Y-%m-%d",  # date-only
)

# State values that mean "this transaction settled and should be imported".
# Matched case-insensitively. Pending / reverted / declined states are dropped.
_ACCEPTED_STATES: Set[str] = {"COMPLETED", "COMPLETE", "SETTLED", "POSTED"}


def _parse_csv_date(date_str: str) -> datetime:
    """Parse a CSV timestamp against every supported format."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {date_str!r}")


def _parse_csv_amount(s: str) -> Decimal:
    """Parse a numeric amount accepting both `.` and `,` decimal separators.

    Revolut's English exports use `.` as decimal and `,` as thousands
    (`-1,234.56`). A localized export might invert those (`-1.234,56`). This
    function picks the decimal separator as the rightmost of `.` / `,`, and
    treats the other as a thousands grouping. If only one kind of separator
    is present, a single occurrence with 1–2 trailing digits is treated as
    the decimal point; anything else as thousands grouping.
    """
    s = s.strip()
    if not s:
        raise ValueError("empty amount")

    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        if s.count(",") == 1 and 1 <= len(s) - s.rfind(",") - 1 <= 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_dot:
        if s.count(".") != 1 or not (1 <= len(s) - s.rfind(".") - 1 <= 2):
            s = s.replace(".", "")

    return Decimal(s)


# ── CSV column resolution ────────────────────────────────────────────────────
#
# Revolut's current CSV header is:
#   Type, Product, Started Date, Completed Date, Description, Amount, Fee,
#   Currency, State, Balance
#
# Rather than hardcoding positional indices we resolve each logical field name
# against the header row at runtime. If Revolut reorders or inserts columns
# we pick them up automatically; if they rename one we add the new name to the
# alias list; if they remove a *required* column we raise a clear error
# instead of silently writing the wrong data into the wrong field.
# Known-language translations of each CSV column header. Observed Revolut
# exports have always used English headers even on localized accounts, but
# listing translations here is cheap insurance — if a future localized export
# renames "Amount" to "Betrag" we pick it up without code changes.
_FIELD_ALIASES: Dict[str, List[str]] = {
    "type": [
        "Type",  # EN
        "Typ",  # DE / PL
        "Type",  # FR / NL
        "Tipo",  # ES / IT / PT
    ],
    "product": [
        "Product",  # EN
        "Produkt",  # DE / PL
        "Produit",  # FR
        "Producto",  # ES
        "Prodotto",  # IT
        "Produto",  # PT
    ],
    "started": [
        "Started Date",
        "Start Date",  # EN
        "Startdatum",  # DE / NL
        "Date de début",
        "Date de debut",  # FR
        "Fecha de inicio",  # ES
        "Data di inizio",  # IT
        "Data de início",
        "Data de inicio",  # PT
        "Data rozpoczęcia",
        "Data rozpoczecia",  # PL
    ],
    "completed": [
        "Completed Date",
        "Completion Date",  # EN
        "Abschlussdatum",
        "Buchungsdatum",  # DE
        "Date d'achèvement",
        "Date d'achevement",  # FR
        "Fecha de finalización",  # ES
        "Data di completamento",  # IT
        "Data de conclusão",
        "Data de conclusao",  # PT
        "Afgerond op",  # NL
        "Data zakończenia",
        "Data zakonczenia",  # PL
    ],
    "description": [
        "Description",  # EN / FR
        "Beschreibung",  # DE
        "Descripción",
        "Descripcion",  # ES
        "Descrizione",  # IT
        "Descrição",
        "Descricao",  # PT
        "Omschrijving",  # NL
        "Opis",  # PL
    ],
    "amount": [
        "Amount",  # EN
        "Betrag",  # DE
        "Montant",  # FR
        "Importe",
        "Cantidad",  # ES
        "Importo",  # IT
        "Valor",
        "Montante",  # PT
        "Bedrag",  # NL
        "Kwota",  # PL
    ],
    "fee": [
        "Fee",  # EN
        "Gebühr",
        "Gebuehr",  # DE
        "Frais",  # FR
        "Tarifa",
        "Comisión",
        "Comision",  # ES
        "Commissione",  # IT
        "Taxa",  # PT
        "Kosten",  # NL
        "Opłata",
        "Oplata",  # PL
    ],
    "currency": [
        "Currency",  # EN
        "Währung",
        "Waehrung",  # DE
        "Devise",  # FR
        "Moneda",
        "Divisa",  # ES
        "Valuta",  # IT / NL
        "Moeda",  # PT
        "Waluta",  # PL
    ],
    "state": [
        "State",
        "Status",  # EN
        "Status",  # DE / NL / PL
        "État",
        "Etat",  # FR
        "Estado",  # ES / PT
        "Stato",  # IT
    ],
    "balance": [
        "Balance",  # EN / FR
        "Saldo",  # DE / ES / IT / PT / NL / PL
        "Solde",  # FR (alt.)
    ],
}

# Fields we cannot produce a valid statement line without. Others are used
# when present and quietly skipped when absent.
_REQUIRED_FIELDS = frozenset(
    {"type", "product", "completed", "description", "amount", "state"}
)


class RevolutCSVFormatError(ValueError):
    """Raised when the CSV header is missing columns the parser requires."""


def _resolve_columns(header: List[str]) -> Dict[str, int]:
    """Map logical field names to positional indices in the given header row.

    Matching is case-insensitive and whitespace-tolerant so small cosmetic
    changes in a future export (lowercased headers, stray whitespace) don't
    break resolution. Raises RevolutCSVFormatError if any required field is
    absent.
    """
    positions = {h.strip().casefold(): i for i, h in enumerate(header)}
    resolved: Dict[str, int] = {}
    missing: List[str] = []
    for field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            idx = positions.get(alias.casefold())
            if idx is not None:
                resolved[field] = idx
                break
        else:
            if field in _REQUIRED_FIELDS:
                missing.append(field)
    if missing:
        raise RevolutCSVFormatError(
            f"CSV is missing required columns {sorted(missing)}. "
            f"Header was: {header!r}. "
            f"Known aliases: "
            f"{ {f: _FIELD_ALIASES[f] for f in missing} }"
        )
    return resolved


# ── OFX transaction type mapping ─────────────────────────────────────────────
#
# Maps the Revolut CSV "Type" column value to an OFX ttype string.
#
# Entries marked ★ are confirmed against real Revolut CSV exports (2026).
# Entries marked ○ are best-effort for types not yet observed in the wild.
# If you encounter a misclassified transaction, please open an issue.
CSV_TXN_TYPE_MAP: List[Tuple[str, str]] = [
    # ── Transfers ─────────────────────────────────────────────────────────
    ("Transfer", "XFER"),  # ★ SEPA transfers, internal moves, person-to-person
    # ── Card payments ─────────────────────────────────────────────────────
    ("Card Payment", "POS"),  # ★ card purchase (VISA/Mastercard)
    # ── Top-ups ───────────────────────────────────────────────────────────
    ("Topup", "DEP"),  # ★ incoming top-up (bank transfer in)
    # ── Currency exchange ─────────────────────────────────────────────────
    ("Exchange", "XFER"),  # ★ currency exchange
    # ── Fees and charges ──────────────────────────────────────────────────
    ("Fee", "FEE"),  # ★ subscription fee (Plus plan, etc.)
    ("Charge", "FEE"),  # ★ fees (Premium plan, etc.)
    ("Charge Refund", "FEE"),  # ★ refund of a prior charge (negative fee)
    # ── Interest ──────────────────────────────────────────────────────────
    ("Interest", "INT"),  # ★ interest earned on deposits
    # ── ATM ───────────────────────────────────────────────────────────────
    ("ATM", "ATM"),  # ★ cash withdrawal
    # ── Rewards ───────────────────────────────────────────────────────────
    ("Reward", "CREDIT"),  # ○ cashback or promotional rewards
    # ── Refunds ───────────────────────────────────────────────────────────
    ("Card Refund", "CREDIT"),  # ★ card payment refund from merchant
    ("Refund", "CREDIT"),  # ○ other merchant refund
]


def _make_id(date: datetime, amount: Decimal, memo: str) -> str:
    """Stable 16-hex-char transaction ID derived from key fields."""
    raw = f"{date.isoformat()}|{amount}|{memo}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _match_csv_txn_type(txn_type: str, description: str) -> str:
    """Match a CSV row's Type column to an OFX transaction type.

    Falls back to a keyword check for "fee" in the description, then to
    amount-sign heuristics (returned as empty string for the caller to handle).
    """
    for prefix, ttype in CSV_TXN_TYPE_MAP:
        if txn_type.lower() == prefix.lower():
            return ttype
    if "fee" in description.lower():
        return "FEE"
    return ""  # caller uses amount-sign fallback


class RevolutCSVParser(AbstractStatementParser):
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
        self._first_balance: Optional[Decimal] = None
        self._last_balance: Optional[Decimal] = None

    def parse(self) -> Statement:
        logger.info("Parsing CSV %s", self.filename)
        statement = Statement()
        statement.account_id = self.account_id
        statement.bank_id = "Revolut"

        # `utf-8-sig` strips a leading BOM if present; some Revolut exports
        # have been seen in the wild with one and a literal "\ufeffType" in
        # the first header cell would otherwise break alias resolution.
        with open(self.filename, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                logger.warning("CSV %s is empty", self.filename)
                statement.currency = self.currency
                return statement
            cols = _resolve_columns(header)
            logger.debug("Resolved CSV columns: %s", cols)
            data_rows = list(reader)

        max_index = max(cols.values())
        detected_currency = self._detect_currency(data_rows, cols, max_index)
        if detected_currency:
            self.currency = detected_currency

        skipped_product = 0
        skipped_state = 0
        skipped_parse = 0
        skipped_currency = 0
        skipped_short = 0

        # Collected diagnostics for the "nothing came through" case.
        products_seen: Counter = Counter()
        states_seen: Counter = Counter()
        currencies_seen: Counter = Counter()

        cur_idx = cols.get("currency")

        for offset, row in enumerate(data_rows):
            row_num = offset + 2  # +1 for header, +1 for 1-indexed
            if len(row) <= max_index:
                logger.debug(
                    "Row %d: too few columns (%d, need > %d), skipping",
                    row_num,
                    len(row),
                    max_index,
                )
                skipped_short += 1
                continue

            product = row[cols["product"]].strip()
            state = row[cols["state"]].strip()
            row_currency = row[cur_idx].strip() if cur_idx is not None else ""
            products_seen[product] += 1
            states_seen[state] += 1
            if row_currency:
                currencies_seen[row_currency] += 1

            if not self._matches_account(product):
                skipped_product += 1
                continue

            if state.upper() not in _ACCEPTED_STATES:
                logger.debug(
                    "Row %d: skipping state=%r (%s)",
                    row_num,
                    state,
                    row[cols["description"]].strip(),
                )
                skipped_state += 1
                continue

            if row_currency and row_currency != self.currency:
                logger.debug(
                    "Row %d: skipping currency=%r (want %s)",
                    row_num,
                    row_currency,
                    self.currency,
                )
                skipped_currency += 1
                continue

            sl = self._parse_row(row, row_num, cols)
            if sl:
                statement.lines.append(sl)
            else:
                skipped_parse += 1

        statement.currency = self.currency
        skipped_parse += skipped_short

        if skipped_product or skipped_state or skipped_parse or skipped_currency:
            logger.info(
                "CSV filtering: %d kept, %d skipped "
                "(product=%d, state=%d, currency=%d, parse=%d)",
                len(statement.lines),
                skipped_product + skipped_state + skipped_currency + skipped_parse,
                skipped_product,
                skipped_state,
                skipped_currency,
                skipped_parse,
            )

        if statement.lines:
            statement.start_date = statement.lines[0].date
            statement.end_date = statement.lines[-1].date
            first_sl = statement.lines[0]
            if self._last_balance is not None:
                statement.end_balance = self._last_balance
            if self._first_balance is not None and first_sl.amount is not None:
                statement.start_balance = self._first_balance - first_sl.amount
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
        elif data_rows:
            # 0 lines despite non-empty file — enumerate what we saw so the
            # user can tell "wrong account filter" from "schema change".
            logger.warning(
                "No transactions emitted for account=%r in %s. "
                "Products seen: %s. States seen: %s. Currencies seen: %s. "
                "Accepted states are %s (case-insensitive).",
                self.account_filter,
                self.filename,
                dict(products_seen) or "(none)",
                dict(states_seen) or "(none)",
                dict(currencies_seen) or "(none)",
                sorted(_ACCEPTED_STATES),
            )

        return statement

    def _matches_account(self, product: str) -> bool:
        return product.lower() == self.account_filter.lower()

    def _detect_currency(
        self,
        rows: List[List[str]],
        cols: Dict[str, int],
        max_index: int,
    ) -> Optional[str]:
        """Return the dominant currency in rows matching the account filter.

        Revolut exports one CSV per currency account, so the expected case is
        a single currency throughout. If multiple are present we pick the most
        frequent one and let the row-level currency filter drop the rest.
        Returns None if the CSV has no Currency column.
        """
        cur_idx = cols.get("currency")
        if cur_idx is None:
            return None

        counts: Dict[str, int] = {}
        for row in rows:
            if len(row) <= max_index:
                continue
            if not self._matches_account(row[cols["product"]].strip()):
                continue
            if row[cols["state"]].strip().upper() not in _ACCEPTED_STATES:
                continue
            cur = row[cur_idx].strip()
            if cur:
                counts[cur] = counts.get(cur, 0) + 1

        if not counts:
            return None
        dominant = max(counts, key=lambda c: counts[c])
        if len(counts) > 1:
            logger.warning(
                "CSV contains multiple currencies %s — using %s; set "
                "`currency` in config to override.",
                dict(counts),
                dominant,
            )
        logger.debug("Detected currency %s from CSV", dominant)
        return dominant

    def _parse_row(
        self,
        row: List[str],
        row_num: int,
        cols: Dict[str, int],
    ) -> Optional[StatementLine]:
        sl = StatementLine()

        # Use completed date as the transaction date
        date_str = row[cols["completed"]].strip()
        try:
            sl.date = _parse_csv_date(date_str)
        except ValueError:
            logger.warning(
                "Row %d: cannot parse date %r, skipping",
                row_num,
                date_str,
            )
            return None

        sl.memo = row[cols["description"]].strip()

        amount_str = row[cols["amount"]].strip()
        fee_idx = cols.get("fee")
        fee_str = row[fee_idx].strip() if fee_idx is not None else ""
        try:
            sl.amount = _parse_csv_amount(amount_str)
            if fee_str:
                fee = _parse_csv_amount(fee_str)
                if fee:
                    sl.amount -= fee
                    logger.debug(
                        "Row %d: amount=%s fee=%s → net=%s (%s)",
                        row_num,
                        amount_str,
                        fee_str,
                        sl.amount,
                        sl.memo,
                    )
        except Exception:
            logger.warning(
                "Row %d: cannot parse amount=%r fee=%r, skipping",
                row_num,
                amount_str,
                fee_str,
            )
            return None

        # Track balances for start/end extraction
        balance_idx = cols.get("balance")
        if balance_idx is not None:
            try:
                balance = _parse_csv_amount(row[balance_idx].strip())
                if self._first_balance is None:
                    self._first_balance = balance
                self._last_balance = balance
            except Exception:
                pass

        sl.id = _make_id(sl.date, sl.amount, sl.memo)

        # Determine transaction type
        txn_type = row[cols["type"]].strip()
        ttype = _match_csv_txn_type(txn_type, sl.memo)
        if ttype:
            sl.trntype = ttype
        elif sl.amount > 0:
            sl.trntype = "CREDIT"
            logger.debug(
                "Row %d: unknown CSV type %r for %r — falling back to CREDIT",
                row_num,
                txn_type,
                sl.memo,
            )
        else:
            sl.trntype = "DEBIT"
            logger.debug(
                "Row %d: unknown CSV type %r for %r — falling back to DEBIT",
                row_num,
                txn_type,
                sl.memo,
            )

        return sl
