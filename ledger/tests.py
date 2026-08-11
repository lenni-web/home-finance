import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .importers import ParsedTransaction
from .models import Account, Document, Person, StatementImport, Tag, Transaction


class DocumentModelTests(TestCase):
    def test_document_hash_and_relations(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                document = Document.objects.create(
                    kind=Document.Kind.INVOICE,
                    title="Testrechnung",
                    document_date=date(2026, 8, 1),
                    original_filename="rechnung.pdf",
                    file=SimpleUploadedFile("rechnung.pdf", b"test-pdf", "application/pdf"),
                )
                tag = Tag.objects.create(name="Garantie")
                person = Person.objects.create(name="Lennart")
                document.tags.add(tag)
                document.people.add(person)

                self.assertEqual(len(document.sha256), 64)
                self.assertEqual(document.tags.get(), tag)
                self.assertEqual(document.people.get(), person)
                self.assertIn("documents/2026/08/", document.file.name)


class StatementWorkflowTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(name="ING Girokonto")

    @patch("ledger.views.INGStatementParser.parse_pdf")
    def test_statement_upload_creates_review_batch(self, parse_pdf):
        parse_pdf.return_value = [ParsedTransaction(
            booking_date="2026-07-01",
            value_date="2026-07-02",
            booking_type="Lastschrift",
            counterparty="Beispiel GmbH",
            description="Test",
            amount="-10.00",
            currency="EUR",
            source_page=1,
        )]
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                response = self.client.post(reverse("upload_document"), {
                    "kind": Document.Kind.BANK_STATEMENT,
                    "account": self.account.pk,
                    "file": SimpleUploadedFile(
                        "kontoauszug.pdf", b"%PDF-test", "application/pdf"
                    ),
                })

        statement = StatementImport.objects.get()
        self.assertRedirects(response, reverse("statement_review", args=[statement.pk]))
        self.assertEqual(statement.status, StatementImport.Status.REVIEW)
        self.assertEqual(statement.transactions.count(), 1)
        self.assertFalse(statement.transactions.get().reviewed)

    def test_confirm_marks_all_transactions_reviewed(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                document = Document.objects.create(
                    kind=Document.Kind.BANK_STATEMENT,
                    original_filename="kontoauszug.pdf",
                    file=SimpleUploadedFile("kontoauszug.pdf", b"%PDF-test", "application/pdf"),
                )
        statement = StatementImport.objects.create(
            document=document,
            account=self.account,
            status=StatementImport.Status.REVIEW,
        )
        item = Transaction.objects.create(
            statement_import=statement,
            booking_date=date(2026, 7, 1),
            value_date=date(2026, 7, 2),
            booking_type="Lastschrift",
            counterparty="Beispiel GmbH",
            description="Test",
            amount="-10.00",
            source_fingerprint="a" * 64,
        )
        response = self.client.post(reverse("statement_review", args=[statement.pk]), {
            "transactions-TOTAL_FORMS": "1",
            "transactions-INITIAL_FORMS": "1",
            "transactions-MIN_NUM_FORMS": "0",
            "transactions-MAX_NUM_FORMS": "1000",
            "transactions-0-id": str(item.pk),
            "transactions-0-booking_date": "2026-07-01",
            "transactions-0-value_date": "2026-07-02",
            "transactions-0-booking_type": "Lastschrift",
            "transactions-0-counterparty": "Beispiel GmbH",
            "transactions-0-description": "Test",
            "transactions-0-amount": "-10.00",
            "transactions-0-category": "",
            "action": "confirm",
        })

        statement.refresh_from_db()
        item.refresh_from_db()
        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(statement.status, StatementImport.Status.IMPORTED)
        self.assertTrue(item.reviewed)
