import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from .document_processing import _parse_date, _parse_merchant, _parse_total
from .document_matching import refresh_unmatched_document_reviews
from .importers import ParsedTransaction
from .models import (
    Account, CategorizationRule, Category, Document, Person, StatementImport, Tag, Transaction,
)
from .rules import apply_categorization_rules


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

    def test_account_can_be_created_from_dashboard(self):
        response = self.client.post(reverse("add_account"), {
            "name": "Gemeinschaftskonto",
            "iban_last_four": "1234",
        })

        self.assertRedirects(response, reverse("dashboard"))
        self.assertTrue(Account.objects.filter(
            name="Gemeinschaftskonto", iban_last_four="1234"
        ).exists())

    def test_account_rejects_invalid_iban_suffix(self):
        response = self.client.post(reverse("add_account"), {
            "name": "Fehlerhaft",
            "iban_last_four": "12AB",
        })

        self.assertRedirects(response, reverse("dashboard"))
        self.assertFalse(Account.objects.filter(name="Fehlerhaft").exists())

    def test_statement_review_field_limit_supports_large_statements(self):
        fields_per_transaction = 8
        expected_large_statement = 500
        self.assertGreaterEqual(
            settings.DATA_UPLOAD_MAX_NUMBER_FIELDS,
            fields_per_transaction * expected_large_statement,
        )

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
        document.refresh_from_db()
        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(statement.status, StatementImport.Status.IMPORTED)
        self.assertTrue(item.reviewed)
        self.assertEqual(document.processing_status, Document.ProcessingStatus.PROCESSED)

    def test_review_renders_dates_in_html_date_input_format(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                document = Document.objects.create(
                    kind=Document.Kind.BANK_STATEMENT,
                    original_filename="kontoauszug.pdf",
                    file=SimpleUploadedFile("kontoauszug.pdf", b"%PDF-date", "application/pdf"),
                )
        statement = StatementImport.objects.create(
            document=document,
            account=self.account,
            status=StatementImport.Status.REVIEW,
        )
        Transaction.objects.create(
            statement_import=statement,
            booking_date=date(2026, 7, 9),
            value_date=date(2026, 7, 8),
            booking_type="Lastschrift",
            counterparty="Beispiel GmbH",
            description="Test",
            amount="-10.00",
            source_fingerprint="b" * 64,
        )

        response = self.client.get(reverse("statement_review", args=[statement.pk]))

        self.assertContains(response, 'value="2026-07-09"')
        self.assertContains(response, 'value="2026-07-08"')


class CategorizationWorkflowTests(TestCase):
    def setUp(self):
        self.account = Account.objects.create(name="ING")
        self.document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT,
            original_filename="statement.pdf",
            file=SimpleUploadedFile("statement.pdf", b"%PDF-categories", "application/pdf"),
        )
        self.statement = StatementImport.objects.create(
            document=self.document,
            account=self.account,
            status=StatementImport.Status.IMPORTED,
        )
        self.item = Transaction.objects.create(
            statement_import=self.statement,
            booking_date=date(2026, 7, 10),
            value_date=date(2026, 7, 10),
            booking_type="Lastschrift",
            counterparty="Beispielmarkt Berlin",
            description="Einkauf",
            amount="-42.50",
            source_fingerprint="c" * 64,
            reviewed=True,
        )

    def test_overview_shows_confirmed_transaction(self):
        response = self.client.get(reverse("transaction_overview"), {"month": "2026-07"})

        self.assertContains(response, "Beispielmarkt Berlin")
        self.assertContains(response, "-42,50")

    def test_bulk_assignment_and_rule_creation(self):
        category = Category.objects.create(name="Lebensmittel")
        tag = Tag.objects.create(name="Haushalt")
        person = Person.objects.create(name="Lennart")
        response = self.client.post(reverse("transaction_overview"), {
            "transactions-TOTAL_FORMS": "1",
            "transactions-INITIAL_FORMS": "1",
            "transactions-MIN_NUM_FORMS": "0",
            "transactions-MAX_NUM_FORMS": "1000",
            "transactions-0-id": str(self.item.pk),
            "transactions-0-category": "",
            "transactions-0-tags": [],
            "transactions-0-people": [],
            "bulk-category": str(category.pk),
            "bulk-tags": [str(tag.pk)],
            "bulk-people": [str(person.pk)],
            "bulk-create_rules": "on",
            "bulk-auto_apply": "on",
            "selected": [str(self.item.pk)],
            "action": "bulk",
        })

        self.assertRedirects(response, reverse("transaction_overview"))
        self.item.refresh_from_db()
        self.assertEqual(self.item.category, category)
        self.assertEqual(self.item.tags.get(), tag)
        self.assertEqual(self.item.people.get(), person)
        rule = CategorizationRule.objects.get()
        self.assertEqual(rule.match_text, "Beispielmarkt Berlin")
        self.assertTrue(rule.auto_apply)

    def test_rule_applies_to_new_transaction(self):
        category = Category.objects.create(name="Lebensmittel")
        rule = CategorizationRule.objects.create(
            name="Marktregel",
            match_text="Beispielmarkt",
            category=category,
            auto_apply=True,
        )
        new_item = Transaction.objects.create(
            statement_import=self.statement,
            booking_date=date(2026, 8, 1),
            booking_type="Lastschrift",
            counterparty="Beispielmarkt Hamburg",
            description="",
            amount="-5.00",
            source_fingerprint="d" * 64,
            reviewed=False,
        )

        applied = apply_categorization_rules(new_item)

        new_item.refresh_from_db()
        rule.refresh_from_db()
        self.assertEqual(new_item.category, category)
        self.assertEqual(applied, [rule])
        self.assertEqual(rule.times_applied, 1)

    def test_non_automatic_rule_is_shown_as_suggestion(self):
        category = Category.objects.create(name="Vorschlag")
        CategorizationRule.objects.create(
            name="Nur vorschlagen",
            match_text="Beispielmarkt",
            category=category,
            auto_apply=False,
        )

        response = self.client.get(reverse("transaction_overview"))

        self.assertContains(response, "Regelvorschlag: Vorschlag")
        self.item.refresh_from_db()
        self.assertIsNone(self.item.category)

    def test_category_can_be_created_and_deactivated(self):
        response = self.client.post(reverse("manage_classification"), {
            "kind": "category",
            "category-name": "Mobilität",
            "category-color": "#123456",
        })
        category = Category.objects.get(name="Mobilität")
        self.assertRedirects(response, reverse("manage_classification"))

        response = self.client.post(reverse(
            "toggle_classification", args=["category", category.pk]
        ))
        category.refresh_from_db()
        self.assertRedirects(response, reverse("manage_classification"))
        self.assertFalse(category.active)


class DocumentProcessingTests(TestCase):
    def test_extracts_receipt_metadata_from_text(self):
        text = """Musterladen Berlin
Rechnung
Rechnungsdatum 05.08.2026
Zwischensumme 10,00 EUR
Gesamt 12,34 EUR
"""

        self.assertEqual(_parse_date(text), date(2026, 8, 5))
        self.assertEqual(_parse_total(text), Decimal("12.34"))
        self.assertEqual(_parse_merchant(text), "Musterladen Berlin")

    @patch("ledger.views.process_document")
    def test_receipt_upload_redirects_to_document_review(self, process):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                response = self.client.post(reverse("upload_document"), {
                    "kind": Document.Kind.RECEIPT,
                    "file": SimpleUploadedFile("beleg.png", b"png-test", "image/png"),
                })

        document = Document.objects.get()
        self.assertRedirects(response, reverse("document_review", args=[document.pk]))
        process.assert_called_once_with(document)

    def test_document_can_be_linked_to_matching_transaction(self):
        account = Account.objects.create(name="ING")
        statement_document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT,
            original_filename="statement.pdf",
            file=SimpleUploadedFile("statement.pdf", b"%PDF-link", "application/pdf"),
        )
        statement = StatementImport.objects.create(
            document=statement_document,
            account=account,
            status=StatementImport.Status.IMPORTED,
        )
        transaction = Transaction.objects.create(
            statement_import=statement,
            booking_date=date(2026, 8, 5),
            value_date=date(2026, 8, 5),
            counterparty="Musterladen",
            amount="-12.34",
            source_fingerprint="e" * 64,
            reviewed=True,
        )
        receipt = Document.objects.create(
            kind=Document.Kind.RECEIPT,
            title="Musterbeleg",
            original_filename="receipt.pdf",
            document_date=date(2026, 8, 5),
            total_amount="12.34",
            file=SimpleUploadedFile("receipt.pdf", b"%PDF-receipt", "application/pdf"),
            processing_status=Document.ProcessingStatus.REVIEW,
        )

        response = self.client.post(reverse("document_review", args=[receipt.pk]), {
            "document-title": "Musterbeleg",
            "document-document_date": "2026-08-05",
            "document-merchant": "Musterladen",
            "document-total_amount": "12.34",
            "document-category": "",
            "document-tags": [],
            "document-people": [],
            "links-transactions": [str(transaction.pk)],
            "action": "save",
        })

        receipt.refresh_from_db()
        if response.status_code == 200:
            self.assertFalse(response.context["review_form"].errors, response.context["review_form"].errors)
            self.assertFalse(response.context["link_form"].errors, response.context["link_form"].errors)
        self.assertRedirects(response, reverse("document_archive"))
        self.assertEqual(receipt.processing_status, Document.ProcessingStatus.PROCESSED)
        self.assertEqual(receipt.transactions.get(), transaction)

    def test_archive_searches_extracted_text(self):
        Document.objects.create(
            kind=Document.Kind.INVOICE,
            title="Test",
            original_filename="search.pdf",
            extracted_text="Unverwechselbarer Suchbegriff",
            file=SimpleUploadedFile("search.pdf", b"%PDF-search", "application/pdf"),
        )

        response = self.client.get(reverse("document_archive"), {"q": "Unverwechselbarer"})

        self.assertContains(response, "Test")

    def test_receipt_uploaded_before_statement_is_rematched_later(self):
        receipt = Document.objects.create(
            kind=Document.Kind.RECEIPT,
            title="Früher Beleg",
            original_filename="early.pdf",
            document_date=date(2026, 8, 5),
            total_amount="12.34",
            processing_status=Document.ProcessingStatus.PROCESSED,
            file=SimpleUploadedFile("early.pdf", b"%PDF-early", "application/pdf"),
        )
        account = Account.objects.create(name="ING später")
        statement_document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT,
            original_filename="later.pdf",
            file=SimpleUploadedFile("later.pdf", b"%PDF-later", "application/pdf"),
        )
        statement = StatementImport.objects.create(
            document=statement_document,
            account=account,
            status=StatementImport.Status.IMPORTED,
        )
        transaction = Transaction.objects.create(
            statement_import=statement,
            booking_date=date(2026, 8, 7),
            value_date=date(2026, 8, 7),
            counterparty="Musterladen",
            amount="-12.34",
            source_fingerprint="f" * 64,
            reviewed=True,
        )

        rematched = refresh_unmatched_document_reviews()

        receipt.refresh_from_db()
        self.assertEqual(rematched, [receipt])
        self.assertEqual(receipt.processing_status, Document.ProcessingStatus.REVIEW)
        self.assertFalse(receipt.transactions.exists())
        response = self.client.get(reverse("document_review", args=[receipt.pk]))
        self.assertContains(response, "Musterladen")
        self.assertContains(response, str(transaction.amount))
