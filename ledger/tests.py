import tempfile
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.template import Context, Template
from django.urls import reverse

from .document_processing import _parse_date, _parse_merchant, _parse_total
from .document_matching import refresh_unmatched_document_reviews
from .importers import ParsedStatement, ParsedTransaction
from .models import (
    Account, CategorizationRule, Category, Document, EmailImportConfig, EmailImportMessage,
    Person, StatementImport, Tag, Transaction,
)
from .rules import apply_categorization_rules, categorization_suggestion, normalize_merchant
from .statement_reconciliation import store_reconciliation
from .statement_processing import process_statement_import


class DocumentModelTests(TestCase):
    def test_money_filter_uses_german_thousands_separator(self):
        rendered = Template(
            "{% load ledger_format %}{{ amount|money }} €"
        ).render(Context({"amount": Decimal("4000.00")}))

        self.assertEqual(rendered, "4.000,00 €")

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


class AccessControlTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("access", password="secret-test-password")
        self.document = Document.objects.create(
            kind=Document.Kind.INVOICE,
            original_filename="private.pdf",
            file=SimpleUploadedFile("private.pdf", b"private-content", "application/pdf"),
        )

    def test_financial_pages_redirect_to_login(self):
        response = self.client.get(reverse("dashboard"))

        self.assertRedirects(response, f"{reverse('login')}?next={reverse('dashboard')}")

    def test_document_download_requires_login(self):
        url = reverse("document_download", args=[self.document.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_health_endpoint_remains_public(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)


class StatementWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="secret-test-password")
        self.client.force_login(self.user)
        self.account = Account.objects.create(name="ING Girokonto")

    def test_account_can_be_created_from_settings(self):
        response = self.client.post(reverse("add_account"), {
            "name": "Gemeinschaftskonto",
            "iban_last_four": "1234",
        })

        self.assertRedirects(response, reverse("settings"))
        self.assertTrue(Account.objects.filter(
            name="Gemeinschaftskonto", iban_last_four="1234"
        ).exists())

    def test_account_rejects_invalid_iban_suffix(self):
        response = self.client.post(reverse("add_account"), {
            "name": "Fehlerhaft",
            "iban_last_four": "12AB",
        })

        self.assertRedirects(response, reverse("settings"))
        self.assertFalse(Account.objects.filter(name="Fehlerhaft").exists())

    def test_account_can_be_edited_without_losing_statement_assignment(self):
        document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT,
            original_filename="account-edit.pdf",
            file=SimpleUploadedFile("account-edit.pdf", b"%PDF-account-edit", "application/pdf"),
        )
        statement = StatementImport.objects.create(document=document, account=self.account)

        response = self.client.post(reverse("edit_account", args=[self.account.pk]), {
            "name": "Gemeinschaftskonto",
            "iban_last_four": "9876",
        })

        self.account.refresh_from_db()
        statement.refresh_from_db()
        self.assertRedirects(response, reverse("settings"))
        self.assertEqual(self.account.name, "Gemeinschaftskonto")
        self.assertEqual(self.account.iban_last_four, "9876")
        self.assertEqual(statement.account, self.account)

    def test_statement_review_field_limit_supports_large_statements(self):
        fields_per_transaction = 8
        expected_large_statement = 500
        self.assertGreaterEqual(
            settings.DATA_UPLOAD_MAX_NUMBER_FIELDS,
            fields_per_transaction * expected_large_statement,
        )

    @patch("ledger.views.process_statement_task.delay")
    def test_statement_upload_queues_background_task(self, delay):
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
        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(statement.status, StatementImport.Status.UPLOADED)
        self.assertEqual(statement.transactions.count(), 0)
        delay.assert_called_once_with(statement.pk)

    @patch("ledger.statement_processing.INGStatementParser.parse_statement_pdf")
    def test_statement_background_processing_creates_review_batch(self, parse_statement_pdf):
        document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT,
            original_filename="kontoauszug.pdf",
            file=SimpleUploadedFile("kontoauszug.pdf", b"%PDF-background", "application/pdf"),
        )
        statement = StatementImport.objects.create(document=document, account=self.account)
        parse_statement_pdf.return_value = ParsedStatement(transactions=[ParsedTransaction(
            booking_date="2026-07-01",
            value_date="2026-07-02",
            booking_type="Lastschrift",
            counterparty="Beispiel GmbH",
            description="Test",
            amount="-10.00",
            currency="EUR",
            source_page=1,
        )], opening_balance=Decimal("100.00"), closing_balance=Decimal("90.00"))

        count = process_statement_import(statement)

        statement.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertEqual(statement.status, StatementImport.Status.REVIEW)
        self.assertEqual(
            statement.reconciliation_status, StatementImport.ReconciliationStatus.BALANCED
        )
        self.assertEqual(statement.reconciliation_difference, Decimal("0.00"))
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
        self.user = get_user_model().objects.create_user("categories", password="secret-test-password")
        self.client.force_login(self.user)
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

        self.assertContains(response, "Vorschlag: Vorschlag")
        self.assertContains(response, "% sicher · Regel")
        self.item.refresh_from_db()
        self.assertIsNone(self.item.category)

    def test_dashboard_contains_monthly_analysis_and_open_tasks(self):
        response = self.client.get(reverse("dashboard"), {"month": "2026-07"})

        self.assertContains(response, "Monatsauswertung")
        self.assertContains(response, "42,50 €")
        self.assertContains(response, "Ohne Kategorie")
        self.assertContains(response, "Offene Aufgaben")

    def test_open_tasks_lists_uncategorized_transactions(self):
        response = self.client.get(reverse("open_tasks"))

        self.assertContains(response, "Beispielmarkt Berlin")
        self.assertContains(response, "Buchungen ohne Kategorie")

    def test_learns_suggestion_from_confirmed_normalized_merchant(self):
        category = Category.objects.create(name="Lebensmittel")
        self.item.category = category
        self.item.save(update_fields=["category", "updated_at"])
        candidate = Transaction.objects.create(
            statement_import=self.statement,
            booking_date=date(2026, 8, 2),
            counterparty="BEISPIELMARKT BERLIN GMBH",
            amount="-12.00",
            source_fingerprint="9" * 64,
            reviewed=True,
        )

        suggestion = categorization_suggestion(candidate)

        self.assertEqual(normalize_merchant(candidate.counterparty), "beispielmarkt berlin")
        self.assertEqual(suggestion.category, category)
        self.assertEqual(suggestion.confidence, 100)
        self.assertEqual(suggestion.source, "Bestätigte Buchungen")

    def test_higher_priority_rule_wins_and_can_be_edited(self):
        low = CategorizationRule.objects.create(
            name="Niedrig", match_text="Beispielmarkt", priority=10, auto_apply=False
        )
        high_category = Category.objects.create(name="Priorisiert")
        high = CategorizationRule.objects.create(
            name="Hoch", match_text="Beispielmarkt Berlin", category=high_category,
            priority=200, auto_apply=False,
        )

        suggestion = categorization_suggestion(self.item)
        self.assertEqual(suggestion.rule, high)

        response = self.client.post(reverse("edit_rule", args=[low.pk]), {
            "name": "Nun hoch",
            "match_text": "Beispielmarkt",
            "priority": "300",
            "category": "",
            "tags": [],
            "people": [],
        })
        low.refresh_from_db()
        self.assertRedirects(response, reverse("manage_classification"))
        self.assertEqual(low.priority, 300)

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

    def test_category_can_be_renamed_and_recolored_without_losing_assignment(self):
        category = Category.objects.create(name="Alt", color="#111111")
        self.item.category = category
        self.item.save(update_fields=["category", "updated_at"])

        response = self.client.post(
            reverse("edit_classification", args=["category", category.pk]),
            {"name": "Neu", "color": "#22aa44"},
        )

        category.refresh_from_db()
        self.item.refresh_from_db()
        self.assertRedirects(response, reverse("manage_classification"))
        self.assertEqual(category.name, "Neu")
        self.assertEqual(category.color, "#22aa44")
        self.assertEqual(self.item.category, category)

    def test_person_can_be_renamed_and_recolored_without_losing_assignment(self):
        person = Person.objects.create(name="Alte Person", color="#111111")
        self.item.people.add(person)

        response = self.client.post(
            reverse("edit_classification", args=["person", person.pk]),
            {"name": "Neue Person", "color": "#663399"},
        )

        person.refresh_from_db()
        self.assertRedirects(response, reverse("manage_classification"))
        self.assertEqual(person.name, "Neue Person")
        self.assertEqual(person.color, "#663399")
        self.assertEqual(self.item.people.get(), person)

    def test_unsupported_classification_edit_redirects(self):
        tag = Tag.objects.create(name="Nicht editierbar")

        response = self.client.get(reverse("edit_classification", args=["tag", tag.pk]))

        self.assertRedirects(response, reverse("manage_classification"))


class DocumentProcessingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("documents", password="secret-test-password")
        self.client.force_login(self.user)

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

    @patch("ledger.views.process_document_task.delay")
    def test_receipt_upload_queues_background_task(self, delay):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                response = self.client.post(reverse("upload_document"), {
                    "kind": Document.Kind.RECEIPT,
                    "file": SimpleUploadedFile("beleg.png", b"png-test", "image/png"),
                })

        document = Document.objects.get()
        self.assertRedirects(response, reverse("dashboard"))
        delay.assert_called_once_with(document.pk)

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


class EmailImportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("email", password="secret-test-password")
        self.client.force_login(self.user)
        self.config = EmailImportConfig.objects.create(
            pk=1, enabled=True, host="imap.example.test", username="archiv@example.test",
            folder="INBOX", allowed_senders="rechnung@example.test",
        )
        self.config.set_password("app-password")
        self.config.save(update_fields=["encrypted_password"])

    def _message(self):
        message = EmailMessage()
        message["From"] = "Rechnungen <rechnung@example.test>"
        message["To"] = "archiv@example.test"
        message["Subject"] = "Neue Rechnung"
        message["Message-ID"] = "<invoice-1@example.test>"
        message.set_content("Anhang beachten")
        message.add_attachment(
            b"%PDF-email-test", maintype="application", subtype="pdf", filename="rechnung.pdf"
        )
        return message.as_bytes()

    @override_settings(EMAIL_CREDENTIAL_KEY="stable-test-encryption-key")
    def test_password_is_encrypted_and_can_be_read(self):
        config = EmailImportConfig(host="imap.example.test")
        config.set_password("very-secret")

        self.assertNotIn("very-secret", config.encrypted_password)
        self.assertEqual(config.get_password(), "very-secret")

    @patch("ledger.email_import.open_imap")
    @patch("ledger.tasks.process_document_task.delay")
    def test_poll_imports_supported_attachment_once(self, delay, open_imap):
        class FakeImap:
            def select(self, folder): return "OK", [b"1"]
            def uid(self, command, *args):
                if command == "search": return "OK", [b"42"]
                if command == "fetch": return "OK", [(b"42 BODY[]", self.message)]
                if command == "store": return "OK", []
                raise AssertionError(command)
            def logout(self): return "BYE", []

        fake = FakeImap()
        fake.message = self._message()
        open_imap.return_value = fake
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                from .email_import import poll_mailbox
                with self.captureOnCommitCallbacks(execute=True):
                    imported = poll_mailbox(self.config, force=True)
                imported_again = poll_mailbox(self.config, force=True)

        self.assertEqual(imported, 1)
        self.assertEqual(imported_again, 0)
        document = Document.objects.get(original_filename="rechnung.pdf")
        self.assertEqual(document.kind, Document.Kind.INVOICE)
        self.assertEqual(EmailImportMessage.objects.get().mailbox_uid, "42")
        delay.assert_called_once_with(document.pk)

    @patch("ledger.views.poll_email_import_task.delay")
    def test_settings_save_password_and_queue_manual_fetch(self, delay):
        response = self.client.post(reverse("save_email_settings"), {
            "email-enabled": "on", "email-host": "imap.example.test",
            "email-port": "993", "email-security": "ssl",
            "email-username": "archiv@example.test", "email-password": "new-password",
            "email-folder": "INBOX", "email-allowed_senders": "rechnung@example.test",
            "email-poll_interval_minutes": "10", "email-mark_as_read": "on",
            "action": "fetch",
        })

        self.config.refresh_from_db()
        self.assertRedirects(response, reverse("settings"))
        self.assertEqual(self.config.get_password(), "new-password")
        delay.assert_called_once_with(self.config.pk, force=True)
        settings_response = self.client.get(reverse("settings"))
        self.assertNotContains(settings_response, "new-password")
        self.assertContains(settings_response, "E-Mail-Import über IMAP")

    def test_unapproved_sender_is_logged_without_import(self):
        message = EmailMessage()
        message["From"] = "unknown@example.test"
        message["Subject"] = "Nicht erlaubt"
        message.set_content("Text")
        message.add_attachment(
            b"%PDF-rejected", maintype="application", subtype="pdf", filename="no.pdf"
        )
        from .email_import import _import_message

        imported = _import_message(self.config, "43", message.as_bytes())

        self.assertEqual(imported, 0)
        self.assertFalse(Document.objects.filter(original_filename="no.pdf").exists())
        self.assertIn("nicht freigegeben", EmailImportMessage.objects.get().error_message)

    @patch("ledger.email_import.test_imap_connection")
    def test_connection_test_uses_saved_configuration(self, test_connection):
        response = self.client.post(reverse("save_email_settings"), {
            "email-enabled": "on", "email-host": "imap.example.test",
            "email-port": "993", "email-security": "ssl",
            "email-username": "archiv@example.test", "email-password": "",
            "email-folder": "INBOX", "email-allowed_senders": "rechnung@example.test",
            "email-poll_interval_minutes": "5", "email-mark_as_read": "on",
            "action": "test",
        })

        self.assertRedirects(response, reverse("settings"))
        test_connection.assert_called_once()

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
        self.assertContains(response, "-12,34 €")


class StatementReconciliationTests(TestCase):
    def setUp(self):
        account = Account.objects.create(name="Saldo-Konto")
        document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT,
            original_filename="saldo.pdf",
            file=SimpleUploadedFile("saldo.pdf", b"%PDF-saldo", "application/pdf"),
        )
        self.statement = StatementImport.objects.create(document=document, account=account)

    def test_marks_balance_mismatch(self):
        parsed = ParsedStatement(
            transactions=[], opening_balance=Decimal("100.00"), closing_balance=Decimal("90.00")
        )

        store_reconciliation(self.statement, parsed)

        self.assertEqual(
            self.statement.reconciliation_status, StatementImport.ReconciliationStatus.MISMATCH
        )
        self.assertEqual(self.statement.reconciliation_difference, Decimal("10.00"))

    def test_marks_missing_balances_unavailable(self):
        parsed = ParsedStatement(transactions=[], opening_balance=None, closing_balance=None)

        store_reconciliation(self.statement, parsed)

        self.assertEqual(
            self.statement.reconciliation_status,
            StatementImport.ReconciliationStatus.UNAVAILABLE,
        )
        self.assertIsNone(self.statement.reconciliation_difference)
