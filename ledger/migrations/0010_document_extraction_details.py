from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ledger", "0009_transaction_comment")]
    operations = [
        migrations.AddField(
            model_name="document", name="invoice_number",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="document", name="extraction_confidence",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
