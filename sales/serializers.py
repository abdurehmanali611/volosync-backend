from decimal import Decimal

from rest_framework import serializers

from approvals.models import Approval

from .models import SalesAgent, SalesSettlement


class SalesAgentSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="full_name", read_only=True)
    assigned_count = serializers.SerializerMethodField()

    class Meta:
        model = SalesAgent
        fields = [
            "id",
            "name",
            "login_username",
            "first_name",
            "last_name",
            "phone",
            "sex",
            "years_experience",
            "is_active",
            "must_change_password",
            "assigned_count",
        ]

    def get_assigned_count(self, obj):
        return obj.assignments.filter(request__status="APPROVED").count()


class SalesSettlementSerializer(serializers.ModelSerializer):
    agent_name = serializers.CharField(source="assignment.sales_agent.full_name", read_only=True)
    item_name = serializers.CharField(source="assignment.lot.item.name", read_only=True)
    voucher_number = serializers.CharField(source="assignment.lot.voucher_number", read_only=True)
    lot = serializers.IntegerField(source="assignment.lot_id", read_only=True)
    assigned_quantity = serializers.DecimalField(
        source="assignment.quantity", max_digits=14, decimal_places=3, read_only=True
    )
    entered_by_name = serializers.SerializerMethodField()
    entered_at = serializers.DateTimeField(source="created_at", read_only=True)
    variance = serializers.SerializerMethodField()
    finance_approval = serializers.SerializerMethodField()
    manager_approval = serializers.SerializerMethodField()

    class Meta:
        model = SalesSettlement
        fields = [
            "id",
            "assignment",
            "lot",
            "voucher_number",
            "item_name",
            "agent_name",
            "assigned_quantity",
            "sold_quantity",
            "returned_quantity",
            "payment_method",
            "sales_amount",
            "expected_amount",
            "variance",
            "penalty",
            "note",
            "status",
            "entered_by",
            "entered_by_name",
            "entered_at",
            "created_at",
            "finance_approval",
            "manager_approval",
        ]
        read_only_fields = [
            "returned_quantity",
            "expected_amount",
            "status",
            "entered_by",
            "created_at",
        ]

    def get_entered_by_name(self, obj):
        if not obj.entered_by:
            return ""
        return obj.entered_by.get_full_name() or obj.entered_by.username

    def get_variance(self, obj):
        return str((obj.sales_amount - obj.expected_amount).quantize(Decimal("0.01")))

    def _approval_stamp(self, obj, step):
        row = (
            Approval.objects.filter(
                entity_type="SETTLEMENT", entity_id=obj.pk, step=step
            )
            .order_by("-decided_at")
            .first()
        )
        if not row:
            return None
        actor = row.actor
        return {
            "decision": row.decision,
            "decided_by": actor.get_full_name() or actor.username if actor else "",
            "decided_at": row.decided_at,
            "note": row.note,
        }

    def get_finance_approval(self, obj):
        return self._approval_stamp(obj, "FINANCE")

    def get_manager_approval(self, obj):
        return self._approval_stamp(obj, "MANAGER")
