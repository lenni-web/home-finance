import tempfile
from datetime import date
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import Document, Person, Tag


class DocumentModelTests(TestCase):
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
