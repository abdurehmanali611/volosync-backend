"""HR module API: roster, leave, attendance, payroll, documents, incidents.

HR raises records; the Manager holds the approval queue for leave, payroll and
incidents. Both roles read everything in the module.
"""

import calendar
import os
import random
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

import secrets
import string

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import connection
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.views import acting_role, require_roles, role_of, tenant_of

from .models import (
    Attendance,
    AttendanceCorrection,
    AtsOtp,
    AtsOtpAudit,
    ChatBlock,
    ChatMember,
    ChatMessage,
    ChatThread,
    ChecklistRun,
    ChecklistRunItem,
    ChecklistTemplate,
    CompensationRequest,
    Department,
    Employee,
    HrAsset,
    HrDocument,
    HrNotification,
    HrWorkflow,
    Incident,
    IncidentType,
    LeaveBalance,
    LeaveRequest,
    LeaveType,
    OtpResetRequest,
    PayrollAdjustment,
    PayrollEntry,
    PayrollPeriod,
    PeopleEvent,
    ShiftTemplate,
    Team,
    WageType,
)
from .serializers import (
    AttendanceCorrectionSerializer,
    AttendanceSerializer,
    AtsOtpAuditSerializer,
    AtsOtpSerializer,
    ChatBlockSerializer,
    ChatMessageSerializer,
    ChatThreadSerializer,
    ChecklistRunSerializer,
    ChecklistTemplateSerializer,
    CompensationRequestSerializer,
    DepartmentSerializer,
    DocumentSerializer,
    EmployeeSerializer,
    HrAssetInputSerializer,
    HrAssetSerializer,
    HrNotificationSerializer,
    HrWorkflowSerializer,
    IncidentSerializer,
    IncidentTypeSerializer,
    LeaveRequestSerializer,
    LeaveTypeSerializer,
    OtpResetRequestSerializer,
    PayrollAdjustmentSerializer,
    PayrollPeriodSerializer,
    PayrollSerializer,
    PeopleEventSerializer,
    ShiftTemplateSerializer,
    TeamSerializer,
    WageTypeSerializer,
)
from .services import (
    actor,
    available_days,
    balance_rows,
    decide_leave,
    leave_step_kind,
    notify,
    resolve_leave_type,
)


def _dates(request):
    day_from = request.query_params.get("from")
    day_to = request.query_params.get("to")
    return day_from, day_to


def _provided(data, name):
    return str(data.get(name) if data.get(name) is not None else "").strip()


def _tenant_fk(model, tenant, value):
    """Tenant-scoped row for an id key, reading an unusable id as unset.

    Django raises ValueError when an id key holds a label and MySQL surfaces it
    as a 500 the portal cannot explain.
    """
    raw = str(value if value is not None else "").strip()
    if not raw.isdigit():
        return None
    return model.objects.filter(pk=int(raw), tenant=tenant).first()


class HrDashboardView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        today = date.today()
        employees = Employee.objects.filter(tenant=tenant)
        attendance_today = Attendance.objects.filter(tenant=tenant, day=today)
        present = attendance_today.filter(status__in=["PRESENT", "LATE", "HALF_DAY"]).count()
        payload = {
            "headcount": employees.count(),
            "departments": Department.objects.filter(tenant=tenant).count(),
            "on_leave": employees.filter(status="ON_LEAVE").count(),
            "pending_leaves": LeaveRequest.objects.filter(
                tenant=tenant, status="PENDING"
            ).count(),
            "pending_payroll": PayrollPeriod.objects.filter(
                tenant=tenant, status="AWAITING_MANAGER"
            ).count(),
            "pending_documents": HrDocument.objects.filter(
                tenant=tenant, status="PENDING"
            ).count(),
            "pending_corrections": AttendanceCorrection.objects.filter(
                tenant=tenant, status="PENDING"
            ).count(),
            "open_incidents": Incident.objects.filter(tenant=tenant, status="OPEN").count(),
            "attendance_today": attendance_today.count(),
            "present_today": present,
            "salary_mass": str(
                employees.aggregate(total=Sum("salary"))["total"] or 0
            ),
            "teams": Team.objects.filter(tenant=tenant).count(),
            "portal_ready": employees.exclude(portal_otp="").count(),
            "unread_notifications": HrNotification.objects.filter(
                tenant=tenant, read_at__isnull=True
            ).count(),
            "open_threads": ChatThread.objects.filter(tenant=tenant).count(),
            "role": role_of(request.user),
        }
        return Response({"data": payload})


class DepartmentListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = Department.objects.filter(tenant=tenant).annotate(
            staff=Count("employees")
        )
        payload = [
            {**DepartmentSerializer(row).data, "staff": row.staff} for row in rows
        ]
        return Response({"data": payload})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        row = Department.objects.create(
            tenant=tenant,
            code=(request.data.get("code") or request.data.get("label", "")).upper()[:40],
            label=request.data.get("label", ""),
            leader=request.data.get("leader", ""),
        )
        return Response({"data": DepartmentSerializer(row).data}, status=201)


class DepartmentDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = Department.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if request.data.get("label", "").strip():
            row.label = request.data["label"].strip()
        if request.data.get("code", "").strip():
            row.code = request.data["code"].strip().upper()[:40]
        if "leader" in request.data:
            row.leader = request.data.get("leader", "")
        row.save()
        return Response({"data": DepartmentSerializer(row).data})

    @require_roles("HR", "MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = Department.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


class TeamListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = Team.objects.filter(tenant=tenant).select_related("department")
        department = request.query_params.get("department")
        if department:
            rows = rows.filter(department_id=department)
        return Response({"data": TeamSerializer(rows, many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        code = (request.data.get("code") or request.data.get("label", "")).upper()[:40]
        if not code:
            return Response({"errors": {"code": "Code or label is required."}}, status=400)
        if Team.objects.filter(tenant=tenant, code=code).exists():
            return Response({"errors": {"code": "This team code already exists."}}, status=400)
        department = None
        if request.data.get("department"):
            department = _tenant_fk(Department, tenant, request.data.get("department"))
        row = Team.objects.create(
            tenant=tenant,
            code=code,
            label=request.data.get("label", ""),
            leader=request.data.get("leader", ""),
            department=department,
            active=bool(request.data.get("active", True)),
        )
        return Response({"data": TeamSerializer(row).data}, status=201)


class TeamDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = Team.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        for field in ["label", "leader", "active"]:
            if field in request.data:
                setattr(row, field, request.data[field])
        if request.data.get("code"):
            row.code = str(request.data["code"]).upper()[:40]
        if "department" in request.data:
            row.department = _tenant_fk(Department, tenant, request.data.get("department"))
        row.save()
        return Response({"data": TeamSerializer(row).data})

    @require_roles("HR", "MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = Team.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


# The roster columns are narrow and MySQL runs strict, so a value that cannot fit
# its column has to come back as a 400 naming the field. Left unchecked the INSERT
# dies on a 1406 DataError and the portal shows an unexplained 500.
EMPLOYEE_SEX = {"": "", "M": "M", "MALE": "M", "F": "F", "FEMALE": "F"}
EMPLOYEE_TEXT_FIELDS = (
    "employee_no",
    "first_name",
    "last_name",
    "phone",
    "email",
    "job_title",
    "org_position",
    "bank_name",
    "account_number",
    "education",
    "personal_tin",
    "medical_note",
    "notes",
    "portal_otp",
    "profile_image_url",
)
EMPLOYEE_RELATIONS = (
    ("department", Department),
    ("team", Team),
    ("shift", ShiftTemplate),
)


def _employee_values(data, tenant, errors):
    """Column-ready values for exactly the keys the portal sent.

    Create and edit share this so both speak one shape: the roster keeps M/F
    sex, an uppercase status, a decimal salary, an ISO hire date and
    tenant-scoped relation ids.
    """
    values = {}
    for name in EMPLOYEE_TEXT_FIELDS:
        if name in data:
            values[name] = _provided(data, name)
    if "sex" in data:
        key = _provided(data, "sex").upper()
        if key not in EMPLOYEE_SEX:
            errors["sex"] = "Use male or female."
        values["sex"] = EMPLOYEE_SEX.get(key, "")
    if "status" in data and _provided(data, "status"):
        key = _provided(data, "status").upper()
        if key not in {choice[0] for choice in Employee.STATUS}:
            errors["status"] = "Use ACTIVE, ON_LEAVE or TERMINATED."
        else:
            values["status"] = key
    if "salary" in data:
        raw = _provided(data, "salary").replace(",", "")
        if raw:
            try:
                values["salary"] = Decimal(raw)
            except InvalidOperation:
                errors["salary"] = "Enter a valid salary."
        else:
            values["salary"] = Decimal(0)
    if "wage_type" in data:
        key = _provided(data, "wage_type").upper()
        if not key:
            values["wage_type"] = "MONTHLY"
        elif key not in {choice[0] for choice in Employee.WAGE_TYPES}:
            errors["wage_type"] = "Use monthly, weekly or daily."
        else:
            values["wage_type"] = key
    if "years_experience" in data:
        raw = _provided(data, "years_experience")
        if not raw:
            values["years_experience"] = 0
        elif not raw.isdigit() or int(raw) > 99:
            errors["years_experience"] = "Enter a realistic number of years."
        else:
            values["years_experience"] = int(raw)
    if "hired_on" in data:
        raw = _provided(data, "hired_on")
        if raw:
            parsed = parse_date(raw[:10])
            if parsed is None:
                errors["hired_on"] = "Use the YYYY-MM-DD format."
            values["hired_on"] = parsed
        else:
            values["hired_on"] = None
    if "must_change_otp" in data:
        values["must_change_otp"] = _provided(data, "must_change_otp").lower() not in (
            "",
            "0",
            "false",
        )
    for name, model in EMPLOYEE_RELATIONS:
        if name not in data:
            continue
        raw = _provided(data, name)
        if not raw:
            values[name] = None
        elif not raw.isdigit():
            errors[name] = "Choose a value from the list."
        else:
            related = _tenant_fk(model, tenant, raw)
            if related is None:
                errors[name] = "No such record for this business."
            values[name] = related
    return values


def _employee_problems(row, changed=None):
    """Field-keyed messages for whatever MySQL would have refused to store."""
    skipped = [
        field.name
        for field in Employee._meta.fields
        if changed is not None and field.name not in changed
    ]
    errors = {}
    try:
        row.full_clean(exclude=skipped)
    except ValidationError as exc:
        for name, messages in exc.message_dict.items():
            errors[name] = " ".join(messages)
    return errors


class EmployeeListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = (
            Employee.objects.filter(tenant=tenant)
            .select_related("department", "shift", "team")
        )
        search = request.query_params.get("search")
        if search:
            rows = rows.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(employee_no__icontains=search)
                | Q(job_title__icontains=search)
            )
        department = request.query_params.get("department")
        if department:
            rows = rows.filter(department_id=department)
        team = request.query_params.get("team")
        if team:
            rows = rows.filter(team_id=team)
        status_param = request.query_params.get("status")
        if status_param:
            rows = rows.filter(status=status_param.upper())
        return Response(
            {"data": EmployeeSerializer(rows, many=True, context={"request": request}).data}
        )

    @require_roles("HR")
    def post(self, request):
        tenant = tenant_of(request.user)
        errors = {}
        values = _employee_values(request.data, tenant, errors)
        if errors:
            return Response({"errors": errors}, status=400)
        count = Employee.objects.filter(tenant=tenant).count()
        row = Employee(tenant=tenant)
        for key, value in values.items():
            setattr(row, key, value)
        row.employee_no = row.employee_no or f"EMP-{count + 1:04d}"
        row.portal_otp = values.get("portal_otp") or _new_portal_otp()
        row.must_change_otp = values.get("must_change_otp", True)
        problems = _employee_problems(row)
        if problems:
            return Response({"errors": problems}, status=400)
        row.save()
        return Response(
            {
                "data": EmployeeSerializer(
                    row, context={"request": request}
                ).data
            },
            status=201,
        )


class EmployeeDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def get(self, request, pk):
        tenant = tenant_of(request.user)
        row = Employee.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        return Response(
            {"data": EmployeeSerializer(row, context={"request": request}).data}
        )

    @require_roles("HR", "MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = Employee.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        errors = {}
        values = _employee_values(request.data, tenant, errors)
        # A fresh OTP always goes back to must-change, whatever the form sent.
        if "portal_otp" in request.data and values.get("portal_otp"):
            values["must_change_otp"] = True
        if errors:
            return Response({"errors": errors}, status=400)
        for key, value in values.items():
            setattr(row, key, value)
        problems = _employee_problems(row, changed=set(values))
        if problems:
            return Response({"errors": problems}, status=400)
        row.save()
        return Response(
            {"data": EmployeeSerializer(row, context={"request": request}).data}
        )


class LeaveTypeListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        return Response(
            {"data": LeaveTypeSerializer(LeaveType.objects.filter(tenant=tenant), many=True).data}
        )

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        code = (request.data.get("code") or request.data.get("label", "")).upper()[:40] or "OTHER"
        row = LeaveType.objects.create(
            tenant=tenant,
            code=code,
            label=request.data.get("label", "Other"),
            paid=bool(request.data.get("paid", True)),
            quota_days=request.data.get("quota_days") or 15,
            active=bool(request.data.get("active", True)),
        )
        return Response({"data": LeaveTypeSerializer(row).data}, status=201)


class LeaveTypeDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = LeaveType.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        for field in ["label", "paid", "quota_days", "active"]:
            if field in request.data:
                setattr(row, field, request.data[field])
        if request.data.get("code"):
            row.code = str(request.data["code"]).upper()[:40]
        row.save()
        return Response({"data": LeaveTypeSerializer(row).data})

    @require_roles("HR", "MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = LeaveType.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


class LeaveListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = LeaveRequest.objects.filter(tenant=tenant).select_related("employee")
        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())
        employee = request.query_params.get("employee")
        if employee:
            qs = qs.filter(employee_id=employee)
        rows = list(qs)
        if request.query_params.get("queue") == "1":
            role = acting_role(request.user)
            wanted = ("hr",) if role == "HR" else ("manager", "admin")
            rows = [
                row
                for row in rows
                if row.status == "PENDING"
                and leave_step_kind(
                    row.tenant, row.employee, row.current_step_index
                )
                in wanted
            ]
        return Response({"data": LeaveRequestSerializer(rows, many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        if not employee:
            return Response(
                {"errors": {"employee": "Choose an employee."}}, status=400
            )
        from_ymd = parse_date(str(request.data.get("from_ymd") or ""))
        to_ymd = parse_date(str(request.data.get("to_ymd") or ""))
        if from_ymd is None or to_ymd is None:
            return Response(
                {"errors": {"from_ymd": "Choose the leave dates."}}, status=400
            )
        if to_ymd < from_ymd:
            return Response(
                {"errors": {"to_ymd": "Leave ends before it starts."}}, status=400
            )
        code = (request.data.get("leave_type") or "ANNUAL").upper()
        leave_type = resolve_leave_type(tenant, code)
        if leave_type is None:
            return Response(
                {"errors": {"leave_type": "Pick an active leave type."}}, status=400
            )
        overlap = LeaveRequest.objects.filter(
            employee=employee,
            status__in=["PENDING", "APPROVED"],
            from_ymd__lte=to_ymd,
            to_ymd__gte=from_ymd,
        ).exists()
        if overlap:
            return Response(
                {"errors": {"from_ymd": "This employee already has leave in that range."}},
                status=400,
            )
        days = request.data.get("days") or ((to_ymd - from_ymd).days + 1)
        allowance = available_days(tenant, employee, leave_type.code, from_ymd.year)
        if (
            leave_type.paid
            and allowance is not None
            and Decimal(str(days)) > allowance
            and not request.data.get("allow_negative")
        ):
            return Response(
                {
                    "errors": {
                        "leave_type": f"Only {allowance} day(s) of {leave_type.label} remain."
                    }
                },
                status=400,
            )
        row = LeaveRequest.objects.create(
            tenant=tenant,
            employee=employee,
            leave_type=leave_type.code,
            from_ymd=from_ymd,
            to_ymd=to_ymd,
            days=days,
            reason=request.data.get("reason", ""),
        )
        notify(
            tenant,
            employee,
            "LEAVE",
            f"{leave_type.label} leave submitted",
            f"{from_ymd} → {to_ymd} · {days} day(s)",
            "/leave",
            "PENDING",
        )
        return Response({"data": LeaveRequestSerializer(row).data}, status=201)


class LeaveDecisionView(APIView):
    @require_roles("HR", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        row = LeaveRequest.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if row.status != "PENDING":
            return Response(
                {"errors": {"message": "This request is already decided."}}, status=400
            )
        approved = (request.data.get("decision") or "").upper() == "APPROVED"
        try:
            decide_leave(
                row,
                role_of(request.user),
                actor(request.user),
                approved,
                request.data.get("note", ""),
            )
        except PermissionError as exc:
            return Response({"errors": {"message": str(exc)}}, status=403)
        return Response({"data": LeaveRequestSerializer(row).data})


class LeaveBalanceView(APIView):
    """Entitlement vs used/pending days, per employee and leave type."""

    @require_roles("HR", "MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        year = request.query_params.get("year")
        year = int(year) if year and year.isdigit() else date.today().year
        employee_id = request.query_params.get("employee")
        if employee_id:
            employee = Employee.objects.filter(
                tenant=tenant, pk=employee_id
            ).first()
            if not employee:
                return Response({"errors": {"message": "Not found."}}, status=404)
            return Response(
                {
                    "data": {
                        "employee_id": employee.id,
                        "employee_name": employee.full_name,
                        "year": year,
                        "balances": balance_rows(tenant, employee, year),
                    }
                }
            )
        rows = []
        for employee in Employee.objects.filter(tenant=tenant).exclude(
            status="TERMINATED"
        ):
            rows.append(
                {
                    "employee_id": employee.id,
                    "employee_name": employee.full_name,
                    "year": year,
                    "balances": balance_rows(tenant, employee, year),
                }
            )
        return Response({"data": rows})

    @require_roles("HR")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        leave_type = resolve_leave_type(
            tenant, (request.data.get("leave_type") or "").upper()
        )
        if not employee or leave_type is None:
            return Response(
                {"errors": {"message": "Choose an employee and leave type."}}, status=400
            )
        year = int(request.data.get("year") or date.today().year)
        row, _ = LeaveBalance.objects.update_or_create(
            employee=employee,
            leave_type=leave_type,
            year=year,
            defaults={
                "tenant": tenant,
                "entitled_days": request.data.get("entitled_days")
                or leave_type.quota_days,
                "adjustment_days": request.data.get("adjustment_days") or 0,
                "note": request.data.get("note", ""),
            },
        )
        return Response(
            {
                "data": {
                    "employee_id": employee.id,
                    "employee_name": employee.full_name,
                    "year": year,
                    "balances": balance_rows(tenant, employee, year),
                }
            },
            status=201,
        )


class AttendanceListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = Attendance.objects.filter(tenant=tenant).select_related("employee")
        day_from, day_to = _dates(request)
        if day_from:
            qs = qs.filter(day__gte=day_from)
        if day_to:
            qs = qs.filter(day__lte=day_to)
        employee = request.query_params.get("employee")
        if employee:
            qs = qs.filter(employee_id=employee)
        return Response({"data": AttendanceSerializer(qs[:400], many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        if not employee:
            return Response({"errors": {"employee": "Choose an employee."}}, status=400)
        shift = None
        if request.data.get("shift"):
            shift = _tenant_fk(ShiftTemplate, tenant, request.data.get("shift"))
        row, created = Attendance.objects.get_or_create(
            employee=employee,
            day=request.data.get("day"),
            defaults={
                "tenant": tenant,
                "status": request.data.get("status", "PRESENT"),
                "check_in": request.data.get("check_in") or None,
                "check_out": request.data.get("check_out") or None,
                "shift": shift,
                "note": request.data.get("note", ""),
            },
        )
        if not created:
            for field in ["status", "check_in", "check_out", "note"]:
                if field in request.data:
                    setattr(row, field, request.data[field])
            if shift is not None:
                row.shift = shift
            row.save()
        return Response({"data": AttendanceSerializer(row).data}, status=201)


def _payroll_period(tenant, pk):
    return (
        PayrollPeriod.objects.filter(tenant=tenant, pk=pk)
        .prefetch_related("payslips__employee")
        .first()
    )


def _period_payload(period, payslips=None):
    rows = list(payslips if payslips is not None else period.payslips.all())
    # The serializer prefers this attribute over a second query.
    period.payslip_rows = rows
    return {
        **PayrollPeriodSerializer(period).data,
        "payslips": PayrollSerializer(rows, many=True).data,
    }


def _sync_period_status(period):
    """Reflect the payslip ladder on the run: unpaid work is HR's again,
    awaiting slips belong to Finance, everything paid means close-ready."""
    statuses = set(period.payslips.values_list("status", flat=True))
    if not statuses:
        return
    if "AWAITING_FINANCE" in statuses:
        period.status = "AWAITING_FINANCE"
    elif period.status in ("AWAITING_FINANCE", "APPROVED"):
        period.status = "APPROVED"
    period.save(update_fields=["status"])


class PayrollListCreateView(APIView):
    """Payslips read across HR, Finance and the Manager workspace."""

    @require_roles("HR", "MANAGER", "FINANCE")
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = (
            PayrollEntry.objects.filter(tenant=tenant)
            .select_related("employee", "payroll_period")
        )
        period_id = request.query_params.get("period")
        if period_id:
            qs = qs.filter(payroll_period_id=period_id)
        status_param = (request.query_params.get("status") or "").upper()
        if status_param:
            qs = qs.filter(status=status_param)
        return Response({"data": PayrollSerializer(qs, many=True).data})


class PayrollPeriodListCreateView(APIView):
    """HR generates a run; every payroll role can read the list."""

    @require_roles("HR", "MANAGER", "FINANCE")
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = (
            PayrollPeriod.objects.filter(tenant=tenant)
            .prefetch_related("payslips")
        )
        return Response({"data": PayrollPeriodSerializer(rows, many=True).data})

    @require_roles("HR")
    def post(self, request):
        tenant = tenant_of(request.user)
        today = date.today()
        period_key = (request.data.get("period_key") or "").strip()
        from_ymd = parse_date(str(request.data.get("from_ymd") or ""))
        to_ymd = parse_date(str(request.data.get("to_ymd") or ""))
        wage_type_key = _provided(request.data, "wage_type")
        wage_type = (
            WageType.objects.filter(
                pk=int(wage_type_key), tenant=tenant, active=True
            ).first()
            if wage_type_key.isdigit()
            else None
        )
        if not period_key:
            period_key = today.strftime("%Y-%m")
        if (from_ymd is None or to_ymd is None) and wage_type is not None:
            start_day = max(1, min(int(wage_type.min_day or 1), 28))
            end_day = max(1, min(int(wage_type.to_day or 30), 28))
            if today.day >= start_day:
                start_year, start_month = today.year, today.month
            else:
                start_year, start_month = (today.year, today.month - 1)
                if start_month == 0:
                    start_year, start_month = start_year - 1, 12
            start = date(start_year, start_month, start_day)
            end_year, end_month = start_year, start_month + 1
            if end_month == 13:
                end_year, end_month = end_year + 1, 1
            from_ymd = from_ymd or start
            to_ymd = to_ymd or date(end_year, end_month, end_day)
        if from_ymd is None or to_ymd is None:
            year, month = (period_key.split("-") + ["1"])[:2]
            try:
                year_i, month_i = int(year), int(month)
            except ValueError:
                return Response(
                    {"errors": {"period_key": "Use a YYYY-MM period."}}, status=400
                )
            from_ymd = from_ymd or date(year_i, month_i, 1)
            last_day = calendar.monthrange(year_i, month_i)[1]
            to_ymd = to_ymd or date(year_i, month_i, last_day)
        if to_ymd < from_ymd:
            return Response(
                {"errors": {"to_ymd": "The period ends before it starts."}}, status=400
            )
        if PayrollPeriod.objects.filter(tenant=tenant, period_key=period_key).exists():
            return Response(
                {"errors": {"period_key": "That payroll run already exists."}},
                status=400,
            )
        employees = list(
            Employee.objects.filter(
                tenant=tenant, status__in=["ACTIVE", "ON_LEAVE"]
            ).select_related("department")
        )
        if not employees:
            return Response(
                {"errors": {"message": "Add employees to the roster before generating payroll."}},
                status=400,
            )
        deductions = {
            row["employee_id"]: row["total"]
            for row in Incident.objects.filter(
                tenant=tenant,
                salary_deduct=True,
                occurred_ymd__range=(from_ymd, to_ymd),
            )
            .values("employee_id")
            .annotate(total=Sum("amount_etb"))
        }
        increments = {
            row["employee_id"]: row["total"]
            for row in Incident.objects.filter(
                tenant=tenant,
                salary_deduct=False,
                occurred_ymd__range=(from_ymd, to_ymd),
            )
            .values("employee_id")
            .annotate(total=Sum("amount_etb"))
        }
        period = PayrollPeriod.objects.create(
            tenant=tenant,
            period_key=period_key,
            month_name=request.data.get("month_name", "") or from_ymd.strftime("%B %Y"),
            from_ymd=from_ymd,
            to_ymd=to_ymd,
            wage_type=wage_type,
            status="AWAITING_MANAGER",
            created_by=actor(request.user),
        )
        comp_requests = list(
            CompensationRequest.objects.filter(
                tenant=tenant, status="APPROVED", applied_period__isnull=True
            ).filter(
                Q(effective_ymd__isnull=True)
                | Q(effective_ymd__range=(from_ymd, to_ymd))
            )
        )
        comp_by_employee = {}
        for req in comp_requests:
            comp_by_employee.setdefault(req.employee_id, []).append(req)
        adjustments = list(
            PayrollAdjustment.objects.filter(tenant=tenant, active=True)
        )
        slips, applied = [], []
        for employee in employees:
            salary_value = employee.salary or Decimal("0")
            gross = salary_value
            added = increments.get(employee.id) or Decimal("0")
            deducted = deductions.get(employee.id) or Decimal("0")
            for req in comp_by_employee.get(employee.id, []):
                amount = req.amount or Decimal("0")
                if req.kind == "salary_change":
                    if amount > 0:
                        gross = amount
                        employee.salary = amount
                        employee.save(update_fields=["salary"])
                elif req.kind in ("bonus", "overtime"):
                    added += amount
                elif req.kind in ("advance", "loan"):
                    deducted += amount
                req.applied_period = period
                applied.append(req)
            for adjustment in adjustments:
                if salary_value < (adjustment.salary_from or 0):
                    continue
                if (
                    adjustment.salary_to is not None
                    and salary_value > adjustment.salary_to
                ):
                    continue
                if adjustment.when_mode == "DAY_RANGE":
                    if adjustment.start_ymd and adjustment.start_ymd > to_ymd:
                        continue
                    if adjustment.end_ymd and adjustment.end_ymd < from_ymd:
                        continue
                amount = (
                    salary_value
                    * Decimal(str(adjustment.percent_of_salary or 0))
                    / Decimal("100")
                ).quantize(Decimal("0.01"))
                if adjustment.kind == "INCREMENT":
                    added += amount
                else:
                    deducted += amount
            gross_total = gross + added
            net = gross_total - deducted
            if net < 0:
                net = Decimal("0")
            slips.append(
                PayrollEntry(
                    tenant=tenant,
                    payroll_period=period,
                    employee=employee,
                    gross=gross_total,
                    deductions=deducted,
                    net=net,
                )
            )
        PayrollEntry.objects.bulk_create(slips)
        if applied:
            CompensationRequest.objects.bulk_update(applied, ["applied_period"])
        return Response({"data": _period_payload(period)}, status=201)


class PayrollPeriodDetailView(APIView):
    @require_roles("HR", "MANAGER", "FINANCE")
    def get(self, request, pk):
        period = _payroll_period(tenant_of(request.user), pk)
        if not period:
            return Response({"errors": {"message": "Not found."}}, status=404)
        return Response({"data": _period_payload(period)})

    @require_roles("HR")
    def delete(self, request, pk):
        period = _payroll_period(tenant_of(request.user), pk)
        if not period:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if period.status != "OPEN":
            return Response(
                {"errors": {"message": "Only a run sent back to HR can be discarded."}},
                status=400,
            )
        period.delete()
        return Response(status=204)


class PayrollPeriodDecisionView(APIView):
    """Manager approves or rejects the generated run."""

    @require_roles("MANAGER")
    def post(self, request, pk):
        period = _payroll_period(tenant_of(request.user), pk)
        if not period:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if period.status != "AWAITING_MANAGER":
            return Response(
                {"errors": {"message": "This run is not awaiting approval."}}, status=400
            )
        approved = (request.data.get("decision") or "").upper() == "APPROVED"
        period.status = "APPROVED" if approved else "OPEN"
        period.decided_by = actor(request.user)
        period.decided_at = timezone.now()
        period.decision_note = request.data.get("note", "")
        period.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
        return Response({"data": _period_payload(period)})


class PayrollMarkPaidView(APIView):
    """HR hands payslips to Finance; the Manager may settle runs directly."""

    @require_roles("HR", "MANAGER")
    def post(self, request, pk):
        period = _payroll_period(tenant_of(request.user), pk)
        if not period:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if period.status not in ("APPROVED", "AWAITING_FINANCE"):
            return Response(
                {"errors": {"message": "The Manager must approve this run first."}},
                status=400,
            )
        qs = period.payslips.filter(status="UNPAID")
        ids = request.data.get("payslip_ids") or []
        if ids:
            qs = qs.filter(id__in=ids)
        slips = list(qs)
        if not slips:
            return Response(
                {"errors": {"message": "No unpaid payslips matched that selection."}},
                status=400,
            )
        now = timezone.now()
        who = actor(request.user)
        for slip in slips:
            slip.status = "AWAITING_FINANCE"
            slip.hr_marked_paid_at = now
            slip.hr_marked_paid_by = who
            slip.manager_approved_at = None
            slip.manager_approved_by = ""
        PayrollEntry.objects.bulk_update(
            slips,
            [
                "status",
                "hr_marked_paid_at",
                "hr_marked_paid_by",
                "manager_approved_at",
                "manager_approved_by",
            ],
        )
        period.refresh_from_db()
        _sync_period_status(period)
        return Response({"data": _period_payload(_payroll_period(period.tenant, period.pk))})


class PayrollPaymentDecisionView(APIView):
    """Finance confirms or rejects the mark-paid hand-off."""

    @require_roles("FINANCE", "MANAGER")
    def post(self, request, pk):
        period = _payroll_period(tenant_of(request.user), pk)
        if not period:
            return Response({"errors": {"message": "Not found."}}, status=404)
        qs = period.payslips.filter(status="AWAITING_FINANCE")
        ids = request.data.get("payslip_ids") or []
        if ids:
            qs = qs.filter(id__in=ids)
        slips = list(qs)
        if not slips:
            return Response(
                {"errors": {"message": "No payslips are awaiting Finance in this run."}},
                status=400,
            )
        approve = (request.data.get("decision") or "").upper() == "APPROVED"
        now = timezone.now()
        who = actor(request.user)
        for slip in slips:
            if approve:
                slip.status = "MARKED_PAID"
                slip.manager_approved_at = now
                slip.manager_approved_by = who
            else:
                slip.status = "UNPAID"
                slip.hr_marked_paid_at = None
                slip.hr_marked_paid_by = ""
                slip.manager_approved_at = None
                slip.manager_approved_by = ""
        PayrollEntry.objects.bulk_update(
            slips,
            [
                "status",
                "hr_marked_paid_at",
                "hr_marked_paid_by",
                "manager_approved_at",
                "manager_approved_by",
            ],
        )
        if approve:
            for slip in slips:
                notify(
                    period.tenant,
                    slip.employee,
                    "PAYROLL",
                    f"Payslip paid · {period.period_key}",
                    f"Net pay {slip.net} confirmed by Finance.",
                    "/payslips",
                    "PAID",
                )
        period.refresh_from_db()
        _sync_period_status(period)
        return Response({"data": _period_payload(_payroll_period(period.tenant, period.pk))})


class PayrollCloseView(APIView):
    """Only a fully paid run closes; nothing auto-closes."""

    @require_roles("FINANCE", "MANAGER")
    def post(self, request, pk):
        period = _payroll_period(tenant_of(request.user), pk)
        if not period:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if period.status == "CLOSED":
            return Response({"errors": {"message": "This run is already closed."}}, status=400)
        open_slips = period.payslips.exclude(status="MARKED_PAID").count()
        if not period.payslips.exists() or open_slips:
            return Response(
                {
                    "errors": {
                        "message": "Every payslip must be marked paid before closing."
                    }
                },
                status=400,
            )
        period.status = "CLOSED"
        period.closed_by = actor(request.user)
        period.closed_at = timezone.now()
        period.save(update_fields=["status", "closed_by", "closed_at"])
        return Response({"data": _period_payload(period)})


class DocumentListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = HrDocument.objects.filter(tenant=tenant).select_related("employee")
        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())
        if request.query_params.get("queue") == "1":
            qs = qs.filter(status="PENDING") if acting_role(request.user) == "MANAGER" else qs.none()
        return Response({"data": DocumentSerializer(qs, many=True).data})

    @require_roles("HR")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee = None
        if request.data.get("employee"):
            employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        row = HrDocument.objects.create(
            tenant=tenant,
            employee=employee,
            title=request.data.get("title", "Untitled"),
            kind=request.data.get("kind", "Contract"),
            file_url=request.data.get("file_url", ""),
            uploaded_by=request.user.get_full_name() or request.user.username,
        )
        # Multipart upload goes straight to media/hr/documents/.
        upload = request.FILES.get("file")
        if upload is not None:
            if upload.size > 10 * 1024 * 1024:
                return Response(
                    {"errors": {"message": "Keep the file under 10 MB."}}, status=400
                )
            extension = os.path.splitext(upload.name)[1].lower() or ".pdf"
            row.file.save(f"{uuid4().hex}{extension}", upload, save=True)
        # A pre-uploaded path (from DocumentUploadView) may be passed as `file`.
        elif request.data.get("file"):
            row.file.name = request.data["file"]
            row.save(update_fields=["file"])
        return Response({"data": DocumentSerializer(row).data}, status=201)


class DocumentUploadView(APIView):
    """Store a raw file under media/hr/documents/ and return its path + URL."""

    @require_roles("HR", "MANAGER")
    def post(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"errors": {"message": "Attach a file."}}, status=400)
        if upload.size > 10 * 1024 * 1024:
            return Response(
                {"errors": {"message": "Keep the file under 10 MB."}}, status=400
            )
        extension = os.path.splitext(upload.name)[1].lower() or ".pdf"
        path = default_storage.save(f"hr/documents/{uuid4().hex}{extension}", upload)
        return Response(
            {
                "data": {
                    "path": path,
                    "url": request.build_absolute_uri(default_storage.url(path)),
                }
            },
            status=201,
        )


class DocumentDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = HrDocument.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


class DocumentDecisionView(APIView):
    """Manager approves or rejects an uploaded HR document."""

    @require_roles("MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        row = HrDocument.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        approved = (request.data.get("decision") or "").upper() == "APPROVED"
        row.status = "APPROVED" if approved else "REJECTED"
        row.decided_by = request.user.get_full_name() or request.user.username
        row.decided_at = timezone.now()
        row.decision_note = request.data.get("note", "")
        row.save()
        notify(
            tenant,
            row.employee,
            "DOCUMENT",
            f"{row.kind} {'approved' if approved else 'rejected'}: {row.title}",
            row.decision_note or f"Decided by {row.decided_by}",
            "/profile",
            row.status,
        )
        return Response({"data": DocumentSerializer(row).data})


class IncidentListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = Incident.objects.filter(tenant=tenant).select_related("employee")
        if request.query_params.get("queue") == "1":
            qs = qs.filter(status="OPEN") if acting_role(request.user) == "MANAGER" else qs.none()
        return Response({"data": IncidentSerializer(qs, many=True).data})

    @require_roles("HR")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee = None
        if request.data.get("employee"):
            employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        kind = request.data.get("kind", "Conduct")
        # Managed incident types supply the default salary-deduct flag.
        incident_type = IncidentType.objects.filter(
            tenant=tenant, active=True
        ).filter(Q(code=kind) | Q(label=kind)).first()
        salary_deduct = request.data.get("salary_deduct")
        if salary_deduct is None and incident_type is not None:
            salary_deduct = incident_type.deduct
        amount = request.data.get("amount_etb") or 0
        if (
            not amount
            and incident_type is not None
            and employee is not None
            and incident_type.percent_of_salary
        ):
            amount = (
                (employee.salary or Decimal("0"))
                * Decimal(str(incident_type.percent_of_salary))
                / Decimal("100")
            ).quantize(Decimal("0.01"))
        row = Incident.objects.create(
            tenant=tenant,
            employee=employee,
            kind=kind,
            title=request.data.get("title", "Incident"),
            detail=request.data.get("detail", ""),
            occurred_ymd=request.data.get("occurred_ymd"),
            salary_deduct=bool(salary_deduct),
            amount_etb=amount,
        )
        return Response({"data": IncidentSerializer(row).data}, status=201)


class IncidentDecisionView(APIView):
    @require_roles("MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        row = Incident.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.status = (
            "RESOLVED" if (request.data.get("decision") or "").upper() == "APPROVED" else "OPEN"
        )
        row.save(update_fields=["status"])
        notify(
            tenant,
            row.employee,
            "INCIDENT",
            f"Incident resolved: {row.title}",
            row.detail,
            "/profile",
            row.status,
        )
        return Response({"data": IncidentSerializer(row).data})


class OtpResetView(APIView):
    """Generate a fresh 6-digit portal OTP. The new code is returned once."""

    @require_roles("HR", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        row = Employee.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        otp = f"{random.randint(0, 999999):06d}"
        row.portal_otp = otp
        row.must_change_otp = True
        row.otp_reset_by = request.user.get_full_name() or request.user.username
        row.otp_reset_at = timezone.now()
        row.save(
            update_fields=[
                "portal_otp",
                "must_change_otp",
                "otp_reset_by",
                "otp_reset_at",
            ]
        )
        notify(
            tenant,
            row,
            "PORTAL",
            "Portal access code reset",
            "A new self-service code was issued. Ask your manager for the new code.",
            "/change-otp",
            "INFO",
        )
        return Response(
            {
                "data": {
                    "employee_id": row.id,
                    "full_name": row.full_name,
                    "portal_otp": otp,
                    "reset_by": row.otp_reset_by,
                    "reset_at": row.otp_reset_at,
                }
            }
        )


class NotificationListCreateView(APIView):
    """HR/Manager view of the portal inbox, and manual sends."""

    @require_roles("HR", "MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = HrNotification.objects.filter(tenant=tenant).select_related("employee")
        employee = request.query_params.get("employee")
        if employee:
            qs = qs.filter(employee_id=employee)
        if request.query_params.get("unread") == "1":
            qs = qs.filter(read_at__isnull=True)
        return Response({"data": HrNotificationSerializer(qs[:200], many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        title = (request.data.get("title") or "").strip()
        if not employee or not title:
            return Response(
                {"errors": {"message": "Choose an employee and a title."}}, status=400
            )
        row = notify(
            tenant,
            employee,
            request.data.get("kind") or "INFO",
            title,
            request.data.get("body", ""),
            request.data.get("href", ""),
        )
        return Response({"data": HrNotificationSerializer(row).data}, status=201)


class NotificationReadView(APIView):
    @require_roles("HR", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        row = HrNotification.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.read_at = timezone.now()
        row.save(update_fields=["read_at"])
        return Response({"data": HrNotificationSerializer(row).data})


class ChatPeerListView(APIView):
    @require_roles("HR", "MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = (
            Employee.objects.filter(tenant=tenant)
            .exclude(status="TERMINATED")
            .select_related("department")
        )
        return Response(
            {
                "data": [
                    {
                        "id": row.id,
                        "full_name": row.full_name,
                        "department": row.department.label if row.department else "",
                        "job_title": row.job_title,
                        "status": row.status,
                    }
                    for row in rows
                ]
            }
        )


class ChatThreadListCreateView(APIView):
    """Manager-side inbox: every employee conversation in the tenant."""

    @require_roles("HR", "MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = (
            ChatThread.objects.filter(tenant=tenant)
            .prefetch_related("members", "messages")
        )
        return Response({"data": ChatThreadSerializer(rows, many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee_ids = request.data.get("employee_ids") or []
        if not employee_ids:
            return Response(
                {"errors": {"message": "Choose at least one employee."}}, status=400
            )
        employees = list(
            Employee.objects.filter(tenant=tenant, pk__in=employee_ids)
        )
        if not employees:
            return Response(
                {"errors": {"message": "Choose employees from this tenant."}}, status=400
            )
        kind = "MANAGER" if len(employees) == 1 and not request.data.get("group") else "GROUP"
        thread = ChatThread.objects.create(
            tenant=tenant,
            kind=kind,
            title=request.data.get("title")
            or ", ".join(row.full_name for row in employees),
            created_by_role=role_of(request.user),
        )
        ChatMember.objects.create(
            thread=thread,
            is_manager=True,
            member_key=f"{role_of(request.user).lower()}-{request.user.pk}",
            employee_name=actor(request.user),
        )
        for employee in employees:
            ChatMember.objects.create(
                thread=thread,
                employee=employee,
                member_key=f"emp-{employee.pk}",
                employee_name=employee.full_name,
            )
        return Response(
            {"data": ChatThreadSerializer(thread).data}, status=201
        )


class ChatMessageListCreateView(APIView):
    @require_roles("HR", "MANAGER")
    def get(self, request, pk):
        tenant = tenant_of(request.user)
        thread = ChatThread.objects.filter(tenant=tenant, pk=pk).first()
        if not thread:
            return Response({"errors": {"message": "Not found."}}, status=404)
        rows = thread.messages.select_related("sender")
        return Response({"data": ChatMessageSerializer(rows, many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        thread = ChatThread.objects.filter(tenant=tenant, pk=pk).first()
        if not thread:
            return Response({"errors": {"message": "Not found."}}, status=404)
        body = (request.data.get("body") or "").strip()
        if not body and not request.data.get("image_url"):
            return Response(
                {"errors": {"message": "Write a message first."}}, status=400
            )
        row = ChatMessage.objects.create(
            thread=thread,
            sender_is_manager=True,
            sender_name=f"{actor(request.user)} · {role_of(request.user)}",
            body=body,
            image_url=request.data.get("image_url", ""),
        )
        thread.last_message_at = row.created_at
        thread.save(update_fields=["last_message_at", "updated_at"])
        for member in thread.members.filter(employee__isnull=False).select_related(
            "employee"
        ):
            notify(
                tenant,
                member.employee,
                "CHAT",
                f"New message in {thread.title or 'chat'}",
                body[:140],
                "/home",
            )
        return Response({"data": ChatMessageSerializer(row).data}, status=201)


class ChatThreadReadView(APIView):
    @require_roles("HR", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        thread = ChatThread.objects.filter(tenant=tenant, pk=pk).first()
        if not thread:
            return Response({"errors": {"message": "Not found."}}, status=404)
        thread.members.filter(employee__isnull=True).update(last_read_at=timezone.now())
        return Response({"data": ChatThreadSerializer(thread).data})


ATS_CREDENTIAL_TABLE = "ats_atsadmincredential"


def _ats_table_exists():
    return ATS_CREDENTIAL_TABLE in connection.introspection.table_names()


class AtsCredentialView(APIView):
    """Issue the unlock code the hiring desk (volosync-ats) signs in with."""

    @require_roles("HR", "MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        if not _ats_table_exists():
            return Response(
                {
                    "data": {
                        "provisioned": False,
                        "tenant_id": tenant.id,
                        "message": "The hiring desk has not been migrated yet.",
                    }
                }
            )
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT otp, must_change_otp, role FROM {ATS_CREDENTIAL_TABLE} WHERE tenant_id=%s",
                [tenant.id],
            )
            row = cursor.fetchone()
        if row is None:
            return Response(
                {
                    "data": {
                        "provisioned": False,
                        "tenant_id": tenant.id,
                        "message": "No hiring desk code has been issued yet.",
                    }
                }
            )
        return Response(
            {
                "data": {
                    "provisioned": True,
                    "tenant_id": tenant.id,
                    "otp": row[0],
                    "must_change_otp": bool(row[1]),
                    "role": row[2],
                }
            }
        )

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        if not _ats_table_exists():
            return Response(
                {"errors": {"message": "The hiring desk has not been migrated yet."}},
                status=400,
            )
        alphabet = string.ascii_uppercase + string.digits
        otp = "ATS" + "".join(secrets.choice(alphabet) for _ in range(5))
        role = (request.data.get("role") or "HR").upper()
        if role not in ("ADMIN", "HR", "MANAGER"):
            role = "HR"
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id FROM {ATS_CREDENTIAL_TABLE} WHERE tenant_id=%s",
                [tenant.id],
            )
            found = cursor.fetchone()
            if found:
                cursor.execute(
                    f"UPDATE {ATS_CREDENTIAL_TABLE} SET otp=%s, must_change_otp=1, role=%s WHERE id=%s",
                    [otp, role, found[0]],
                )
            else:
                cursor.execute(
                    f"INSERT INTO {ATS_CREDENTIAL_TABLE} (tenant_id, otp, must_change_otp, role) VALUES (%s,%s,%s,%s)",
                    [tenant.id, otp, 1, role],
                )
        return Response(
            {
                "data": {
                    "provisioned": True,
                    "tenant_id": tenant.id,
                    "otp": otp,
                    "must_change_otp": True,
                    "role": role,
                }
            }
        )


class IncidentTypeListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        return Response(
            {
                "data": IncidentTypeSerializer(
                    IncidentType.objects.filter(tenant=tenant), many=True
                ).data
            }
        )

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        code = (request.data.get("code") or request.data.get("label", "")).upper()[:40]
        if not code:
            return Response({"errors": {"code": "Code or label is required."}}, status=400)
        row, created = IncidentType.objects.get_or_create(
            tenant=tenant,
            code=code,
            defaults={
                "label": request.data.get("label", code.title()),
                "deduct": bool(request.data.get("deduct")),
                "percent_of_salary": request.data.get("percent_of_salary") or 0,
                "attendance_link": request.data.get("attendance_link") or "NONE",
            },
        )
        if not created:
            return Response(
                {"errors": {"code": "This code already exists."}}, status=400
            )
        return Response({"data": IncidentTypeSerializer(row).data}, status=201)


class IncidentTypeBulkCreateView(APIView):
    """Batch incident-type creation from the Manager's Add-line editor."""

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        created, skipped = [], []
        for item in request.data.get("types") or []:
            label = str(item.get("label", "")).strip()
            if not label:
                skipped.append({"label": label, "reason": "Type name is required."})
                continue
            code = (item.get("code") or label).upper()[:40]
            if IncidentType.objects.filter(tenant=tenant, code=code).exists():
                skipped.append({"label": label, "reason": "This code already exists."})
                continue
            row = IncidentType.objects.create(
                tenant=tenant,
                code=code,
                label=label,
                deduct=bool(item.get("deduct", False)),
                percent_of_salary=item.get("percent_of_salary") or 0,
                attendance_link=item.get("attendance_link") or "NONE",
                active=bool(item.get("active", True)),
            )
            created.append(IncidentTypeSerializer(row).data)
        return Response({"data": {"created": created, "skipped": skipped}}, status=201)


class IncidentTypeDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = IncidentType.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        for field in ["label", "deduct", "percent_of_salary", "attendance_link", "active"]:
            if field in request.data:
                setattr(row, field, request.data[field])
        row.save()
        return Response({"data": IncidentTypeSerializer(row).data})

    @require_roles("HR", "MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = IncidentType.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


class ShiftTemplateListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        return Response(
            {
                "data": ShiftTemplateSerializer(
                    ShiftTemplate.objects.filter(tenant=tenant).select_related("department"),
                    many=True,
                ).data
            }
        )

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        code = (request.data.get("code") or request.data.get("name", "")).upper()[:40]
        if not code:
            return Response({"errors": {"code": "Code or name is required."}}, status=400)
        if ShiftTemplate.objects.filter(tenant=tenant, code=code).exists():
            return Response(
                {"errors": {"code": "This code already exists."}}, status=400
            )
        department = None
        if request.data.get("department"):
            department = _tenant_fk(Department, tenant, request.data.get("department"))
        row = ShiftTemplate.objects.create(
            tenant=tenant,
            code=code,
            name=request.data.get("name", ""),
            department=department,
            start_time=request.data.get("start_time") or "09:00",
            end_time=request.data.get("end_time") or "17:00",
            weekdays=request.data.get("weekdays") or [1, 2, 3, 4, 5],
            note=request.data.get("note", ""),
            active=bool(request.data.get("active", True)),
        )
        return Response({"data": ShiftTemplateSerializer(row).data}, status=201)


class ShiftTemplateBulkCreateView(APIView):
    """Batch shift-template creation (Add line), sharing one default scope."""

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        department = None
        if request.data.get("department"):
            department = _tenant_fk(Department, tenant, request.data.get("department"))
        created, skipped = [], []
        for item in request.data.get("shifts") or []:
            name = str(item.get("name", "")).strip()
            if not name:
                skipped.append({"name": name, "reason": "Shift name is required."})
                continue
            code = str(item.get("code") or name).upper()[:40]
            if ShiftTemplate.objects.filter(tenant=tenant, code=code).exists():
                skipped.append({"name": name, "reason": "This code already exists."})
                continue
            row = ShiftTemplate.objects.create(
                tenant=tenant,
                code=code,
                name=name,
                department=(
                    Department.objects.filter(
                        pk=item.get("department"), tenant=tenant
                    ).first()
                    if item.get("department")
                    else department
                ),
                start_time=item.get("start_time") or "09:00",
                end_time=item.get("end_time") or "17:00",
                weekdays=item.get("weekdays") or [1, 2, 3, 4, 5],
                note=item.get("note", ""),
                active=bool(item.get("active", True)),
            )
            created.append(ShiftTemplateSerializer(row).data)
        return Response({"data": {"created": created, "skipped": skipped}}, status=201)


class ShiftTemplateDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = ShiftTemplate.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        for field in ["name", "start_time", "end_time", "weekdays", "note", "active"]:
            if field in request.data:
                setattr(row, field, request.data[field])
        if request.data.get("code"):
            row.code = str(request.data["code"]).upper()[:40]
        if "department" in request.data:
            row.department = _tenant_fk(Department, tenant, request.data.get("department"))
        row.save()
        return Response({"data": ShiftTemplateSerializer(row).data})

    @require_roles("HR", "MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = ShiftTemplate.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


class WorkflowListCreateView(APIView):
    """HR approval-flow config (self-contained; no imports from approvals app)."""

    def get(self, request):
        tenant = tenant_of(request.user)
        rows = HrWorkflow.objects.filter(tenant=tenant).select_related("department")
        request_type = request.query_params.get("request_type")
        if request_type:
            rows = rows.filter(request_type=request_type)
        return Response({"data": HrWorkflowSerializer(rows, many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        request_type = (request.data.get("request_type") or "").strip()
        if not request_type:
            return Response(
                {"errors": {"request_type": "Request type is required."}}, status=400
            )
        department = None
        if request.data.get("department"):
            department = _tenant_fk(Department, tenant, request.data.get("department"))
        row = HrWorkflow.objects.filter(
            tenant=tenant,
            request_type=request_type,
            department=department,
        ).first()
        if row is None:
            row = HrWorkflow(tenant=tenant, request_type=request_type)
        row.department = department
        row.require_team_leader_first = bool(
            request.data.get("require_team_leader_first", row.require_team_leader_first)
        )
        if request.data.get("steps") is not None:
            row.steps = request.data["steps"]
        if "active" in request.data:
            row.active = bool(request.data["active"])
        row.save()
        return Response({"data": HrWorkflowSerializer(row).data}, status=201)


class WorkflowDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = HrWorkflow.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if request.data.get("steps") is not None:
            row.steps = request.data["steps"]
        for field in ["require_team_leader_first", "active"]:
            if field in request.data:
                setattr(row, field, bool(request.data[field]))
        if "department" in request.data:
            row.department = _tenant_fk(Department, tenant, request.data.get("department"))
        row.save()
        return Response({"data": HrWorkflowSerializer(row).data})

    @require_roles("HR", "MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = HrWorkflow.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


class PeopleEventListCreateView(APIView):
    """Employee lifecycle events: promotion / transfer / termination / disciplinary."""

    def get(self, request):
        tenant = tenant_of(request.user)
        qs = PeopleEvent.objects.filter(tenant=tenant).select_related(
            "employee", "from_department", "to_department"
        )
        kind = request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())
        employee = request.query_params.get("employee")
        if employee:
            qs = qs.filter(employee_id=employee)
        return Response({"data": PeopleEventSerializer(qs, many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        if not employee:
            return Response({"errors": {"employee": "Choose an employee."}}, status=400)
        kind = request.data.get("kind") or "other"
        from_department = employee.department
        row = PeopleEvent.objects.create(
            tenant=tenant,
            employee=employee,
            kind=kind,
            detail=request.data.get("detail", ""),
            from_department=from_department,
            to_department=_tenant_fk(Department, tenant, request.data.get("to_department"))
            if request.data.get("to_department")
            else None,
            from_title=employee.job_title,
            to_title=request.data.get("to_title", ""),
            effective_ymd=request.data.get("effective_ymd") or None,
            created_by=request.user.get_full_name() or request.user.username,
        )
        return Response({"data": PeopleEventSerializer(row).data}, status=201)


class PeopleEventDecisionView(APIView):
    """Manager approves a lifecycle event; approval applies it to the employee."""

    @require_roles("MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        row = PeopleEvent.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        approved = (request.data.get("decision") or "").upper() == "APPROVED"
        row.status = "APPROVED" if approved else "REJECTED"
        row.decided_by = request.user.get_full_name() or request.user.username
        row.decided_at = timezone.now()
        row.decision_note = request.data.get("note", "")
        row.save()
        if approved:
            employee = row.employee
            if row.kind in ("promotion", "transfer"):
                if row.to_title:
                    employee.job_title = row.to_title
                if row.to_department is not None:
                    employee.department = row.to_department
                employee.save()
            elif row.kind == "termination":
                employee.status = "TERMINATED"
                employee.save(update_fields=["status"])
        notify(
            tenant,
            row.employee,
            "PEOPLE",
            f"{row.get_kind_display()} {'approved' if approved else 'rejected'}",
            row.decision_note or row.detail,
            "/profile",
            row.status,
        )
        return Response({"data": PeopleEventSerializer(row).data})


class ChecklistTemplateListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = ChecklistTemplate.objects.filter(tenant=tenant).prefetch_related("items")
        kind = request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        return Response({"data": ChecklistTemplateSerializer(qs, many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        name = request.data.get("name", "").strip()
        if not name:
            return Response({"errors": {"name": "Name is required."}}, status=400)
        department = None
        if request.data.get("department"):
            department = _tenant_fk(Department, tenant, request.data.get("department"))
        team = None
        if request.data.get("team"):
            team = _tenant_fk(Team, tenant, request.data.get("team"))
        row = ChecklistTemplate.objects.create(
            tenant=tenant,
            kind=request.data.get("kind") or "onboarding",
            name=name,
            department=department,
            team=team,
            job_title=request.data.get("job_title", ""),
            active=bool(request.data.get("active", True)),
        )
        for index, item in enumerate(request.data.get("items") or []):
            ChecklistTemplateItem.objects.create(
                template=row,
                label=str(item.get("label", f"Step {index + 1}")),
                required=bool(item.get("required", True)),
                owner_role=item.get("owner_role", ""),
                sort_order=int(item.get("sort_order", index)),
            )
        row = ChecklistTemplate.objects.prefetch_related("items").get(pk=row.pk)
        return Response({"data": ChecklistTemplateSerializer(row).data}, status=201)


class ChecklistTemplateDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = ChecklistTemplate.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        for field in ["kind", "name", "job_title", "active"]:
            if field in request.data:
                setattr(row, field, request.data[field])
        if "department" in request.data:
            row.department = _tenant_fk(Department, tenant, request.data.get("department"))
        if "team" in request.data:
            row.team = _tenant_fk(Team, tenant, request.data.get("team"))
        row.save()
        if request.data.get("items") is not None:
            row.items.all().delete()
            for index, item in enumerate(request.data["items"]):
                ChecklistTemplateItem.objects.create(
                    template=row,
                    label=str(item.get("label", f"Step {index + 1}")),
                    required=bool(item.get("required", True)),
                    owner_role=item.get("owner_role", ""),
                    sort_order=int(item.get("sort_order", index)),
                )
        row = ChecklistTemplate.objects.prefetch_related("items").get(pk=row.pk)
        return Response({"data": ChecklistTemplateSerializer(row).data})

    @require_roles("HR", "MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = ChecklistTemplate.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


class ChecklistRunListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = ChecklistRun.objects.filter(tenant=tenant).select_related("employee").prefetch_related("items")
        employee = request.query_params.get("employee")
        if employee:
            qs = qs.filter(employee_id=employee)
        kind = request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())
        day_from, day_to = _dates(request)
        if day_from:
            qs = qs.filter(created_at__date__gte=day_from)
        if day_to:
            qs = qs.filter(created_at__date__lte=day_to)
        return Response({"data": ChecklistRunSerializer(qs, many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        """Start a run for an employee; items are copied from the template."""
        tenant = tenant_of(request.user)
        employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        if not employee:
            return Response({"errors": {"employee": "Choose an employee."}}, status=400)
        template = _tenant_fk(ChecklistTemplate, tenant, request.data.get("template"))
        if not template:
            return Response({"errors": {"template": "Choose a checklist template."}}, status=400)
        run = ChecklistRun.objects.create(
            tenant=tenant,
            template=template,
            employee=employee,
            kind=template.kind,
            created_by=request.user.get_full_name() or request.user.username,
        )
        for item in template.items.all():
            ChecklistRunItem.objects.create(
                run=run,
                label=item.label,
                required=item.required,
                owner_role=item.owner_role,
                sort_order=item.sort_order,
            )
        run = ChecklistRun.objects.prefetch_related("items").get(pk=run.pk)
        return Response({"data": ChecklistRunSerializer(run).data}, status=201)


class ChecklistRunDetailView(APIView):
    def get(self, request, pk):
        tenant = tenant_of(request.user)
        row = ChecklistRun.objects.filter(tenant=tenant, pk=pk).prefetch_related("items").first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        return Response({"data": ChecklistRunSerializer(row).data})


class ChecklistRunItemToggleView(APIView):
    @require_roles("HR", "MANAGER")
    def post(self, request, pk, item_id):
        tenant = tenant_of(request.user)
        run = ChecklistRun.objects.filter(tenant=tenant, pk=pk).first()
        if not run:
            return Response({"errors": {"message": "Not found."}}, status=404)
        item = ChecklistRunItem.objects.filter(run=run, pk=item_id).first()
        if not item:
            return Response({"errors": {"message": "Not found."}}, status=404)
        done = request.data.get("done")
        item.done = bool(done) if done is not None else not item.done
        if item.done:
            item.completed_by = request.user.get_full_name() or request.user.username
            item.completed_at = timezone.now()
        else:
            item.completed_by = ""
            item.completed_at = None
        if "note" in request.data:
            item.note = request.data.get("note", "")
        item.save()
        return Response({"data": {"id": item.id, "done": item.done, "completed_by": item.completed_by, "completed_at": item.completed_at, "note": item.note}})


class ChecklistRunCompleteView(APIView):
    """Mark a run COMPLETED once every required item is done."""

    @require_roles("HR", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        run = ChecklistRun.objects.filter(tenant=tenant, pk=pk).prefetch_related("items").first()
        if not run:
            return Response({"errors": {"message": "Not found."}}, status=404)
        missing = [item.label for item in run.items.all() if item.required and not item.done]
        if missing:
            return Response(
                {"errors": {"message": "Required items still open.", "items": missing}},
                status=400,
            )
        run.status = "COMPLETED"
        run.save(update_fields=["status"])
        return Response({"data": ChecklistRunSerializer(run).data})


class ChecklistRunDecisionView(APIView):
    """Manager approves or rejects a completed run (HotCol exit checklist flow)."""

    @require_roles("MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        run = ChecklistRun.objects.filter(tenant=tenant, pk=pk).prefetch_related("items").first()
        if not run:
            return Response({"errors": {"message": "Not found."}}, status=404)
        approved = (request.data.get("decision") or "").upper() == "APPROVED"
        run.status = "APPROVED" if approved else "REJECTED"
        run.decided_by = request.user.get_full_name() or request.user.username
        run.decided_at = timezone.now()
        run.save()
        notify(
            tenant,
            run.employee,
            "CHECKLIST",
            f"{run.get_kind_display()} checklist {'approved' if approved else 'rejected'}",
            f"Decided by {run.decided_by}",
            "/home",
            run.status,
        )
        return Response({"data": ChecklistRunSerializer(run).data})


class AttendanceCorrectionListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = AttendanceCorrection.objects.filter(tenant=tenant).select_related("employee")
        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())
        if request.query_params.get("queue") == "1":
            qs = qs.filter(status="PENDING") if acting_role(request.user) == "MANAGER" else qs.none()
        return Response({"data": AttendanceCorrectionSerializer(qs, many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        if not employee:
            return Response({"errors": {"employee": "Choose an employee."}}, status=400)
        day = request.data.get("day")
        attendance = Attendance.objects.filter(employee=employee, day=day).first()
        row = AttendanceCorrection.objects.create(
            tenant=tenant,
            attendance=attendance,
            employee=employee,
            day=day,
            new_status=request.data.get("new_status") or request.data.get("status") or "PRESENT",
            new_check_in=request.data.get("new_check_in") or request.data.get("check_in") or None,
            new_check_out=request.data.get("new_check_out") or request.data.get("check_out") or None,
            reason=request.data.get("reason", ""),
            requested_by=request.user.get_full_name() or request.user.username,
        )
        return Response({"data": AttendanceCorrectionSerializer(row).data}, status=201)


class AttendanceCorrectionDecisionView(APIView):
    """Manager decides; approval writes the corrected values onto Attendance."""

    @require_roles("MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        row = AttendanceCorrection.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        approved = (request.data.get("decision") or "").upper() == "APPROVED"
        row.status = "APPROVED" if approved else "REJECTED"
        row.decided_by = request.user.get_full_name() or request.user.username
        row.decided_at = timezone.now()
        row.decision_note = request.data.get("note", "")
        row.save()
        if approved:
            attendance = row.attendance or Attendance.objects.filter(
                employee=row.employee, day=row.day
            ).first()
            if attendance is None:
                attendance = Attendance.objects.create(
                    tenant=tenant,
                    employee=row.employee,
                    day=row.day,
                )
            attendance.status = row.new_status
            attendance.check_in = row.new_check_in
            attendance.check_out = row.new_check_out
            attendance.note = row.reason or attendance.note
            attendance.save()
            row.attendance = attendance
            row.save(update_fields=["attendance"])
        notify(
            tenant,
            row.employee,
            "ATTENDANCE",
            f"Attendance correction {'approved' if approved else 'rejected'} · {row.day}",
            row.decision_note or f"Recorded as {row.new_status}",
            "/attendance",
            row.status,
        )
        return Response({"data": AttendanceCorrectionSerializer(row).data})


class ManagerApprovalQueueView(APIView):
    """Every HR item awaiting a Manager decision, as typed summary cards."""

    @require_roles("MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        who = request.user.get_full_name() or request.user.username
        items = []

        for row in LeaveRequest.objects.filter(
            tenant=tenant, status="PENDING"
        ).select_related("employee"):
            if leave_step_kind(
                row.tenant, row.employee, row.current_step_index
            ) not in ("manager", "admin"):
                continue
            items.append(
                {
                    "type": "leave",
                    "id": row.id,
                    "employee_id": row.employee_id,
                    "employee_name": row.employee.full_name,
                    "title": f"{row.leave_type} leave {row.from_ymd} → {row.to_ymd}",
                    "detail": row.reason,
                    "status": row.status,
                    "created_at": row.created_at,
                    "payload": LeaveRequestSerializer(row).data,
                }
            )
        for row in PayrollPeriod.objects.filter(
            tenant=tenant, status="AWAITING_MANAGER"
        ).prefetch_related("payslips"):
            count = row.payslips.count()
            items.append(
                {
                    "type": "payroll_period",
                    "id": row.id,
                    "employee_id": None,
                    "employee_name": None,
                    "title": f"Payroll run {row.period_key} · {count} payslip"
                    + ("" if count == 1 else "s"),
                    "detail": f"{row.from_ymd} → {row.to_ymd} · generated by {row.created_by or 'HR'}",
                    "status": row.status,
                    "created_at": row.created_at,
                    "payload": PayrollPeriodSerializer(row).data,
                }
            )
        for row in Incident.objects.filter(tenant=tenant, status="OPEN").select_related("employee"):
            items.append(
                {
                    "type": "incident",
                    "id": row.id,
                    "employee_id": row.employee_id,
                    "employee_name": row.employee.full_name if row.employee else None,
                    "title": row.title,
                    "detail": row.detail,
                    "status": row.status,
                    "created_at": row.created_at,
                    "payload": IncidentSerializer(row).data,
                }
            )
        for row in HrDocument.objects.filter(tenant=tenant, status="PENDING").select_related("employee"):
            items.append(
                {
                    "type": "document",
                    "id": row.id,
                    "employee_id": row.employee_id,
                    "employee_name": row.employee.full_name if row.employee else None,
                    "title": f"{row.kind}: {row.title}",
                    "detail": f"Uploaded by {row.uploaded_by or 'HR'}",
                    "status": row.status,
                    "created_at": row.uploaded_at,
                    "payload": DocumentSerializer(row).data,
                }
            )
        for row in AttendanceCorrection.objects.filter(tenant=tenant, status="PENDING").select_related("employee"):
            items.append(
                {
                    "type": "attendance_correction",
                    "id": row.id,
                    "employee_id": row.employee_id,
                    "employee_name": row.employee.full_name,
                    "title": f"Attendance correction {row.day} → {row.new_status}",
                    "detail": row.reason,
                    "status": row.status,
                    "created_at": row.created_at,
                    "payload": AttendanceCorrectionSerializer(row).data,
                }
            )
        for row in PeopleEvent.objects.filter(tenant=tenant, status="PENDING").select_related("employee"):
            items.append(
                {
                    "type": "people_event",
                    "id": row.id,
                    "employee_id": row.employee_id,
                    "employee_name": row.employee.full_name,
                    "title": f"{row.get_kind_display()} {row.employee.full_name}",
                    "detail": row.detail,
                    "status": row.status,
                    "created_at": row.created_at,
                    "payload": PeopleEventSerializer(row).data,
                }
            )
        for row in OtpResetRequest.objects.filter(
            tenant=tenant, status="PENDING"
        ).select_related("employee"):
            items.append(
                {
                    "type": "otp_reset",
                    "id": row.id,
                    "employee_id": row.employee_id,
                    "employee_name": row.employee.full_name,
                    "title": f"Portal OTP reset · {row.employee.full_name}",
                    "detail": row.reason,
                    "status": row.status,
                    "created_at": row.created_at,
                    "payload": OtpResetRequestSerializer(row).data,
                }
            )
        for row in ChecklistRun.objects.filter(tenant=tenant, status="COMPLETED").select_related("employee"):
            items.append(
                {
                    "type": "checklist_run",
                    "id": row.id,
                    "employee_id": row.employee_id,
                    "employee_name": row.employee.full_name,
                    "title": f"{row.get_kind_display()} checklist · {row.employee.full_name}",
                    "detail": f"Started by {row.created_by or 'HR'}",
                    "status": row.status,
                    "created_at": row.created_at,
                    "payload": ChecklistRunSerializer(row).data,
                }
            )

        items.sort(key=lambda item: item["created_at"] or timezone.now(), reverse=True)
        payload = [
            {**item, "created_at": item["created_at"].isoformat() if item["created_at"] else None}
            for item in items
        ]
        return Response({"data": {"count": len(payload), "items": payload, "role": role_of(request.user), "who": who}})


def _new_portal_otp():
    return f"{random.randint(0, 999999):06d}"


class OtpResetListCreateView(APIView):
    """HR raises portal OTP reset requests; the Manager decides them."""

    @require_roles("HR", "MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = OtpResetRequest.objects.filter(tenant=tenant).select_related(
            "employee", "employee__department"
        )
        if request.query_params.get("queue") == "1":
            rows = rows.filter(status__in=["PENDING", "APPROVED"])
        status_param = request.query_params.get("status")
        if status_param:
            rows = rows.filter(status=status_param.upper())
        return Response({"data": OtpResetRequestSerializer(rows[:200], many=True).data})

    @require_roles("HR")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        if not employee:
            return Response(
                {"errors": {"employee": "Choose an employee."}}, status=400
            )
        if employee.status == "TERMINATED":
            return Response(
                {"errors": {"employee": "This employee has left the roster."}},
                status=400,
            )
        open_request = OtpResetRequest.objects.filter(
            tenant=tenant, employee=employee, status__in=["PENDING", "APPROVED"]
        ).exists()
        if open_request:
            return Response(
                {
                    "errors": {
                        "message": "This employee already has an open OTP reset request."
                    }
                },
                status=400,
            )
        row = OtpResetRequest.objects.create(
            tenant=tenant,
            employee=employee,
            reason=request.data.get("reason", ""),
            requested_by=actor(request.user),
        )
        return Response({"data": OtpResetRequestSerializer(row).data}, status=201)


class OtpResetDecisionView(APIView):
    """Manager approval issues the actual portal code."""

    @require_roles("MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        row = OtpResetRequest.objects.filter(
            tenant=tenant, pk=pk, status="PENDING"
        ).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        approved = (request.data.get("decision") or "").upper() == "APPROVED"
        who = actor(request.user)
        note = request.data.get("note", "")
        if approved:
            otp = _new_portal_otp()
            employee = row.employee
            employee.portal_otp = otp
            employee.must_change_otp = True
            employee.otp_reset_by = who
            employee.otp_reset_at = timezone.now()
            employee.save(
                update_fields=[
                    "portal_otp",
                    "must_change_otp",
                    "otp_reset_by",
                    "otp_reset_at",
                ]
            )
            row.status = "APPROVED"
            row.otp_code = otp
            notify(
                tenant,
                employee,
                "PORTAL",
                "Portal access code approved",
                "Your reset was approved. Sign in with the code from your manager and set a new one.",
                "/change-otp",
                "APPROVED",
            )
        else:
            row.status = "REJECTED"
            notify(
                tenant,
                row.employee,
                "PORTAL",
                "Portal access reset declined",
                note or "Ask HR if you still need a new code.",
                "/home",
                "REJECTED",
            )
        row.decided_by = who
        row.decided_at = timezone.now()
        row.decision_note = note
        row.save(
            update_fields=[
                "status",
                "otp_code",
                "decided_by",
                "decided_at",
                "decision_note",
            ]
        )
        return Response({"data": OtpResetRequestSerializer(row).data})


def _ats_code():
    alphabet = string.ascii_uppercase + string.digits
    return "ATS" + "".join(secrets.choice(alphabet) for _ in range(5))


def _ats_role(value):
    role = (value or "HR").upper()
    return role if role in ("HR", "MANAGER") else "HR"


class AtsOtpListCreateView(APIView):
    """Hiring-desk unlock codes for the HR and Manager ATS seats."""

    @require_roles("MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = AtsOtp.objects.filter(tenant=tenant)
        status_param = (request.query_params.get("status") or "ACTIVE").upper()
        if status_param != "ALL":
            rows = rows.filter(status=status_param)
        return Response({"data": AtsOtpSerializer(rows, many=True).data})

    @require_roles("MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        role = _ats_role(request.data.get("role"))
        existing = AtsOtp.objects.filter(tenant=tenant, role=role).first()
        if existing and existing.status == "ACTIVE":
            return Response(
                {
                    "errors": {
                        "message": f"An active {role.lower()} code already exists — reset it instead."
                    }
                },
                status=400,
            )
        code = _ats_code()
        if existing:
            AtsOtp.objects.filter(pk=existing.pk).update(
                code=code,
                status="ACTIVE",
                must_change_otp=True,
                generated_by=actor(request.user),
                generated_at=timezone.now(),
                used_at=None,
            )
            row = AtsOtp.objects.get(pk=existing.pk)
        else:
            row = AtsOtp.objects.create(
                tenant=tenant,
                role=role,
                code=code,
                generated_by=actor(request.user),
            )
        AtsOtpAudit.objects.create(
            tenant=tenant,
            role=role,
            code=code,
            action="CREATED",
            actor=actor(request.user),
        )
        return Response({"data": AtsOtpSerializer(row).data}, status=201)


class AtsOtpResetView(APIView):
    @require_roles("MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        row = AtsOtp.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        code = _ats_code()
        AtsOtp.objects.filter(pk=row.pk).update(
            code=code,
            status="ACTIVE",
            must_change_otp=True,
            generated_by=actor(request.user),
            generated_at=timezone.now(),
            used_at=None,
        )
        row.refresh_from_db()
        AtsOtpAudit.objects.create(
            tenant=tenant,
            role=row.role,
            code=code,
            action="RESET",
            actor=actor(request.user),
        )
        return Response({"data": AtsOtpSerializer(row).data})


class AtsOtpDetailView(APIView):
    @require_roles("MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = AtsOtp.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        AtsOtpAudit.objects.create(
            tenant=tenant,
            role=row.role,
            code=row.code,
            action="DELETED",
            actor=actor(request.user),
            note=request.query_params.get("note", ""),
        )
        row.delete()
        return Response({"data": {"deleted": True}})


class AtsOtpAuditView(APIView):
    @require_roles("MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = AtsOtpAudit.objects.filter(tenant=tenant)
        role = request.query_params.get("role")
        if role:
            rows = rows.filter(role=role.upper())
        return Response({"data": AtsOtpAuditSerializer(rows[:200], many=True).data})


class ChatBlockListCreateView(APIView):
    """Blocked chat pairs; supports a batch of pairs in one call."""

    @require_roles("MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = ChatBlock.objects.filter(tenant=tenant).select_related(
            "employee_a", "employee_b"
        )
        if request.query_params.get("all") != "1":
            rows = rows.filter(active=True)
        return Response({"data": ChatBlockSerializer(rows[:300], many=True).data})

    @require_roles("MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        payload = request.data.get("blocks")
        if payload is None:
            payload = [request.data]
        if not isinstance(payload, list) or not payload:
            return Response(
                {"errors": {"blocks": "Send at least one block."}}, status=400
            )
        who = actor(request.user)
        created = []
        skipped = []
        for entry in payload:
            employee_a = Employee.objects.filter(
                tenant=tenant, pk=entry.get("employee_a")
            ).first()
            if not employee_a:
                skipped.append({**entry, "reason": "Pick employee A."})
                continue
            employee_b = None
            if entry.get("employee_b"):
                employee_b = Employee.objects.filter(
                    tenant=tenant, pk=entry.get("employee_b")
                ).first()
                if not employee_b:
                    skipped.append({**entry, "reason": "Employee B not found."})
                    continue
                if employee_b.pk == employee_a.pk:
                    skipped.append({**entry, "reason": "Pick two different employees."})
                    continue
            duplicate = ChatBlock.objects.filter(
                tenant=tenant,
                employee_a=employee_a,
                employee_b=employee_b,
                active=True,
            ).exists()
            if duplicate:
                skipped.append({**entry, "reason": "Already blocked."})
                continue
            row = ChatBlock.objects.create(
                tenant=tenant,
                employee_a=employee_a,
                employee_b=employee_b,
                note=entry.get("note", request.data.get("note", "")),
                created_by=who,
            )
            created.append(ChatBlockSerializer(row).data)
        return Response(
            {"data": {"created": created, "skipped": skipped}}, status=201
        )


class ChatBlockDetailView(APIView):
    @require_roles("MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = ChatBlock.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if "note" in request.data:
            row.note = str(request.data.get("note") or "")
            row.save(update_fields=["note"])
        return Response({"data": ChatBlockSerializer(row).data})

    @require_roles("MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = ChatBlock.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


class ChatHistoryView(APIView):
    """Read-only transcript search across direct, group and manager threads."""

    @require_roles("MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        rows = ChatMessage.objects.filter(thread__tenant=tenant).select_related(
            "thread", "sender"
        )
        day_from = parse_date(request.query_params.get("from") or "")
        day_to = parse_date(request.query_params.get("to") or "")
        if day_from:
            rows = rows.filter(created_at__date__gte=day_from)
        if day_to:
            rows = rows.filter(created_at__date__lte=day_to)
        employee = request.query_params.get("employee")
        if employee:
            rows = rows.filter(thread__members__employee_id=employee).distinct()
        thread_type = (request.query_params.get("thread_type") or "ALL").upper()
        if thread_type == "DIRECT":
            rows = rows.filter(thread__kind__in=["DIRECT", "MANAGER"])
        elif thread_type == "GROUP":
            rows = rows.filter(thread__kind="GROUP")
        manager_filter = (request.query_params.get("manager") or "ALL").upper()
        if manager_filter in ("WITH", "INCL"):
            rows = rows.filter(thread__members__is_manager=True).distinct()
        elif manager_filter in ("EMP_ONLY", "EMPLOYEES"):
            rows = rows.exclude(thread__members__is_manager=True)
        payload = []
        for row in rows.order_by("-created_at")[:500]:
            payload.append(
                {
                    "id": row.id,
                    "thread": row.thread_id,
                    "thread_title": row.thread.title,
                    "thread_kind": row.thread.kind,
                    "sender": row.sender_id,
                    "sender_name": row.sender_name
                    or (row.sender.full_name if row.sender else "Manager"),
                    "sender_is_manager": row.sender_is_manager,
                    "body": row.body,
                    "image_url": row.image_url,
                    "created_at": row.created_at.isoformat(),
                }
            )
        return Response({"data": payload})


class TeamBulkCreateView(APIView):
    """Create many teams at once; duplicate codes are reported, not fatal."""

    @require_roles("MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        payload = request.data.get("teams")
        if not isinstance(payload, list) or not payload:
            return Response(
                {"errors": {"teams": "Send at least one team."}}, status=400
            )
        created = []
        skipped = []
        for entry in payload:
            code = (
                entry.get("code") or entry.get("label", "")
            ).strip().upper()[:40]
            label = (entry.get("label") or "").strip()
            if not code or not label:
                skipped.append({**entry, "reason": "Code and label are required."})
                continue
            if Team.objects.filter(tenant=tenant, code=code).exists():
                skipped.append({**entry, "reason": "Code already exists."})
                continue
            department = None
            department_id = entry.get("department") or request.data.get("department")
            if department_id:
                department = Department.objects.filter(
                    pk=department_id, tenant=tenant
                ).first()
            row = Team.objects.create(
                tenant=tenant,
                code=code,
                label=label,
                leader=entry.get("leader", ""),
                department=department,
                active=True,
            )
            created.append(TeamSerializer(row).data)
        return Response(
            {"data": {"created": created, "skipped": skipped}}, status=201
        )


class LeaveTypeBulkCreateView(APIView):
    @require_roles("MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        payload = request.data.get("types")
        if not isinstance(payload, list) or not payload:
            return Response(
                {"errors": {"types": "Send at least one leave type."}}, status=400
            )
        created = []
        skipped = []
        for entry in payload:
            label = (entry.get("label") or "").strip()
            if not label:
                skipped.append({**entry, "reason": "Label is required."})
                continue
            code = (entry.get("code") or label).strip().upper()[:40]
            if LeaveType.objects.filter(tenant=tenant, code=code).exists():
                skipped.append({**entry, "reason": "Code already exists."})
                continue
            row = LeaveType.objects.create(
                tenant=tenant,
                code=code,
                label=label,
                paid=bool(entry.get("paid", True)),
                quota_days=entry.get("quota_days") or 0,
                active=True,
            )
            created.append(LeaveTypeSerializer(row).data)
        return Response(
            {"data": {"created": created, "skipped": skipped}}, status=201
        )


class ManagerHrDashboardView(APIView):
    """Reporting payload behind the Manager HR dashboard."""

    @require_roles("MANAGER")
    def get(self, request):
        tenant = tenant_of(request.user)
        today = date.today()
        year_start = date(today.year, 1, 1)
        employees = Employee.objects.filter(tenant=tenant)

        by_department = list(
            employees.values("department__label")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        departments = [
            {
                "label": row["department__label"] or "Unassigned",
                "count": row["count"],
            }
            for row in by_department
        ]

        start = today - timedelta(days=13)
        attendance_rows = {
            row["day"]: row
            for row in Attendance.objects.filter(
                tenant=tenant, day__gte=start, day__lte=today
            )
            .values("day")
            .annotate(
                total=Count("id"),
                present=Count(
                    "id", filter=Q(status__in=["PRESENT", "LATE", "HALF_DAY"])
                ),
                late=Count("id", filter=Q(status="LATE")),
                absent=Count("id", filter=Q(status="ABSENT")),
            )
        }
        attendance = []
        for offset in range(14):
            day = start + timedelta(days=offset)
            row = attendance_rows.get(day, {})
            attendance.append(
                {
                    "day": day.isoformat(),
                    "total": row.get("total", 0),
                    "present": row.get("present", 0),
                    "late": row.get("late", 0),
                    "absent": row.get("absent", 0),
                }
            )

        leave_labels = {
            row.code: row.label for row in LeaveType.objects.filter(tenant=tenant)
        }
        leave_usage = [
            {
                "code": row["leave_type"],
                "label": leave_labels.get(row["leave_type"], row["leave_type"]),
                "days": float(row["days"] or 0),
            }
            for row in LeaveRequest.objects.filter(
                tenant=tenant,
                status="APPROVED",
                from_ymd__gte=year_start,
            )
            .values("leave_type")
            .annotate(days=Sum("days"))
            .order_by("-days")
        ]

        latest_period = (
            PayrollPeriod.objects.filter(tenant=tenant)
            .order_by("-from_ymd", "-id")
            .first()
        )
        payroll = None
        if latest_period:
            totals = PayrollEntry.objects.filter(
                payroll_period=latest_period
            ).aggregate(
                gross=Sum("gross"), deductions=Sum("deductions"), net=Sum("net")
            )
            payroll = {
                "id": latest_period.id,
                "period_key": latest_period.period_key,
                "status": latest_period.status,
                "payslips": latest_period.payslips.count(),
                "gross": str(totals["gross"] or 0),
                "deductions": str(totals["deductions"] or 0),
                "net": str(totals["net"] or 0),
            }

        recent_terminations = [
            {
                "id": row.id,
                "employee": row.employee.full_name,
                "effective_ymd": row.effective_ymd.isoformat()
                if row.effective_ymd
                else None,
                "status": row.status,
            }
            for row in PeopleEvent.objects.filter(
                tenant=tenant, kind="termination"
            )
            .select_related("employee")
            .order_by("-created_at")[:5]
        ]

        status_counts = {
            row["status"]: row["count"]
            for row in employees.values("status").annotate(count=Count("id"))
        }

        payload = {
            "headcount": employees.count(),
            "active": status_counts.get("ACTIVE", 0),
            "on_leave": status_counts.get("ON_LEAVE", 0),
            "terminated": status_counts.get("TERMINATED", 0),
            "departments": departments,
            "attendance": attendance,
            "leave_usage": leave_usage,
            "payroll": payroll,
            "pending": {
                "terminations": PeopleEvent.objects.filter(
                    tenant=tenant, kind="termination", status="PENDING"
                ).count(),
                "attendance": AttendanceCorrection.objects.filter(
                    tenant=tenant, status="PENDING"
                ).count(),
                "payroll": PayrollPeriod.objects.filter(
                    tenant=tenant, status="AWAITING_MANAGER"
                ).count(),
                "otp_resets": OtpResetRequest.objects.filter(
                    tenant=tenant, status="PENDING"
                ).count(),
                "otp_issued": OtpResetRequest.objects.filter(
                    tenant=tenant, status="APPROVED"
                ).count(),
            },
            "otp": {
                "portal_ready": employees.exclude(portal_otp="").count(),
                "first_login_pending": employees.filter(must_change_otp=True).count(),
                "ats_active": AtsOtp.objects.filter(
                    tenant=tenant, status="ACTIVE"
                ).count(),
                "resets_30d": OtpResetRequest.objects.filter(
                    tenant=tenant,
                    created_at__date__gte=today - timedelta(days=30),
                ).count(),
            },
            "recent_terminations": recent_terminations,
        }
        return Response({"data": payload})


class CompensationRequestListCreateView(APIView):
    """Payroll-affecting requests: salary change, advance, loan, bonus, overtime."""

    def get(self, request):
        tenant = tenant_of(request.user)
        qs = CompensationRequest.objects.filter(tenant=tenant).select_related(
            "employee", "employee__department", "applied_period"
        )
        kind = request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())
        employee = request.query_params.get("employee")
        if employee:
            qs = qs.filter(employee_id=employee)
        return Response({"data": CompensationRequestSerializer(qs, many=True).data})

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        if not employee:
            return Response({"errors": {"employee": "Choose an employee."}}, status=400)
        try:
            amount = Decimal(str(request.data.get("amount") or 0))
        except Exception:
            return Response({"errors": {"amount": "Enter a valid amount."}}, status=400)
        if amount <= 0:
            return Response(
                {"errors": {"amount": "Amount must be greater than zero."}}, status=400
            )
        row = CompensationRequest.objects.create(
            tenant=tenant,
            employee=employee,
            kind=request.data.get("kind") or "bonus",
            amount=amount,
            effective_ymd=request.data.get("effective_ymd") or None,
            detail=request.data.get("detail", ""),
            created_by=actor(request.user),
        )
        return Response({"data": CompensationRequestSerializer(row).data}, status=201)


class CompensationRequestDecisionView(APIView):
    @require_roles("MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        row = CompensationRequest.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if row.status != "PENDING":
            return Response(
                {"errors": {"message": "This request is already decided."}}, status=400
            )
        approved = (request.data.get("decision") or "").upper() == "APPROVED"
        row.status = "APPROVED" if approved else "REJECTED"
        row.decided_by = actor(request.user)
        row.decided_at = timezone.now()
        row.decision_note = request.data.get("note", "")
        row.save()
        notify(
            tenant,
            row.employee,
            "COMPENSATION",
            f"{row.get_kind_display()} {'approved' if approved else 'rejected'}",
            row.decision_note or row.detail,
            "/payslips",
            row.status,
        )
        return Response({"data": CompensationRequestSerializer(row).data})


class WageTypeListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        return Response(
            {
                "data": WageTypeSerializer(
                    WageType.objects.filter(tenant=tenant), many=True
                ).data
            }
        )

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        name = str(request.data.get("name", "")).strip()
        if not name:
            return Response({"errors": {"name": "Wage type name is required."}}, status=400)
        code = str(request.data.get("code") or name).upper()[:40]
        if WageType.objects.filter(tenant=tenant, code=code).exists():
            return Response(
                {"errors": {"code": "This wage type already exists."}}, status=400
            )
        row = WageType.objects.create(
            tenant=tenant,
            code=code,
            name=name,
            frequency=request.data.get("frequency") or "MONTHLY",
            min_day=max(1, min(int(request.data.get("min_day") or 1), 31)),
            to_day=max(1, min(int(request.data.get("to_day") or 30), 31)),
            active=bool(request.data.get("active", True)),
        )
        return Response({"data": WageTypeSerializer(row).data}, status=201)


class WageTypeBulkCreateView(APIView):
    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        created, skipped = [], []
        for item in request.data.get("wage_types") or []:
            name = str(item.get("name", "")).strip()
            if not name:
                skipped.append({"name": name, "reason": "Wage type name is required."})
                continue
            code = str(item.get("code") or name).upper()[:40]
            if WageType.objects.filter(tenant=tenant, code=code).exists():
                skipped.append({"name": name, "reason": "This wage type already exists."})
                continue
            row = WageType.objects.create(
                tenant=tenant,
                code=code,
                name=name,
                frequency=item.get("frequency") or "MONTHLY",
                min_day=max(1, min(int(item.get("min_day") or 1), 31)),
                to_day=max(1, min(int(item.get("to_day") or 30), 31)),
                active=bool(item.get("active", True)),
            )
            created.append(WageTypeSerializer(row).data)
        return Response({"data": {"created": created, "skipped": skipped}}, status=201)


class WageTypeDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = WageType.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        for field in ["name", "frequency", "active"]:
            if field in request.data:
                setattr(row, field, request.data[field])
        if request.data.get("code", "").strip():
            row.code = request.data["code"].strip().upper()[:40]
        if "min_day" in request.data:
            row.min_day = max(1, min(int(request.data["min_day"] or 1), 31))
        if "to_day" in request.data:
            row.to_day = max(1, min(int(request.data["to_day"] or 30), 31))
        row.save()
        return Response({"data": WageTypeSerializer(row).data})

    @require_roles("HR", "MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = WageType.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


class PayrollAdjustmentListCreateView(APIView):
    """Common deductions and increments applied to every payroll run."""

    def get(self, request):
        tenant = tenant_of(request.user)
        return Response(
            {
                "data": PayrollAdjustmentSerializer(
                    PayrollAdjustment.objects.filter(tenant=tenant), many=True
                ).data
            }
        )

    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        label = str(request.data.get("label", "")).strip()
        if not label:
            return Response({"errors": {"label": "Label is required."}}, status=400)
        row = PayrollAdjustment.objects.create(
            tenant=tenant,
            kind=(request.data.get("kind") or "DEDUCTION").upper(),
            label=label,
            percent_of_salary=request.data.get("percent_of_salary") or 0,
            when_mode=(request.data.get("when_mode") or "ALWAYS").upper(),
            salary_from=request.data.get("salary_from") or 0,
            salary_to=request.data.get("salary_to") or None,
            start_ymd=request.data.get("start_ymd") or None,
            end_ymd=request.data.get("end_ymd") or None,
            active=bool(request.data.get("active", True)),
        )
        return Response({"data": PayrollAdjustmentSerializer(row).data}, status=201)


class PayrollAdjustmentBulkCreateView(APIView):
    @require_roles("HR", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        created, skipped = [], []
        for item in request.data.get("adjustments") or []:
            label = str(item.get("label", "")).strip()
            if not label:
                skipped.append({"label": label, "reason": "Label is required."})
                continue
            row = PayrollAdjustment.objects.create(
                tenant=tenant,
                kind=(item.get("kind") or "DEDUCTION").upper(),
                label=label,
                percent_of_salary=item.get("percent_of_salary") or 0,
                when_mode=(item.get("when_mode") or "ALWAYS").upper(),
                salary_from=item.get("salary_from") or 0,
                salary_to=item.get("salary_to") or None,
                start_ymd=item.get("start_ymd") or None,
                end_ymd=item.get("end_ymd") or None,
                active=bool(item.get("active", True)),
            )
            created.append(PayrollAdjustmentSerializer(row).data)
        return Response({"data": {"created": created, "skipped": skipped}}, status=201)


class PayrollAdjustmentDetailView(APIView):
    @require_roles("HR", "MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        row = PayrollAdjustment.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        for field in [
            "kind",
            "label",
            "percent_of_salary",
            "when_mode",
            "salary_from",
            "active",
        ]:
            if field in request.data:
                setattr(row, field, request.data[field])
        if "salary_to" in request.data:
            row.salary_to = request.data.get("salary_to") or None
        if "start_ymd" in request.data:
            row.start_ymd = request.data.get("start_ymd") or None
        if "end_ymd" in request.data:
            row.end_ymd = request.data.get("end_ymd") or None
        row.save()
        return Response({"data": PayrollAdjustmentSerializer(row).data})

    @require_roles("HR", "MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        row = PayrollAdjustment.objects.filter(tenant=tenant, pk=pk).first()
        if not row:
            return Response({"errors": {"message": "Not found."}}, status=404)
        row.delete()
        return Response({"data": {"deleted": True}})


class PayrollHistoryView(APIView):
    """Settled payroll lines filtered by window, employee, amount and cycle."""

    @require_roles("HR", "MANAGER", "FINANCE")
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = PayrollEntry.objects.filter(tenant=tenant).select_related(
            "employee", "payroll_period", "payroll_period__wage_type"
        )
        day_from, day_to = _dates(request)
        if day_from:
            qs = qs.filter(payroll_period__to_ymd__gte=day_from)
        if day_to:
            qs = qs.filter(payroll_period__from_ymd__lte=day_to)
        employee = request.query_params.get("employee")
        if employee:
            qs = qs.filter(employee_id=employee)
        wage_type = request.query_params.get("wage_type")
        if wage_type:
            qs = qs.filter(payroll_period__wage_type_id=wage_type)
        amount_from = request.query_params.get("amount_from")
        if amount_from:
            qs = qs.filter(net__gte=amount_from)
        amount_to = request.query_params.get("amount_to")
        if amount_to:
            qs = qs.filter(net__lte=amount_to)
        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.upper())
        return Response({"data": PayrollSerializer(qs[:400], many=True).data})


class HrAssetListCreateView(APIView):
    """HR's own property register; the inventory asset table is untouched."""

    def get(self, request):
        tenant = tenant_of(request.user)
        qs = HrAsset.objects.filter(tenant=tenant).select_related("issued_to")
        state = request.query_params.get("status")
        if state:
            qs = qs.filter(status=state.upper())
        employee = request.query_params.get("employee")
        if employee:
            qs = qs.filter(issued_to_id=employee)
        return Response({"data": HrAssetSerializer(qs, many=True).data})

    @require_roles("MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        serializer = HrAssetInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = HrAsset.objects.create(
            tenant=tenant, created_by=actor(request.user), **serializer.validated_data
        )
        return Response({"data": HrAssetSerializer(row).data}, status=201)


class HrAssetDetailView(APIView):
    @require_roles("MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        asset = HrAsset.objects.filter(tenant=tenant, pk=pk).first()
        if not asset:
            return Response({"errors": {"message": "Not found."}}, status=404)
        serializer = HrAssetInputSerializer(asset, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": HrAssetSerializer(asset).data})

    @require_roles("MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        deleted = HrAsset.objects.filter(tenant=tenant, pk=pk).delete()[0]
        if not deleted:
            return Response({"errors": {"message": "Not found."}}, status=404)
        return Response({"data": {"id": int(pk), "deleted": True}})


class HrAssetIssueView(APIView):
    @require_roles("HR", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        asset = HrAsset.objects.filter(tenant=tenant, pk=pk).first()
        if not asset:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if asset.status == "ISSUED":
            return Response(
                {"errors": {"message": "This asset is already issued."}}, status=400
            )
        employee = _tenant_fk(Employee, tenant, request.data.get("employee"))
        if not employee:
            return Response(
                {"errors": {"employee": "Select the employee."}}, status=400
            )
        asset.issued_to = employee
        asset.issued_at = (
            parse_date(request.data.get("issued_at") or "") or timezone.localdate()
        )
        asset.returned_at = None
        asset.status = "ISSUED"
        asset.save(update_fields=["issued_to", "issued_at", "returned_at", "status"])
        return Response({"data": HrAssetSerializer(asset).data})


class HrAssetReturnView(APIView):
    @require_roles("HR", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        asset = HrAsset.objects.filter(tenant=tenant, pk=pk).first()
        if not asset:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if asset.status != "ISSUED":
            return Response(
                {"errors": {"message": "This asset is not issued."}}, status=400
            )
        asset.issued_to = None
        asset.issued_at = None
        asset.returned_at = (
            parse_date(request.data.get("returned_at") or "") or timezone.localdate()
        )
        asset.status = "AVAILABLE"
        asset.save(update_fields=["issued_to", "issued_at", "returned_at", "status"])
        return Response({"data": HrAssetSerializer(asset).data})
