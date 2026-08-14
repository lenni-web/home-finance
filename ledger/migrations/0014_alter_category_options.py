from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0013_internal_transfers"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="category",
            options={"ordering": ["name"]},
        ),
    ]
