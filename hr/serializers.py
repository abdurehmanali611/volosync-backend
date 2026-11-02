from rest_framework import serializers

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
    ChecklistTemplateItem,
    CompensationRequest,
    Department,
    Employee,
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


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ["id", "code", "label", "leader"]


class TeamSerializer(serializers.ModelSerializer):
    department_label = serializers.CharField(
        source="department.label", read_only=True, default=""
    )
    staff = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = ["id", "code", "label", "leader", "department", "department_label", "active", "staff"]

    def get_staff(self, obj):
        return obj.employees.count()


class ShiftTemplateSerializer(serializers.ModelSerializer):
    department_label = serializers.CharField(
        source="department.label", read_only=True, default=""
    )

    class Meta:
        model = ShiftTemplate
        fields = [
            "id",
            "code",
            "name",
            "department",
            "department_label",
            "start_time",
            "end_time",
            "weekdays",
            "note",
            "active",
        ]


class EmployeeSerializer(serializers.ModelSerializer):
    department_label = serializers.CharField(
        source="department.label", read_only=True, default=""
    )
    shift_label = serializers.CharField(
        source="shift.name", read_only=True, default=""
    )
    team_label = serializers.CharField(
        source="team.label", read_only=True, default=""
    )
    full_name = serializers.SerializerMethodField()
    portal_otp = serializers.SerializerMethodField()

    class Meta:
        model = Employee
        fields = [
            "id",
            "employee_no",
            "first_name",
            "last_name",
            "full_name",
            "sex",
            "phone",
            "email",
            "department",
            "department_label",
            "job_title",
            "hired_on",
            "salary",
            "status",
            "portal_otp",
            "shift",
            "shift_label",
            "team",
            "team_label",
            "org_position",
            "must_change_otp",
            "portal_first_login_at",
            "profile_image_url",
            "otp_reset_by",
            "otp_reset_at",
            "created_at",
        ]
        read_only_fields = ["employee_no", "created_at", "otp_reset_by", "otp_reset_at"]

    def get_full_name(self, obj):
        return obj.full_name

    def get_portal_otp(self, obj):
        # Shown once on reset; list/detail responses only reveal it to HR & Manager.
        from inventory.views import role_of

        request = self.context.get("request")
        if request is None or role_of(request.user) not in ("HR", "MANAGER"):
            return ""
        return obj.portal_otp


class LeaveTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveType
        fields = ["id", "code", "label", "paid", "quota_days", "active"]


class LeaveRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_id = serializers.IntegerField(source="employee.id", read_only=True)
    step_kind = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        fields = [
            "id",
            "employee",
            "employee_id",
            "employee_name",
            "leave_type",
            "from_ymd",
            "to_ymd",
            "days",
            "reason",
            "status",
            "current_step_index",
            "step_kind",
            "decided_by",
            "decision_note",
            "decided_at",
            "created_at",
        ]
        read_only_fields = [
            "status",
            "current_step_index",
            "decided_by",
            "decision_note",
            "decided_at",
            "created_at",
        ]

    def get_step_kind(self, obj):
        from .services import leave_step_kind

        return leave_step_kind(obj.tenant, obj.employee, obj.current_step_index)


class AttendanceSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_id = serializers.IntegerField(source="employee.id", read_only=True)
    shift_label = serializers.CharField(
        source="shift.name", read_only=True, default=""
    )

    class Meta:
        model = Attendance
        fields = [
            "id",
            "employee_id",
            "employee_name",
            "day",
            "status",
            "check_in",
            "check_out",
            "shift",
            "shift_label",
            "note",
        ]


class AttendanceCorrectionSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_id = serializers.IntegerField(source="employee.id", read_only=True)

    class Meta:
        model = AttendanceCorrection
        fields = [
            "id",
            "employee",
            "employee_id",
            "employee_name",
            "attendance",
            "day",
            "new_status",
            "new_check_in",
            "new_check_out",
            "reason",
            "status",
            "requested_by",
            "decided_by",
            "decided_at",
            "decision_note",
            "created_at",
        ]
        read_only_fields = [
            "status",
            "requested_by",
            "decided_by",
            "decided_at",
            "decision_note",
            "created_at",
        ]


class PayrollPeriodSerializer(serializers.ModelSerializer):
    counts = serializers.SerializerMethodField()
    totals = serializers.SerializerMethodField()
    payslip_count = serializers.SerializerMethodField()
    wage_type_label = serializers.CharField(
        source="wage_type.name", read_only=True, default=""
    )

    class Meta:
        model = PayrollPeriod
        fields = [
            "id",
            "period_key",
            "month_name",
            "from_ymd",
            "to_ymd",
            "status",
            "created_by",
            "created_at",
            "decided_by",
            "decided_at",
            "decision_note",
            "closed_by",
            "closed_at",
            "wage_type",
            "wage_type_label",
            "payslip_count",
            "counts",
            "totals",
        ]

    def _slips(self, row):
        return list(getattr(row, "payslip_rows", row.payslips.all()))

    def get_payslip_count(self, row):
        return len(self._slips(row))

    def get_counts(self, row):
        slips = self._slips(row)
        return {
            "unpaid": sum(1 for s in slips if s.status == "UNPAID"),
            "awaiting_finance": sum(
                1 for s in slips if s.status == "AWAITING_FINANCE"
            ),
            "marked_paid": sum(1 for s in slips if s.status == "MARKED_PAID"),
        }

    def get_totals(self, row):
        slips = self._slips(row)
        return {
            "gross": str(sum((s.gross for s in slips), 0)),
            "deductions": str(sum((s.deductions for s in slips), 0)),
            "net": str(sum((s.net for s in slips), 0)),
        }


class PayrollSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_id = serializers.IntegerField(source="employee.id", read_only=True)
    job_title = serializers.CharField(source="employee.job_title", read_only=True)
    period_id = serializers.IntegerField(source="payroll_period.id", read_only=True)
    period_key = serializers.CharField(source="payroll_period.period_key", read_only=True)

    class Meta:
        model = PayrollEntry
        fields = [
            "id",
            "employee_id",
            "employee_name",
            "job_title",
            "period_id",
            "period_key",
            "gross",
            "deductions",
            "net",
            "status",
            "hr_marked_paid_at",
            "hr_marked_paid_by",
            "manager_approved_at",
            "manager_approved_by",
            "created_at",
        ]
        read_only_fields = fields[1:]


class DocumentSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(
        source="employee.full_name", read_only=True, default=None
    )
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = HrDocument
        fields = [
            "id",
            "employee",
            "employee_name",
            "title",
            "kind",
            "file_url",
            "file",
            "status",
            "decided_by",
            "decided_at",
            "decision_note",
            "uploaded_by",
            "uploaded_at",
        ]

    def get_file_url(self, obj):
        # Keep legacy plain-URL field working; prefer stored file when present.
        if obj.file:
            try:
                return obj.file.url
            except ValueError:
                pass
        return obj.file_url


class IncidentSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(
        source="employee.full_name", read_only=True, default=None
    )

    class Meta:
        model = Incident
        fields = [
            "id",
            "employee",
            "employee_name",
            "kind",
            "title",
            "detail",
            "occurred_ymd",
            "salary_deduct",
            "amount_etb",
            "status",
            "created_at",
        ]


class IncidentTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentType
        fields = [
            "id",
            "code",
            "label",
            "deduct",
            "percent_of_salary",
            "attendance_link",
            "active",
        ]


class HrWorkflowSerializer(serializers.ModelSerializer):
    department_label = serializers.CharField(
        source="department.label", read_only=True, default=""
    )

    class Meta:
        model = HrWorkflow
        fields = [
            "id",
            "request_type",
            "department",
            "department_label",
            "require_team_leader_first",
            "steps",
            "active",
        ]


class PeopleEventSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_id = serializers.IntegerField(source="employee.id", read_only=True)
    from_department_label = serializers.CharField(
        source="from_department.label", read_only=True, default=""
    )
    to_department_label = serializers.CharField(
        source="to_department.label", read_only=True, default=""
    )

    class Meta:
        model = PeopleEvent
        fields = [
            "id",
            "employee",
            "employee_id",
            "employee_name",
            "kind",
            "detail",
            "from_department",
            "from_department_label",
            "to_department",
            "to_department_label",
            "from_title",
            "to_title",
            "effective_ymd",
            "status",
            "created_by",
            "decided_by",
            "decided_at",
            "decision_note",
            "created_at",
        ]
        read_only_fields = [
            "status",
            "created_by",
            "decided_by",
            "decided_at",
            "decision_note",
            "created_at",
        ]


class ChecklistTemplateItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChecklistTemplateItem
        fields = ["id", "label", "required", "owner_role", "sort_order"]


class ChecklistTemplateSerializer(serializers.ModelSerializer):
    items = ChecklistTemplateItemSerializer(many=True, read_only=True)
    department_label = serializers.CharField(
        source="department.label", read_only=True, default=""
    )
    team_label = serializers.CharField(source="team.label", read_only=True, default="")

    class Meta:
        model = ChecklistTemplate
        fields = [
            "id",
            "kind",
            "name",
            "department",
            "department_label",
            "team",
            "team_label",
            "job_title",
            "active",
            "items",
            "created_at",
        ]


class ChecklistRunItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChecklistRunItem
        fields = [
            "id",
            "label",
            "required",
            "owner_role",
            "done",
            "completed_by",
            "completed_at",
            "note",
            "sort_order",
        ]


class ChecklistRunSerializer(serializers.ModelSerializer):
    items = ChecklistRunItemSerializer(many=True, read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_id = serializers.IntegerField(source="employee.id", read_only=True)

    class Meta:
        model = ChecklistRun
        fields = [
            "id",
            "template",
            "employee",
            "employee_id",
            "employee_name",
            "kind",
            "status",
            "created_by",
            "decided_by",
            "decided_at",
            "items",
            "created_at",
        ]
        read_only_fields = [
            "status",
            "created_by",
            "decided_by",
            "decided_at",
            "created_at",
        ]


class HrNotificationSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)

    class Meta:
        model = HrNotification
        fields = [
            "id",
            "employee",
            "employee_name",
            "kind",
            "title",
            "body",
            "href",
            "action_status",
            "read_at",
            "created_at",
        ]
        read_only_fields = fields[1:]


class ChatMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMember
        fields = [
            "id",
            "thread",
            "employee",
            "is_manager",
            "member_key",
            "employee_name",
            "joined_at",
            "last_read_at",
        ]


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = [
            "id",
            "thread",
            "sender",
            "sender_is_manager",
            "sender_name",
            "body",
            "image_url",
            "created_at",
        ]


class ChatThreadSerializer(serializers.ModelSerializer):
    members = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    message_count = serializers.SerializerMethodField()
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)

    class Meta:
        model = ChatThread
        fields = [
            "id",
            "tenant_name",
            "kind",
            "title",
            "created_by_employee",
            "created_by_role",
            "last_message_at",
            "created_at",
            "updated_at",
            "members",
            "last_message",
            "message_count",
        ]

    def get_members(self, obj):
        return ChatMemberSerializer(obj.members.all(), many=True).data

    def get_last_message(self, obj):
        row = getattr(obj, "last_row", None)
        if row is None:
            row = obj.messages.order_by("-created_at").first()
        return ChatMessageSerializer(row).data if row else None

    def get_message_count(self, obj):
        return getattr(obj, "total_messages", None) or obj.messages.count()


class OtpResetRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_no = serializers.CharField(source="employee.employee_no", read_only=True)
    department_label = serializers.CharField(
        source="employee.department.label", read_only=True, default=""
    )

    class Meta:
        model = OtpResetRequest
        fields = [
            "id",
            "employee",
            "employee_name",
            "employee_no",
            "department_label",
            "reason",
            "status",
            "otp_code",
            "requested_by",
            "decided_by",
            "decided_at",
            "decision_note",
            "used_at",
            "created_at",
        ]
        read_only_fields = fields[3:]


class AtsOtpSerializer(serializers.ModelSerializer):
    class Meta:
        model = AtsOtp
        fields = [
            "id",
            "role",
            "code",
            "status",
            "must_change_otp",
            "generated_by",
            "generated_at",
            "used_at",
        ]
        read_only_fields = fields[1:]


class AtsOtpAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = AtsOtpAudit
        fields = ["id", "role", "code", "action", "actor", "note", "created_at"]


class ChatBlockSerializer(serializers.ModelSerializer):
    employee_a_name = serializers.CharField(source="employee_a.full_name", read_only=True)
    employee_b_name = serializers.SerializerMethodField()

    class Meta:
        model = ChatBlock
        fields = [
            "id",
            "employee_a",
            "employee_a_name",
            "employee_b",
            "employee_b_name",
            "note",
            "active",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields[1:]

    def get_employee_b_name(self, obj):
        return obj.employee_b.full_name if obj.employee_b else "Manager"


class WageTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = WageType
        fields = [
            "id",
            "code",
            "name",
            "frequency",
            "min_day",
            "to_day",
            "active",
        ]


class PayrollAdjustmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayrollAdjustment
        fields = [
            "id",
            "kind",
            "label",
            "percent_of_salary",
            "when_mode",
            "salary_from",
            "salary_to",
            "start_ymd",
            "end_ymd",
            "active",
            "created_at",
        ]


class CompensationRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_id = serializers.IntegerField(source="employee.id", read_only=True)
    department_label = serializers.CharField(
        source="employee.department.label", read_only=True, default=""
    )
    applied_period_key = serializers.CharField(
        source="applied_period.period_key", read_only=True, default=""
    )

    class Meta:
        model = CompensationRequest
        fields = [
            "id",
            "employee",
            "employee_id",
            "employee_name",
            "department_label",
            "kind",
            "amount",
            "effective_ymd",
            "detail",
            "status",
            "created_by",
            "decided_by",
            "decided_at",
            "decision_note",
            "applied_period",
            "applied_period_key",
            "created_at",
        ]
        read_only_fields = [
            "status",
            "created_by",
            "decided_by",
            "decided_at",
            "decision_note",
            "applied_period",
            "created_at",
        ]
