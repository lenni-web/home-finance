import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ledger", "0010_document_extraction_details")]
    operations = [
        migrations.AddField(
            model_name="document", name="auto_match_confidence",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="document", name="auto_match_reasons",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="document", name="auto_matched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="document", name="auto_matched_transaction",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="automatically_matched_documents", to="ledger.transaction",
            ),
        ),
    ]
