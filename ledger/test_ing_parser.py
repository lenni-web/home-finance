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

    def test_parses_turnover_display_with_running_balance_column(self):
        fragments = [[
            TextFragment(1, 46, 731, "Umsatzanzeige"),
            TextFragment(1, 47, 356, "14.08.2026"),
            TextFragment(1, 47, 347, "14.08.2026"),
            TextFragment(1, 125, 356, "Lennart Barfod"),
            TextFragment(1, 125, 347, "Überweisung"),
            TextFragment(1, 125, 338, "Autoversicherung"),
            TextFragment(1, 426.5, 356, "1.700,00 €"),
            TextFragment(1, 501.5, 356, "-1.300,00 €"),
            TextFragment(1, 47, 314, "21.05.2026"),
            TextFragment(1, 47, 305, "20.05.2026"),
            TextFragment(1, 125, 314, "Julia Barfod"),
            TextFragment(1, 125, 305, "Gutschrift"),
            TextFragment(1, 297, 300, "Kontoname"),
            TextFragment(1, 426.5, 314, "3.000,00 €"),
            TextFragment(1, 499.2, 314, "+750,00 €"),
        ]]

        rows = INGStatementParser()._parse_turnover_rows(fragments)
        result = [transaction for transaction, _balance in rows]

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].booking_date, "2026-08-14")
        self.assertEqual(result[0].value_date, "2026-08-14")
        self.assertEqual(result[0].counterparty, "Lennart Barfod")
        self.assertEqual(result[0].booking_type, "Überweisung")
        self.assertEqual(result[0].description, "Autoversicherung")
        self.assertEqual(result[0].amount, "-1300.00")
        self.assertEqual(result[1].value_date, "2026-05-20")
        self.assertEqual(result[1].amount, "750.00")
        self.assertEqual(result[1].description, "")
        self.assertEqual(rows[0][1], Decimal("1700.00"))
