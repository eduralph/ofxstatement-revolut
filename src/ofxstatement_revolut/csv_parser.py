import csv
import hashlib
import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from ofxstatement.parser import AbstractStatementParser
from ofxstatement.statement import Statement, StatementLine

logger = logging.getLogger(__name__)

# CSV columns:
# Type, Product, Started Date, Completed Date, Description, Amount, Fee, Currency, State, Balance
_COL_TYPE = 0
_COL_PRODUCT = 1
_COL_STARTED = 2
_COL_COMPLETED = 3
_COL_DESCRIPTION = 4
_COL_AMOUNT = 5
_COL_FEE = 6
_COL_CURRENCY = 7
_COL_STATE = 8
_COL_BALANCE = 9

# ── OFX transaction type mapping ─────────────────────────────────────────────
#
# Maps the Revolut CSV "Type" column value to an OFX ttype string.
#
# Entries marked ★ are confirmed against real Revolut CSV exports (2026).
# Entries marked ○ are best-effort for types not yet observed in the wild.
# If you encounter a misclassified transaction, please open an issue.
CSV_TXN_TYPE_MAP: List[Tuple[str, str]] = [
    # ── Transfers ─────────────────────────────────────────────────────────
    ("Transfer", "XFER"),           # ★ SEPA transfers, internal moves, person-to-person
    # ── Card payments ─────────────────────────────────────────────────────
    ("Card Payment", "POS"),        # ★ card purchase (VISA/Mastercard)
    # ── Top-ups ───────────────────────────────────────────────────────────
    ("Topup", "DEP"),               # ★ incoming top-up (bank transfer in)
    # ── Currency exchange ─────────────────────────────────────────────────
    ("Exchange", "XFER"),           # ★ currency exchange
    # ── Fees and charges ──────────────────────────────────────────────────
    ("Fee", "FEE"),                 # ★ subscription fee (Plus plan, etc.)
    ("Charge", "FEE"),              # ★ fees (Premium plan, etc.)
    ("Charge Refund", "FEE"),       # ★ refund of a prior charge (negative fee)
    # ── Interest ──────────────────────────────────────────────────────────
    ("Interest", "INT"),            # ★ interest earned on deposits
    # ── ATM ───────────────────────────────────────────────────────────────
    ("ATM", "ATM"),                 # ★ cash withdrawal
    # ── Rewards ───────────────────────────────────────────────────────────
    ("Reward", "CREDIT"),           # ○ cashback or promotional rewards
    # ── Refunds ───────────────────────────────────────────────────────────
    ("Card Refund", "CREDIT"),      # ★ card payment refund from merchant
    ("Refund", "CREDIT"),           # ○ other merchant refund
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
        statement.currency = self.currency
        statement.account_id = self.account_id
        statement.bank_id = "Revolut"

        skipped_product = 0
        skipped_state = 0
        skipped_parse = 0

        with open(self.filename, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader)  # skip header row

            for row_num, row in enumerate(reader, 2):  # 2 = first data row
                if len(row) < 10:
                    logger.debug("Row %d: too few columns (%d), skipping", row_num, len(row))
                    continue

                # Filter by product type
                product = row[_COL_PRODUCT].strip()
                if not self._matches_account(product):
                    skipped_product += 1
                    continue

                # Only include completed transactions
                state = row[_COL_STATE].strip()
                if state != "COMPLETED":
                    logger.debug(
                        "Row %d: skipping state=%r (%s)",
                        row_num, state, row[_COL_DESCRIPTION].strip(),
                    )
                    skipped_state += 1
                    continue

                sl = self._parse_row(row, row_num)
                if sl:
                    statement.lines.append(sl)
                else:
                    skipped_parse += 1

        if skipped_product or skipped_state or skipped_parse:
            logger.info(
                "CSV filtering: %d kept, %d skipped (product=%d, state=%d, parse=%d)",
                len(statement.lines), skipped_product + skipped_state + skipped_parse,
                skipped_product, skipped_state, skipped_parse,
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
                statement.start_date.strftime("%Y-%m-%d") if statement.start_date else "?",
                statement.end_date.strftime("%Y-%m-%d") if statement.end_date else "?",
                len(statement.lines),
                statement.start_balance,
                statement.end_balance,
            )
        else:
            logger.warning(
                "No transactions found for account=%r in %s", self.account_filter, self.filename,
            )

        return statement

    def _matches_account(self, product: str) -> bool:
        return product.lower() == self.account_filter.lower()

    def _parse_row(self, row: List[str], row_num: int) -> Optional[StatementLine]:
        sl = StatementLine()

        # Use completed date as the transaction date
        date_str = row[_COL_COMPLETED].strip()
        try:
            sl.date = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            logger.warning(
                "Row %d: cannot parse date %r, skipping", row_num, date_str,
            )
            return None

        sl.memo = row[_COL_DESCRIPTION].strip()

        amount_str = row[_COL_AMOUNT].strip()
        fee_str = row[_COL_FEE].strip()
        try:
            sl.amount = Decimal(amount_str)
            fee = Decimal(fee_str)
            if fee:
                sl.amount -= fee
                logger.debug(
                    "Row %d: amount=%s fee=%s → net=%s (%s)",
                    row_num, amount_str, fee_str, sl.amount, sl.memo,
                )
        except Exception:
            logger.warning(
                "Row %d: cannot parse amount=%r fee=%r, skipping",
                row_num, amount_str, fee_str,
            )
            return None

        # Track balances for start/end extraction
        balance_str = row[_COL_BALANCE].strip()
        try:
            balance = Decimal(balance_str)
            if self._first_balance is None:
                self._first_balance = balance
            self._last_balance = balance
        except Exception:
            pass

        sl.id = _make_id(sl.date, sl.amount, sl.memo)

        # Determine transaction type
        txn_type = row[_COL_TYPE].strip()
        ttype = _match_csv_txn_type(txn_type, sl.memo)
        if ttype:
            sl.trntype = ttype
        elif sl.amount > 0:
            sl.trntype = "CREDIT"
            logger.debug(
                "Row %d: unknown CSV type %r for %r — falling back to CREDIT",
                row_num, txn_type, sl.memo,
            )
        else:
            sl.trntype = "DEBIT"
            logger.debug(
                "Row %d: unknown CSV type %r for %r — falling back to DEBIT",
                row_num, txn_type, sl.memo,
            )

        return sl
