from decimal import Decimal
from unittest import TestCase

from django.test import TestCase as DjangoTestCase

from ledger.importers import SparkasseStatementParser, get_parser, parser_choices
from ledger.importers.ing import TextFragment


def row(page, y, text, amount, *details):
    """Eine Buchung in der Spaltenlage des Sparkassen-Auszugs."""
    fragments = [TextFragment(page, 69.93, y, text)]
    for index, line in enumerate(details):
        fragments.append(TextFragment(page, 123.34, y - 10.7 - index * 8.5, line))
    fragments.append(TextFragment(page, 505.5, y, f"              {amount}\n"))
    return fragments


def page_header(page, header_y):
    return [
        TextFragment(page, 68.03, header_y + 40, "Kontoauszug 10/2026\n"),
        TextFragment(page, 70.87, header_y, "Datum Erläuterung Betrag EUR\n"),
        TextFragment(page, 69.45, 52.7, "Fußzeile der Sparkasse\n"),
    ]


def two_page_statement():
    page_one = [
        *page_header(1, 496.39),
        TextFragment(1, 123.05, 481.5, "Kontostand am 30.09.2026, Auszug 9    1.000,00\n"),
        *row(1, 467.77, "01.10.2026 Lastschrift", "-45,50",
             "Supermarkt Beispiel GmbH\n", "Einkauf Filiale 12\n", "Mandatsreferenz ABC123\n"),
        *row(1, 425.57, "02.10.2026 Dauerauftrag", "-850,00", "Vermieter Muster\n"),
        *row(1, 375.34, "05.10.2026 Gutschrift", "2.500,00", "Arbeitgeber AG\n", "Gehalt Oktober\n"),
        TextFragment(1, 421.41, 91.36, "Übertrag:         2.604,50"),
    ]
    page_two = [
        *page_header(2, 691.33),
        TextFragment(2, 421.41, 677.31, "Übertrag:         2.604,50\n"),
        *row(2, 663.56, "07.10.2026 Überweisung", "-104,50", "Versicherung Beispiel\n", "Beitrag 4/2026\n"),
        TextFragment(2, 123.05, 600.0, "Kontostand am 31.10.2026 um 18:00 Uhr    2.500,00\n"),
        TextFragment(2, 69.45, 575.0, "Kontostand und Kontoauszug werden geprüft. 12,00\n"),
        TextFragment(2, 70.87, 500.0, "01.01.2027 Hinweistext mit Datum\n"),
    ]
    return [page_one, page_two]


class SparkasseParserTests(TestCase):
    def test_parses_rows_across_pages_with_details_and_balances(self):
        result = SparkasseStatementParser().parse_fragments(two_page_statement())

        first, second, third, fourth = result.transactions
        self.assertEqual(
            (first.booking_date, first.value_date, first.amount, first.booking_type, first.source_page),
            ("2026-10-01", "2026-10-01", "-45.50", "Lastschrift", 1),
        )
        self.assertEqual(first.counterparty, "Supermarkt Beispiel GmbH")
        self.assertEqual(first.description, "Einkauf Filiale 12\nMandatsreferenz ABC123")
        self.assertEqual((second.amount, second.counterparty, second.description),
                         ("-850.00", "Vermieter Muster", ""))
        self.assertEqual((third.amount, third.description), ("2500.00", "Gehalt Oktober"))
        self.assertEqual((fourth.booking_date, fourth.amount, fourth.source_page),
                         ("2026-10-07", "-104.50", 2))
        self.assertEqual(result.opening_balance, Decimal("1000.00"))
        self.assertEqual(result.closing_balance, Decimal("2500.00"))
        self.assertEqual(result.reconciliation_difference, Decimal("0.00"))

    def test_text_below_the_closing_balance_is_not_a_transaction(self):
        result = SparkasseStatementParser().parse_fragments(two_page_statement())

        self.assertEqual(len(result.transactions), 4)  # kein Treffer für "01.01.2027 Hinweistext"

    def test_description_does_not_leak_into_the_next_row_or_the_carry_line(self):
        result = SparkasseStatementParser().parse_fragments(two_page_statement())

        for item in result.transactions:
            self.assertNotIn("Übertrag", item.description + item.counterparty)
            self.assertNotIn("Kontostand", item.description + item.counterparty)
        self.assertNotIn("Vermieter", result.transactions[0].description)

    def test_missing_amount_shows_up_as_reconciliation_difference(self):
        pages = two_page_statement()
        pages[0] = [item for item in pages[0] if not (item.x == 505.5 and item.y == 425.57)]

        result = SparkasseStatementParser().parse_fragments(pages)

        self.assertEqual(len(result.transactions), 3)
        self.assertEqual(result.reconciliation_difference, Decimal("-850.00") * -1)

    def test_balance_and_amount_sign_variants(self):
        pages = two_page_statement()

        def replace(page, starts_with, text):
            pages[page] = [
                TextFragment(item.page, item.x, item.y, text)
                if item.text.startswith(starts_with) else item
                for item in pages[page]
            ]

        replace(0, "Kontostand", "Kontostand am 30.09.2026    1.000,00 H\n")
        replace(1, "Kontostand am", "Kontostand am 31.10.2026    2.500,00 H\n")
        replace(0, "              -45,50", "     45,50 S\n")

        result = SparkasseStatementParser().parse_fragments(pages)

        self.assertEqual(result.transactions[0].amount, "-45.50")
        self.assertEqual(result.opening_balance, Decimal("1000.00"))
        negative = [
            *page_header(1, 496.39),
            TextFragment(1, 123.05, 481.5, "Kontostand am 30.09.2026    -120,00\n"),
            *row(1, 467.77, "01.10.2026 Lastschrift", "-5,00", "X\n"),
            TextFragment(1, 123.05, 300.0, "Kontostand am 01.10.2026    125,00-\n"),
        ]
        result = SparkasseStatementParser().parse_fragments([negative])
        self.assertEqual(result.opening_balance, Decimal("-120.00"))
        self.assertEqual(result.closing_balance, Decimal("-125.00"))
        self.assertEqual(result.reconciliation_difference, Decimal("0.00"))

    def test_pages_without_a_table_are_ignored(self):
        info_page = [TextFragment(3, 70.0, 700.0, "Hinweise zum Kontoauszug\n")]

        result = SparkasseStatementParser().parse_fragments([*two_page_statement(), info_page])

        self.assertEqual(len(result.transactions), 4)

    def test_is_registered_as_account_format(self):
        self.assertIn("sparkasse_pdf", [name for name, _label in parser_choices()])
        self.assertEqual(get_parser("sparkasse_pdf").label, "Sparkasse (PDF)")


def build_pdf(pages):
    """Minimal-PDF mit exakt positionierten Textfragmenten (x, y, Text) je Seite."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None]
    kids = []
    for fragments in pages:
        page_id = len(objects) + 1
        content_id = page_id + 1
        kids.append(f"{page_id} 0 R")
        stream = "BT /F1 10 Tf\n" + "".join(
            f"1 0 0 1 {x} {y} Tm ({text}) Tj\n" for x, y, text in fragments
        ) + "ET"
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_id} 0 R "
            "/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    output = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    output += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return output


class SparkassePdfEndToEndTests(TestCase):
    def test_parses_a_real_pdf_file(self):
        import tempfile
        from pathlib import Path

        page = [
            (68, 545, "Kontoauszug 10/2026"),
            (70.87, 496.39, "Datum Erlaeuterung Betrag EUR"),
            (123.05, 481.5, "Kontostand am 30.09.2026    1.000,00"),
            (69.93, 467.77, "01.10.2026 Lastschrift"),
            (123.34, 457.05, "Supermarkt Beispiel"),
            (123.34, 448.51, "Einkauf Filiale"),
            (504.2, 467.77, "-45,50"),
            (69.93, 425.57, "05.10.2026 Gutschrift"),
            (123.34, 414.85, "Arbeitgeber AG"),
            (508.97, 425.57, "2.500,00"),
            (123.05, 327.99, "Kontostand am 31.10.2026    3.454,50"),
            (69.45, 52.7, "Fusszeile"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auszug.pdf"
            path.write_bytes(build_pdf([page]))

            result = SparkasseStatementParser().parse_statement(path)

        first, second = result.transactions
        self.assertEqual((first.booking_date, first.amount, first.booking_type),
                         ("2026-10-01", "-45.50", "Lastschrift"))
        self.assertEqual((first.counterparty, first.description),
                         ("Supermarkt Beispiel", "Einkauf Filiale"))
        self.assertEqual((second.amount, second.counterparty), ("2500.00", "Arbeitgeber AG"))
        self.assertEqual(result.reconciliation_difference, Decimal("0.00"))


class SparkasseImportWorkflowTests(DjangoTestCase):
    def test_statement_import_uses_the_account_format_and_reconciles(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from ledger.models import Account, Document, StatementImport
        from ledger.statement_processing import process_statement_import

        page = [
            (70.87, 496.39, "Datum Erlaeuterung Betrag EUR"),
            (123.05, 481.5, "Kontostand am 30.09.2026    100,00"),
            (69.93, 467.77, "01.10.2026 Lastschrift"),
            (123.34, 457.05, "Laden Beispiel"),
            (504.2, 467.77, "-40,00"),
            (123.05, 327.99, "Kontostand am 31.10.2026    60,00"),
        ]
        account = Account.objects.create(name="Sparkasse Giro", import_format="sparkasse_pdf")
        document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="auszug.pdf",
            file=SimpleUploadedFile("auszug.pdf", build_pdf([page]), "application/pdf"),
        )
        statement = StatementImport.objects.create(
            document=document, account=account, parser_name="sparkasse_pdf"
        )

        process_statement_import(statement)

        statement.refresh_from_db()
        self.assertEqual(statement.status, StatementImport.Status.REVIEW)
        self.assertEqual(
            statement.reconciliation_status, StatementImport.ReconciliationStatus.BALANCED
        )
        item = statement.transactions.get()
        self.assertEqual((item.counterparty, str(item.amount)), ("Laden Beispiel", "-40.00"))
