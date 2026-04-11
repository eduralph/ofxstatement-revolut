from ofxstatement.plugin import Plugin
from ofxstatement.parser import AbstractStatementParser

from ofxstatement_revolut.pdf_parser import RevolutPDFParser
from ofxstatement_revolut.csv_parser import RevolutCSVParser


class RevolutPlugin(Plugin):
    """Revolut bank statement parser (PDF and CSV)"""

    def get_parser(self, filename: str) -> AbstractStatementParser:
        account = self.settings.get("account", "Current")
        currency = self.settings.get("currency", "EUR")
        account_id = self.settings.get("account_id", "")

        if filename.lower().endswith(".pdf"):
            return RevolutPDFParser(filename, account, currency, account_id)
        else:
            return RevolutCSVParser(filename, account, currency, account_id)
