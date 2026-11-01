from datetime import time

from django.db import models

from tenants.models import Tenant


class Department(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="departments")
    code = models.CharField(max_length=40)
    label = models.CharField(max_length=120)
    leader = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["label"]
        unique_together = ("tenant", "code")

    def __str__(self):
        return self.label


class Team(models.Model):
    """Branch/outlet/delivery team under a department (wholesale org unit)."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="teams")
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="teams",
    )
    code = models.CharField(max_length=40)
    label = models.CharField(max_length=120)
    leader = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["label"]
        unique_together = ("tenant", "code")

    def __str__(self):
        return self.label


class ShiftTemplate(models.Model):
    """Reusable weekday shift pattern (HotCol HrShiftTemplate)."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_shift_templates"
    )
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shift_templates",
    )
    start_time = models.TimeField(default=time(9, 0))
    end_time = models.TimeField(default=time(17, 0))
    # List of weekday ints, JS convention 0=Sun..6=Sat, e.g. [1,2,3,4,5].
    weekdays = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        unique_together = ("tenant", "code")

    def __str__(self):
        return f"{self.code} {self.name}"


class Employee(models.Model):
    STATUS = (
        ("ACTIVE", "Active"),
        ("ON_LEAVE", "On leave"),
        ("TERMINATED", "Terminated"),
    )
    SEX = (("M", "Male"), ("F", "Female"), ("", "—"))

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="hr_employees")
    employee_no = models.CharField(max_length=40, blank=True)
    first_name = models.CharField(max_length=120)
    last_name = models.CharField(max_length=120, blank=True)
    sex = models.CharField(max_length=1, choices=SEX, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.CharField(max_length=255, blank=True)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="employees"
    )
    job_title = models.CharField(max_length=120, blank=True)
    hired_on = models.DateField(null=True, blank=True)
    salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS, default="ACTIVE")
    portal_otp = models.CharField(max_length=10, blank=True)
    shift = models.ForeignKey(
        ShiftTemplate, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="employees",
    )
    # Self-service portal identity (volosync-emp signs in with portal_otp).
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="employees",
    )
    org_position = models.CharField(max_length=40, blank=True)
    must_change_otp = models.BooleanField(default=True)
    portal_first_login_at = models.DateTimeField(null=True, blank=True)
    profile_image_url = models.URLField(blank=True)
    # OTP reset audit (reset endpoint writes these).
    otp_reset_by = models.CharField(max_length=255, blank=True)
    otp_reset_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["first_name", "last_name"]

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.full_name


class LeaveType(models.Model):
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_leave_types"
    )
    code = models.CharField(max_length=40)
    label = models.CharField(max_length=80)
    paid = models.BooleanField(default=True)
    quota_days = models.DecimalField(max_digits=5, decimal_places=1, default=15)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["label"]
        unique_together = ("tenant", "code")


class LeaveRequest(models.Model):
    STATUS = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    )

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="hr_leaves")
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="leave_requests"
    )
    leave_type = models.CharField(max_length=40)
    from_ymd = models.DateField()
    to_ymd = models.DateField()
    days = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    # Workflow cursor: index into HrWorkflow.steps for this request.
    current_step_index = models.IntegerField(default=0)
    decided_by = models.CharField(max_length=255, blank=True)
    decision_note = models.TextField(blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class LeaveBalance(models.Model):
    """Per-employee yearly entitlement override and manual adjustment.

    Available = entitlement + adjustment - approved - pending, all computed in
    the serializer so HR never has to reconcile a stale stored counter.
    """

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_leave_balances"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="leave_balances"
    )
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.CASCADE, related_name="balances"
    )
    year = models.IntegerField()
    entitled_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    adjustment_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    note = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year", "leave_type__label"]
        unique_together = ("employee", "leave_type", "year")


class Attendance(models.Model):
    STATUS = (
        ("PRESENT", "Present"),
        ("LATE", "Late"),
        ("ABSENT", "Absent"),
        ("HALF_DAY", "Half day"),
        ("ON_LEAVE", "On leave"),
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_attendance"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="attendance"
    )
    day = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS, default="PRESENT")
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    shift = models.ForeignKey(
        ShiftTemplate, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="attendance",
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-day"]
        unique_together = ("employee", "day")


class PayrollPeriod(models.Model):
    """A payroll run: generated by HR, approved by the Manager, settled by Finance.

    OPEN -> AWAITING_MANAGER -> APPROVED -> AWAITING_FINANCE -> CLOSED.
    HR can only hand payslips over; Finance (or the Manager) finalises payment.
    """

    STATUS = (
        ("OPEN", "Open"),
        ("AWAITING_MANAGER", "Awaiting Manager"),
        ("APPROVED", "Approved"),
        ("AWAITING_FINANCE", "Awaiting Finance"),
        ("CLOSED", "Closed"),
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_payroll_periods"
    )
    # YYYY-MM; the run label the roster, payroll and finance screens share.
    period_key = models.CharField(max_length=20)
    month_name = models.CharField(max_length=40, blank=True)
    from_ymd = models.DateField()
    to_ymd = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS, default="OPEN")
    created_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.CharField(max_length=255, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    closed_by = models.CharField(max_length=255, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-from_ymd", "-id"]
        unique_together = ("tenant", "period_key")

    def __str__(self):
        return f"{self.period_key} ({self.status})"


class PayrollEntry(models.Model):
    """One payslip inside a payroll run.

    UNPAID -> AWAITING_FINANCE (HR mark paid) -> MARKED_PAID (Finance confirms).
    Finance can bounce a slip back to UNPAID.
    """

    STATUS = (
        ("UNPAID", "Unpaid"),
        ("AWAITING_FINANCE", "Awaiting Finance"),
        ("MARKED_PAID", "Marked paid"),
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_payroll"
    )
    payroll_period = models.ForeignKey(
        PayrollPeriod, on_delete=models.CASCADE, related_name="payslips"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="payslips"
    )
    gross = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    deductions = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS, default="UNPAID")
    hr_marked_paid_at = models.DateTimeField(null=True, blank=True)
    hr_marked_paid_by = models.CharField(max_length=255, blank=True)
    manager_approved_at = models.DateTimeField(null=True, blank=True)
    manager_approved_by = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["employee__first_name", "employee__last_name"]
        unique_together = ("payroll_period", "employee")


class HrDocument(models.Model):
    STATUS = (
        ("PENDING", "Pending approval"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_documents"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="documents", null=True, blank=True
    )
    title = models.CharField(max_length=255)
    kind = models.CharField(max_length=60, default="Contract")
    file_url = models.CharField(max_length=500, blank=True)
    file = models.FileField(upload_to="hr/documents/", blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    decided_by = models.CharField(max_length=255, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    uploaded_by = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]


class Incident(models.Model):
    STATUS = (("OPEN", "Open"), ("RESOLVED", "Resolved"))

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_incidents"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="incidents", null=True, blank=True
    )
    kind = models.CharField(max_length=60, default="Conduct")
    title = models.CharField(max_length=255)
    detail = models.TextField(blank=True)
    occurred_ymd = models.DateField()
    salary_deduct = models.BooleanField(default=False)
    amount_etb = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS, default="OPEN")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_ymd"]


class IncidentType(models.Model):
    """Managed incident kind with a default salary-deduct flag (HotCol HrIncidentType)."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_incident_types"
    )
    code = models.CharField(max_length=40)
    label = models.CharField(max_length=80)
    deduct = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["label"]
        unique_together = ("tenant", "code")


class AttendanceCorrection(models.Model):
    """HR-entered attendance correction awaiting Manager approval.

    HotCol raises these from the Attendance screen ("Correct attendance");
    on approval the target Attendance row is updated in place.
    """

    STATUS = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_attendance_corrections"
    )
    attendance = models.ForeignKey(
        Attendance, null=True, blank=True, on_delete=models.CASCADE,
        related_name="corrections",
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="attendance_corrections"
    )
    day = models.DateField()
    new_status = models.CharField(max_length=20, choices=Attendance.STATUS, default="PRESENT")
    new_check_in = models.TimeField(null=True, blank=True)
    new_check_out = models.TimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    requested_by = models.CharField(max_length=255, blank=True)
    decided_by = models.CharField(max_length=255, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class ChecklistTemplate(models.Model):
    KINDS = (("onboarding", "Onboarding"), ("exit", "Exit"), ("custom", "Custom"))

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_checklist_templates"
    )
    kind = models.CharField(max_length=20, choices=KINDS, default="onboarding")
    name = models.CharField(max_length=120)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="checklist_templates",
    )
    job_title = models.CharField(max_length=120, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "name"]


class ChecklistTemplateItem(models.Model):
    template = models.ForeignKey(
        ChecklistTemplate, on_delete=models.CASCADE, related_name="items"
    )
    label = models.CharField(max_length=200)
    required = models.BooleanField(default=True)
    owner_role = models.CharField(max_length=40, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class ChecklistRun(models.Model):
    STATUS = (
        ("OPEN", "Open"),
        ("COMPLETED", "Completed"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_checklist_runs"
    )
    template = models.ForeignKey(
        ChecklistTemplate, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="runs",
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="checklist_runs"
    )
    kind = models.CharField(max_length=20, choices=ChecklistTemplate.KINDS, default="onboarding")
    status = models.CharField(max_length=20, choices=STATUS, default="OPEN")
    created_by = models.CharField(max_length=255, blank=True)
    decided_by = models.CharField(max_length=255, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class ChecklistRunItem(models.Model):
    run = models.ForeignKey(
        ChecklistRun, on_delete=models.CASCADE, related_name="items"
    )
    label = models.CharField(max_length=200)
    required = models.BooleanField(default=True)
    owner_role = models.CharField(max_length=40, blank=True)
    done = models.BooleanField(default=False)
    completed_by = models.CharField(max_length=255, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]


class HrWorkflow(models.Model):
    """Approval flow config for an HR request type (HotCol HrApprovalFlow).

    steps is a JSON list like [{"kind": "department_leader"}, {"kind": "manager"}].
    Kept self-contained: the HR app never imports the approvals app.
    """

    STEP_KINDS = (
        ("team_leader", "Team leader"),
        ("department_leader", "Department leader"),
        ("hr", "HR Manager"),
        ("manager", "Manager"),
        ("admin", "Admin"),
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_workflows"
    )
    request_type = models.CharField(max_length=40)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="workflows",
    )
    require_team_leader_first = models.BooleanField(default=False)
    steps = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["request_type"]
        unique_together = ("tenant", "request_type")


class PeopleEvent(models.Model):
    """Employee lifecycle event (HotCol HrCareerAction + disciplinary records)."""

    KINDS = (
        ("promotion", "Promotion"),
        ("transfer", "Transfer"),
        ("termination", "Termination"),
        ("disciplinary", "Disciplinary"),
        ("other", "Other"),
    )
    STATUS = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_people_events"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="people_events"
    )
    kind = models.CharField(max_length=20, choices=KINDS, default="other")
    detail = models.TextField(blank=True)
    from_department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    to_department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    from_title = models.CharField(max_length=120, blank=True)
    to_title = models.CharField(max_length=120, blank=True)
    effective_ymd = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    created_by = models.CharField(max_length=255, blank=True)
    decided_by = models.CharField(max_length=255, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class HrNotification(models.Model):
    """Inbox item for the employee self-service portal (volosync-emp)."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_notifications"
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name="notifications"
    )
    kind = models.CharField(max_length=40, default="INFO")
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    href = models.CharField(max_length=255, blank=True)
    action_status = models.CharField(max_length=40, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class ChatThread(models.Model):
    """Employee chat, shared by the HR console and the employee portal."""

    KINDS = (("DIRECT", "Direct"), ("GROUP", "Group"), ("MANAGER", "Manager"))

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="hr_chat_threads"
    )
    title = models.CharField(max_length=255, blank=True)
    kind = models.CharField(max_length=20, choices=KINDS, default="DIRECT")
    created_by_employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    created_by_role = models.CharField(max_length=20, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_message_at", "-created_at"]


class ChatMember(models.Model):
    thread = models.ForeignKey(
        ChatThread, on_delete=models.CASCADE, related_name="members"
    )
    employee = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.CASCADE,
        related_name="chat_memberships",
    )
    # Null employee = the HR/Manager side of a MANAGER thread.
    is_manager = models.BooleanField(default=False)
    member_key = models.CharField(max_length=80, blank=True)
    employee_name = models.CharField(max_length=255, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["joined_at"]


class ChatMessage(models.Model):
    thread = models.ForeignKey(
        ChatThread, on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.CASCADE,
        related_name="sent_messages",
    )
    sender_is_manager = models.BooleanField(default=False)
    sender_name = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    image_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
