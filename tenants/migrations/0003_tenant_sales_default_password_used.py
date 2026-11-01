from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0002_tenant_subscription_paid_on_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="sales_default_password_used",
            field=models.BooleanField(default=False),
        ),
    ]
