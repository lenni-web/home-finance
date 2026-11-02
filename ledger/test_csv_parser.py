import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import TestCase

from ledger.importers import CSVStatementParser, get_parser, parser_choices
from ledger.importers.csv_generic import parse_amount


def parse(content, encoding="utf-8"):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "umsaetze.csv"
        path.write_bytes(content.encode(encoding))
        return CSVStatementParser().parse_statement(path)


class AmountTests(TestCase):
    def test_german_and_english_notation(self):
        cases = {
            "1.234,56": "1234.56", "-12,30": "-12.30", "+7,5": "7.50", "1,234.56": "1234.56",
            "-1.234.567,89": "-1234567.89", "12.30": "12.30", "1.234": "1234.00",
            "12,30-": "-12.30", "-12,30 €": "-12.30", "1.234,56\xa0EUR": "1234.56",
        }
        for text, expected in cases.items():
            self.assertEqual(str(parse_amount(text)), expected, text)

    def test_non_numeric_is_none(self):
        self.assertIsNone(parse_amount(""))
        self.assertIsNone(parse_amount("Summe"))


class CSVParserTests(TestCase):
    def test_sparkasse_like_export_with_semicolons_and_cp1252(self):
        parsed = parse(
            'Auftragskonto;Buchungstag;Valutadatum;Buchungstext;Verwendungszweck;'
            'Beguenstigter/Zahlungspflichtiger;IBAN;BIC;Betrag;Waehrung\n'
            '"DE00";"15.07.26";"16.07.26";"LASTSCHRIFT";"Miete Juli";"Vermieter Müller";"DE11";"X";"-850,00";"EUR"\n'
            '"DE00";"20.07.26";"20.07.26";"GUTSCHRIFT";"Gehalt";"Arbeitgeber AG";"DE22";"Y";"2.500,00";"EUR"\n',
            encoding="cp1252",
        )

        first, second = parsed.transactions
        self.assertEqual(
            (first.booking_date, first.value_date, first.amount, first.currency),
            ("2026-07-15", "2026-07-16", "-850.00", "EUR"),
        )
        self.assertEqual((first.counterparty, first.description, first.booking_type),
                         ("Vermieter Müller", "Miete Juli", "LASTSCHRIFT"))
        self.assertEqual(second.amount, "2500.00")
        self.assertIsNone(parsed.opening_balance)

    def test_preamble_balance_column_and_reconciliation(self):
        parsed = parse(
            "Umsatzanzeige;Datei erstellt am 01.08.2026\n"
            "IBAN;DE00 0000\n\n"
            "Buchung;Valuta;Auftraggeber/Empfänger;Buchungstext;Verwendungszweck;Saldo;Währung;Betrag;Währung\n"
            "02.07.2026;02.07.2026;Shop A;Lastschrift;Kauf;900,00;EUR;-100,00;EUR\n"
            "03.07.2026;03.07.2026;Shop B;Lastschrift;Kauf;870,00;EUR;-30,00;EUR\n"
            "Summe;;;;;;;-130,00;\n"
        )

        self.assertEqual(len(parsed.transactions), 2)
        self.assertEqual(parsed.opening_balance, Decimal("1000.00"))
        self.assertEqual(parsed.closing_balance, Decimal("870.00"))
        self.assertEqual(parsed.reconciliation_difference, Decimal("0.00"))

    def test_descending_order_uses_first_row_as_closing_balance(self):
        parsed = parse(
            "Buchungstag;Betrag;Saldo;Verwendungszweck\n"
            "2026-07-03;-30,00;870,00;B\n"
            "2026-07-02;-100,00;900,00;A\n"
        )

        self.assertEqual((parsed.opening_balance, parsed.closing_balance),
                         (Decimal("1000.00"), Decimal("870.00")))

    def test_inconsistent_balance_is_reported_as_difference(self):
        parsed = parse(
            "Buchungstag;Betrag;Saldo\n02.07.2026;-100,00;900,00\n03.07.2026;-30,00;800,00\n"
        )

        self.assertEqual(parsed.reconciliation_difference, Decimal("70.00"))

    def test_debit_credit_columns_and_comma_delimiter(self):
        parsed = parse(
            "Datum,Beschreibung,Soll,Haben\n"
            "01.08.2026,Einkauf,\"12,50\",\n"
            "02.08.2026,Erstattung,,\"3,00\"\n"
        )

        self.assertEqual([item.amount for item in parsed.transactions], ["-12.50", "3.00"])

    def test_dkb_like_payer_payee_columns_and_pending_rows(self):
        parsed = parse(
            "Buchungsdatum;Wertstellung;Status;Zahlungspflichtige*r;Zahlungsempfänger*in;"
            "Verwendungszweck;Umsatztyp;IBAN;Betrag (€)\n"
            "05.09.26;05.09.26;Gebucht;Ich;Supermarkt;Einkauf;Ausgang;DE1;-23,40\n"
            "06.09.26;06.09.26;Gebucht;Arbeitgeber;Ich;Lohn;Eingang;DE2;1.000,00\n"
            "07.09.26;07.09.26;Vorgemerkt;Ich;Kiosk;Zeitung;Ausgang;DE3;-2,00\n"
        )

        self.assertEqual(
            [(item.counterparty, item.amount) for item in parsed.transactions],
            [("Supermarkt", "-23.40"), ("Arbeitgeber", "1000.00")],
        )

    def test_unrecognised_files_give_clear_errors(self):
        with self.assertRaisesRegex(ValueError, "Kopfzeile"):
            parse("Name;Wert\nA;B\n")
        with self.assertRaisesRegex(ValueError, "keine Buchungen"):
            parse("Buchungstag;Betrag\n")


class RegistryTests(TestCase):
    def test_registry_lists_formats_and_rejects_unknown(self):
        self.assertEqual([name for name, _ in parser_choices()], ["ing_de_pdf", "csv_generic"])
        self.assertEqual(get_parser("").name, "ing_de_pdf")
        self.assertEqual(get_parser("csv_generic").file_extension, ".csv")
        with self.assertRaises(ValueError):
            get_parser("gibt-es-nicht")
