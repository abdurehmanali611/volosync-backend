from django.db import migrations, models


def rename_it_category(apps, schema_editor):
    """Canonicalize the old ``IT`` code to ``IT_EQUIPMENT`` (frontend parity)."""
    Asset = apps.get_model("assets", "Asset")
    Asset.objects.filter(category="IT").update(category="IT_EQUIPMENT")


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(rename_it_category, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="asset",
            name="category",
            field=models.CharField(
                choices=[
                    ("IT_EQUIPMENT", "IT equipment"),
                    ("FURNITURE", "Furniture"),
                    ("VEHICLE", "Vehicle"),
                    ("MACHINERY", "Machinery"),
                    ("TOOL", "Tool"),
                    ("OTHER", "Other"),
                ],
                default="OTHER",
                max_length=20,
            ),
        ),
    ]
