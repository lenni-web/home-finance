from datetime import date

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .forms import (
    BulkCategorizationForm, CategorizationRuleForm, DocumentArchiveFilterForm, DocumentReviewForm,
    TransactionCategorizationForm, TransactionFilterForm,
)
from .models import (
    Account, AppSettings, CategorizationRule, Category, Document, Person, StatementImport, Transaction,
)
from .rules import apply_categorization_rules, apply_rule_application_plan, build_rule_application_plan


class PeopleSettingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("personen", password="secret-test-password")
        self.client.force_login(self.user)
        self.anna = Person.objects.create(name="Anna Beispiel", color="#336699")
        self.food = Category.objects.create(name="Essen")
        document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="p.pdf",
            file=SimpleUploadedFile("p.pdf", b"%PDF-people", "application/pdf"),
        )
        statement = StatementImport.objects.create(
            document=document, account=Account.objects.create(name="Giro")
        )
        self.booking = Transaction.objects.create(
            statement_import=statement, booking_date=date(2026, 6, 5), amount="-50.00",
            counterparty="Supermarkt", category=self.food, reviewed=True,
            source_fingerprint="p" * 64,
        )
        self.booking.people.add(self.anna)
        self.invoice = Document.objects.create(
            kind=Document.Kind.INVOICE, original_filename="r.pdf", document_date=date(2026, 6, 5),
            file=SimpleUploadedFile("r.pdf", b"%PDF-rechnung", "application/pdf"),
        )
        self.invoice.people.add(self.anna)

    def disable(self):
        AppSettings.objects.update_or_create(pk=1, defaults={"people_enabled": False})

    def test_enabled_by_default_and_everything_is_visible(self):
        self.assertTrue(AppSettings.current().people_enabled)

        analytics = self.client.get(reverse("analytics"), {"month": "2026-06"})
        transactions = self.client.get(reverse("transaction_overview"))
        classification = self.client.get(reverse("manage_classification"))

        self.assertContains(analytics, "Ausgaben nach Person")
        self.assertContains(transactions, "<th>Personen</th>")
        self.assertContains(transactions, 'name="person"')
        self.assertContains(classification, 'id="people"')
        self.assertContains(classification, "Person anlegen")

    def test_disabled_hides_people_on_every_overview(self):
        self.disable()

        analytics = self.client.get(reverse("analytics"), {"month": "2026-06"})
        transactions = self.client.get(reverse("transaction_overview"))
        archive = self.client.get(reverse("document_archive"))
        classification = self.client.get(reverse("manage_classification"))

        self.assertNotContains(analytics, "Ausgaben nach Person")
        self.assertContains(analytics, "Ausgaben nach Kategorie")
        self.assertNotContains(transactions, "<th>Personen</th>")
        self.assertNotContains(transactions, 'name="person"')
        self.assertNotContains(transactions, 'name="bulk-people"')
        self.assertNotContains(archive, 'name="person"')
        self.assertNotContains(archive, "Anna Beispiel")
        self.assertNotContains(classification, 'id="people"')
        self.assertNotContains(classification, "Person anlegen")
        self.assertNotContains(classification, "Anna Beispiel")

    def test_forms_drop_the_people_fields_only_while_disabled(self):
        forms = [
            lambda: TransactionFilterForm(), lambda: DocumentArchiveFilterForm(),
            lambda: TransactionCategorizationForm(instance=self.booking),
            lambda: BulkCategorizationForm(), lambda: CategorizationRuleForm(),
            lambda: DocumentReviewForm(instance=self.invoice),
        ]
        names = ("person", "people")
        self.assertTrue(all(any(n in make().fields for n in names) for make in forms))

        self.disable()

        self.assertFalse(any(any(n in make().fields for n in names) for make in forms))

    def test_existing_assignments_survive_saving_while_disabled(self):
        self.disable()
        review = DocumentReviewForm(
            {"title": "Neu", "document_date": "2026-06-05", "total_amount": "9.99"},
            instance=self.invoice,
        )
        self.assertTrue(review.is_valid(), review.errors)
        review.save()
        form = TransactionCategorizationForm(
            {"category": self.food.pk, "comment": "ok"}, instance=self.booking
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.assertEqual(list(self.invoice.people.all()), [self.anna])
        self.assertEqual(list(self.booking.people.all()), [self.anna])

    def test_bulk_action_and_person_creation_work_without_people(self):
        self.disable()

        response = self.client.post(reverse("manage_classification"), {
            "kind": "person", "person-name": "Heimlich", "person-color": "#000000",
        })
        self.assertFalse(Person.objects.filter(name="Heimlich").exists())
        self.assertEqual(response.status_code, 200)
        for kind_url in (
            reverse("edit_classification", args=["person", self.anna.pk]),
            reverse("toggle_classification", args=["person", self.anna.pk]),
        ):
            self.client.post(kind_url, {"name": "X", "color": "#000000"})
        self.anna.refresh_from_db()
        self.assertEqual((self.anna.name, self.anna.active), ("Anna Beispiel", True))

    def test_rules_do_not_assign_people_while_disabled(self):
        rule = CategorizationRule.objects.create(name="Regel", match_text="Laden", category=self.food)
        rule.people.add(self.anna)
        statement = self.booking.statement_import
        fresh = Transaction.objects.create(
            statement_import=statement, booking_date=date(2026, 6, 6), amount="-5.00",
            counterparty="Laden", source_fingerprint="q" * 64, reviewed=True,
        )
        self.disable()

        apply_categorization_rules(fresh)
        plan = build_rule_application_plan("all")
        apply_rule_application_plan(plan)

        self.assertEqual(fresh.people.count(), 0)
        self.assertEqual(list(self.booking.people.all()), [self.anna])

        AppSettings.objects.filter(pk=1).update(people_enabled=True)
        apply_rule_application_plan(build_rule_application_plan("all"))
        self.assertEqual(list(fresh.people.all()), [self.anna])

    def test_toggle_switches_the_setting_and_keeps_the_data(self):
        url = reverse("toggle_people_assignment")

        response = self.client.post(url, {"next": reverse("settings")}, follow=True)
        self.assertFalse(AppSettings.current().people_enabled)
        self.assertContains(response, "Personen-Zuordnung einschalten")
        self.assertContains(response, "nur ausgeblendet")
        self.assertTrue(Person.objects.filter(pk=self.anna.pk).exists())

        self.client.post(url)
        self.assertTrue(AppSettings.current().people_enabled)
        self.assertContains(
            self.client.get(reverse("transaction_overview")), "Anna Beispiel"
        )

    def test_toggle_requires_post_login_and_local_redirects(self):
        url = reverse("toggle_people_assignment")
        self.assertEqual(self.client.get(url).status_code, 405)

        response = self.client.post(url, {"next": "https://evil.example/"})
        self.assertRedirects(response, reverse("settings"), fetch_redirect_response=False)

        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(AppSettings.current().people_enabled)  # nur der eine Umschaltvorgang


class IncomeOverviewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("eingaenge", password="secret-test-password")
        self.client.force_login(self.user)
        document = Document.objects.create(
            kind=Document.Kind.BANK_STATEMENT, original_filename="e.pdf",
            file=SimpleUploadedFile("e.pdf", b"%PDF-income", "application/pdf"),
        )
        self.account = Account.objects.create(name="Giro")
        self.other_account = Account.objects.create(name="Tagesgeld")
        self.statement = StatementImport.objects.create(document=document, account=self.account)
        self.counter = 0

    def book(self, amount, counterparty, *, day=date(2026, 6, 10), reviewed=True, transfer=False,
             statement=None):
        self.counter += 1
        return Transaction.objects.create(
            statement_import=statement or self.statement, booking_date=day, amount=amount,
            counterparty=counterparty, reviewed=reviewed, is_internal_transfer=transfer,
            source_fingerprint=f"{self.counter:064d}",
        )

    def get(self, **params):
        params.setdefault("month", "2026-06")
        return self.client.get(reverse("analytics"), params).context["income_sources"]

    def test_groups_by_sender_largest_first_and_ignores_transfers_and_expenses(self):
        self.book("2500.00", "Arbeitgeber AG")
        self.book("2000.00", "Arbeitgeber AG", day=date(2026, 6, 15))
        self.book("500.00", "ARBEITGEBER AG", day=date(2026, 6, 20))  # gleicher Absender
        self.book("100.00", "Max Mustermann")
        self.book("-80.00", "Supermarkt")  # Ausgabe
        self.book("999.00", "Umbuchung", transfer=True)
        self.book("55.00", "Ungeprüft", reviewed=False)
        self.book("700.00", "Altmonat", day=date(2026, 5, 31))

        income = self.get()

        self.assertEqual([item["name"] for item in income["items"]], ["Arbeitgeber AG", "Max Mustermann"])
        first, second = income["items"]
        self.assertEqual((first["amount"], first["count"], first["percent"]), (5000, 3, 98.0))
        self.assertEqual((second["amount"], second["percent"], second["bar_percent"]), (100, 2.0, 2))
        self.assertEqual((income["total"], income["count"], income["source_count"]), (5100, 4, 2))
        self.assertIsNone(income["other"])

    def test_long_tails_are_summarised_and_drill_down_links_keep_the_filters(self):
        names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta", "Iota",
                 "Kappa", "Lambda"]
        for index, name in enumerate(names):
            self.book(f"{100 - index}.00", name)

        income = self.get(account=str(self.account.pk))

        self.assertEqual(len(income["items"]), 8)
        self.assertEqual(income["other"]["name"], "3 weitere Absender")
        self.assertEqual(income["other"]["count"], 3)
        url = income["items"][0]["drill_url"]
        self.assertIn("month=2026-06", url)
        self.assertIn(f"account={self.account.pk}", url)
        self.assertIn("q=Alpha", url)
        self.assertIn("direction=income", url)

    def test_account_filter_year_view_and_empty_state(self):
        self.book("40.00", "Giro-Quelle")
        other = StatementImport.objects.create(
            document=Document.objects.create(
                kind=Document.Kind.BANK_STATEMENT, original_filename="t.pdf",
                file=SimpleUploadedFile("t.pdf", b"%PDF-tages", "application/pdf"),
            ),
            account=self.other_account,
        )
        self.book("60.00", "Tagesgeld-Quelle", statement=other)

        only_giro = self.get(account=str(self.account.pk))
        self.assertEqual([item["name"] for item in only_giro["items"]], ["Giro-Quelle"])

        year = self.client.get(reverse("analytics"), {"period": "year", "year": "2026"})
        self.assertEqual(year.context["income_sources"]["count"], 2)
        self.assertContains(year, "class=\"income\">+")

        empty = self.client.get(reverse("analytics"), {"month": "2020-01"})
        self.assertContains(empty, "Keine Eingänge im gewählten Zeitraum.")

    def test_page_shows_the_section_with_formatted_amounts(self):
        self.book("1234.50", "Arbeitgeber AG")
        self.book("10.00", "")

        page = self.client.get(reverse("analytics"), {"month": "2026-06"})

        self.assertContains(page, "<h2>Eingänge nach Absender</h2>")
        self.assertContains(page, "2 Eingänge von 2 Absendern")
        self.assertContains(page, "1.234,50 €")
        self.assertContains(page, "Ohne Absender")

    def test_presentation_mode_shows_only_percentages_for_income(self):
        self.book("1234.50", "Arbeitgeber AG")
        self.book("-100.00", "Laden")
        AppSettings.objects.update_or_create(pk=1, defaults={"presentation_mode": True})

        html = self.client.get(reverse("analytics"), {"month": "2026-06"}).content.decode()

        self.assertIn("Eingänge", html)
        self.assertNotIn("1.234,50", html)
        self.assertNotIn("1234.50", html)


    def test_income_by_category_links_to_incoming_bookings_only(self):
        wages = Category.objects.create(name="Gehalt", color="#336699")
        item = self.book("3000.00", "Arbeitgeber AG")
        item.category = wages
        item.save()
        self.book("1000.00", "Sonstiger Absender")
        self.book("-500.00", "Laden")

        page = self.client.get(reverse("analytics"), {"month": "2026-06"})

        self.assertContains(page, "<h2>Einnahmen nach Kategorie</h2>")
        items = page.context["income_category_items"]
        self.assertEqual([(entry["name"], entry["percent"]) for entry in items],
                         [("Gehalt", 75.0), ("Ohne Kategorie", 25.0)])
        self.assertIn("direction=income", items[0]["drill_url"])
        self.assertIn(f"category={wages.pk}", items[0]["drill_url"])
        self.assertIn("uncategorized=on", items[1]["drill_url"])

    def test_transaction_filter_direction_separates_income_from_expenses(self):
        self.book("100.00", "Eingang")
        self.book("-40.00", "Ausgabe")

        income = self.client.get(reverse("transaction_overview"), {"direction": "income"})
        expense = self.client.get(reverse("transaction_overview"), {"direction": "expense"})
        both = self.client.get(reverse("transaction_overview"))

        self.assertEqual(income.context["transaction_count"], 1)
        self.assertContains(income, "Eingang")
        self.assertNotContains(income, "Ausgabe</")
        self.assertEqual(expense.context["transaction_count"], 1)
        self.assertEqual(both.context["transaction_count"], 2)
