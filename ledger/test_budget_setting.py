from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import AppSettings, Budget, Category


class BudgetSettingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("budgetwahl", password="secret-test-password")
        self.client.force_login(self.user)
        self.food = Category.objects.create(name="Essen")
        self.budget = Budget.objects.create(category=self.food, monthly_amount="300.00")

    def disable(self):
        AppSettings.objects.update_or_create(pk=1, defaults={"budgets_enabled": False})

    def test_enabled_by_default_with_menu_entry_and_page(self):
        self.assertTrue(AppSettings.current().budgets_enabled)

        dashboard = self.client.get(reverse("dashboard"))
        page = self.client.get(reverse("budgets"))

        self.assertContains(dashboard, reverse("budgets"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Essen")

    def test_disabled_hides_the_menu_entries_on_every_page(self):
        self.disable()

        for name in ("dashboard", "analytics", "transaction_overview", "settings"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertNotIn(f'href="{reverse("budgets")}"', html, name)

    def test_disabled_blocks_the_budget_pages_with_a_hint(self):
        self.disable()

        response = self.client.get(reverse("budgets"), follow=True)
        self.assertRedirects(response, reverse("dashboard"))
        self.assertContains(response, "Budget-Funktion ist ausgeschaltet")

        saved = self.client.post(
            reverse("budgets"), {"category": self.food.pk, "monthly_amount": "999,00"}
        )
        self.assertRedirects(saved, reverse("dashboard"), fetch_redirect_response=False)
        deleted = self.client.post(reverse("delete_budget", args=[self.budget.pk]))
        self.assertRedirects(deleted, reverse("dashboard"), fetch_redirect_response=False)
        self.budget.refresh_from_db()
        self.assertEqual(str(self.budget.monthly_amount), "300.00")

    def test_budgets_survive_switching_off_and_on(self):
        url = reverse("toggle_budgets")

        response = self.client.post(url, {"next": reverse("settings")}, follow=True)
        self.assertFalse(AppSettings.current().budgets_enabled)
        self.assertContains(response, "Budgets einschalten")
        self.assertContains(response, "Bestehende Budgets bleiben erhalten")
        self.assertTrue(Budget.objects.filter(pk=self.budget.pk).exists())

        self.client.post(url)
        self.assertTrue(AppSettings.current().budgets_enabled)
        self.assertContains(self.client.get(reverse("budgets")), "Essen")

    def test_toggle_requires_post_login_and_local_redirects(self):
        url = reverse("toggle_budgets")
        self.assertEqual(self.client.get(url).status_code, 405)

        response = self.client.post(url, {"next": "https://evil.example/"})
        self.assertRedirects(response, reverse("settings"), fetch_redirect_response=False)
        self.assertFalse(AppSettings.current().budgets_enabled)

        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(AppSettings.current().budgets_enabled)  # nur der eine Umschaltvorgang

    def test_settings_are_independent_of_each_other(self):
        self.disable()

        settings_row = AppSettings.current()
        self.assertTrue(settings_row.people_enabled)
        self.assertFalse(settings_row.presentation_mode)
