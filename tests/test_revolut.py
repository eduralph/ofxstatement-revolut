import csv
import os
import tempfile
from decimal import Decimal
from typing import List, Optional, Tuple

import pytest
from fpdf import FPDF
from ofxstatement.ui import UI

from ofxstatement_revolut.csv_parser import (
    RevolutCSVFormatError,
    _parse_csv_amount,
)
from ofxstatement_revolut.pdf_parser import (
    RevolutPDFFormatError,
    _CURRENCY_RE,
    _SECTION_RE,
    _canonical_section,
    _looks_like_header_row,
    _match_txn_type,
    _parse_date,
)
from ofxstatement_revolut.plugin import RevolutPlugin


# fpdf mm coordinates that produce the pdfplumber point-coordinates the parser expects
_X_DATE = 15.2      # -> ~43pt
_X_DESC = 44.1      # -> ~125pt
_X_MONEY_OUT = 118.2 # -> ~335pt
_X_MONEY_IN = 147.1  # -> ~417pt
_X_BALANCE = 185.6   # -> ~526pt

_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _write_section(
    pdf: FPDF,
    y: float,
    header: str,
    transactions: List[Tuple[str, str, Optional[str], Optional[str], str]],
) -> float:
    """Write a section header, table header, transaction rows, and footer.

    Returns the updated y position.
    """
    pdf.set_font("dejavu", size=12)
    pdf.text(10, y, header)
    y += 8

    pdf.set_font("dejavu", size=10)
    pdf.text(_X_DATE, y, "Date")
    pdf.text(_X_DESC, y, "Description")
    pdf.text(_X_MONEY_OUT, y, "Money out")
    pdf.text(_X_MONEY_IN, y, "Money in")
    pdf.text(_X_BALANCE, y, "Balance")
    y += 6

    for date, desc, money_out, money_in, balance in transactions:
        pdf.text(_X_DATE, y, date)
        pdf.text(_X_DESC, y, desc)
        if money_out:
            pdf.text(_X_MONEY_OUT, y, money_out)
        if money_in:
            pdf.text(_X_MONEY_IN, y, money_in)
        pdf.text(_X_BALANCE, y, balance)
        y += 6

    y += 4
    pdf.text(10, y, "Report lost or stolen card")
    y += 10
    return y


# Type alias for sub-account sections: (owner, section_type, transactions)
_SubAccountSection = Tuple[
    str, str, List[Tuple[str, str, Optional[str], Optional[str], str]]
]


def _make_pdf(
    transactions: List[Tuple[str, str, Optional[str], Optional[str], str]],
    iban: str = "DE89370400440532013000",
    currency: str = "EUR",
    section: str = "Account",
    date_range: Tuple[str, str] = ("January 1, 2025", "January 31, 2025"),
    deposit_transactions: Optional[
        List[Tuple[str, str, Optional[str], Optional[str], str]]
    ] = None,
    sub_accounts: Optional[List[_SubAccountSection]] = None,
    sort_code: Optional[str] = None,
    account_number: Optional[str] = None,
) -> str:
    """Generate a minimal Revolut-style PDF and return its path.

    Each transaction is (date, description, money_out, money_in, balance).

    sub_accounts is a list of (owner, section_type, transactions) tuples.
    section_type is "account" or "Deposit" (lowercase "account" matches real
    Revolut PDFs for sub-accounts).  The header is rendered as:
        "Alice's account transactions from ..."
    """
    pdf = FPDF()
    pdf.add_font("dejavu", "", _FONT_PATH)
    pdf.set_font("dejavu", size=10)

    pdf.add_page()
    y = 15.0

    # Header
    pdf.set_font("dejavu", size=16)
    pdf.text(140, y, f"{currency} Statement")
    y += 8
    pdf.set_font("dejavu", size=10)
    pdf.text(10, y, "JANE DOE")
    y += 6
    if iban:
        pdf.text(10, y, f"IBAN {iban}")
        y += 4
        pdf.text(10, y, "BIC TESTDE2XXXX")
        y += 4
    if sort_code and account_number:
        pdf.text(10, y, f"Sort Code {sort_code}")
        y += 4
        pdf.text(10, y, f"Account Number {account_number}")
        y += 4
    y += 6

    # Main account section
    header = f"{section} transactions from {date_range[0]} to {date_range[1]}"
    y = _write_section(pdf, y, header, transactions)

    # Optional deposit section
    if deposit_transactions:
        header = f"Deposit transactions from {date_range[0]} to {date_range[1]}"
        y = _write_section(pdf, y, header, deposit_transactions)

    # Optional sub-account sections
    if sub_accounts:
        for owner, section_type, sub_txns in sub_accounts:
            header = (
                f"{owner}'s {section_type} transactions from "
                f"{date_range[0]} to {date_range[1]}"
            )
            y = _write_section(pdf, y, header, sub_txns)

    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    pdf.output(path)
    return path


def _make_csv(
    rows: List[Tuple[str, str, str, str, str, str, str, str, str, str]],
) -> str:
    """Generate a minimal Revolut CSV file and return its path.

    Each row is (Type, Product, Started Date, Completed Date, Description,
                 Amount, Fee, Currency, State, Balance).
    """
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Type", "Product", "Started Date", "Completed Date",
            "Description", "Amount", "Fee", "Currency", "State", "Balance",
        ])
        for row in rows:
            writer.writerow(row)
    return path


class TestPDFParser:
    def test_parse_account_transactions(self) -> None:
        path = _make_pdf([
            ("Jan 5, 2025", "Transfer to John Smith", "€50.00", None, "€950.00"),
            ("Jan 10, 2025", "Payment from Acme Corp", None, "€200.00", "€1,150.00"),
            ("Jan 15, 2025", "Grocery Store", "€35.50", None, "€1,114.50"),
        ])
        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            assert stmt.currency == "EUR"
            assert stmt.account_id == "DE89370400440532013000"
            assert stmt.bank_id == "Revolut"
            assert len(stmt.lines) == 3

            assert stmt.lines[0].amount == Decimal("-50.00")
            assert stmt.lines[0].trntype == "XFER"
            assert stmt.lines[0].date.strftime("%Y-%m-%d") == "2025-01-05"
            assert stmt.lines[0].memo == "Transfer to John Smith"

            assert stmt.lines[1].amount == Decimal("200.00")
            assert stmt.lines[1].trntype == "DEP"
            assert stmt.lines[1].date.strftime("%Y-%m-%d") == "2025-01-10"

            assert stmt.lines[2].amount == Decimal("-35.50")
            assert stmt.lines[2].date.strftime("%Y-%m-%d") == "2025-01-15"

            # Balances: start = first_balance - first_amount = 950 - (-50) = 1000
            assert stmt.start_balance == Decimal("1000.00")
            assert stmt.end_balance == Decimal("1114.50")

            # IDs should be stable SHA256 hashes, not index-based
            assert len(stmt.lines[0].id) == 16
            assert all(c in "0123456789abcdef" for c in stmt.lines[0].id)
        finally:
            os.unlink(path)

    def test_stable_ids_across_runs(self) -> None:
        """Same input should produce the same IDs every time."""
        path = _make_pdf([
            ("Jan 5, 2025", "Transfer to John Smith", "€50.00", None, "€950.00"),
        ])
        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            id1 = plugin.get_parser(path).parse().lines[0].id
            id2 = plugin.get_parser(path).parse().lines[0].id
            assert id1 == id2
        finally:
            os.unlink(path)

    def test_parse_deposit_transactions(self) -> None:
        path = _make_pdf(
            transactions=[
                ("Jan 5, 2025", "Transfer to someone", "€50.00", None, "€950.00"),
            ],
            deposit_transactions=[
                ("Jan 1, 2025", "Net Interest Paid for Jan 1", None, "€0.05", "€10,000.05"),
                ("Jan 2, 2025", "Net Interest Paid for Jan 2", None, "€0.05", "€10,000.10"),
                ("Jan 3, 2025", "From EUR Savings", "€100.00", None, "€9,900.10"),
            ],
        )
        try:
            plugin = RevolutPlugin(UI(), {"account": "Deposit"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            assert len(stmt.lines) == 3
            assert stmt.lines[0].memo == "Net Interest Paid for Jan 1"
            assert stmt.lines[0].amount == Decimal("0.05")
            assert stmt.lines[0].trntype == "INT"
            assert stmt.lines[2].amount == Decimal("-100.00")
        finally:
            os.unlink(path)

    def test_account_id_from_settings_overrides_iban(self) -> None:
        path = _make_pdf([
            ("Jan 5, 2025", "Some payment", "€10.00", None, "€90.00"),
        ])
        try:
            plugin = RevolutPlugin(UI(), {"account_id": "MY_CUSTOM_ID"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()
            assert stmt.account_id == "MY_CUSTOM_ID"
        finally:
            os.unlink(path)

    def test_negative_amounts_for_outgoing(self) -> None:
        path = _make_pdf([
            ("Jan 5, 2025", "To EUR Savings", "€500.00", None, "€500.00"),
            ("Jan 10, 2025", "From EUR Savings", None, "€200.00", "€700.00"),
        ])
        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            assert stmt.lines[0].amount == Decimal("-500.00")
            assert stmt.lines[0].trntype == "XFER"
            assert stmt.lines[1].amount == Decimal("200.00")
            assert stmt.lines[1].trntype == "XFER"
        finally:
            os.unlink(path)

    def test_transaction_type_detection(self) -> None:
        path = _make_pdf([
            ("Jan 1, 2025", "Transfer to Someone", "€10.00", None, "€990.00"),
            ("Jan 2, 2025", "Payment from Someone", None, "€50.00", "€1,040.00"),
            ("Jan 3, 2025", "Premium plan fee", "€7.99", None, "€1,032.01"),
            ("Jan 4, 2025", "Exchanged to GBP", "€100.00", None, "€932.01"),
            ("Jan 5, 2025", "Coffee Shop", "€4.50", None, "€927.51"),
            ("Jan 6, 2025", "Net Interest Paid", None, "€0.05", "€927.56"),
        ])
        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            assert stmt.lines[0].trntype == "XFER"
            assert stmt.lines[1].trntype == "DEP"
            assert stmt.lines[2].trntype == "FEE"
            assert stmt.lines[3].trntype == "XFER"
            assert stmt.lines[4].trntype == "DEBIT"
            assert stmt.lines[5].trntype == "INT"
        finally:
            os.unlink(path)

    def test_detail_lines_in_memo(self) -> None:
        """Detail/continuation lines should be appended to memo."""
        pdf = FPDF()
        pdf.add_font("dejavu", "", _FONT_PATH)
        pdf.set_font("dejavu", size=10)
        pdf.add_page()

        y = 15.0
        pdf.set_font("dejavu", size=16)
        pdf.text(140, y, "EUR Statement")
        y += 8
        pdf.set_font("dejavu", size=10)
        pdf.text(10, y, "IBAN DE89370400440532013000")
        y += 10

        pdf.set_font("dejavu", size=12)
        pdf.text(10, y, "Account transactions from January 1, 2025 to January 31, 2025")
        y += 8

        pdf.set_font("dejavu", size=10)
        pdf.text(_X_DATE, y, "Date")
        pdf.text(_X_DESC, y, "Description")
        pdf.text(_X_MONEY_OUT, y, "Money out")
        pdf.text(_X_MONEY_IN, y, "Money in")
        pdf.text(_X_BALANCE, y, "Balance")
        y += 6

        # Transaction with detail lines
        pdf.text(_X_DATE, y, "Jan 5, 2025")
        pdf.text(_X_DESC, y, "Online Store")
        pdf.text(_X_MONEY_OUT, y, "€29.90")
        pdf.text(_X_BALANCE, y, "€970.10")
        y += 5
        pdf.text(_X_DESC, y, "To: Example Shop GmbH, Berlin")
        y += 5
        pdf.text(_X_DESC, y, "Card: 1234******5678")
        y += 8

        pdf.text(10, y, "Report lost or stolen card")

        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        pdf.output(path)

        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            assert len(stmt.lines) == 1
            assert "Online Store" in stmt.lines[0].memo
            assert "Example Shop GmbH" in stmt.lines[0].memo
            assert "1234******5678" in stmt.lines[0].memo
        finally:
            os.unlink(path)

    def test_start_and_end_dates(self) -> None:
        path = _make_pdf([
            ("Mar 1, 2025", "First txn", "€10.00", None, "€990.00"),
            ("Mar 15, 2025", "Middle txn", None, "€50.00", "€1,040.00"),
            ("Mar 31, 2025", "Last txn", "€5.00", None, "€1,035.00"),
        ], date_range=("March 1, 2025", "March 31, 2025"))
        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            assert stmt.start_date.strftime("%Y-%m-%d") == "2025-03-01"
            assert stmt.end_date.strftime("%Y-%m-%d") == "2025-03-31"
        finally:
            os.unlink(path)

    def test_sub_account_selection(self) -> None:
        """account="Alice" returns only Alice's transactions across all sections."""
        path = _make_pdf(
            transactions=[
                ("Jan 5, 2025", "Transfer to someone", "€50.00", None, "€950.00"),
            ],
            sub_accounts=[
                ("Alice", "account", [
                    ("Jan 3, 2025", "Transfer from Bob", None, "€100.00", "€600.00"),
                    ("Jan 7, 2025", "To pocket Vacation", "€50.00", None, "€550.00"),
                ]),
                ("Alice", "Deposit", [
                    ("Jan 2, 2025", "Net Interest Paid", None, "€0.03", "€2,000.03"),
                ]),
                ("Bob", "account", [
                    ("Jan 4, 2025", "Transfer from Alice", None, "€50.00", "€350.00"),
                ]),
            ],
        )
        try:
            plugin = RevolutPlugin(UI(), {"account": "Alice"})
            stmt = plugin.get_parser(path).parse()

            # Alice has 2 account + 1 deposit = 3 transactions
            assert len(stmt.lines) == 3
            assert stmt.lines[0].memo == "Transfer from Bob"
            assert stmt.lines[0].trntype == "XFER"
            assert stmt.lines[1].memo == "To pocket Vacation"
            assert stmt.lines[1].trntype == "XFER"
            assert stmt.lines[2].memo == "Net Interest Paid"
            assert stmt.lines[2].trntype == "INT"
        finally:
            os.unlink(path)

    def test_sub_account_excluded_from_current(self) -> None:
        """account="Current" must not include sub-account transactions."""
        path = _make_pdf(
            transactions=[
                ("Jan 5, 2025", "Main account txn", "€50.00", None, "€950.00"),
            ],
            sub_accounts=[
                ("Alice", "account", [
                    ("Jan 3, 2025", "Alice txn", None, "€100.00", "€600.00"),
                ]),
            ],
        )
        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            stmt = plugin.get_parser(path).parse()

            assert len(stmt.lines) == 1
            assert stmt.lines[0].memo == "Main account txn"
        finally:
            os.unlink(path)

    def test_pocket_transaction_types(self) -> None:
        """Pocket transactions should be classified as XFER."""
        path = _make_pdf(
            transactions=[
                ("Jan 1, 2025", "To pocket Holiday", "€200.00", None, "€800.00"),
                ("Jan 2, 2025", "Pocket Withdrawal Holiday", None, "€100.00", "€900.00"),
            ],
        )
        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            stmt = plugin.get_parser(path).parse()

            assert stmt.lines[0].trntype == "XFER"
            assert stmt.lines[1].trntype == "XFER"
        finally:
            os.unlink(path)

    def test_usd_statement(self) -> None:
        """USD statements use $ as amount prefix."""
        path = _make_pdf(
            currency="USD",
            transactions=[
                ("Jan 5, 2025", "OnlyFans", "$8.93", None, "$100.00"),
                ("Jan 10, 2025", "Exchanged to USD", None, "$150.00", "$250.00"),
            ],
        )
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert stmt.currency == "USD"
            assert len(stmt.lines) == 2
            assert stmt.lines[0].amount == Decimal("-8.93")
            assert stmt.lines[1].amount == Decimal("150.00")
            assert stmt.lines[1].trntype == "XFER"
        finally:
            os.unlink(path)

    def test_gbp_statement_with_sort_code(self) -> None:
        """GBP statements use £ prefix and Sort Code + Account Number as ID."""
        path = _make_pdf(
            currency="GBP",
            iban="",
            sort_code="042909",
            account_number="78370523",
            transactions=[
                ("Jan 23, 2025", "NOW TV", "£34.99", None, "£165.01"),
                ("Jan 26, 2025", "SWIFT Transfer to Buckles", "£100.00", None, "£65.01"),
            ],
        )
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert stmt.currency == "GBP"
            assert stmt.account_id == "GB-042909-78370523"
            assert len(stmt.lines) == 2
            assert stmt.lines[0].amount == Decimal("-34.99")
            assert stmt.lines[1].trntype == "XFER"  # SWIFT Transfer to → XFER
        finally:
            os.unlink(path)

    def test_try_suffix_currency(self) -> None:
        """Turkish Lira uses 'TRY' as trailing code, not a prefix symbol."""
        path = _make_pdf(
            currency="TRY",
            transactions=[
                ("Dec 1, 2025", "Exchanged to TRY", None, "2,452.59 TRY", "2,452.59 TRY"),
                ("Dec 27, 2025", "Yasarlar Iletisim", "695.00 TRY", None, "1,757.59 TRY"),
            ],
        )
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert stmt.currency == "TRY"
            assert len(stmt.lines) == 2
            assert stmt.lines[0].amount == Decimal("2452.59")
            assert stmt.lines[1].amount == Decimal("-695.00")
        finally:
            os.unlink(path)

    def test_column_thresholds_calibrated_from_header(self) -> None:
        """Parser must adapt to PDFs whose columns are offset from defaults.

        Shifts every column by +40 mm so the date/desc/amount words land
        well outside the hardcoded default thresholds. Without runtime
        calibration from the header row, parsing would misclassify every
        word.
        """
        x_offset = 40.0
        pdf = FPDF()
        pdf.add_font("dejavu", "", _FONT_PATH)
        pdf.set_font("dejavu", size=10)
        pdf.add_page()
        y = 15.0
        pdf.set_font("dejavu", size=16)
        pdf.text(140, y, "EUR Statement")
        y += 8
        pdf.set_font("dejavu", size=10)
        pdf.text(10, y, "JANE DOE")
        y += 6
        pdf.text(10, y, "IBAN DE89370400440532013000")
        y += 10

        pdf.set_font("dejavu", size=12)
        pdf.text(10, y, "Account transactions from January 1, 2025 to January 31, 2025")
        y += 8

        pdf.set_font("dejavu", size=10)
        pdf.text(_X_DATE + x_offset, y, "Date")
        pdf.text(_X_DESC + x_offset, y, "Description")
        pdf.text(_X_MONEY_OUT + x_offset, y, "Money out")
        pdf.text(_X_MONEY_IN + x_offset, y, "Money in")
        pdf.text(_X_BALANCE + x_offset, y, "Balance")
        y += 6

        pdf.text(_X_DATE + x_offset, y, "Jan 5, 2025")
        pdf.text(_X_DESC + x_offset, y, "Transfer to John Smith")
        pdf.text(_X_MONEY_OUT + x_offset, y, "€50.00")
        pdf.text(_X_BALANCE + x_offset, y, "€950.00")
        y += 10
        pdf.text(10, y, "Report lost or stolen card")

        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        pdf.output(path)

        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            # Calibrated thresholds must have shifted well past the defaults
            # (a +40mm shift is ~113pt; defaults are 120/300/400/500).
            assert parser._desc_x > 200
            assert parser._money_out_x > 320
            assert parser._money_in_x > 450
            assert parser._balance_x > 550

            assert len(stmt.lines) == 1
            assert stmt.lines[0].amount == Decimal("-50.00")
            assert stmt.lines[0].memo == "Transfer to John Smith"
        finally:
            os.unlink(path)

    def test_header_row_fuzzy_match_renamed_columns(self) -> None:
        """A renamed column (e.g. Withdrawals/Deposits) must still be parsed."""
        pdf = FPDF()
        pdf.add_font("dejavu", "", _FONT_PATH)
        pdf.add_page()
        y = 15.0
        pdf.set_font("dejavu", size=16)
        pdf.text(140, y, "EUR Statement")
        y += 8
        pdf.set_font("dejavu", size=10)
        pdf.text(10, y, "IBAN DE89370400440532013000")
        y += 10

        pdf.set_font("dejavu", size=12)
        pdf.text(10, y, "Account transactions from January 1, 2025 to January 31, 2025")
        y += 8

        pdf.set_font("dejavu", size=10)
        pdf.text(_X_DATE, y, "Date")
        pdf.text(_X_DESC, y, "Details")
        pdf.text(_X_MONEY_OUT, y, "Withdrawals")
        pdf.text(_X_MONEY_IN, y, "Deposits")
        pdf.text(_X_BALANCE, y, "Balance")
        y += 6
        pdf.text(_X_DATE, y, "Jan 5, 2025")
        pdf.text(_X_DESC, y, "Transfer to John Smith")
        pdf.text(_X_MONEY_OUT, y, "€50.00")
        pdf.text(_X_BALANCE, y, "€950.00")
        y += 10
        pdf.text(10, y, "Report lost or stolen card")

        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        pdf.output(path)
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert len(stmt.lines) == 1
            assert stmt.lines[0].amount == Decimal("-50.00")
            assert stmt.lines[0].memo == "Transfer to John Smith"
        finally:
            os.unlink(path)

    def test_alternate_date_format_is_parsed(self) -> None:
        """`15 Jan 2025` must be parsed as a transaction date."""
        path = _make_pdf(
            transactions=[
                ("15 Jan 2025", "Transfer to John", "€50.00", None, "€950.00"),
                ("20 Jan 2025", "Payment from Acme", None, "€100.00", "€1,050.00"),
            ],
            date_range=("15 Jan 2025", "31 Jan 2025"),
        )
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert len(stmt.lines) == 2
            assert stmt.lines[0].date.strftime("%Y-%m-%d") == "2025-01-15"
            assert stmt.lines[1].date.strftime("%Y-%m-%d") == "2025-01-20"
        finally:
            os.unlink(path)

    def test_unrecognised_pdf_raises_format_error(self) -> None:
        """A PDF with no section headers and no table must fail loudly."""
        pdf = FPDF()
        pdf.add_font("dejavu", "", _FONT_PATH)
        pdf.add_page()
        pdf.set_font("dejavu", size=12)
        pdf.text(20, 30, "This is definitely not a Revolut statement.")
        pdf.text(20, 40, "Nothing to see here.")
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        pdf.output(path)
        try:
            parser = RevolutPlugin(UI(), {}).get_parser(path)
            with pytest.raises(RevolutPDFFormatError, match="layout may have changed"):
                parser.parse()
        finally:
            os.unlink(path)

    def test_calibration_rejects_non_monotonic_thresholds(self) -> None:
        """If calibration produces out-of-order thresholds, defaults are kept."""
        # Build a fake header row where "Balance" appears before the Money
        # words — calibration must detect this and refuse to commit.
        plugin = RevolutPlugin(UI(), {})
        parser = plugin.get_parser("ignored.pdf")
        before = (
            parser._desc_x, parser._money_out_x,
            parser._money_in_x, parser._balance_x,
        )
        bogus_words = [
            {"text": "Date", "x0": 40.0},
            {"text": "Description", "x0": 125.0},
            {"text": "Balance", "x0": 200.0},   # out of order
            {"text": "Money out", "x0": 335.0},
            {"text": "Money in", "x0": 417.0},
        ]
        parser._calibrate_from_header(bogus_words)
        after = (
            parser._desc_x, parser._money_out_x,
            parser._money_in_x, parser._balance_x,
        )
        assert before == after

    # ── Multi-language resilience ──────────────────────────────────────────

    @pytest.mark.parametrize("date_str,year,month,day", [
        ("Jan 15, 2025", 2025, 1, 15),       # English
        ("15 January 2025", 2025, 1, 15),    # English, alternate order
        ("15 Januar 2025", 2025, 1, 15),     # German
        ("15 janvier 2025", 2025, 1, 15),    # French
        ("15 enero 2025", 2025, 1, 15),      # Spanish
        ("15 gennaio 2025", 2025, 1, 15),    # Italian
        ("15 janeiro 2025", 2025, 1, 15),    # Portuguese
        ("15 januari 2025", 2025, 1, 15),    # Dutch
        ("15 stycznia 2025", 2025, 1, 15),   # Polish (genitive — standard form)
        ("15 stycznia 2025", 2025, 1, 15),   # dup guard
        ("15 sierpnia 2025", 2025, 8, 15),   # Polish genitive
        ("15 grudnia 2025", 2025, 12, 15),   # Polish genitive
        ("15 März 2025", 2025, 3, 15),       # German (umlaut)
        ("15 août 2025", 2025, 8, 15),       # French (circumflex)
        ("15 października 2025", 2025, 10, 15),  # Polish (diacritics)
        ("2025-01-15", 2025, 1, 15),         # ISO
        ("15/01/2025", 2025, 1, 15),         # Numeric
    ])
    def test_parse_date_multilingual(
        self, date_str: str, year: int, month: int, day: int,
    ) -> None:
        """Month-name dates must parse across supported languages."""
        d = _parse_date(date_str)
        assert (d.year, d.month, d.day) == (year, month, day)

    @pytest.mark.parametrize("line", [
        "Date Description Money out Money in Balance",                   # EN
        "Datum Beschreibung Ausgehend Eingehend Saldo",                  # DE
        "Date Description Débit Crédit Solde",                           # FR
        "Fecha Descripción Débito Crédito Saldo",                        # ES
        "Data Descrizione Uscite Entrate Saldo",                         # IT
        "Data Descrição Saída Entrada Saldo",                            # PT
        "Datum Omschrijving Uit In Saldo",                               # NL
        "Data Opis Wypłaty Wpłaty Saldo",                                # PL
    ])
    def test_looks_like_header_row_multilingual(self, line: str) -> None:
        """Column-header detection must accept all supported languages."""
        assert _looks_like_header_row(line), line

    @pytest.mark.parametrize("line,currency", [
        ("EUR Statement", "EUR"),              # EN
        ("USD Kontoauszug", "USD"),            # DE
        ("GBP Relevé", "GBP"),                 # FR
        ("USD Extracto", "USD"),               # ES
        ("EUR Estratto conto", "EUR"),         # IT (multi-word)
        ("EUR Extrato", "EUR"),                # PT
        ("EUR Afschrift", "EUR"),              # NL
        ("PLN Wyciąg", "PLN"),                 # PL
    ])
    def test_currency_detection_multilingual(
        self, line: str, currency: str,
    ) -> None:
        m = _CURRENCY_RE.search(line)
        assert m and m.group(1) == currency

    def test_currency_detection_rejects_non_statement_line(self) -> None:
        """A 3-letter uppercase word followed by any word must NOT match."""
        # "CEO JANE" would have matched the loose `[A-Z]{3}\s+\S+` pattern;
        # the statement-word whitelist prevents that.
        assert _CURRENCY_RE.search("CEO JANE") is None
        assert _CURRENCY_RE.search("BIC TESTDE2XXXX") is None

    @pytest.mark.parametrize("header,section,owner", [
        ("Account transactions from January 1, 2025 to January 31, 2025",
         "Account", None),
        ("Alice's Account transactions from Jan 1, 2025 to Jan 31, 2025",
         "Account", "Alice"),
        ("Konto Transaktionen vom 1. Januar 2025 bis 31. Januar 2025",
         "Account", None),
        ("Compte transactions du 1 janvier 2025 au 31 janvier 2025",
         "Account", None),
        ("Cuenta movimientos del 1 enero 2025 al 31 enero 2025",
         "Account", None),
        ("Depot Umsätze vom 1. Januar 2025 bis 31. Januar 2025",
         "Deposit", None),
        ("Deposit transactions from Jan 1, 2025 to Jan 31, 2025",
         "Deposit", None),
    ])
    def test_section_regex_multilingual(
        self, header: str, section: str, owner: Optional[str],
    ) -> None:
        """Section header must be recognised across languages + connectors."""
        m = _SECTION_RE.match(header)
        assert m is not None, header
        assert m.group(1) == owner
        assert _canonical_section(m.group(2)) == section

    @pytest.mark.parametrize("memo", [
        "Monthly fee",              # EN
        "Monatliche Gebühr",        # DE
        "Frais de dossier",         # FR
        "Comisión mensual",         # ES
        "Tassa mensile",            # IT (via _FEE_WORDS "tassa")
        "Taxa de serviço",          # PT
        "Opłata miesięczna",        # PL
    ])
    def test_fee_fallback_multilingual(self, memo: str) -> None:
        """_match_txn_type must map multilingual fee keywords to FEE."""
        assert _match_txn_type(memo) == "FEE", memo

    def test_fee_fallback_does_not_match_inside_words(self) -> None:
        """Word-boundary match must not misfire on 'Coffee' → 'fee'."""
        assert _match_txn_type("Coffee shop") == ""

    def test_german_pdf_end_to_end(self) -> None:
        """Full German PDF must parse correctly: header, section, dates."""
        x_offset = 0.0
        pdf = FPDF()
        pdf.add_font("dejavu", "", _FONT_PATH)
        pdf.set_font("dejavu", size=10)
        pdf.add_page()
        y = 15.0
        pdf.set_font("dejavu", size=16)
        pdf.text(140, y, "EUR Kontoauszug")
        y += 8
        pdf.set_font("dejavu", size=10)
        pdf.text(10, y, "JANE DOE")
        y += 6
        pdf.text(10, y, "IBAN DE89370400440532013000")
        y += 10

        pdf.set_font("dejavu", size=12)
        pdf.text(10, y,
                 "Konto Umsätze vom 1. Januar 2025 bis 31. Januar 2025")
        y += 8

        pdf.set_font("dejavu", size=10)
        pdf.text(_X_DATE + x_offset, y, "Datum")
        pdf.text(_X_DESC + x_offset, y, "Beschreibung")
        pdf.text(_X_MONEY_OUT + x_offset, y, "Ausgehend")
        pdf.text(_X_MONEY_IN + x_offset, y, "Eingehend")
        pdf.text(_X_BALANCE + x_offset, y, "Saldo")
        y += 6

        pdf.text(_X_DATE + x_offset, y, "5 Januar 2025")
        pdf.text(_X_DESC + x_offset, y, "Monatliche Gebühr")
        pdf.text(_X_MONEY_OUT + x_offset, y, "€10.00")
        pdf.text(_X_BALANCE + x_offset, y, "€90.00")
        y += 10
        pdf.text(10, y, "Report lost or stolen card")

        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        pdf.output(path)
        try:
            stmt = RevolutPlugin(UI(), {"account": "Current"}).get_parser(
                path
            ).parse()
            assert stmt.currency == "EUR"
            assert stmt.account_id == "DE89370400440532013000"
            assert len(stmt.lines) == 1
            assert stmt.lines[0].amount == Decimal("-10.00")
            assert stmt.lines[0].memo == "Monatliche Gebühr"
            assert stmt.lines[0].trntype == "FEE"  # matched via _FEE_RE
            assert stmt.lines[0].date.month == 1
            assert stmt.lines[0].date.day == 5
        finally:
            os.unlink(path)

    def test_secondary_currency_amount_filtered(self) -> None:
        """An amount in a non-primary currency must be ignored."""
        # In a USD statement, an EUR amount appearing in the Money in column
        # must be filtered (it's noise from a currency-exchange entry).
        path = _make_pdf(
            currency="USD",
            transactions=[
                ("Feb 1, 2025", "Exchanged to USD", None, "€150.00", "$150.00"),
            ],
        )
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            # No primary (USD) amount present → falls back to 0
            assert stmt.lines[0].amount == Decimal("0")
        finally:
            os.unlink(path)


class TestCSVParser:
    def test_parse_csv_transactions(self) -> None:
        path = _make_csv([
            ("Transfer", "Current", "2025-01-05 10:00:00", "2025-01-05 10:00:00",
             "Transfer from Jane Doe", "200.00", "0.00", "EUR", "COMPLETED", "1200.00"),
            ("Card Payment", "Current", "2025-01-06 14:30:00", "2025-01-07 09:00:00",
             "Grocery Store", "-45.50", "0.00", "EUR", "COMPLETED", "1154.50"),
            ("Transfer", "Current", "2025-01-08 08:00:00", "2025-01-08 08:00:00",
             "To EUR Savings", "-500.00", "0.00", "EUR", "COMPLETED", "654.50"),
        ])
        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            assert stmt.currency == "EUR"
            assert stmt.bank_id == "Revolut"
            assert len(stmt.lines) == 3

            assert stmt.lines[0].amount == Decimal("200.00")
            assert stmt.lines[0].trntype == "XFER"
            assert stmt.lines[0].date.strftime("%Y-%m-%d") == "2025-01-05"

            assert stmt.lines[1].amount == Decimal("-45.50")
            assert stmt.lines[1].trntype == "POS"

            assert stmt.lines[2].amount == Decimal("-500.00")
            assert stmt.lines[2].trntype == "XFER"

            # Balances: start = first_balance - first_amount = 1200 - 200 = 1000
            assert stmt.start_balance == Decimal("1000.00")
            assert stmt.end_balance == Decimal("654.50")

            # IDs should be stable hex hashes
            assert len(stmt.lines[0].id) == 16
            assert all(c in "0123456789abcdef" for c in stmt.lines[0].id)
        finally:
            os.unlink(path)

    def test_csv_deposit_filter(self) -> None:
        path = _make_csv([
            ("Transfer", "Current", "2025-01-05 10:00:00", "2025-01-05 10:00:00",
             "Some transfer", "100.00", "0.00", "EUR", "COMPLETED", "1100.00"),
            ("Interest", "Deposit", "2025-01-05 02:00:00", "2025-01-05 02:00:00",
             "Interest earned - Savings", "0.05", "0.01", "EUR", "COMPLETED", "5000.04"),
            ("Transfer", "Deposit", "2025-01-06 10:00:00", "2025-01-06 10:00:00",
             "To EUR Savings", "300.00", "0.00", "EUR", "COMPLETED", "5300.04"),
        ])
        try:
            plugin = RevolutPlugin(UI(), {"account": "Deposit"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            assert len(stmt.lines) == 2
            assert stmt.lines[0].memo == "Interest earned - Savings"
            assert stmt.lines[0].trntype == "INT"
        finally:
            os.unlink(path)

    def test_csv_fee_subtracted(self) -> None:
        path = _make_csv([
            ("Charge", "Current", "2025-01-09 02:00:00", "2025-01-09 02:00:00",
             "Premium plan fee", "0.00", "7.99", "EUR", "COMPLETED", "992.01"),
        ])
        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            assert len(stmt.lines) == 1
            assert stmt.lines[0].amount == Decimal("-7.99")
            assert stmt.lines[0].trntype == "FEE"
        finally:
            os.unlink(path)

    def test_csv_skips_non_completed(self) -> None:
        path = _make_csv([
            ("Transfer", "Current", "2025-01-05 10:00:00", "2025-01-05 10:00:00",
             "Completed one", "100.00", "0.00", "EUR", "COMPLETED", "1100.00"),
            ("Transfer", "Current", "2025-01-06 10:00:00", "",
             "Pending one", "50.00", "0.00", "EUR", "PENDING", "1150.00"),
            ("Transfer", "Current", "2025-01-07 10:00:00", "",
             "Reverted one", "-50.00", "0.00", "EUR", "REVERTED", "1100.00"),
        ])
        try:
            plugin = RevolutPlugin(UI(), {"account": "Current"})
            parser = plugin.get_parser(path)
            stmt = parser.parse()

            assert len(stmt.lines) == 1
            assert stmt.lines[0].memo == "Completed one"
        finally:
            os.unlink(path)

    def test_csv_auto_detects_non_eur_currency(self) -> None:
        """statement.currency must reflect the CSV's Currency column."""
        path = _make_csv([
            ("Card Payment", "Current", "2025-01-23 10:30:10", "2025-01-23 19:56:30",
             "NOW TV", "-34.99", "0.00", "GBP", "COMPLETED", "166.00"),
            ("Card Payment", "Current", "2025-02-01 08:00:00", "2025-02-01 08:00:00",
             "Shop", "-12.50", "0.00", "GBP", "COMPLETED", "153.50"),
        ])
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert stmt.currency == "GBP"
            assert len(stmt.lines) == 2
        finally:
            os.unlink(path)

    def test_csv_filters_mixed_currency_rows(self) -> None:
        """Dominant-currency detection must drop stray rows in other currencies."""
        path = _make_csv([
            ("Card Payment", "Current", "2025-01-01 10:00:00", "2025-01-01 10:00:00",
             "A", "-10.00", "0.00", "EUR", "COMPLETED", "90.00"),
            ("Card Payment", "Current", "2025-01-02 10:00:00", "2025-01-02 10:00:00",
             "B", "-5.00", "0.00", "EUR", "COMPLETED", "85.00"),
            ("Exchange", "Current", "2025-01-03 10:00:00", "2025-01-03 10:00:00",
             "Exchanged to USD", "8.50", "0.00", "USD", "COMPLETED", "8.50"),
        ])
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert stmt.currency == "EUR"
            assert len(stmt.lines) == 2
            assert all(sl.memo in ("A", "B") for sl in stmt.lines)
        finally:
            os.unlink(path)

    def test_csv_tolerates_reordered_columns(self) -> None:
        """Columns resolved by name — order in the file should not matter."""
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            # Reordered header + a trailing unknown column.
            w.writerow([
                "Balance", "State", "Currency", "Fee", "Amount",
                "Description", "Completed Date", "Started Date",
                "Product", "Type", "Extra",
            ])
            w.writerow([
                "90.00", "COMPLETED", "EUR", "0.00", "-10.00",
                "Coffee", "2025-01-01 10:00:00", "2025-01-01 10:00:00",
                "Current", "Card Payment", "ignored",
            ])
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert stmt.currency == "EUR"
            assert len(stmt.lines) == 1
            assert stmt.lines[0].amount == Decimal("-10.00")
            assert stmt.lines[0].trntype == "POS"
            assert stmt.end_balance == Decimal("90.00")
        finally:
            os.unlink(path)

    def test_csv_missing_required_column_raises(self) -> None:
        """Dropping a required column must fail loudly, not silently."""
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            # Missing "Amount" column.
            w.writerow([
                "Type", "Product", "Started Date", "Completed Date",
                "Description", "Fee", "Currency", "State", "Balance",
            ])
            w.writerow([
                "Card Payment", "Current", "2025-01-01 10:00:00",
                "2025-01-01 10:00:00", "Coffee",
                "0.00", "EUR", "COMPLETED", "90.00",
            ])
        try:
            parser = RevolutPlugin(UI(), {}).get_parser(path)
            with pytest.raises(RevolutCSVFormatError, match="amount"):
                parser.parse()
        finally:
            os.unlink(path)

    def test_csv_optional_column_missing_is_tolerated(self) -> None:
        """A CSV without Fee/Balance/Currency still parses (they are optional)."""
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "Type", "Product", "Started Date", "Completed Date",
                "Description", "Amount", "State",
            ])
            w.writerow([
                "Card Payment", "Current", "2025-01-01 10:00:00",
                "2025-01-01 10:00:00", "Coffee", "-10.00", "COMPLETED",
            ])
        try:
            # currency setting survives since the CSV has no Currency column
            stmt = RevolutPlugin(UI(), {"currency": "EUR"}).get_parser(path).parse()
            assert stmt.currency == "EUR"
            assert len(stmt.lines) == 1
            assert stmt.lines[0].amount == Decimal("-10.00")
        finally:
            os.unlink(path)

    def test_csv_accepts_alternate_date_formats(self) -> None:
        """ISO-8601 and date-only timestamps must parse."""
        path = _make_csv([
            ("Card Payment", "Current", "2025-01-01T10:30:00", "2025-01-01T10:30:00",
             "ISO row", "-10.00", "0.00", "EUR", "COMPLETED", "90.00"),
            ("Card Payment", "Current", "2025-01-02", "2025-01-02",
             "Date only row", "-5.00", "0.00", "EUR", "COMPLETED", "85.00"),
        ])
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert len(stmt.lines) == 2
            assert stmt.lines[0].date.strftime("%Y-%m-%d") == "2025-01-01"
            assert stmt.lines[1].date.strftime("%Y-%m-%d") == "2025-01-02"
        finally:
            os.unlink(path)

    def test_csv_accepts_state_synonyms_case_insensitive(self) -> None:
        """`Completed` / `SETTLED` / `POSTED` must be treated as successful."""
        path = _make_csv([
            ("Card Payment", "Current", "2025-01-01 10:00:00", "2025-01-01 10:00:00",
             "lowercase state", "-10.00", "0.00", "EUR", "Completed", "90.00"),
            ("Card Payment", "Current", "2025-01-02 10:00:00", "2025-01-02 10:00:00",
             "settled state", "-5.00", "0.00", "EUR", "SETTLED", "85.00"),
            ("Card Payment", "Current", "2025-01-03 10:00:00", "2025-01-03 10:00:00",
             "pending row", "-3.00", "0.00", "EUR", "PENDING", "82.00"),
        ])
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert len(stmt.lines) == 2
            assert {sl.memo for sl in stmt.lines} == {"lowercase state", "settled state"}
        finally:
            os.unlink(path)

    def test_csv_resolves_case_insensitive_headers_with_bom(self) -> None:
        """BOM + lowercased headers must resolve to the same logical fields."""
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        # Write a UTF-8 BOM manually and lowercase every header name.
        with open(path, "wb") as f:
            f.write(b"\xef\xbb\xbf")
            f.write(
                b"type,product,started date,completed date,description,"
                b"amount,fee,currency,state,balance\n"
            )
            f.write(
                b"Card Payment,Current,2025-01-01 10:00:00,2025-01-01 10:00:00,"
                b"Coffee,-10.00,0.00,EUR,COMPLETED,90.00\n"
            )
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert len(stmt.lines) == 1
            assert stmt.lines[0].amount == Decimal("-10.00")
            assert stmt.currency == "EUR"
        finally:
            os.unlink(path)

    def test_csv_diagnostic_log_when_nothing_matches(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """0 kept rows from a non-empty CSV must log products/states seen."""
        path = _make_csv([
            # Neither row passes: product is "Savings" (not the Current default).
            ("Card Payment", "Savings", "2025-01-01 10:00:00", "2025-01-01 10:00:00",
             "A", "-10.00", "0.00", "EUR", "COMPLETED", "90.00"),
            ("Card Payment", "Savings", "2025-01-02 10:00:00", "2025-01-02 10:00:00",
             "B", "-5.00", "0.00", "EUR", "PENDING", "85.00"),
        ])
        try:
            import logging as _logging
            with caplog.at_level(_logging.WARNING):
                stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert len(stmt.lines) == 0
            msg = caplog.text
            assert "Savings" in msg
            assert "COMPLETED" in msg
            assert "PENDING" in msg
        finally:
            os.unlink(path)

    def test_csv_amount_accepts_european_locale(self) -> None:
        """A localized CSV with `.` thousands and `,` decimals must parse."""
        path = _make_csv([
            ("Card Payment", "Current", "2025-01-01 10:00:00",
             "2025-01-01 10:00:00", "Rent", "-1.234,56", "0,00",
             "EUR", "COMPLETED", "8.765,44"),
            ("Topup", "Current", "2025-01-02 10:00:00",
             "2025-01-02 10:00:00", "Salary", "2.000,00", "",
             "EUR", "COMPLETED", "10.765,44"),
        ])
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert len(stmt.lines) == 2
            assert stmt.lines[0].amount == Decimal("-1234.56")
            assert stmt.lines[1].amount == Decimal("2000.00")
            assert stmt.end_balance == Decimal("10765.44")
        finally:
            os.unlink(path)

    def test_parse_csv_amount_handles_both_separators(self) -> None:
        """Unit test: rightmost of `.` or `,` wins as decimal separator."""
        # English locale: `,` thousands, `.` decimal
        assert _parse_csv_amount("1,234.56") == Decimal("1234.56")
        assert _parse_csv_amount("-1,234.56") == Decimal("-1234.56")
        # European locale: `.` thousands, `,` decimal
        assert _parse_csv_amount("1.234,56") == Decimal("1234.56")
        assert _parse_csv_amount("-1.234,56") == Decimal("-1234.56")
        # Single separator + 1-2 trailing digits -> decimal point
        assert _parse_csv_amount("10.00") == Decimal("10.00")
        assert _parse_csv_amount("10,00") == Decimal("10.00")
        assert _parse_csv_amount("1,5") == Decimal("1.5")
        # Thousands-only (no decimal)
        assert _parse_csv_amount("1,234") == Decimal("1234")
        assert _parse_csv_amount("1.234") == Decimal("1234")
        assert _parse_csv_amount("1,234,567") == Decimal("1234567")
        # Plain integers / negatives / whitespace
        assert _parse_csv_amount("0") == Decimal("0")
        assert _parse_csv_amount("  -42  ") == Decimal("-42")

    def test_csv_header_resolves_localized_aliases(self) -> None:
        """CSV with German column headers must resolve via alias table."""
        path = tempfile.mktemp(suffix=".csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "Typ", "Produkt", "Startdatum", "Buchungsdatum",
                "Beschreibung", "Betrag", "Gebühr", "Währung", "Status", "Saldo",
            ])
            w.writerow([
                "Card Payment", "Current", "2025-01-01 10:00:00",
                "2025-01-01 10:00:00", "Kaffee", "-3.50", "0",
                "EUR", "COMPLETED", "96.50",
            ])
        try:
            stmt = RevolutPlugin(UI(), {}).get_parser(path).parse()
            assert len(stmt.lines) == 1
            assert stmt.lines[0].amount == Decimal("-3.50")
            assert stmt.currency == "EUR"
        finally:
            os.unlink(path)

    def test_csv_file_auto_detection(self) -> None:
        plugin = RevolutPlugin(UI(), {})
        csv_parser = plugin.get_parser("test.csv")
        pdf_parser = plugin.get_parser("test.pdf")

        assert type(csv_parser).__name__ == "RevolutCSVParser"
        assert type(pdf_parser).__name__ == "RevolutPDFParser"
