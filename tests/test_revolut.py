import csv
import os
import tempfile
from decimal import Decimal
from typing import List, Optional, Tuple

from fpdf import FPDF
from ofxstatement.ui import UI

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
    pdf.text(10, y, f"IBAN {iban}")
    y += 4
    pdf.text(10, y, "BIC TESTDE2XXXX")
    y += 10

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

    def test_csv_file_auto_detection(self) -> None:
        plugin = RevolutPlugin(UI(), {})
        csv_parser = plugin.get_parser("test.csv")
        pdf_parser = plugin.get_parser("test.pdf")

        assert type(csv_parser).__name__ == "RevolutCSVParser"
        assert type(pdf_parser).__name__ == "RevolutPDFParser"
