from unittest import TestCase

from tools.sanitize_statement import Sanitizer


class SanitizerTests(TestCase):
    def test_keeps_structure_but_replaces_private_values(self):
        sanitizer = Sanitizer()
        result = sanitizer.text("01.07.2026 Lastschrift Beispiel GmbH -99,10 Referenz: ABC123")

        self.assertIn("01.01.2000", result)
        self.assertIn("Lastschrift", result)
        self.assertIn("-10,00", result)
        self.assertIn("Referenz", result)
        self.assertNotIn("Beispiel", result)
        self.assertNotIn("GmbH", result)
        self.assertNotIn("ABC123", result)

    def test_same_private_word_gets_same_placeholder(self):
        sanitizer = Sanitizer()
        first = sanitizer.text("Geheim")
        second = sanitizer.text("Geheim")

        self.assertEqual(first, second)

