# 0004 could not add the run uniqueness because the model state already
# declared it (AlterUniqueTogether is a no-op when old == new). Add the
# missing index directly, guarded so fresh databases that created it with the
# table are untouched.

from django.db import migrations


def ensure_period_unique(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor, "hr_payrollperiod"
        )
    for info in constraints.values():
        if info.get("unique") and set(info.get("columns") or []) == {
            "tenant_id",
            "period_key",
        }:
            return
    schema_editor.execute(
        "CREATE UNIQUE INDEX hr_payrollperiod_tenant_period_uniq "
        "ON hr_payrollperiod (tenant_id, period_key)"
    )


class Migration(migrations.Migration):

    # DDL on MySQL/MariaDB cannot roll back; keep this out of a transaction.
    atomic = False

    dependencies = [
        ("hr", "0004_fix_payroll_indexes"),
    ]

    operations = [
        migrations.RunPython(ensure_period_unique, migrations.RunPython.noop),
    ]
