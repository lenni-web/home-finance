from datetime import date

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import Account, CategorizationRule, Document, PayeeName, StatementImport, Transaction
from .payees import apply_split, candidate_keys, learn_payee, match_length
from .test_sparkasse_parser import build_pdf


def keys(*names):
    from .text_normalization import normalize_comparison_text

    return sorted((normalize_comparison_text(name).split() for name in names), key=lambda w: -len(w))


class SplitTests(TestCase):
    def test_splits_at_known_name_ignoring_case_umlauts_and_punctuation(self):
        candidates = keys("Müller & Söhne GmbH")

        name, subject = apply_split(
            "MUELLER & SOEHNE GMBH, Rechnung 4711", "Kundennr 12\nDanke", candidates
        )

        self.assertEqual(name, "MUELLER & SOEHNE GMBH")
        self.assertEqual(subject, "Rechnung 4711 Kundennr 12 Danke")

    def test_longest_known_name_wins(self):
        candidates = keys("Rewe", "Rewe Markt GmbH")

        name, subject = apply_split("REWE Markt GmbH Einkauf 12", "", candidates)

        self.assertEqual((name, subject), ("REWE Markt GmbH", "Einkauf 12"))

    def test_only_splits_on_word_boundaries(self):
        candidates = keys("Rewe")

        self.assertEqual(apply_split("Rewe/Köln Einkauf", "", candidates), ("Rewe/Köln Einkauf", ""))
        self.assertEqual(apply_split("Rewerk GmbH Einkauf", "", candidates), ("Rewerk GmbH Einkauf", ""))
        self.assertIsNone(match_length("Köln Rewe", ["rewe"]))

    def test_name_may_be_the_whole_text_and_separators_are_dropped(self):
        candidates = keys("Mustermann")

        self.assertEqual(apply_split("Mustermann", "", candidates), ("Mustermann", ""))
        self.assertEqual(
            apply_split("Mustermann, Hans / Miete", "", candidates), ("Mustermann", "Hans / Miete")
        )

    def test_unknown_text_is_left_unchanged(self):
        self.assertEqual(
            apply_split("Unbekannt AG Rechnung 1", "Zeile 2", keys("Rewe")),
            ("Unbekannt AG Rechnung 1", "Zeile 2"),
        )

    def test_technical_details_move_out_of_the_name(self):
        name, subject = apply_split(
            "Stadtwerke Strom Abschlag Gläubiger-ID:", "DE98ZZZ09999999999", []
        )

        self.assertEqual(name, "Stadtwerke Strom Abschlag")
        self.assertEqual(subject, "Gläubiger-ID:\nDE98ZZZ09999999999")
        # wiederholte Anwendung verändert nichts mehr
        self.assertEqual(apply_split(name, subject, []), (name, subject))

    def test_candidates_include_learned_names_and_active_rules_only(self):
        PayeeName.objects.create(name="Vermieter Muster", key="vermieter muster")
        CategorizationRule.objects.create(name="Rewe", match_text="REWE")
        CategorizationRule.objects.create(name="Alt", match_text="Altlast", active=False)
        CategorizationRule.objects.create(name="Kurz", match_text="ab")

        found = candidate_keys()

        self.assertIn(["vermieter", "muster"], found)
        self.assertIn(["rewe"], found)
        self.assertNotIn(["altlast"], found)
        self.assertNotIn(["ab"], found)  # zu kurz
        self.assertEqual(found[0], ["vermieter", "muster"])  # längster zuerst


class LearnTests(TestCase):
    def test_learns_a_proper_prefix_and_returns_the_subject(self):
        result = learn_payee("Rewe Markt GmbH Einkauf Nr. 12", "Mandat: 5", "Rewe Markt GmbH")

        self.assertEqual(result, ("Einkauf Nr. 12 Mandat: 5", "Rewe Markt GmbH"))
        self.assertEqual(PayeeName.objects.get().key, "rewe markt gmbh")

    def test_does_not_learn_reworded_whole_or_tiny_names(self):
        self.assertIsNone(learn_payee("Rewe Markt Einkauf", "", "Supermarkt"))
        self.assertIsNone(learn_payee("Rewe Markt", "", "Rewe Markt"))  # nichts zu trennen
        self.assertIsNone(learn_payee("Ab Einkauf", "", "Ab"))
        self.assertFalse(PayeeName.objects.exists())

    def test_learning_the_same_name_twice_keeps_one_entry(self):
        learn_payee("Rewe Einkauf 1", "", "Rewe")
        learn_payee("REWE Einkauf 2", "", "REWE")

        self.assertEqual(PayeeName.objects.count(), 1)


def make_statement(parser_name="sparkasse_pdf"):
    document = Document.objects.create(
        kind=Document.Kind.BANK_STATEMENT, original_filename="x.pdf",
        file=SimpleUploadedFile("x.pdf", f"%PDF-{parser_name}".encode(), "application/pdf"),
    )
    account = Account.objects.create(name="Giro", import_format=parser_name)
    return StatementImport.objects.create(
        document=document, account=account, parser_name=parser_name,
        status=StatementImport.Status.REVIEW,
    )


def add_transactions(statement, texts):
    created = []
    for index, (counterparty, description) in enumerate(texts):
        created.append(Transaction.objects.create(
            statement_import=statement, booking_date=date(2026, 10, 1 + index), amount="-10.00",
            counterparty=counterparty, description=description,
            source_fingerprint=f"{statement.pk}{index}".ljust(64, "x"),
        ))
    return created


def post_data(statement, overrides=None):
    """Formset-POST wie ihn der Browser aus den aktuellen Zeilen erzeugt."""
    rows = list(statement.transactions.order_by("source_page", "id"))
    data = {
        "transactions-TOTAL_FORMS": len(rows), "transactions-INITIAL_FORMS": len(rows),
        "transactions-MIN_NUM_FORMS": 0, "transactions-MAX_NUM_FORMS": 1000,
        "action": "save",
    }
    for index, row in enumerate(rows):
        values = {
            "id": row.pk, "booking_date": row.booking_date.isoformat(),
            "value_date": "", "booking_type": row.booking_type,
            "counterparty": row.counterparty, "description": row.description,
            "amount": str(row.amount), "category": "",
        }
        values.update((overrides or {}).get(row.pk, {}))
        for field, value in values.items():
            data[f"transactions-{index}-{field}"] = value
    return data


class StatementReviewLearningTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("lerner", password="secret-test-password")
        self.client.force_login(self.user)
        self.statement = make_statement()
        self.first, self.second, self.third, self.other = add_transactions(self.statement, [
            ("Rewe Markt GmbH Einkauf Nr. 1 Gläubiger-ID:", "DE98ZZZ09999999999"),
            ("REWE MARKT GMBH Einkauf Nr. 2", ""),
            ("Rewe Markt GmbH Einkauf Nr. 3", "Zeile 2"),
            ("Unbekannter Laden Einkauf", ""),
        ])
        self.url = reverse("statement_review", args=[self.statement.pk])

    def test_shortening_the_name_learns_it_and_splits_the_other_pending_rows(self):
        response = self.client.post(
            self.url, post_data(self.statement, {self.first.pk: {"counterparty": "Rewe Markt GmbH"}}),
            follow=True,
        )

        self.assertContains(response, "Zahlungspartner gemerkt")
        self.assertEqual(PayeeName.objects.get().name, "Rewe Markt GmbH")
        for item in (self.first, self.second, self.third, self.other):
            item.refresh_from_db()
        self.assertEqual(self.first.counterparty, "Rewe Markt GmbH")
        self.assertEqual(
            self.first.description, "Einkauf Nr. 1 Gläubiger-ID: DE98ZZZ09999999999"
        )
        self.assertEqual((self.second.counterparty, self.second.description),
                         ("REWE MARKT GMBH", "Einkauf Nr. 2"))
        self.assertEqual((self.third.counterparty, self.third.description),
                         ("Rewe Markt GmbH", "Einkauf Nr. 3 Zeile 2"))
        self.assertEqual(self.other.counterparty, "Unbekannter Laden Einkauf")

    def test_an_edited_description_is_respected(self):
        self.client.post(self.url, post_data(self.statement, {
            self.first.pk: {"counterparty": "Rewe Markt GmbH", "description": "Meine Notiz"},
        }))

        self.first.refresh_from_db()
        self.assertEqual((self.first.counterparty, self.first.description),
                         ("Rewe Markt GmbH", "Meine Notiz"))
        self.assertTrue(PayeeName.objects.exists())

    def test_reworded_partner_is_saved_but_not_learned(self):
        self.client.post(
            self.url, post_data(self.statement, {self.first.pk: {"counterparty": "Supermarkt"}})
        )

        self.first.refresh_from_db()
        self.assertEqual(self.first.counterparty, "Supermarkt")
        self.assertFalse(PayeeName.objects.exists())

    def test_confirmed_rows_are_not_touched_by_propagation(self):
        Transaction.objects.filter(pk=self.third.pk).update(reviewed=True)

        self.client.post(
            self.url, post_data(self.statement, {self.first.pk: {"counterparty": "Rewe Markt GmbH"}})
        )

        self.third.refresh_from_db()
        self.assertEqual(self.third.counterparty, "Rewe Markt GmbH Einkauf Nr. 3")

    def test_formats_that_already_separate_the_partner_do_not_learn(self):
        ing = make_statement("ing_de_pdf")
        row, = add_transactions(ing, [("Rewe Markt GmbH Einkauf", "")])

        self.client.post(
            reverse("statement_review", args=[ing.pk]),
            post_data(ing, {row.pk: {"counterparty": "Rewe Markt GmbH"}}),
        )

        self.assertFalse(PayeeName.objects.exists())


class EmptyDescriptionTests(TestCase):
    def test_review_can_save_and_confirm_rows_without_a_description(self):
        user = get_user_model().objects.create_user("leer", password="secret-test-password")
        self.client.force_login(user)
        statement = make_statement("ing_de_pdf")
        add_transactions(statement, [("Nur ein Name", ""), ("Anderer Name", "")])

        data = post_data(statement)
        data["action"] = "confirm"
        response = self.client.post(reverse("statement_review", args=[statement.pk]), data)

        self.assertEqual(response.status_code, 302)
        statement.refresh_from_db()
        self.assertEqual(statement.status, StatementImport.Status.IMPORTED)
        self.assertEqual(statement.transactions.filter(reviewed=True).count(), 2)


class ImportSplitTests(TestCase):
    def test_import_splits_known_names_and_keeps_unknown_text(self):
        from .statement_processing import process_statement_import

        page = [
            (70.87, 496.39, "Datum Erlaeuterung Betrag EUR"),
            (123.05, 481.5, "Kontostand am 30.09.2026    100,00"),
            (69.93, 467.77, "01.10.2026 Lastschrift"),
            (123.34, 457.05, "Rewe Markt GmbH Einkauf Nr. 5 Glaeubiger"),
            (504.2, 467.77, "-40,00"),
            (69.93, 425.57, "02.10.2026 Lastschrift"),
            (123.34, 414.85, "Unbekannter Laden Einkauf"),
            (504.2, 425.57, "-20,00"),
            (123.05, 327.99, "Kontostand am 31.10.2026    40,00"),
        ]
        PayeeName.objects.create(name="Rewe Markt GmbH", key="rewe markt gmbh")
        statement = make_statement()
        statement.document.file.save("auszug.pdf", SimpleUploadedFile("auszug.pdf", build_pdf([page])))
        statement.status = StatementImport.Status.UPLOADED
        statement.save()

        process_statement_import(statement)

        known, unknown = statement.transactions.order_by("booking_date")
        self.assertEqual((known.counterparty, known.description),
                         ("Rewe Markt GmbH", "Einkauf Nr. 5 Glaeubiger"))
        self.assertEqual((unknown.counterparty, unknown.description),
                         ("Unbekannter Laden Einkauf", ""))


class PayeeManagementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("verwalter", password="secret-test-password")
        self.client.force_login(self.user)
        self.payee = PayeeName.objects.create(name="Rewe Markt GmbH", key="rewe markt gmbh")

    def test_list_and_forget(self):
        page = self.client.get(reverse("manage_classification"))
        self.assertContains(page, "Gelernte Zahlungspartner")
        self.assertContains(page, "Rewe Markt GmbH")

        url = reverse("delete_payee_name", args=[self.payee.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        self.assertFalse(PayeeName.objects.exists())

        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)


class DeleteUnprocessedDocumentTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("loescher", password="secret-test-password")
        self.client.force_login(self.user)

    def invoice(self, status, name="beleg.pdf"):
        return Document.objects.create(
            kind=Document.Kind.INVOICE, original_filename=name, processing_status=status,
            file=SimpleUploadedFile(name, f"%PDF-{status}".encode(), "application/pdf"),
        )

    def test_every_status_except_processed_can_be_deleted_with_its_file(self):
        for status in (Document.ProcessingStatus.PENDING, Document.ProcessingStatus.PROCESSING,
                       Document.ProcessingStatus.REVIEW, Document.ProcessingStatus.FAILED):
            document = self.invoice(status)
            storage, file_name = document.file.storage, document.file.name

            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("delete_document", args=[document.pk]))

            self.assertRedirects(response, reverse("document_archive"))
            self.assertFalse(Document.objects.filter(pk=document.pk).exists(), status)
            self.assertFalse(storage.exists(file_name), status)

    def test_processed_documents_are_kept(self):
        document = self.invoice(Document.ProcessingStatus.PROCESSED)

        response = self.client.post(reverse("delete_document", args=[document.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())

    def test_auto_matched_review_document_is_deleted_without_touching_the_booking(self):
        statement = make_statement()
        statement.status = StatementImport.Status.IMPORTED
        statement.save()
        booking, = add_transactions(statement, [("Shop", "")])
        Transaction.objects.filter(pk=booking.pk).update(reviewed=True)
        document = self.invoice(Document.ProcessingStatus.REVIEW)
        document.transactions.add(booking)
        document.auto_matched_transaction = booking
        document.save()

        self.client.post(reverse("delete_document", args=[document.pk]))

        self.assertFalse(Document.objects.filter(pk=document.pk).exists())
        self.assertTrue(Transaction.objects.filter(pk=booking.pk).exists())

    def test_statement_under_review_is_deleted_with_its_pending_transactions(self):
        statement = make_statement()
        rows = add_transactions(statement, [("A", "x"), ("B", "y")])
        document_pk, statement_pk = statement.document.pk, statement.pk
        storage, file_name = statement.document.file.storage, statement.document.file.name

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("delete_document", args=[document_pk]))

        self.assertRedirects(response, reverse("document_archive"))
        self.assertFalse(StatementImport.objects.filter(pk=statement_pk).exists())
        self.assertFalse(Transaction.objects.filter(pk__in=[row.pk for row in rows]).exists())
        self.assertFalse(storage.exists(file_name))

    def test_statement_with_confirmed_transactions_is_protected(self):
        statement = make_statement()
        row, = add_transactions(statement, [("A", "x")])
        Transaction.objects.filter(pk=row.pk).update(reviewed=True)

        response = self.client.post(reverse("delete_document", args=[statement.document.pk]))

        self.assertRedirects(
            response, reverse("document_review", args=[statement.document.pk]),
            fetch_redirect_response=False,
        )
        self.assertTrue(StatementImport.objects.filter(pk=statement.pk).exists())
        self.assertTrue(Transaction.objects.filter(pk=row.pk).exists())

    def test_delete_buttons_are_offered_on_the_right_pages(self):
        pending = self.invoice(Document.ProcessingStatus.PENDING, "offen.pdf")
        processed = self.invoice(Document.ProcessingStatus.PROCESSED, "fertig.pdf")
        statement = make_statement()
        add_transactions(statement, [("A", "x")])
        delete_url = lambda pk: reverse("delete_document", args=[pk])  # noqa: E731

        self.assertContains(
            self.client.get(reverse("document_review", args=[pending.pk])), delete_url(pending.pk)
        )
        self.assertNotContains(
            self.client.get(reverse("document_review", args=[processed.pk])), delete_url(processed.pk)
        )
        self.assertContains(
            self.client.get(reverse("statement_review", args=[statement.pk])),
            delete_url(statement.document.pk),
        )


class MenuTests(TestCase):
    def test_presentation_toggle_lives_in_the_administration_menu_only(self):
        user = get_user_model().objects.create_user("menue", password="secret-test-password")
        self.client.force_login(user)

        html = self.client.get(reverse("dashboard")).content.decode()
        panel_start = html.index('class="nav-menu-panel"')
        panel = html[panel_start:html.index("</details>", panel_start)]
        toggle = reverse("toggle_presentation_mode")

        self.assertIn(toggle, panel)
        outside_panels = html.replace(panel, "")
        desktop_nav = outside_panels[outside_panels.index('class="desktop-nav"'):]
        desktop_nav = desktop_nav[:desktop_nav.index('class="mobile-nav"')]
        self.assertNotIn(toggle, desktop_nav)
