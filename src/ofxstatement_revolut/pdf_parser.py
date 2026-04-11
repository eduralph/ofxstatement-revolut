import hashlib
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import pdfplumber

from ofxstatement.parser import AbstractStatementParser
from ofxstatement.statement import Statement, StatementLine

logger = logging.getLogger(__name__)

# ── X-coordinate thresholds for column detection ─────────────────────────────
# Derived from PDF layout analysis of Revolut EUR statements.
# Words with x0 < _DESC_X land in the Date column, etc.
_DESC_X = 120
_MONEY_OUT_X = 300
_MONEY_IN_X = 400
_BALANCE_X = 500

# ── Regex patterns ───────────────────────────────────────────────────────────

_SECTION_RE = re.compile(
    r"^(?:(\w+)'s )?"
    r"(Account|account|Deposit) transactions from "
    r"(\w+ \d{1,2}, \d{4}) to (\w+ \d{1,2}, \d{4})$"
)

_DATE_RE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$")
_HEADER_RE = re.compile(r"^Date\s+Description\s+Money out\s+Money in\s+Balance$")
_IBAN_RE = re.compile(r"IBAN\s+([A-Z]{2}\d{2}[A-Z0-9]+)")
_CURRENCY_RE = re.compile(r"^(\w+) Statement$")

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
    ("Transfer to", "XFER"),            # ★ outgoing transfer to person/account
    ("Transfer from", "XFER"),          # ★ incoming transfer from person/account
    # ── Incoming payments ─────────────────────────────────────────────────
    ("Payment from", "DEP"),            # ★ incoming payment (SEPA credit transfer)
    # ── Interest ──────────────────────────────────────────────────────────
    ("Net Interest Paid", "INT"),       # ★ daily interest on savings/deposit
    ("Withheld Tax Refund", "INT"),     # ★ tax refund on interest (Freistellungsauftrag)
    ("Interest earned", "INT"),         # ○ alternate interest label
    # ── Fees and charges ──────────────────────────────────────────────────
    ("Premium plan fee", "FEE"),        # ★ monthly subscription fee
    ("Plus plan fee", "FEE"),           # ★ monthly subscription fee (Plus tier)
    # ── Currency exchange ─────────────────────────────────────────────────
    ("Exchanged to", "XFER"),           # ★ currency exchange (outgoing leg)
    ("Exchanged from", "XFER"),         # ○ currency exchange (incoming leg)
    # ── Internal savings moves ────────────────────────────────────────────
    ("To EUR", "XFER"),                 # ★ move to savings vault / pocket
    ("From EUR", "XFER"),              # ★ move from savings vault / pocket
    # ── Pockets ───────────────────────────────────────────────────────────
    ("To pocket", "XFER"),              # ★ move money into a pocket
    ("Pocket Withdrawal", "XFER"),      # ★ withdraw money from a pocket
    # ── ATM ───────────────────────────────────────────────────────────────
    ("Cash withdrawal at", "ATM"),      # ★ ATM cash withdrawal
    # ── Top-ups ───────────────────────────────────────────────────────────
    ("Top-up by", "DEP"),               # ★ incoming top-up
    # ── Plan refunds ─────────────────────────────────────────────────────
    ("Plan termination refund", "FEE"), # ★ refund of a cancelled plan
]


def _parse_amount(text: str) -> Decimal:
    cleaned = text.replace("€", "").replace(",", "").strip()
    return Decimal(cleaned)


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
    # Word-boundary check avoids "Coffee" matching "fee"
    if re.search(r"\bfee\b", description, re.IGNORECASE):
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

    def parse(self) -> Statement:
        logger.info("Parsing PDF %s", self.filename)
        statement = Statement()
        statement.currency = self.currency
        statement.bank_id = "Revolut"

        raw_transactions = self._extract_all_transactions()
        filtered = self._filter_transactions(raw_transactions)
        logger.info(
            "Extracted %d total transactions, %d match account=%r",
            len(raw_transactions), len(filtered), self.account_filter,
        )

        for raw in filtered:
            sl = self._to_statement_line(raw)
            statement.lines.append(sl)

        statement.account_id = self.account_id

        if statement.lines:
            statement.start_date = statement.lines[0].date
            statement.end_date = statement.lines[-1].date
            # Derive balances from first and last transaction
            if filtered[0].balance:
                first_amount = (
                    -_parse_amount(filtered[0].money_out)
                    if filtered[0].money_out and filtered[0].money_out.startswith("€")
                    else _parse_amount(filtered[0].money_in)
                    if filtered[0].money_in and filtered[0].money_in.startswith("€")
                    else Decimal("0")
                )
                first_balance = _parse_amount(filtered[0].balance)
                statement.start_balance = first_balance - first_amount
            if filtered[-1].balance:
                statement.end_balance = _parse_amount(filtered[-1].balance)
            logger.info(
                "Statement: %s to %s, %d lines, start_balance=%s, end_balance=%s",
                statement.start_date.strftime("%Y-%m-%d") if statement.start_date else "?",
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
                t for t in raw_transactions
                if t.section.lower() == section_type.lower() and t.owner is None
            ]

        # Sub-account: match owner name case-insensitively, include all sections
        return [
            t for t in raw_transactions
            if t.owner is not None and t.owner.lower() == acct
        ]

    def _extract_all_transactions(self) -> list:
        """Parse every transaction from the PDF across all pages and sections."""
        transactions: list = []
        current_section: Optional[str] = None
        current_owner: Optional[str] = None
        current_txn: Optional[_RawTransaction] = None
        in_table = False

        with pdfplumber.open(self.filename) as pdf:
            n_pages = len(pdf.pages)
            first_page_text = pdf.pages[0].extract_text() or ""
            self._extract_header_info(first_page_text)
            logger.info("PDF: %d page(s), currency=%s, account_id=%s",
                        n_pages, self.currency, self.account_id or "(not set)")

            for page_num, page in enumerate(pdf.pages, 1):
                words = page.extract_words(keep_blank_chars=True)
                word_lines = self._group_words_by_line(words)
                logger.debug("  Page %d/%d: %d word-lines", page_num, n_pages, len(word_lines))

                for y, line_words in sorted(word_lines.items()):
                    line_text = " ".join(
                        w["text"] for w in sorted(line_words, key=lambda w: w["x0"])
                    )

                    # Check for section header
                    m = _SECTION_RE.match(line_text.strip())
                    if m:
                        if current_txn:
                            transactions.append(current_txn)
                            current_txn = None
                        current_owner = m.group(1)
                        current_section = m.group(2)
                        in_table = False
                        logger.debug(
                            "  Page %d: section=%r owner=%r (%s to %s)",
                            page_num, current_section, current_owner,
                            m.group(3), m.group(4),
                        )
                        continue

                    # Check for table header
                    if _HEADER_RE.match(line_text.strip()):
                        in_table = True
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
                        if x < _DESC_X:
                            date_words.append(w["text"])
                        elif x < _MONEY_OUT_X:
                            desc_words.append(w["text"])
                        elif x < _MONEY_IN_X:
                            money_out_words.append(w["text"])
                        elif x < _BALANCE_X:
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

                        # Filter non-EUR secondary amounts
                        if money_out_text and not money_out_text.startswith("€"):
                            logger.debug(
                                "  Skipping non-EUR money_out %r on %s %s",
                                money_out_text, date_text, desc_text,
                            )
                            money_out_text = None
                        if money_in_text and not money_in_text.startswith("€"):
                            logger.debug(
                                "  Skipping non-EUR money_in %r on %s %s",
                                money_in_text, date_text, desc_text,
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
                        if not re.match(r"^[\$£¥]\d", desc_text):
                            current_txn.detail_lines.append(desc_text)

        if current_txn:
            transactions.append(current_txn)

        return transactions

    def _extract_header_info(self, first_page_text: str) -> None:
        m = _IBAN_RE.search(first_page_text)
        if m and not self.account_id:
            self.account_id = m.group(1)
            logger.debug("IBAN extracted from PDF: %s", self.account_id)

        m = _CURRENCY_RE.search(first_page_text)
        if m:
            self.currency = m.group(1)
            logger.debug("Currency extracted from PDF: %s", self.currency)

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
        sl.date = datetime.strptime(raw.date_str, "%b %d, %Y")

        if raw.money_out and raw.money_out.startswith("€"):
            sl.amount = -_parse_amount(raw.money_out)
        elif raw.money_in and raw.money_in.startswith("€"):
            sl.amount = _parse_amount(raw.money_in)
        else:
            sl.amount = Decimal("0")
            logger.warning(
                "Transaction on %s has no EUR amount: %r (out=%r, in=%r)",
                raw.date_str, raw.description, raw.money_out, raw.money_in,
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
                raw.description, sl.amount,
            )
        else:
            sl.trntype = "DEBIT"
            logger.debug(
                "No type-map match for %r — falling back to DEBIT (amount=%s)",
                raw.description, sl.amount,
            )

        return sl
