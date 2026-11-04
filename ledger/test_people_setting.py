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
