"""Shared HR rules: approval routing, leave balances and portal notifications.

Used by the HR API and by the serializers so a request always reports the step
it is actually waiting on.
"""

from datetime import date
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    Employee,
    HrNotification,
    HrWorkflow,
    LeaveBalance,
    LeaveRequest,
    LeaveType,
)

DEFAULT_LEAVE_STEPS = [{"kind": "manager"}]

STEP_ROLES = {
    "team_leader": ("HR", "MANAGER"),
    "department_leader": ("HR", "MANAGER"),
    "hr": ("HR",),
    "manager": ("MANAGER",),
    "admin": ("MANAGER",),
}


def actor(user):
    return user.get_full_name() or user.username


def notify(tenant, employee, kind, title, body="", href="", action_status=""):
    if employee is None:
        return None
    return HrNotification.objects.create(
        tenant=tenant,
        employee=employee,
        kind=kind,
        title=title,
        body=body,
        href=href,
        action_status=action_status,
    )


def leave_steps(tenant, employee):
    """Resolve the configured flow for this employee's department."""
    flows = HrWorkflow.objects.filter(
        tenant=tenant, request_type="leave", active=True
    )
    if employee.department_id:
        flow = flows.filter(department_id=employee.department_id).first()
    else:
        flow = None
    flow = flow or flows.filter(department__isnull=True).first()
    if flow is None:
        return list(DEFAULT_LEAVE_STEPS)
    steps = []
    if flow.require_team_leader_first:
        steps.append({"kind": "team_leader"})
    for step in flow.steps or []:
        kind = step.get("kind") if isinstance(step, dict) else None
        if kind:
            steps.append({"kind": kind})
    return steps or list(DEFAULT_LEAVE_STEPS)


def leave_step_kind(tenant, employee, index):
    steps = leave_steps(tenant, employee)
    if 0 <= index < len(steps):
        return steps[index]["kind"]
    return steps[-1]["kind"]


def decide_leave(row, role, who, approve, note=""):
    """Advance or reject a leave request through its configured steps."""
    kind = leave_step_kind(row.tenant, row.employee, row.current_step_index)
    allowed = STEP_ROLES.get(kind, ("MANAGER",))
    if role not in allowed:
        raise PermissionError("This step belongs to another role.")

    note = note or ""
    if not approve:
        row.status = "REJECTED"
        row.decided_by = who
        row.decided_at = timezone.now()
        row.decision_note = note
        row.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
        notify(
            row.tenant,
            row.employee,
            "LEAVE",
            f"{row.leave_type} leave declined",
            note or f"{row.from_ymd} → {row.to_ymd}",
            "/leave",
            "REJECTED",
        )
        return row

    steps = leave_steps(row.tenant, row.employee)
    if row.current_step_index + 1 < len(steps):
        row.current_step_index += 1
        row.save(update_fields=["current_step_index"])
        next_kind = leave_step_kind(row.tenant, row.employee, row.current_step_index)
        notify(
            row.tenant,
            row.employee,
            "LEAVE",
            f"{row.leave_type} leave moved to {next_kind.replace('_', ' ')}",
            f"{row.from_ymd} → {row.to_ymd}",
            "/leave",
            "PENDING",
        )
        return row

    row.status = "APPROVED"
    row.decided_by = who
    row.decided_at = timezone.now()
    row.decision_note = note
    row.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
    sync_leave_status(row.employee)
    notify(
        row.tenant,
        row.employee,
        "LEAVE",
        f"{row.leave_type} leave approved",
        f"{row.from_ymd} → {row.to_ymd} · {row.days} day(s)",
        "/leave",
        "APPROVED",
    )
    return row


def sync_leave_status(employee):
    today = date.today()
    on_leave = LeaveRequest.objects.filter(
        employee=employee,
        status="APPROVED",
        from_ymd__lte=today,
        to_ymd__gte=today,
    ).exists()
    target = "ON_LEAVE" if on_leave else "ACTIVE"
    if employee.status in ("ACTIVE", "ON_LEAVE") and employee.status != target:
        employee.status = target
        employee.save(update_fields=["status"])


def _days_used(employee, leave_type_code, year, status):
    total = LeaveRequest.objects.filter(
        employee=employee,
        leave_type=leave_type_code,
        status=status,
        from_ymd__year=year,
    ).aggregate(total=Sum("days"))["total"]
    return Decimal(total or 0)


def balance_rows(tenant, employee, year=None):
    """Entitlement, used, pending and available for every active leave type."""
    year = year or date.today().year
    types = LeaveType.objects.filter(tenant=tenant, active=True)
    overrides = {
        row.leave_type_id: row
        for row in LeaveBalance.objects.filter(employee=employee, year=year)
    }
    rows = []
    for leave_type in types:
        override = overrides.get(leave_type.id)
        entitled = (
            override.entitled_days
            if override is not None
            else leave_type.quota_days
        )
        adjustment = override.adjustment_days if override is not None else Decimal(0)
        used = _days_used(employee, leave_type.code, year, "APPROVED")
        pending = _days_used(employee, leave_type.code, year, "PENDING")
        available = entitled + adjustment - used - pending
        rows.append(
            {
                "id": override.id if override is not None else None,
                "leave_type": leave_type.code,
                "label": leave_type.label,
                "paid": leave_type.paid,
                "year": year,
                "entitled_days": str(entitled),
                "adjustment_days": str(adjustment),
                "used_days": str(used),
                "pending_days": str(pending),
                "available_days": str(available),
                "note": override.note if override is not None else "",
            }
        )
    return rows


def available_days(tenant, employee, leave_type_code, year=None):
    for row in balance_rows(tenant, employee, year):
        if row["leave_type"] == leave_type_code:
            return Decimal(row["available_days"])
    return None


def resolve_leave_type(tenant, code):
    return LeaveType.objects.filter(tenant=tenant, code=code, active=True).first()
