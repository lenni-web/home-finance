from unittest import TestCase
from decimal import Decimal

from ledger.importers.ing import INGStatementParser, TextFragment


class INGStatementParserTests(TestCase):
    def test_parses_transaction_from_fragment_order(self):
        fragments = [[
            TextFragment(1, 70.8, 600, "01.07.2026"),
            TextFragment(1, 0, 0, " Lastschrift"),
            TextFragment(1, 0, 0, " Beispiel GmbH"),
            TextFragment(1, 0, 0, " -1.234,56"),
            TextFragment(1, 70.8, 588, "02.07.2026"),
            TextFragment(1, 141.6, 570, "Mandat:"),
            TextFragment(1, 0, 0, " ABC"),
        ]]

        result = INGStatementParser().parse_fragments(fragments)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].booking_date, "2026-07-01")
        self.assertEqual(result[0].value_date, "2026-07-02")
        self.assertEqual(result[0].amount, "-1234.56")
        self.assertEqual(result[0].booking_type, "Lastschrift")
        self.assertEqual(result[0].counterparty, "Beispiel GmbH")
        self.assertEqual(result[0].description, "Mandat:\nABC")

    def test_ignores_header_date_outside_left_column(self):
        fragments = [[
            TextFragment(1, 425.1, 700, "Datum"),
            TextFragment(1, 0, 0, "31.07.2026"),
        ]]

        self.assertEqual(INGStatementParser().parse_fragments(fragments), [])

    def test_does_not_append_page_header_to_last_transaction(self):
        fragments = [[
            TextFragment(1, 70.8, 200, "01.07.2026"),
            TextFragment(1, 0, 0, " Lastschrift"),
            TextFragment(1, 0, 0, " Beispiel GmbH"),
            TextFragment(1, 0, 0, " -10,00"),
            TextFragment(1, 70.8, 188, "01.07.2026"),
            TextFragment(1, 141.6, 170, "Referenz:"),
            TextFragment(1, 0, 0, " ABC"),
            TextFragment(1, 425.1, 700, "Datum"),
            TextFragment(1, 0, 0, "31.07.2026"),
        ]]

        result = INGStatementParser().parse_fragments(fragments)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].description, "Referenz:\nABC")

    def test_extracts_opening_and_last_closing_balance(self):
        pages = [[
            TextFragment(1, 311.7, 627.3, "Alter Saldo"),
            TextFragment(1, 0, 0, " 1.000,00 Euro"),
            TextFragment(1, 311.7, 615.0, "Neuer Saldo"),
            TextFragment(1, 0, 0, " 900,00 Euro"),
        ], [
            TextFragment(14, 141.6, 572.4, "Neuer Saldo"),
            TextFragment(14, 0, 0, " 875,50"),
        ]]

        opening, closing = INGStatementParser.extract_balances(pages)

        self.assertEqual(opening, Decimal("1000.00"))
        self.assertEqual(closing, Decimal("875.50"))
