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


    def test_short_dates_and_debit_credit_markers_keep_their_structure(self):
        sanitizer = Sanitizer()
        result = sanitizer.text("01.07. 02.07.26 Überweisung Max Mustermann 1.234,56 S Kontostand 12,34 H")

        self.assertEqual(result.count("01.01."), 2)
        self.assertIn("01.01.00", result)
        self.assertIn("Überweisung", result)
        self.assertIn("Kontostand", result)
        self.assertIn("10,00 S", result)
        self.assertIn("10,00 H", result)
        self.assertNotIn("Mustermann", result)
        self.assertNotIn("1.234", result)
        self.assertNotIn("07", result)

    def test_numbers_inside_words_are_not_mistaken_for_dates(self):
        sanitizer = Sanitizer()
        result = sanitizer.text("Ref 12.34.5678X")

        self.assertNotIn("5678", result)
        self.assertNotIn("12.34", result)
