from django.db import migrations


def mark_imported_statements_processed(apps, schema_editor):
    Document = apps.get_model("ledger", "Document")
    Document.objects.filter(
        statementimport__status="imported",
        processing_status="pending",
    ).update(processing_status="processed")


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0004_document_extracted_at_document_processing_error_and_more"),
    ]

    operations = [
        migrations.RunPython(mark_imported_statements_processed, migrations.RunPython.noop),
    ]
