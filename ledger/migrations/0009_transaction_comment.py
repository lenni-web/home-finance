from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ledger", "0008_emailimportconfig_emailimportmessage"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="comment",
            field=models.TextField(blank=True),
        ),
    ]
