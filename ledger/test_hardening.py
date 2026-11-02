import importlib
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse


class LoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        get_user_model().objects.create_user("hans", password="correct-horse-battery")

    def login(self, password, username="hans"):
        return self.client.post(
            reverse("login"), {"username": username, "password": password}
        )

    @override_settings(LOGIN_MAX_FAILURES=3)
    def test_locks_after_repeated_failures_even_with_correct_password(self):
        for _ in range(3):
            self.assertEqual(self.login("wrong").status_code, 200)

        response = self.login("correct-horse-battery")

        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Zu viele Fehlversuche", status_code=429)
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(LOGIN_MAX_FAILURES=3)
    def test_success_resets_counter_and_other_users_are_unaffected(self):
        self.login("wrong")
        self.login("wrong")
        self.assertEqual(self.login("correct-horse-battery").status_code, 302)
        self.client.logout()
        for _ in range(3):
            self.login("wrong", username="someone-else")

        self.assertEqual(self.login("correct-horse-battery").status_code, 302)

    @override_settings(LOGIN_MAX_FAILURES=1)
    def test_cache_failure_does_not_block_login(self):
        with patch("ledger.login_throttle.cache") as broken:
            broken.get.side_effect = ConnectionError
            broken.add.side_effect = ConnectionError
            broken.delete.side_effect = ConnectionError
            self.assertEqual(self.login("correct-horse-battery").status_code, 302)


class SettingsHardeningTests(TestCase):
    def load_settings(self, **env):
        base = {"DJANGO_DEBUG": "0", "DJANGO_SECRET_KEY": "x" * 50, "USE_SQLITE": "1"}
        with patch.dict(os.environ, {**base, **env}, clear=True):
            import config.settings as module
            return importlib.reload(module)

    def tearDown(self):
        importlib.reload(importlib.import_module("config.settings"))

    def test_production_requires_email_credential_key(self):
        with self.assertRaisesMessage(RuntimeError, "EMAIL_CREDENTIAL_KEY"):
            self.load_settings()

    def test_production_accepts_explicit_key_and_admin_path(self):
        module = self.load_settings(EMAIL_CREDENTIAL_KEY="k", DJANGO_ADMIN_URL="/geheim/")
        self.assertEqual(module.EMAIL_CREDENTIAL_KEY, "k")
        self.assertEqual(module.ADMIN_URL, "geheim/")

    def test_debug_falls_back_to_secret_key(self):
        module = self.load_settings(DJANGO_DEBUG="1")
        self.assertEqual(module.EMAIL_CREDENTIAL_KEY, module.SECRET_KEY)
        self.assertEqual(module.ADMIN_URL, "admin/")


class AnalyticsFilterChoiceTests(TestCase):
    def setUp(self):
        from datetime import date

        from .models import Account, Document, StatementImport, Transaction

        self.user = get_user_model().objects.create_user("auswerter", password="secret-test-password")
        self.client.force_login(self.user)
        document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="a.pdf",
            file=SimpleUploadedFile("a.pdf", b"%PDF-choices", "application/pdf"),
        )
        statement = StatementImport.objects.create(
            document=document, account=Account.objects.create(name="Giro")
        )
        for index, booking in enumerate([date(2025, 12, 3), date(2026, 3, 9), date(2026, 3, 20)]):
            Transaction.objects.create(
                statement_import=statement, booking_date=booking, amount="-1.00",
                counterparty="X", source_fingerprint=f"{index}" * 64, reviewed=True,
            )
        Transaction.objects.create(
            statement_import=statement, booking_date=date(2026, 5, 1), amount="-1.00",
            counterparty="Ungeprüft", source_fingerprint="9" * 64, reviewed=False,
        )

    def test_dropdowns_list_only_existing_months_and_years_newest_first(self):
        response = self.client.get(reverse("analytics"))

        form = response.context["filters"]
        self.assertEqual([value for value, _ in form.fields["month"].choices], ["2026-03", "2025-12"])
        self.assertEqual([value for value, _ in form.fields["year"].choices], ["2026", "2025"])
        self.assertContains(response, "<select name=\"month\"")
        self.assertContains(response, "März 2026")

    def test_year_selection_is_used(self):
        response = self.client.get(reverse("analytics"), {"period": "year", "year": "2025"})

        self.assertContains(response, "Jahresüberblick 2025")

    def test_empty_database_still_offers_current_month(self):
        from .models import Transaction

        Transaction.objects.all().delete()
        response = self.client.get(reverse("analytics"))

        self.assertEqual(len(response.context["filters"].fields["month"].choices), 1)


class DocumentArchiveQueryTests(TestCase):
    def test_archive_groups_in_database_without_loading_ocr_text(self):
        from datetime import date

        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import Document

        user = get_user_model().objects.create_user("archiv", password="secret-test-password")
        self.client.force_login(user)
        for index in range(4):
            Document.objects.create(
                kind=Document.Kind.INVOICE, original_filename=f"d{index}.pdf",
                document_date=date(2026, 1 + index % 2, 5), extracted_text="GEHEIMER OCR TEXT " * 50,
                file=SimpleUploadedFile(f"d{index}.pdf", f"%PDF-{index}".encode(), "application/pdf"),
            )
        Document.objects.create(
            kind=Document.Kind.OTHER, original_filename="undated.pdf",
            file=SimpleUploadedFile("undated.pdf", b"%PDF-undated", "application/pdf"),
        )

        response = self.client.get(reverse("document_archive"))

        keys = [group["key"] for group in response.context["archive_groups"]]
        self.assertEqual(keys, ["2026_02", "2026_01", "undated"])
        for group in response.context["archive_groups"]:
            for document in group["page"].object_list:
                self.assertIn("extracted_text", document.get_deferred_fields())


class BatchSuggestionTests(TestCase):
    def setUp(self):
        from datetime import date

        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import Account, Category, Document, StatementImport, Transaction

        self.Transaction = Transaction
        self.food = Category.objects.create(name="Essen")
        self.other = Category.objects.create(name="Sonstiges")
        document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="b.pdf",
            file=SimpleUploadedFile("b.pdf", b"%PDF-batch", "application/pdf"),
        )
        self.giro = Account.objects.create(name="Giro")
        self.tages = Account.objects.create(name="Tagesgeld")
        self.statement = StatementImport.objects.create(document=document, account=self.giro)
        other_document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="c.pdf",
            file=SimpleUploadedFile("c.pdf", b"%PDF-batch2", "application/pdf"),
        )
        self.other_statement = StatementImport.objects.create(
            document=other_document, account=self.tages
        )
        self.date = date(2026, 6, 10)
        self.counter = 0

    def make(self, counterparty="Bäckerei Müller", amount="-5.00", category=None,
             statement=None, day_offset=0):
        from datetime import timedelta

        self.counter += 1
        return self.Transaction.objects.create(
            statement_import=statement or self.statement,
            booking_date=self.date + timedelta(days=day_offset), counterparty=counterparty,
            amount=amount, category=category, reviewed=True,
            source_fingerprint=f"{self.counter:064d}",
        )

    def test_batch_equals_single_calls_and_excludes_own_category(self):
        from .rules import categorization_suggestion, categorization_suggestions

        learned = [self.make(category=self.food) for _ in range(3)]
        fresh = self.make()
        unknown = self.make(counterparty="Niemand")

        batch = categorization_suggestions([*learned, fresh, unknown])

        self.assertEqual(batch[fresh.pk].category, self.food)
        self.assertEqual(batch[fresh.pk].sample_count, 3)
        self.assertIsNone(batch[unknown.pk])
        # Zwei übrige Beispiele reichen für eine bereits kategorisierte Buchung nicht.
        self.assertIsNone(batch[learned[0].pk])
        for item in [*learned, fresh, unknown]:
            single = categorization_suggestion(item)
            self.assertEqual(
                (single.category, single.sample_count) if single else None,
                (batch[item.pk].category, batch[item.pk].sample_count) if batch[item.pk] else None,
            )

    def test_suggestions_use_constant_queries(self):
        from .rules import categorization_suggestions

        for _ in range(3):
            self.make(category=self.food)
        items = [self.make() for _ in range(20)]

        with self.assertNumQueries(3):
            categorization_suggestions(items)

    def test_batch_transfer_candidates_match_single_lookup(self):
        from .internal_transfers import best_transfer_candidate, best_transfer_candidates

        out = self.make(counterparty="Umbuchung", amount="-100.00")
        near = self.make(counterparty="Gegen", amount="100.00", statement=self.other_statement, day_offset=2)
        self.make(counterparty="Zu weit", amount="100.00", statement=self.other_statement, day_offset=9)
        self.make(counterparty="Gleiches Konto", amount="100.00", day_offset=1)

        batch = best_transfer_candidates([out, near])

        self.assertEqual(batch[out.pk], near)
        self.assertEqual(batch[near.pk], out)
        self.assertEqual(best_transfer_candidate(out), near)
        with self.assertNumQueries(1):
            best_transfer_candidates([out, near])
