import importlib
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
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
