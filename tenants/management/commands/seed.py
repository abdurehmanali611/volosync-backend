from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from assets.models import Asset
from hr.models import (
    Attendance,
    Department,
    Employee,
    HrDocument,
    Incident,
    LeaveRequest,
    LeaveType,
    PayrollEntry,
    PayrollPeriod,
)
from inventory.models import (
    Item,
    RegistrationLine,
    RegistrationRequest,
    StockLot,
    StockMovement,
    next_voucher,
)
from sales.models import SalesAgent
from tenants.models import Tenant
from user.models import Profile


class Command(BaseCommand):
    help = "Seed the VoloSync demo data."

    def handle(self, *args, **options):
        tenant, _ = Tenant.objects.get_or_create(
            tin="0012345678",
            defaults={
                "name": "Apex Wholesale Trading",
                "phone": "+251911000111",
                "address": "Merkato, Addis Ababa",
                "sales_role_enabled": True,
                "subscription_expires_on": date.today() + timedelta(days=365),
            },
        )

        staff = [
            ("store", "Store", "Kebede", "STORE"),
            ("finance", "Finance", "Marta", "FINANCE"),
            ("manager", "Manager", "Yohannes", "MANAGER"),
            ("hr", "HR", "Selam", "HR"),
            ("owner", "Owner", "Abebe", "OWNER"),
        ]
        for username, first, last, role in staff:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"first_name": first, "last_name": last},
            )
            if created:
                user.set_password(f"{username}123")
                user.save()
            Profile.objects.get_or_create(
                user=user, defaults={"tenant": tenant, "role": role}
            )

        if not StockLot.objects.filter(tenant=tenant).exists():
            rows = [
                ("Cooking Oil 5L", "Groceries", "carton", 40, 1150, 1320),
                ("Sugar 50kg", "Groceries", "sack", 25, 4200, 4850),
                ("Bar Soap", "Household", "carton", 60, 870, 1050),
            ]
            for name, category, unit, qty, cost, sales in rows:
                item, _ = Item.objects.get_or_create(
                    tenant=tenant, name=name, category=category, defaults={"unit": unit}
                )
                lot = StockLot.objects.create(
                    tenant=tenant,
                    item=item,
                    voucher_number=next_voucher(StockLot, tenant, "VS"),
                    quantity=qty,
                    remaining_quantity=qty,
                    unit_cost=cost,
                    unit_sales=sales,
                    vat_rate=tenant.vat_rate,
                    registration_date=date.today() - timedelta(days=10),
                    supplier_name="East Africa Foods PLC",
                    supplier_phone="+251911222333",
                    supplier_address="Adama",
                )
                StockMovement.objects.create(
                    tenant=tenant,
                    lot=lot,
                    type="REGISTRATION",
                    quantity_delta=qty,
                    role="STORE",
                    ref_type="REGISTRATION",
                    ref_id=0,
                    note=lot.voucher_number,
                )

            item = Item.objects.get(tenant=tenant, name="Cooking Oil 5L")
            lot = StockLot.objects.filter(item=item).first()
            lot.remaining_quantity = Decimal("24")
            lot.save(update_fields=["remaining_quantity"])
            StockMovement.objects.create(
                tenant=tenant,
                lot=lot,
                type="STOCKOUT",
                quantity_delta=Decimal("-16"),
                role="STORE",
                ref_type="STOCKOUT",
                ref_id=0,
                note="Assigned to sales agents",
            )

        store_user = User.objects.get(username="store")
        if not RegistrationRequest.objects.filter(tenant=tenant, status="PENDING_FINANCE").exists():
            doc = RegistrationRequest.objects.create(
                tenant=tenant,
                voucher_number=next_voucher(RegistrationRequest, tenant, "REG"),
                status="PENDING_FINANCE",
                created_by=store_user,
                submitted_at=timezone.now(),
                supplier_name="Highland Mills",
                supplier_phone="+251911444555",
                supplier_address="Bishoftu",
                supplier_tin="0099887766",
            )
            RegistrationLine.objects.create(
                request=doc,
                item_name="Wheat Flour 25kg",
                category="Groceries",
                unit="sack",
                quantity=30,
                unit_cost=2400,
                unit_sales=2750,
                vat_rate=tenant.vat_rate,
                registration_date=date.today(),
                note="New season stock",
            )

        username = "sales-apex"
        if not SalesAgent.objects.filter(tenant=tenant).exists():
            agents = [
                ("Hanna", "Tesfaye", "F", 4, "+251911777888"),
                ("Dawit", "Bekele", "M", 2, "+251911777999"),
                ("Meron", "Alemu", "F", 6, "+251911777000"),
            ]
            for index, (first, last, sex, years, phone) in enumerate(agents):
                user = User.objects.create_user(
                    username=f"agent-{tenant.id}-{index}",
                    password="agent123",
                    first_name=first,
                    last_name=last,
                )
                Profile.objects.create(user=user, tenant=tenant, role="SALES")
                SalesAgent.objects.create(
                    tenant=tenant,
                    user=user,
                    login_username=username,
                    first_name=first,
                    last_name=last,
                    phone=phone,
                    sex=sex,
                    years_experience=years,
                )

        manager = User.objects.get(username="manager")
        if not Asset.objects.filter(tenant=tenant).exists():
            for index, (name, category, cost) in enumerate(
                [
                    ("Dell Latitude 5540", "IT_EQUIPMENT", 78000),
                    ("Delivery Van", "VEHICLE", 1250000),
                    ("Office Desk", "FURNITURE", 14500),
                ]
            ):
                Asset.objects.create(
                    tenant=tenant,
                    asset_tag=f"AST-{date.today().year}-{index + 1:06d}",
                    name=name,
                    category=category,
                    unit_cost=cost,
                    useful_life_years=5 if category != "VEHICLE" else 8,
                    created_by=manager,
                )

        if not Department.objects.filter(tenant=tenant).exists():
            teams = [
                ("OPS", "Operations", "Manager"),
                ("FIN", "Finance", "Finance lead"),
                ("WH", "Warehouse", "Store supervisor"),
            ]
            departments = [
                Department.objects.create(tenant=tenant, code=code, label=label, leader=leader)
                for code, label, leader in teams
            ]

            for code, label, paid, quota in [
                ("ANNUAL", "Annual leave", True, 20),
                ("SICK", "Sick leave", True, 15),
                ("MATERNITY", "Maternity", True, 90),
                ("UNPAID", "Unpaid", False, 30),
            ]:
                LeaveType.objects.create(
                    tenant=tenant, code=code, label=label, paid=paid, quota_days=quota
                )

            people = [
                ("Amanuel", "Girma", "M", "Warehouse supervisor", 0, 18500, "EMP-0001", "123456"),
                ("Selam", "Tadesse", "F", "Sales clerk", 2, 12000, "EMP-0002", "234567"),
                ("Yohannes", "Kebede", "M", "Accountant", 1, 24000, "EMP-0003", "345678"),
                ("Bethel", "Mekonnen", "F", "HR officer", 0, 21000, "EMP-0004", "456789"),
            ]
            employees = []
            for first, last, sex, title, dept, salary, number, otp in people:
                employees.append(
                    Employee.objects.create(
                        tenant=tenant,
                        employee_no=number,
                        first_name=first,
                        last_name=last,
                        sex=sex,
                        phone="+251911000" + number[-1],
                        email=f"{first.lower()}@apexwholesale.et",
                        department=departments[dept],
                        job_title=title,
                        hired_on=date.today().replace(year=date.today().year - (2 if dept == 1 else 1)),
                        salary=salary,
                        portal_otp=otp,
                    )
                )

            for employee in employees:
                Attendance.objects.create(
                    tenant=tenant,
                    employee=employee,
                    day=date.today(),
                    status="PRESENT",
                    check_in=time(8, 15),
                    check_out=time(17, 30),
                )

            LeaveRequest.objects.create(
                tenant=tenant,
                employee=employees[1],
                leave_type="SICK",
                from_ymd=date.today(),
                to_ymd=date.today(),
                days=1,
                reason="Clinic visit",
            )
            LeaveRequest.objects.create(
                tenant=tenant,
                employee=employees[2],
                leave_type="ANNUAL",
                from_ymd=date.today(),
                to_ymd=date.today(),
                days=1,
                reason="Family event",
                status="APPROVED",
                decided_by="HR officer",
            )

            first_day = date.today().replace(day=1)
            next_month = (first_day + timedelta(days=32)).replace(day=1)
            payroll_period = PayrollPeriod.objects.create(
                tenant=tenant,
                period_key=first_day.strftime("%Y-%m"),
                month_name=first_day.strftime("%B %Y"),
                from_ymd=first_day,
                to_ymd=next_month - timedelta(days=1),
                status="AWAITING_MANAGER",
                created_by="HR officer",
            )
            for employee in employees:
                gross = employee.salary
                deductions = (gross * Decimal("0.07")).quantize(Decimal("0.01"))
                PayrollEntry.objects.create(
                    tenant=tenant,
                    payroll_period=payroll_period,
                    employee=employee,
                    gross=gross,
                    deductions=deductions,
                    net=gross - deductions,
                )

            Incident.objects.create(
                tenant=tenant,
                employee=employees[0],
                kind="Damage",
                title="Forklift bumper damaged",
                detail="Bumper cracked while moving pallets.",
                occurred_ymd=date.today(),
                salary_deduct=False,
                status="OPEN",
            )

            HrDocument.objects.create(
                tenant=tenant,
                employee=employees[0],
                title="Employment contract",
                kind="Contract",
                file_url="",
            )
            HrDocument.objects.create(
                tenant=tenant,
                employee=employees[3],
                title="Safety training certificate",
                kind="Certificate",
                file_url="",
            )

        self.stdout.write(self.style.SUCCESS("VoloSync demo data seeded."))
