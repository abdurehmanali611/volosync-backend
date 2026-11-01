# Repair the payroll indexes the interrupted 0003 attempt left behind:
# a stray single-column unique index on employee_id (which would cap an
# employee at one payslip ever) and the missing run uniqueness.

from django.db import migrations


def drop_stray_employee_unique(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor, "hr_payrollentry"
        )
    for name, info in constraints.items():
        columns = info.get("columns") or []
        if (
            info.get("unique")
            and info.get("index")
            and columns == ["employee_id"]
        ):
            schema_editor.execute(
                f"ALTER TABLE hr_payrollentry DROP INDEX {schema_editor.quote_name(name)}"
            )


class Migration(migrations.Migration):

    # MySQL/MariaDB cannot roll back DDL, so this migration must not run in a
    # transaction (the index drop below is plain ALTER TABLE).
    atomic = False

    dependencies = [
        ("hr", "0003_payroll_runs_and_mark_paid_hand_off"),
    ]

    operations = [
        migrations.RunPython(drop_stray_employee_unique, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name="payrollperiod",
            unique_together={("tenant", "period_key")},
        ),
    ]
