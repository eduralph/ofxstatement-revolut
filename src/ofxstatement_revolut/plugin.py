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
