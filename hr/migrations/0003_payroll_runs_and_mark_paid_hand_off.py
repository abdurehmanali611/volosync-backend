# Payroll runs with the HR -> Finance mark-paid hand-off.
#
# The legacy `period` column and the run table are handled in Python so the
# migration replays cleanly on a fresh database and on the database where an
# earlier attempt of this migration was interrupted midway.

import django.db.models.deletion
from django.db import migrations, models

RUN_TABLE = "hr_payrollperiod"
SLIP_TABLE = "hr_payrollentry"


def _columns(schema_editor, table):
    with schema_editor.connection.cursor() as cursor:
        return {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(
                cursor, table
            )
        }


def create_run_table(apps, schema_editor):
    if RUN_TABLE in schema_editor.connection.introspection.table_names():
        return
    schema_editor.create_model(apps.get_model("hr", "PayrollPeriod"))


def drop_legacy_period(apps, schema_editor):
    if "period" not in _columns(schema_editor, SLIP_TABLE):
        return
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor, SLIP_TABLE
        )
    for name, info in constraints.items():
        if info.get("unique") and set(info.get("columns") or []) == {
            "employee_id",
            "period",
        }:
            schema_editor.execute(
                f"ALTER TABLE {SLIP_TABLE} DROP INDEX {schema_editor.quote_name(name)}"
            )
    schema_editor.execute(
        f"ALTER TABLE {SLIP_TABLE} DROP COLUMN {schema_editor.quote_name('period')}"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("hr", "0002_employee_otp_reset_at_employee_otp_reset_by_and_more"),
        ("tenants", "0002_tenant_subscription_paid_on_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="PayrollPeriod",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("period_key", models.CharField(max_length=20)),
                        ("month_name", models.CharField(blank=True, max_length=40)),
                        ("from_ymd", models.DateField()),
                        ("to_ymd", models.DateField()),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("OPEN", "Open"),
                                    ("AWAITING_MANAGER", "Awaiting Manager"),
                                    ("APPROVED", "Approved"),
                                    ("AWAITING_FINANCE", "Awaiting Finance"),
                                    ("CLOSED", "Closed"),
                                ],
                                default="OPEN",
                                max_length=20,
                            ),
                        ),
                        ("created_by", models.CharField(blank=True, max_length=255)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("decided_by", models.CharField(blank=True, max_length=255)),
                        ("decided_at", models.DateTimeField(blank=True, null=True)),
                        ("decision_note", models.TextField(blank=True)),
                        ("closed_by", models.CharField(blank=True, max_length=255)),
                        ("closed_at", models.DateTimeField(blank=True, null=True)),
                        (
                            "tenant",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="hr_payroll_periods",
                                to="tenants.tenant",
                            ),
                        ),
                    ],
                    options={
                        "ordering": ["-from_ymd", "-id"],
                        "unique_together": {("tenant", "period_key")},
                    },
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(create_run_table, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterUniqueTogether(
                    name="payrollentry",
                    unique_together=set(),
                ),
                migrations.RemoveField(
                    model_name="payrollentry",
                    name="period",
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(drop_legacy_period, migrations.RunPython.noop),
        migrations.AddField(
            model_name="payrollentry",
            name="hr_marked_paid_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payrollentry",
            name="hr_marked_paid_by",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="payrollentry",
            name="manager_approved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payrollentry",
            name="manager_approved_by",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="payrollentry",
            name="payroll_period",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="payslips",
                to="hr.payrollperiod",
            ),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="payrollentry",
            name="status",
            field=models.CharField(
                choices=[
                    ("UNPAID", "Unpaid"),
                    ("AWAITING_FINANCE", "Awaiting Finance"),
                    ("MARKED_PAID", "Marked paid"),
                ],
                default="UNPAID",
                max_length=20,
            ),
        ),
        migrations.AlterModelOptions(
            name="payrollentry",
            options={"ordering": ["employee__first_name", "employee__last_name"]},
        ),
        migrations.AlterUniqueTogether(
            name="payrollentry",
            unique_together={("payroll_period", "employee")},
        ),
    ]
