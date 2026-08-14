import tempfile
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.template import Context, Template
from django.urls import reverse

from .document_processing import (
    ExtractedContent, OCRWord, _merchant_analysis, _ocr_receipt_quality, _parse_date,
    _parse_invoice_number, _parse_merchant, _parse_total,
)
from .document_matching import auto_match_document, refresh_unmatched_document_reviews
from .importers import ParsedStatement, ParsedTransaction
from .internal_transfers import best_transfer_candidate, link_transfer_pair, unlink_transfer
from .models import (
    Account, BackupRecord, CategorizationRule, Category, Document, EmailImportConfig,
    EmailImportMessage, Person, ServiceHeartbeat, StatementImport, Tag, Transaction,
)
from .rules import apply_categorization_rules, categorization_suggestion, normalize_merchant
from .statement_reconciliation import store_reconciliation
from .statement_processing import process_statement_import
from .text_normalization import normalize_comparison_text


class DocumentModelTests(TestCase):
    def test_comparison_text_tolerates_umlaut_variants_and_pdf_replacement_characters(self):
        self.assertEqual(normalize_comparison_text("Müller"), "muller")
        self.assertEqual(normalize_comparison_text("Mueller"), "muller")
        self.assertEqual(normalize_comparison_text("Muller"), "muller")
        self.assertEqual(normalize_comparison_text("Straße"), "strasse")
        self.assertEqual(normalize_comparison_text("Straße"), normalize_comparison_text("Strasse"))
        self.assertEqual(normalize_comparison_text("M□ller"), "mller")

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

    def test_invoice_number_is_extracted(self):
        self.assertEqual(
            _parse_invoice_number("Rechnungsnummer: RE-2026/4711\nGesamt 12,50 EUR"),
            "RE-2026/4711",
        )

    def test_receipt_merchant_is_found_even_below_noisy_first_lines(self):
        text = "a > Aa DT\nLar!-Benz-StraRe 4\nLEERGUTRÜCKNAHME\nALDI SE & Co. KG"

        self.assertEqual(_parse_merchant(text), "ALDI")

    def test_receipt_ocr_quality_prefers_structured_result(self):
        noisy = "a > Aa DT\nYona ee\nDSC DS) <5"
        structured = (
            "ALDI\nZU ZAHLEN 40,08 €\nKARTENZAHLUNG 40,08 €\n"
            "Datum 11.08.26 15:16 Uhr\nMWST 19,00%"
        )

        self.assertGreater(_ocr_receipt_quality(structured), _ocr_receipt_quality(noisy))


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
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

    def test_health_endpoint_remains_public(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)

    def test_favicon_redirects_to_brand_asset(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/static/ledger/brand/favicon.ico")

    def test_operational_status_requires_login(self):
        url = reverse("operational_status")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(url), "Betriebsstatus")

    @override_settings(DEPLOY_TAG="v0.3.7", DEPLOY_REVISION="abc123")
    def test_operational_status_shows_installed_git_tag(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("operational_status"))

        self.assertContains(response, "Installierte Version")
        self.assertContains(response, "v0.3.7")
        self.assertContains(response, "abc123")

    @override_settings(TIME_ZONE="Europe/Berlin")
    def test_operational_status_displays_timestamps_in_german_local_time(self):
        utc_timestamp = datetime(2026, 8, 12, 15, 55, tzinfo=ZoneInfo("UTC"))
        BackupRecord.objects.create(filename="backup.tar.gz", created_at=utc_timestamp)
        ServiceHeartbeat.objects.create(name="worker", last_seen_at=utc_timestamp)
        self.client.force_login(self.user)

        response = self.client.get(reverse("operational_status"))

        self.assertContains(response, "12.08.2026 17:55", count=2)
        self.assertNotContains(response, "12.08.2026 15:55")


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

    def _create_transfer_counterpart(self):
        other_account = Account.objects.create(name="Tagesgeld")
        other_document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT,
            original_filename="other-statement.pdf",
            file=SimpleUploadedFile("other-statement.pdf", b"%PDF-other", "application/pdf"),
        )
        other_statement = StatementImport.objects.create(
            document=other_document,
            account=other_account,
            status=StatementImport.Status.IMPORTED,
        )
        return Transaction.objects.create(
            statement_import=other_statement,
            booking_date=date(2026, 7, 11),
            counterparty="Eigenes Girokonto",
            amount="42.50",
            source_fingerprint="t" * 64,
            reviewed=True,
        )

    def test_transfer_candidate_requires_opposite_amount_and_another_account(self):
        counterpart = self._create_transfer_counterpart()

        self.assertEqual(best_transfer_candidate(self.item), counterpart)

    def test_suggested_transfer_pair_can_be_confirmed_in_overview(self):
        counterpart = self._create_transfer_counterpart()
        response = self.client.get(reverse("transaction_overview"))
        self.assertContains(response, "Mögliche Gegenbuchung")

        response = self.client.post(reverse("transaction_overview"), {
            "action": f"confirm_transfer:{self.item.pk}:{counterpart.pk}",
        })

        self.assertRedirects(response, reverse("transaction_overview"))
        self.item.refresh_from_db()
        counterpart.refresh_from_db()
        self.assertTrue(self.item.is_internal_transfer)
        self.assertEqual(self.item.transfer_counterpart, counterpart)
        self.assertEqual(counterpart.transfer_counterpart, self.item)

    def test_internal_transfers_are_excluded_from_dashboard_and_analytics_totals(self):
        counterpart = self._create_transfer_counterpart()
        link_transfer_pair(self.item, counterpart)

        dashboard = self.client.get(reverse("dashboard"), {"month": "2026-07"})
        analytics = self.client.get(reverse("analytics"), {"month": "2026-07"})

        self.assertContains(dashboard, "0,00 €")
        self.assertContains(analytics, "Ausgaben<strong class=\"negative\">0,00 €")
        self.assertContains(analytics, "Ausgabenbuchungen<strong>0")

    def test_transfer_can_remain_marked_until_counterpart_is_imported(self):
        self.item.is_internal_transfer = True
        self.item.save(update_fields=["is_internal_transfer", "updated_at"])

        response = self.client.get(reverse("transaction_overview"), {"transfer_status": "open"})

        self.assertContains(response, "Gegenbuchung noch nicht vorhanden")

    def test_unlinking_transfer_clears_both_sides(self):
        counterpart = self._create_transfer_counterpart()
        link_transfer_pair(self.item, counterpart)

        unlink_transfer(self.item)

        self.item.refresh_from_db()
        counterpart.refresh_from_db()
        self.assertFalse(self.item.is_internal_transfer)
        self.assertFalse(counterpart.is_internal_transfer)
        self.assertIsNone(self.item.transfer_counterpart)
        self.assertIsNone(counterpart.transfer_counterpart)

    def test_automatic_rule_can_mark_internal_transfer(self):
        rule = CategorizationRule.objects.create(
            name="Eigenübertrag",
            match_text="Beispielmarkt",
            marks_internal_transfer=True,
            auto_apply=True,
        )

        apply_categorization_rules(self.item)

        self.item.refresh_from_db()
        rule.refresh_from_db()
        self.assertTrue(self.item.is_internal_transfer)
        self.assertEqual(rule.times_applied, 1)

    def test_people_selects_show_at_least_four_entries(self):
        response = self.client.get(reverse("transaction_overview"))

        self.assertContains(response, 'name="transactions-0-people" size="4"')
        self.assertContains(response, 'name="bulk-people" size="4"')

    def test_categories_are_alphabetical_in_lists_and_select_fields(self):
        Category.objects.create(name="Wohnen")
        renamed = Category.objects.create(name="Zwischenablage")
        Category.objects.create(name="Auto")
        renamed.name = "Bildung"
        renamed.save(update_fields=["name", "updated_at"])

        self.assertEqual(
            list(Category.objects.values_list("name", flat=True)),
            ["Auto", "Bildung", "Wohnen"],
        )
        response = self.client.get(reverse("transaction_overview"))
        content = response.content.decode()
        self.assertLess(content.index(">Auto</option>"), content.index(">Bildung</option>"))
        self.assertLess(content.index(">Bildung</option>"), content.index(">Wohnen</option>"))

    def test_transaction_comment_can_be_saved_and_searched(self):
        response = self.client.post(reverse("transaction_overview"), {
            "transactions-TOTAL_FORMS": "1",
            "transactions-INITIAL_FORMS": "1",
            "transactions-MIN_NUM_FORMS": "0",
            "transactions-MAX_NUM_FORMS": "1000",
            "transactions-0-id": str(self.item.pk),
            "transactions-0-category": "",
            "transactions-0-tags": [],
            "transactions-0-people": [],
            "transactions-0-comment": "Geburtstagsgeschenk für die Familie",
            "action": "inline_save",
        })

        self.assertRedirects(response, reverse("transaction_overview"))
        self.item.refresh_from_db()
        self.assertEqual(self.item.comment, "Geburtstagsgeschenk für die Familie")

        response = self.client.get(reverse("transaction_overview"), {"q": "Geburtstagsgeschenk"})
        self.assertContains(response, "Beispielmarkt Berlin")
        self.assertContains(response, "Geburtstagsgeschenk für die Familie")

    def test_existing_transaction_tag_can_be_removed_with_checkbox(self):
        tag = Tag.objects.create(name="Entfernbar")
        self.item.tags.add(tag)

        response = self.client.get(reverse("transaction_overview"))

        self.assertContains(response, 'type="checkbox" name="transactions-0-tags"')
        self.assertContains(response, f'value="{tag.pk}" class="tag-toggle-list"')
        self.assertContains(response, 'id="id_transactions-0-tags_0" checked')

        response = self.client.post(reverse("transaction_overview"), {
            "transactions-TOTAL_FORMS": "1",
            "transactions-INITIAL_FORMS": "1",
            "transactions-MIN_NUM_FORMS": "0",
            "transactions-MAX_NUM_FORMS": "1000",
            "transactions-0-id": str(self.item.pk),
            "transactions-0-category": "",
            "transactions-0-tags": [],
            "transactions-0-people": [],
            "transactions-0-comment": "",
            "action": "inline_save",
        })

        self.assertRedirects(response, reverse("transaction_overview"))
        self.assertFalse(self.item.tags.exists())

    def test_transaction_selection_uses_large_checkbox_style(self):
        response = self.client.get(reverse("transaction_overview"))

        self.assertContains(response, "selection-checkbox")
        self.assertContains(response, "width:22px; height:22px")

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

    def test_bulk_action_can_remove_selected_tag(self):
        tag = Tag.objects.create(name="Alt")
        self.item.tags.add(tag)

        response = self.client.post(reverse("transaction_overview"), {
            "transactions-TOTAL_FORMS": "1",
            "transactions-INITIAL_FORMS": "1",
            "transactions-MIN_NUM_FORMS": "0",
            "transactions-MAX_NUM_FORMS": "1000",
            "transactions-0-id": str(self.item.pk),
            "transactions-0-category": "",
            "transactions-0-tags": [str(tag.pk)],
            "transactions-0-people": [],
            "bulk-category": "",
            "bulk-tags": [],
            "bulk-remove_tags": [str(tag.pk)],
            "bulk-people": [],
            "selected": [str(self.item.pk)],
            "action": "bulk",
        })

        self.assertRedirects(response, reverse("transaction_overview"))
        self.assertFalse(self.item.tags.exists())

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

    def test_existing_rule_preview_does_not_change_transaction(self):
        category = Category.objects.create(name="Vorschau")
        CategorizationRule.objects.create(
            name="Markt automatisch", match_text="Beispielmarkt",
            category=category, auto_apply=True,
        )

        response = self.client.post(reverse("manage_classification"), {
            "kind": "apply_rules", "scope": "uncategorized", "action": "preview",
        })

        self.item.refresh_from_db()
        self.assertContains(response, "Vorschau: 1 Buchung(en)")
        self.assertContains(response, "Markt automatisch")
        self.assertIsNone(self.item.category)

    def test_existing_rules_apply_to_uncategorized_transactions_after_confirmation(self):
        category = Category.objects.create(name="Automatisch")
        tag = Tag.objects.create(name="Regeltag")
        person = Person.objects.create(name="Regelperson")
        rule = CategorizationRule.objects.create(
            name="Markt automatisch", match_text="Beispielmarkt",
            category=category, auto_apply=True,
        )
        rule.tags.add(tag)
        rule.people.add(person)

        response = self.client.post(reverse("manage_classification"), {
            "kind": "apply_rules", "scope": "uncategorized", "action": "apply",
        })

        self.item.refresh_from_db()
        rule.refresh_from_db()
        self.assertRedirects(response, reverse("manage_classification"))
        self.assertEqual(self.item.category, category)
        self.assertEqual(self.item.tags.get(), tag)
        self.assertEqual(self.item.people.get(), person)
        self.assertEqual(rule.times_applied, 1)

    def test_rule_matches_umlaut_variants_and_one_pdf_character_error(self):
        category = Category.objects.create(name="Drogerie")
        rule = CategorizationRule.objects.create(
            name="Müller", match_text="Müller Markt", category=category, auto_apply=True,
        )
        self.item.counterparty = "MUELLER MARKT"
        self.item.save(update_fields=["counterparty", "updated_at"])

        self.assertTrue(rule.matches(self.item))

        self.item.counterparty = "M□LLER MARKT"
        self.item.save(update_fields=["counterparty", "updated_at"])
        self.assertTrue(rule.matches(self.item))

    def test_default_rule_application_preserves_existing_category(self):
        existing = Category.objects.create(name="Manuell")
        automatic = Category.objects.create(name="Automatisch")
        self.item.category = existing
        self.item.save(update_fields=["category", "updated_at"])
        CategorizationRule.objects.create(
            name="Markt automatisch", match_text="Beispielmarkt",
            category=automatic, auto_apply=True,
        )

        response = self.client.post(reverse("manage_classification"), {
            "kind": "apply_rules", "scope": "uncategorized", "action": "apply",
        })

        self.item.refresh_from_db()
        self.assertRedirects(response, reverse("manage_classification"))
        self.assertEqual(self.item.category, existing)

    def test_all_scope_replaces_existing_category_using_priority(self):
        existing = Category.objects.create(name="Alt")
        automatic = Category.objects.create(name="Neu")
        self.item.category = existing
        self.item.save(update_fields=["category", "updated_at"])
        CategorizationRule.objects.create(
            name="Markt automatisch", match_text="Beispielmarkt",
            category=automatic, auto_apply=True, priority=200,
        )

        self.client.post(reverse("manage_classification"), {
            "kind": "apply_rules", "scope": "all", "action": "apply",
        })

        self.item.refresh_from_db()
        self.assertEqual(self.item.category, automatic)

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

        self.assertContains(response, "dashboard-brand")
        self.assertNotContains(
            response, "Dokumente erfassen, Buchungen prüfen und Ausgaben zuordnen."
        )
        self.assertContains(response, "Monatsauswertung")
        self.assertContains(response, "42,50 €")
        self.assertContains(response, "Ohne Kategorie")
        self.assertContains(response, "Offene Aufgaben")

    def test_analytics_shows_category_and_person_pie_charts_with_drill_down(self):
        category = Category.objects.create(name="Lebensmittel", color="#22aa44")
        person = Person.objects.create(name="Lennart", color="#663399")
        self.item.category = category
        self.item.save(update_fields=["category", "updated_at"])
        self.item.people.add(person)
        Transaction.objects.create(
            statement_import=self.statement, booking_date=date(2026, 7, 11),
            counterparty="Ohne Zuordnung", amount="-7.50",
            source_fingerprint="1" * 64, reviewed=True,
        )

        response = self.client.get(reverse("analytics"), {"month": "2026-07"})

        self.assertContains(response, "Ausgaben nach Kategorie")
        self.assertContains(response, "Ausgaben nach Person")
        self.assertContains(response, "conic-gradient")
        self.assertContains(response, "Lebensmittel")
        self.assertContains(response, "Lennart")
        self.assertContains(response, "Ohne Kategorie")
        self.assertContains(response, "Ohne Person")
        self.assertContains(response, "50,00 €")
        self.assertContains(response, f"category={category.pk}")
        self.assertContains(response, f"person={person.pk}")

    def test_analytics_can_be_filtered_by_account(self):
        other_account = Account.objects.create(name="Zweites Konto")
        other_document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="analytics-other.pdf",
            file=SimpleUploadedFile("analytics-other.pdf", b"%PDF-analytics-other", "application/pdf"),
        )
        other_statement = StatementImport.objects.create(
            document=other_document, account=other_account, status=StatementImport.Status.IMPORTED,
        )
        Transaction.objects.create(
            statement_import=other_statement, booking_date=date(2026, 7, 12),
            counterparty="Anderes Konto", amount="-100.00",
            source_fingerprint="2" * 64, reviewed=True,
        )

        response = self.client.get(reverse("analytics"), {
            "month": "2026-07", "account": str(self.account.pk),
        })

        self.assertContains(response, "42,50 €")
        self.assertNotContains(response, "142,50 €")

    def test_annual_analytics_shows_monthly_trend_totals_and_drill_downs(self):
        category = Category.objects.create(name="Wohnen", color="#336699")
        self.item.category = category
        self.item.save(update_fields=["category", "updated_at"])
        Transaction.objects.create(
            statement_import=self.statement,
            booking_date=date(2026, 1, 15),
            counterparty="Januarausgabe",
            amount="-100.00",
            category=category,
            source_fingerprint="y" * 64,
            reviewed=True,
        )
        Transaction.objects.create(
            statement_import=self.statement,
            booking_date=date(2026, 3, 1),
            counterparty="Einnahme",
            amount="500.00",
            source_fingerprint="i" * 64,
            reviewed=True,
        )
        Transaction.objects.create(
            statement_import=self.statement,
            booking_date=date(2026, 4, 1),
            counterparty="Umbuchung",
            amount="-250.00",
            source_fingerprint="u" * 64,
            reviewed=True,
            is_internal_transfer=True,
        )

        response = self.client.get(reverse("analytics"), {
            "period": "year", "year": "2026", "month": "2026-07",
        })

        self.assertContains(response, "Jahresüberblick 2026")
        self.assertContains(response, "Monatsverlauf 2026")
        self.assertContains(response, "142,50 €")
        self.assertContains(response, "500,00 €")
        self.assertContains(response, "357,50 €")
        self.assertNotContains(response, "392,50 €")
        self.assertContains(response, "?month=2026-01")
        self.assertContains(response, f"year=2026&amp;category={category.pk}")

    def test_transaction_overview_can_filter_a_whole_year(self):
        Transaction.objects.create(
            statement_import=self.statement,
            booking_date=date(2025, 12, 31),
            counterparty="Vorjahr",
            amount="-10.00",
            source_fingerprint="v" * 64,
            reviewed=True,
        )

        response = self.client.get(reverse("transaction_overview"), {"year": "2026"})

        self.assertContains(response, "Beispielmarkt Berlin")
        self.assertNotContains(response, "Vorjahr")

    def test_open_tasks_shows_uncategorized_count_without_transaction_list(self):
        response = self.client.get(reverse("open_tasks"))

        self.assertContains(response, "Buchungen ohne Kategorie")
        self.assertContains(response, "<strong>1</strong>", html=True)
        self.assertContains(response, "Buchung müssen noch kategorisiert werden")
        self.assertContains(response, "Unkategorisierte Buchungen bearbeiten")
        self.assertNotContains(response, "Beispielmarkt Berlin")

    def test_open_tasks_counts_all_uncategorized_transactions_without_listing_them(self):
        Transaction.objects.bulk_create([
            Transaction(
                statement_import=self.statement,
                booking_date=date(2026, 8, 2),
                counterparty=f"Noch offen {number:02d}",
                amount="-1.00",
                source_fingerprint=f"{number:064x}",
                reviewed=True,
            )
            for number in range(1, 27)
        ])

        response = self.client.get(reverse("open_tasks"))

        self.assertNotContains(response, "Noch offen 01")
        self.assertNotContains(response, "Noch offen 26")
        self.assertContains(response, '<div class="card metric">Ohne Kategorie<strong>27</strong></div>', html=True)

    def test_transaction_overview_defaults_to_30_rows_and_allows_page_size_selection(self):
        Transaction.objects.bulk_create([
            Transaction(
                statement_import=self.statement,
                booking_date=date(2026, 8, 3),
                counterparty=f"Listenbuchung {number:02d}",
                amount="-1.00",
                source_fingerprint=f"{number + 100:064x}",
                reviewed=True,
            )
            for number in range(35)
        ])

        default_response = self.client.get(reverse("transaction_overview"))
        second_page_response = self.client.get(
            reverse("transaction_overview"), {"page": "2"}
        )
        fifty_response = self.client.get(reverse("transaction_overview"), {"per_page": "50"})
        all_response = self.client.get(reverse("transaction_overview"), {"per_page": "all"})

        self.assertEqual(len(default_response.context["formset"].forms), 30)
        self.assertContains(default_response, "1–30 von 36 Buchungen angezeigt")
        self.assertContains(default_response, "Weiter →")
        self.assertContains(default_response, "?page=2")
        self.assertEqual(len(second_page_response.context["formset"].forms), 6)
        self.assertContains(second_page_response, "31–36 von 36 Buchungen angezeigt")
        self.assertContains(second_page_response, "← Zurück")
        self.assertEqual(len(fifty_response.context["formset"].forms), 36)
        self.assertEqual(len(all_response.context["formset"].forms), 36)
        self.assertContains(default_response, ">100</option>", html=False)
        self.assertContains(default_response, ">Alle</option>", html=False)

    def test_learns_suggestion_from_confirmed_normalized_merchant(self):
        category = Category.objects.create(name="Lebensmittel")
        self.item.category = category
        self.item.save(update_fields=["category", "updated_at"])
        for number in range(2):
            Transaction.objects.create(
                statement_import=self.statement,
                booking_date=date(2026, 7, 20 + number),
                counterparty="Beispielmarkt Berlin",
                amount="-10.00",
                category=category,
                source_fingerprint=f"{900 + number:064x}",
                reviewed=True,
            )
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
        self.assertEqual(suggestion.sample_count, 3)
        self.assertContains(
            self.client.get(reverse("transaction_overview")), "3 Vergleichsbuchungen"
        )

    def test_learned_suggestion_requires_at_least_three_comparable_transactions(self):
        category = Category.objects.create(name="Einzelfall")
        self.item.category = category
        self.item.save(update_fields=["category", "updated_at"])
        candidate = Transaction.objects.create(
            statement_import=self.statement,
            booking_date=date(2026, 8, 2),
            counterparty="BEISPIELMARKT BERLIN GMBH",
            amount="-12.00",
            source_fingerprint="l" * 64,
            reviewed=True,
        )

        self.assertIsNone(categorization_suggestion(candidate))

    def test_payment_intermediary_does_not_learn_from_counterparty_alone(self):
        category = Category.objects.create(name="Falscher PayPal-Vorschlag")
        for number in range(3):
            Transaction.objects.create(
                statement_import=self.statement,
                booking_date=date(2026, 7, 20 + number),
                counterparty="PayPal Europe S.a.r.l. et Cie S.C.A",
                description=f"Unterschiedlicher Händler {number}",
                amount="-10.00",
                category=category,
                source_fingerprint=f"{950 + number:064x}",
                reviewed=True,
            )
        candidate = Transaction.objects.create(
            statement_import=self.statement,
            booking_date=date(2026, 8, 2),
            counterparty="PayPal Europe S.a.r.l. et Cie S.C.A",
            description="Noch ein anderer Händler",
            amount="-12.00",
            source_fingerprint="p" * 64,
            reviewed=True,
        )

        self.assertIsNone(categorization_suggestion(candidate))

    def test_explicit_rule_still_proposes_category_for_payment_intermediary(self):
        category = Category.objects.create(name="PayPal-Regel")
        rule = CategorizationRule.objects.create(
            name="PayPal ausdrücklich", match_text="PayPal Europe", category=category,
            auto_apply=False,
        )
        self.item.counterparty = "PayPal Europe S.a.r.l. et Cie S.C.A"
        self.item.save(update_fields=["counterparty", "updated_at"])

        suggestion = categorization_suggestion(self.item)

        self.assertEqual(suggestion.category, category)
        self.assertEqual(suggestion.rule, rule)

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

    def test_rule_management_offers_direct_open_create_form(self):
        response = self.client.get(reverse("manage_classification"), {"new": "rule"})

        self.assertContains(response, "＋ Neue Regel anlegen")
        self.assertContains(
            response,
            '<details class="card create-card" id="new-rule" open>',
        )
        self.assertContains(response, 'name="rule-match_text"')

    def test_rule_can_be_deleted_without_changing_existing_transaction_assignment(self):
        category = Category.objects.create(name="Bleibt zugeordnet")
        self.item.category = category
        self.item.save(update_fields=["category", "updated_at"])
        rule = CategorizationRule.objects.create(
            name="Nicht mehr benötigt",
            match_text="Nicht mehr benötigt",
            category=category,
        )

        response = self.client.post(reverse("delete_rule", args=[rule.pk]))

        self.assertRedirects(response, reverse("manage_classification"))
        self.assertFalse(CategorizationRule.objects.filter(pk=rule.pk).exists())
        self.item.refresh_from_db()
        self.assertEqual(self.item.category, category)

    def test_rule_cannot_be_deleted_via_get(self):
        rule = CategorizationRule.objects.create(
            name="Geschützt", match_text="Geschützt"
        )

        response = self.client.get(reverse("delete_rule", args=[rule.pk]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(CategorizationRule.objects.filter(pk=rule.pk).exists())

    def test_rules_can_be_searched_filtered_and_sorted(self):
        category = Category.objects.create(name="Versicherungen")
        CategorizationRule.objects.create(
            name="Zweite Regel", match_text="Versicherer", category=category,
            priority=20, active=True, auto_apply=True,
        )
        CategorizationRule.objects.create(
            name="Erste Regel", match_text="Anderer Händler",
            priority=10, active=False, auto_apply=False,
        )

        response = self.client.get(reverse("manage_classification"), {
            "q": "Versicherungen",
            "status": "active",
            "mode": "automatic",
            "sort": "name",
        })

        self.assertContains(response, "Zweite Regel")
        self.assertNotContains(response, "Erste Regel")
        self.assertContains(response, "1 von 2")
        self.assertContains(response, "page-title")
        self.assertNotContains(
            response, "Zuordnungen zentral verwalten und Automatisierung nachvollziehbar halten."
        )

        response = self.client.get(reverse("manage_classification"), {
            "sort": "priority_asc",
        })
        self.assertEqual(
            [rule.name for rule in response.context["rules"]],
            ["Erste Regel", "Zweite Regel"],
        )

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

    def test_extracts_congstar_invoice_without_confusing_data_volume_for_total(self):
        text = """22,60 €Gesamtbetrag:
Rechnung für Juli 2026
congstar Kundenservice • Postfach 1165 • 61466 Kronberg
22,60 € Zu zahlender Betrag:
7675138395Rechnungsnummer
Leistung Brutto (EUR)
19 10,08 € 1,92 € 12,00 €
Dein insgesamt verbrauchtes Datenvolumen: 1,56 GB
congstar - eine Marke der Telekom Deutschland GmbH
Netto Steuer Brutto
"""

        self.assertEqual(_parse_merchant(text), "congstar")
        self.assertEqual(_parse_total(text), Decimal("22.60"))
        self.assertEqual(_parse_invoice_number(text), "7675138395")

    def test_generic_total_hint_does_not_match_word_insgesamt(self):
        text = "Dein insgesamt verbrauchtes Datenvolumen: 1,56 GB\nPreis 12,00 €"

        self.assertEqual(_parse_total(text), Decimal("12.00"))

    def test_extracts_amazon_seller_and_textual_invoice_date(self):
        text = """Rechnung
LU-BIO-04
Amazon EU S.à r.l. - 38 avenue John F. Kennedy
Gesamtpreis 6,95 €
Bestelldatum 12 August 2026
Verkauft von Amazon EU S.à r.l., Niederlassung Deutschland
Rechnungsdatum
/Lieferdatum 13 August 2026
Rechnungsnummer DE62RHEQ3AEUI
Zahlbetrag 6,95 €
"""

        self.assertEqual(
            _parse_merchant(text), "Amazon EU S.à r.l., Niederlassung Deutschland"
        )
        self.assertEqual(_parse_date(text), date(2026, 8, 13))
        self.assertEqual(_parse_total(text), Decimal("6.95"))
        self.assertEqual(_parse_invoice_number(text), "DE62RHEQ3AEUI")

    def test_extracts_third_party_seller_from_amazon_invoice(self):
        text = """Amazon.de Rechnung
AMZ-123-ABC
Verkauft von Beispiel Handel GmbH
Zahlbetrag 19,90 €
"""

        self.assertEqual(_parse_merchant(text), "Beispiel Handel GmbH")

    def test_extracts_biller_below_label_instead_of_navigation_text(self):
        text = """Rechnungsempfänger                        Bestellungsübersicht
Alle Bestellungen anzeigen
Bestellnummer: 40321100
In Rechnung gestellt von                    Versandkosten -
Umsatzsteuer _
Faithlife LLC
315 Prospect St #759
Gesamtsumme 7,49 $ USD
"""

        self.assertEqual(_parse_merchant(text), "Faithlife LLC")

    def test_extracts_inline_english_biller(self):
        text = """Invoice
Billed by: Example Software Ltd.
Total 12,00 EUR
"""

        self.assertEqual(_parse_merchant(text), "Example Software Ltd")

    def test_extracts_abbreviated_english_date_with_comma(self):
        self.assertEqual(_parse_date("Invoice date: 14 Aug, 2026"), date(2026, 8, 14))

    def test_layout_prefers_company_below_biller_over_recipient(self):
        words = (
            OCRWord("Rechnungsempfänger", 95, 20, 20, 180, 20, (1, 1, 1, 1)),
            OCRWord("Lennart", 94, 20, 60, 70, 20, (1, 1, 1, 2)),
            OCRWord("Barfod", 94, 95, 60, 70, 20, (1, 1, 1, 2)),
            OCRWord("In", 96, 500, 200, 20, 20, (1, 2, 1, 1)),
            OCRWord("Rechnung", 96, 525, 200, 90, 20, (1, 2, 1, 1)),
            OCRWord("gestellt", 96, 620, 200, 70, 20, (1, 2, 1, 1)),
            OCRWord("von", 96, 695, 200, 35, 20, (1, 2, 1, 1)),
            OCRWord("Faithlife", 93, 500, 245, 90, 20, (1, 2, 1, 2)),
            OCRWord("LLC", 93, 595, 245, 35, 20, (1, 2, 1, 2)),
        )
        content = ExtractedContent(
            "Rechnungsempfänger\nLennart Barfod\nIn Rechnung gestellt von\nFaithlife LLC",
            "image-ocr", "grayscale/psm-11", words,
        )

        merchant, confidence, reasons = _merchant_analysis(content)

        self.assertEqual(merchant, "Faithlife LLC")
        self.assertGreaterEqual(confidence, 90)
        self.assertIn("räumlich beim Feld für den Rechnungssteller", reasons)

    def test_layout_does_not_treat_weight_or_product_text_as_company(self):
        words = (
            OCRWord("BIRNEN", 92, 20, 200, 80, 20, (1, 1, 1, 1)),
            OCRWord("KG-WARE", 92, 105, 200, 80, 20, (1, 1, 1, 1)),
            OCRWord("1,55", 92, 300, 200, 40, 20, (1, 1, 1, 1)),
        )
        content = ExtractedContent("ALDI\nBIRNEN KG-WARE 1,55", "image-ocr", words=words)

        merchant, confidence, _reasons = _merchant_analysis(content)

        self.assertEqual(merchant, "ALDI")
        self.assertEqual(confidence, 90)

    @patch("ledger.views.process_document_task.delay")
    def test_retry_clears_incorrect_extracted_fields_before_reanalysis(self, delay):
        document = Document.objects.create(
            kind=Document.Kind.INVOICE,
            title="congstar Rechnung Juli",
            original_filename="congstar.pdf",
            file=SimpleUploadedFile("congstar.pdf", b"%PDF-congstar", "application/pdf"),
            merchant="Netto Marken-Discount",
            total_amount="1.56",
            invoice_number="Seite",
            extraction_confidence={"merchant": 75, "total_amount": 90},
        )

        response = self.client.post(
            reverse("document_review", args=[document.pk]), {"action": "retry"}
        )

        document.refresh_from_db()
        self.assertRedirects(response, reverse("document_review", args=[document.pk]))
        self.assertEqual(document.merchant, "")
        self.assertIsNone(document.total_amount)
        self.assertEqual(document.invoice_number, "")
        self.assertEqual(document.extraction_confidence, {})
        delay.assert_called_once_with(document.pk)

    @patch("ledger.views.process_document_task.delay")
    def test_retry_clears_automatically_generated_title_with_wrong_merchant(self, delay):
        document = Document.objects.create(
            kind=Document.Kind.INVOICE,
            title="LU-BIO-04",
            original_filename="amazon.pdf",
            file=SimpleUploadedFile("amazon.pdf", b"%PDF-amazon", "application/pdf"),
            merchant="LU-BIO-04",
            total_amount="6.95",
        )

        self.client.post(reverse("document_review", args=[document.pk]), {"action": "retry"})

        document.refresh_from_db()
        self.assertEqual(document.title, "")
        self.assertEqual(document.merchant, "")
        delay.assert_called_once_with(document.pk)

    def test_bank_statement_detail_prioritizes_original_without_transaction_matching(self):
        account = Account.objects.create(name="Vorschaukonto")
        document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT,
            original_filename="kontoauszug-juli.pdf",
            file=SimpleUploadedFile(
                "kontoauszug-juli.pdf", b"%PDF-statement-preview", "application/pdf"
            ),
            processing_status=Document.ProcessingStatus.PROCESSED,
        )
        statement = StatementImport.objects.create(
            document=document,
            account=account,
            status=StatementImport.Status.IMPORTED,
        )
        Transaction.objects.create(
            statement_import=statement,
            booking_date=date(2026, 7, 1),
            counterparty="Testbuchung",
            amount="-10.00",
            source_fingerprint="b" * 64,
            reviewed=True,
        )

        with patch("ledger.views.scored_document_transaction_candidates") as scored:
            response = self.client.get(reverse("document_review", args=[document.pk]))

        scored.assert_not_called()
        self.assertContains(response, "Original-Kontoauszug")
        self.assertContains(response, "Vorschaukonto")
        self.assertContains(response, "PDF herunterladen")
        self.assertContains(response, reverse("document_download", args=[document.pk]))
        self.assertNotContains(response, "Erkannte Angaben")
        self.assertNotContains(response, "Erkannten Text anzeigen")
        self.assertNotContains(response, "Passende Kontobewegungen")

    @patch("ledger.views.process_document_task.delay")
    def test_receipt_upload_queues_background_task(self, delay):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=Path(media_root)):
                response = self.client.post(reverse("upload_document"), {
                    "kind": Document.Kind.RECEIPT,
                    "file": SimpleUploadedFile(
                        "beleg.png", b"\x89PNG\r\n\x1a\npng-test", "image/png"
                    ),
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

    def test_document_is_automatically_linked_for_one_high_confidence_match(self):
        account = Account.objects.create(name="Automatik")
        statement_document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="auto-statement.pdf",
            file=SimpleUploadedFile("auto-statement.pdf", b"%PDF-auto-statement", "application/pdf"),
        )
        statement = StatementImport.objects.create(
            document=statement_document, account=account, status=StatementImport.Status.IMPORTED,
        )
        transaction = Transaction.objects.create(
            statement_import=statement, booking_date=date(2026, 8, 5),
            counterparty="Musterladen Berlin", amount="-12.34",
            source_fingerprint="7" * 64, reviewed=True,
        )
        receipt = Document.objects.create(
            kind=Document.Kind.RECEIPT, original_filename="auto-receipt.pdf",
            file=SimpleUploadedFile("auto-receipt.pdf", b"%PDF-auto-receipt", "application/pdf"),
            document_date=date(2026, 8, 5), merchant="Musterladen", total_amount="12.34",
            processing_status=Document.ProcessingStatus.REVIEW,
        )

        match = auto_match_document(receipt)

        receipt.refresh_from_db()
        self.assertIsNotNone(match)
        self.assertEqual(match.confidence, 90)
        self.assertEqual(receipt.transactions.get(), transaction)
        self.assertEqual(receipt.processing_status, Document.ProcessingStatus.PROCESSED)
        self.assertEqual(receipt.auto_matched_transaction, transaction)
        self.assertEqual(receipt.auto_match_confidence, 90)
        self.assertIn("gleicher Betrag", receipt.auto_match_reasons)
        self.assertIsNotNone(receipt.auto_matched_at)

        response = self.client.get(reverse("automatic_matches"))
        self.assertContains(response, "Musterladen Berlin")
        self.assertContains(response, "90 %")
        self.assertContains(response, "gleicher Betrag")

    def test_automatic_match_can_be_revoked_without_deleting_data(self):
        account = Account.objects.create(name="Widerruf")
        statement_document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="revoke-statement.pdf",
            file=SimpleUploadedFile("revoke-statement.pdf", b"%PDF-revoke-statement", "application/pdf"),
        )
        statement = StatementImport.objects.create(
            document=statement_document, account=account, status=StatementImport.Status.IMPORTED,
        )
        transaction = Transaction.objects.create(
            statement_import=statement, booking_date=date(2026, 8, 6),
            counterparty="Widerrufladen", amount="-15.00",
            source_fingerprint="9" * 64, reviewed=True,
        )
        receipt = Document.objects.create(
            kind=Document.Kind.RECEIPT, original_filename="revoke-receipt.pdf",
            file=SimpleUploadedFile("revoke-receipt.pdf", b"%PDF-revoke-receipt", "application/pdf"),
            document_date=date(2026, 8, 6), merchant="Widerrufladen", total_amount="15.00",
        )
        auto_match_document(receipt)

        response = self.client.post(reverse("revoke_automatic_match", args=[receipt.pk]))

        self.assertRedirects(response, reverse("automatic_matches"))
        receipt.refresh_from_db()
        self.assertTrue(Document.objects.filter(pk=receipt.pk).exists())
        self.assertTrue(Transaction.objects.filter(pk=transaction.pk).exists())
        self.assertFalse(receipt.transactions.exists())
        self.assertIsNone(receipt.auto_matched_transaction)
        self.assertIsNone(receipt.auto_match_confidence)
        self.assertEqual(receipt.auto_match_reasons, [])
        self.assertEqual(receipt.processing_status, Document.ProcessingStatus.REVIEW)

    def test_automatic_match_cannot_be_revoked_via_get(self):
        receipt = Document.objects.create(
            kind=Document.Kind.RECEIPT, original_filename="protected-revoke.pdf",
            file=SimpleUploadedFile("protected-revoke.pdf", b"%PDF-protected-revoke", "application/pdf"),
        )

        response = self.client.get(reverse("revoke_automatic_match", args=[receipt.pk]))

        self.assertEqual(response.status_code, 405)

    def test_ambiguous_document_matches_are_not_linked_automatically(self):
        account = Account.objects.create(name="Mehrdeutig")
        statement_document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="ambiguous.pdf",
            file=SimpleUploadedFile("ambiguous.pdf", b"%PDF-ambiguous", "application/pdf"),
        )
        statement = StatementImport.objects.create(
            document=statement_document, account=account, status=StatementImport.Status.IMPORTED,
        )
        for number in range(2):
            Transaction.objects.create(
                statement_import=statement, booking_date=date(2026, 8, 5),
                counterparty="Musterladen", amount="-12.34",
                source_fingerprint=str(number + 4) * 64, reviewed=True,
            )
        receipt = Document.objects.create(
            kind=Document.Kind.RECEIPT, original_filename="ambiguous-receipt.pdf",
            file=SimpleUploadedFile("ambiguous-receipt.pdf", b"%PDF-ambiguous-receipt", "application/pdf"),
            document_date=date(2026, 8, 5), merchant="Musterladen", total_amount="12.34",
        )

        self.assertIsNone(auto_match_document(receipt))
        self.assertFalse(receipt.transactions.exists())

    def test_document_matching_tolerates_umlaut_variants_when_dates_differ(self):
        account = Account.objects.create(name="Umlaut")
        statement_document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="umlaut-statement.pdf",
            file=SimpleUploadedFile("umlaut-statement.pdf", b"%PDF-umlaut-statement", "application/pdf"),
        )
        statement = StatementImport.objects.create(
            document=statement_document, account=account, status=StatementImport.Status.IMPORTED,
        )
        transaction = Transaction.objects.create(
            statement_import=statement, booking_date=date(2026, 8, 6),
            counterparty="MUELLER MARKT", amount="-23.45",
            source_fingerprint="a" * 64, reviewed=True,
        )
        receipt = Document.objects.create(
            kind=Document.Kind.RECEIPT, original_filename="umlaut-receipt.pdf",
            file=SimpleUploadedFile("umlaut-receipt.pdf", b"%PDF-umlaut-receipt", "application/pdf"),
            document_date=date(2026, 8, 5), merchant="Müller Markt", total_amount="23.45",
        )

        match = auto_match_document(receipt)

        self.assertIsNotNone(match)
        self.assertEqual(match.confidence, 85)
        self.assertIn("passender Händlertext", match.reasons)
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

    def test_archive_filters_invoice_number_date_category_and_link_status(self):
        category = Category.objects.create(name="Büro")
        matching = Document.objects.create(
            kind=Document.Kind.INVOICE, title="Gesuchte Rechnung",
            original_filename="invoice-filter.pdf", invoice_number="RE-4711",
            document_date=date(2026, 8, 3), category=category,
            file=SimpleUploadedFile("invoice-filter.pdf", b"%PDF-filter", "application/pdf"),
        )
        Document.objects.create(
            kind=Document.Kind.INVOICE, title="Andere Rechnung",
            original_filename="other-filter.pdf", invoice_number="RE-9999",
            document_date=date(2026, 7, 3),
            file=SimpleUploadedFile("other-filter.pdf", b"%PDF-other-filter", "application/pdf"),
        )

        response = self.client.get(reverse("document_archive"), {
            "q": "RE-4711", "date_from": "2026-08-01", "date_to": "2026-08-31",
            "category": str(category.pk), "link_status": "unlinked",
        })

        self.assertContains(response, matching.title)
        self.assertNotContains(response, "Andere Rechnung")

    def test_document_review_explains_match_confidence(self):
        account = Account.objects.create(name="Erklärtes Matching")
        statement_document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="explained.pdf",
            file=SimpleUploadedFile("explained.pdf", b"%PDF-explained", "application/pdf"),
        )
        statement = StatementImport.objects.create(
            document=statement_document, account=account, status=StatementImport.Status.IMPORTED,
        )
        Transaction.objects.create(
            statement_import=statement, booking_date=date(2026, 8, 8),
            counterparty="Erklärladen", amount="-21.00",
            source_fingerprint="8" * 64, reviewed=True,
        )
        receipt = Document.objects.create(
            kind=Document.Kind.RECEIPT, original_filename="explained-receipt.pdf",
            file=SimpleUploadedFile("explained-receipt.pdf", b"%PDF-explained-receipt", "application/pdf"),
            document_date=date(2026, 8, 8), merchant="Erklärladen", total_amount="21.00",
            extraction_confidence={"merchant": 75, "document_date": 90, "total_amount": 90},
        )

        response = self.client.get(reverse("document_review", args=[receipt.pk]))

        self.assertContains(response, "90 % passend")
        self.assertContains(response, "gleicher Betrag")
        self.assertContains(response, "Händler: 75 %")


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
