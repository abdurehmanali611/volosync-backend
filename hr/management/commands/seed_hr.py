"""Seed HR reference data for the demo tenant: leave types, incident types,
shift templates and an onboarding checklist. Idempotent — safe to re-run.

The demo tenant itself (Apex Wholesale) comes from `tenants` seed command;
this command layers HR config on top of it.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from hr.models import (
    ChecklistTemplate,
    ChecklistTemplateItem,
    IncidentType,
    LeaveType,
    ShiftTemplate,
)
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Seed HR config (leave types, incident types, shift templates, checklist) for the demo tenant."

    @transaction.atomic
    def handle(self, *args, **options):
        tenant = Tenant.objects.filter(tin="0012345678").first()
        if tenant is None:
            self.stdout.write(
                self.style.WARNING(
                    "Demo tenant not found — run `python manage.py seed` first. Nothing seeded."
                )
            )
            return

        for code, label, paid, quota, active in [
            ("ANNUAL", "Annual leave", True, 20, True),
            ("SICK", "Sick leave", True, 15, True),
            ("MATERNITY", "Maternity", True, 90, True),
            ("UNPAID", "Unpaid", False, 30, True),
            ("BEREAVEMENT", "Bereavement", True, 5, True),
        ]:
            LeaveType.objects.get_or_create(
                tenant=tenant,
                code=code,
                defaults={
                    "label": label,
                    "paid": paid,
                    "quota_days": quota,
                    "active": active,
                },
            )

        for code, label, deduct in [
            ("CONDUCT", "Conduct", False),
            ("DAMAGE", "Damage", True),
            ("ABSENCE", "Unexcused absence", True),
            ("SAFETY", "Safety violation", False),
            ("THEFT", "Theft", True),
        ]:
            IncidentType.objects.get_or_create(
                tenant=tenant,
                code=code,
                defaults={"label": label, "deduct": deduct, "active": True},
            )

        for code, name, start, end, weekdays, note in [
            ("DAY", "Day shift", "08:00", "17:00", [1, 2, 3, 4, 5], "Standard office hours"),
            ("EVE", "Evening shift", "14:00", "23:00", [1, 2, 3, 4, 5], "Late showroom cover"),
            ("WAREHOUSE_AM", "Warehouse morning", "07:00", "15:00", [1, 2, 3, 4, 5, 6], "Loading dock"),
        ]:
            ShiftTemplate.objects.get_or_create(
                tenant=tenant,
                code=code,
                defaults={
                    "name": name,
                    "start_time": start,
                    "end_time": end,
                    "weekdays": weekdays,
                    "note": note,
                    "active": True,
                },
            )

        template, created = ChecklistTemplate.objects.get_or_create(
            tenant=tenant,
            kind="onboarding",
            name="New hire onboarding",
            defaults={"active": True},
        )
        if created:
            for index, (label, required, owner) in enumerate(
                [
                    ("Collect signed contract", True, "hr"),
                    ("Create employee file", True, "hr"),
                    ("Issue uniform & badge", False, "manager"),
                    ("Warehouse safety briefing", True, "manager"),
                    ("Portal login walkthrough", False, "hr"),
                    ("First-week check-in", False, "manager"),
                ]
            ):
                ChecklistTemplateItem.objects.create(
                    template=template,
                    label=label,
                    required=required,
                    owner_role=owner,
                    sort_order=index,
                )

        self.stdout.write(
            self.style.SUCCESS("HR config seeded (leave types, incident types, shifts, checklist).")
        )
