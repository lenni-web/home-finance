from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("ledger", "0012_backuprecord_serviceheartbeat")]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="is_internal_transfer",
            field=models.BooleanField(
                db_index=True, default=False, verbose_name="Interne Umbuchung"
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="transfer_counterpart",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="transfer_reverse",
                to="ledger.transaction",
                verbose_name="Gegenbuchung",
            ),
        ),
        migrations.AddField(
            model_name="categorizationrule",
            name="marks_internal_transfer",
            field=models.BooleanField(
                default=False, verbose_name="Als interne Umbuchung kennzeichnen"
            ),
        ),
    ]
