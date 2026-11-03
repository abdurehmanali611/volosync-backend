from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Case, IntegerField, Sum, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from approvals.services import decide
from inventory.views import require_roles, role_of, tenant_of

from .models import SalesAgent, SalesSettlement
from .serializers import SalesAgentSerializer, SalesSettlementSerializer


def sales_role_disabled(request):
    """True when the SALES role is switched off for this tenant (spec §5.4)."""
    if role_of(request.user) != "SALES":
        return False
    tenant = tenant_of(request.user)
    return not (tenant and tenant.sales_role_enabled)


def sales_disabled_response():
    return Response(
        {"errors": {"message": "Sales role is disabled for this tenant."}},
        status=status.HTTP_403_FORBIDDEN,
    )


def sales_access_profile(tenant):
    """The Sales login created in Access, if the tenant has one."""
    from user.models import Profile

    return (
        Profile.objects.filter(tenant=tenant, role="SALES")
        .select_related("user")
        .first()
    )


def visible_agent_ids(user):
    """Agent rows the caller acts on; ``None`` means every agent in the tenant.

    The tenant's shared Sales account oversees the whole team, so stock raised
    for any agent shows up for it. Only a legacy per-agent login — one that is
    not that Access account — stays pinned to its own rows.
    """
    if role_of(user) != "SALES":
        return None
    access = sales_access_profile(tenant_of(user))
    if access and access.user_id == user.id:
        return None
    own = SalesAgent.objects.filter(user=user).values_list("id", flat=True).first()
    return [own] if own else None


class SalesAgentListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        agents = SalesAgent.objects.filter(tenant=tenant)
        access_profile = sales_access_profile(tenant)
        return Response(
            {
                "data": SalesAgentSerializer(agents, many=True).data,
                "sales_username": access_profile.user.username if access_profile else "",
            }
        )

    @require_roles("MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        if not tenant or not tenant.sales_role_enabled:
            return Response(
                {"errors": {"message": "Sales is switched off for this tenant."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = request.data
        access_profile = sales_access_profile(tenant)
        if not access_profile:
            return Response(
                {
                    "errors": {
                        "message": "Create the Sales account in Access first — agents belong to that shared login."
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Agents are records on the tenant's shared Sales login: they take no
        # individual credentials, so there is no per-agent user or password.
        agent = SalesAgent.objects.create(
            tenant=tenant,
            login_username=access_profile.user.username,
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            phone=data.get("phone", ""),
            sex=data.get("sex", ""),
            years_experience=data.get("years_experience", 0) or 0,
        )
        return Response(
            {"data": SalesAgentSerializer(agent).data},
            status=201,
        )


class SalesAgentLoginView(APIView):
    """Legacy agents only: shared username + the agent's own password."""

    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get("username", "")
        password = request.data.get("password", "")
        candidates = SalesAgent.objects.filter(
            login_username__iexact=username, is_active=True
        ).select_related("user")
        for agent in candidates:
            if agent.user and agent.user.check_password(password):
                refresh = RefreshToken.for_user(agent.user)
                return Response(
                    {
                        "data": {
                            "access": str(refresh.access_token),
                            "refresh": str(refresh),
                            "user": {
                                "id": agent.user_id,
                                "name": agent.full_name,
                                "role": "SALES",
                                "tenant": agent.tenant.name,
                                "must_change_password": agent.must_change_password,
                            },
                        }
                    }
                )
        return Response(
            {"errors": {"message": "Invalid credentials."}},
            status=status.HTTP_401_UNAUTHORIZED,
        )


class SalesAgentChangePasswordView(APIView):
    """First sign-in gate: each agent replaces the password the manager issued."""

    def post(self, request):
        agent = SalesAgent.objects.filter(user=request.user).first()
        if agent is None:
            return Response(
                {"errors": {"message": "Only sales agents can change this password."}},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not agent.tenant.sales_role_enabled:
            return sales_disabled_response()
        current = request.data.get("current_password", "")
        new = request.data.get("new_password", "")
        confirm = request.data.get("confirm_password", new)
        if not agent.user.check_password(current):
            return Response(
                {"errors": {"message": "Current password is not correct."}}, status=400
            )
        if len(new) < 6:
            return Response(
                {"errors": {"message": "Use at least 6 characters."}}, status=400
            )
        if new != confirm:
            return Response(
                {"errors": {"message": "The two new passwords do not match."}}, status=400
            )
        if agent.user.check_password(new):
            return Response(
                {"errors": {"message": "Pick a password you have not used before."}},
                status=400,
            )
        agent.user.set_password(new)
        agent.user.save(update_fields=["password"])
        agent.must_change_password = False
        agent.save(update_fields=["must_change_password"])
        return Response({"data": {"message": "Password changed."}})


class SalesAgentMeView(APIView):
    """Return the SalesAgent record for the currently authenticated agent."""

    def get(self, request):
        if sales_role_disabled(request):
            return sales_disabled_response()
        agent = SalesAgent.objects.filter(user=request.user).first()
        if agent is None:
            return Response(
                {"errors": {"message": "No sales agent found for this session."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"data": SalesAgentSerializer(agent).data})


def ranking_period_start(period):
    """Ranking windows in Africa/Addis_Ababa (settings TIME_ZONE).

    weekly   = last 7 days (rolling)
    monthly  = current calendar month
    quarterly= last 3 months (rolling — no calendar-quarter split in v1)
    halfyear = last 6 months (rolling)
    yearly   = current calendar year
    alltime  = no window
    """
    today = timezone.localdate()
    if period == "weekly":
        day = today - timedelta(days=6)
    elif period == "monthly":
        day = today.replace(day=1)
    elif period == "quarterly":
        day = today - timedelta(days=90)
    elif period == "halfyear":
        day = today - timedelta(days=182)
    elif period == "yearly":
        day = today.replace(month=1, day=1)
    else:
        return None
    return timezone.make_aware(datetime.combine(day, time.min))


class SalesAgentRankingView(APIView):
    @require_roles("MANAGER", "FINANCE", "OWNER")
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = SalesSettlement.objects.filter(tenant=tenant, status="APPROVED")
        since = ranking_period_start(request.query_params.get("period", "alltime"))
        if since:
            qs = qs.filter(created_at__gte=since)
        rows = (
            qs.values("assignment__sales_agent_id")
            .annotate(
                revenue=Sum("sales_amount"),
                units=Sum("sold_quantity"),
                returned=Sum("returned_quantity"),
                penalties=Sum(
                    Case(
                        When(penalty=True, then=Value(1)),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                ),
            )
        )
        stats = {row["assignment__sales_agent_id"]: row for row in rows}
        data = []
        for agent in SalesAgent.objects.filter(tenant=tenant, is_active=True):
            row = stats.get(agent.id, {})
            revenue = (row.get("revenue") or Decimal("0")).quantize(Decimal("0.01"))
            units = row.get("units") or Decimal("0")
            returned = row.get("returned") or Decimal("0")
            penalties = row.get("penalties") or 0
            data.append(
                {
                    "agent_id": agent.id,
                    "name": agent.full_name,
                    "revenue": str(revenue),
                    "units_sold": str(units),
                    "returned_quantity": str(returned),
                    "penalty_count": penalties,
                    "has_penalty": penalties > 0,
                }
            )
        # Penalized agents always rank below clean ones; then revenue, then units.
        data.sort(key=lambda row: (row["has_penalty"], -Decimal(row["revenue"]), -Decimal(row["units_sold"])))
        # Competition ranking: identical scores share a rank and the next
        # distinct score skips the tied places (1, 1, 3 / 1, 1, 1, 4).
        previous_key = None
        previous_rank = 0
        for index, row in enumerate(data, start=1):
            key = (row["has_penalty"], row["revenue"], row["units_sold"])
            if key != previous_key:
                previous_rank = index
                previous_key = key
            row["rank"] = previous_rank
        return Response({"data": data})


class SettlementListCreateView(APIView):
    def get(self, request):
        if sales_role_disabled(request):
            return sales_disabled_response()
        tenant = tenant_of(request.user)
        qs = SalesSettlement.objects.filter(tenant=tenant).select_related(
            "assignment__sales_agent", "assignment__lot__item", "entered_by"
        )
        if role_of(request.user) == "SALES":
            # The shared Sales login oversees every agent; a legacy per-agent
            # login keeps seeing only its own settlements.
            agent_ids = visible_agent_ids(request.user)
            if agent_ids:
                qs = qs.filter(assignment__sales_agent_id__in=agent_ids)
        if request.query_params.get("queue") == "1":
            wanted = {"FINANCE": "PENDING_FINANCE", "MANAGER": "PENDING_MANAGER"}.get(
                role_of(request.user)
            )
            qs = qs.filter(status=wanted) if wanted else qs.none()
        date_from = request.query_params.get("from")
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        date_to = request.query_params.get("to")
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        agent_id = request.query_params.get("agent_id")
        if agent_id:
            qs = qs.filter(assignment__sales_agent_id=agent_id)
        item_id = request.query_params.get("item_id")
        if item_id:
            qs = qs.filter(assignment__lot_id=item_id)
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        method = request.query_params.get("payment_method")
        if method:
            qs = qs.filter(payment_method__iexact=method)
        return Response({"data": SalesSettlementSerializer(qs, many=True).data})

    @require_roles("FINANCE", "SALES")
    def post(self, request):
        if sales_role_disabled(request):
            return sales_disabled_response()
        tenant = tenant_of(request.user)
        serializer = SalesSettlementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment = serializer.validated_data["assignment"]
        if assignment.request.tenant_id != tenant.id:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if assignment.request.status != "APPROVED":
            return Response(
                {"errors": {"assignment": "This assignment is not approved yet."}},
                status=400,
            )
        if hasattr(assignment, "settlement"):
            return Response(
                {"errors": {"assignment": "This assignment is already settled."}},
                status=400,
            )
        unit_price = assignment.unit_sales_at_assignment + assignment.changing_amount
        sold = serializer.validated_data["sold_quantity"]
        if sold > assignment.quantity:
            return Response(
                {"errors": {"sold_quantity": "Cannot sell more than the assigned quantity."}},
                status=400,
            )
        role = role_of(request.user)
        expected = (unit_price * sold).quantize(Decimal("0.01"))
        settlement = serializer.save(
            tenant=tenant,
            returned_quantity=assignment.quantity - sold,
            sales_amount=expected,
            expected_amount=expected,
            entered_by=request.user,
            status="PENDING_FINANCE" if role == "SALES" else "PENDING_MANAGER",
        )
        return Response({"data": SalesSettlementSerializer(settlement).data}, status=201)


class SettlementDecisionView(APIView):
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        settlement = SalesSettlement.objects.filter(tenant=tenant, pk=pk).first()
        if not settlement:
            return Response({"errors": {"message": "Not found."}}, status=404)
        # The agent's penalty tick is a declaration; Finance owns the verdict,
        # and a penalty only makes sense when stock came back unsold.
        if "penalty" in request.data and settlement.status == "PENDING_FINANCE":
            penalty_on = request.data.get("penalty") is True or str(
                request.data.get("penalty")
            ).lower() in {"1", "true", "yes"}
            if penalty_on and settlement.returned_quantity <= 0:
                return Response(
                    {
                        "errors": {
                            "penalty": "A penalty needs unsold remainings on this sale."
                        }
                    },
                    status=400,
                )
            settlement.penalty = penalty_on
            settlement.save(update_fields=["penalty"])
        decide(
            settlement,
            request.user,
            request.data.get("decision"),
            request.data.get("note", ""),
        )
        return Response({"data": SalesSettlementSerializer(settlement).data})


class OpenAssignmentListView(APIView):
    """Everything the Sales-approval form needs: items with unsettled stock and
    the agents those items are assigned to."""

    def get(self, request):
        from inventory.models import StockAssignment

        if sales_role_disabled(request):
            return sales_disabled_response()
        tenant = tenant_of(request.user)
        rows = (
            StockAssignment.objects.filter(
                request__tenant=tenant,
                request__status="APPROVED",
                settlement__isnull=True,
            )
            .select_related("lot__item", "sales_agent", "request")
            .order_by("lot__item__name")
        )
        # A legacy per-agent login only ever sees its own stock.
        agent_ids = visible_agent_ids(request.user)
        if agent_ids:
            rows = rows.filter(sales_agent_id__in=agent_ids)
        item_param = request.query_params.get("item")

        items, seen = [], set()
        for row in rows:
            if row.lot_id in seen:
                continue
            seen.add(row.lot_id)
            siblings = [r for r in rows if r.lot_id == row.lot_id]
            items.append(
                {
                    "lot": row.lot_id,
                    "item_name": row.lot.item.name,
                    "voucher_number": row.lot.voucher_number,
                    "unit_price": str(
                        (row.unit_sales_at_assignment + row.changing_amount).quantize(
                            Decimal("0.01")
                        )
                    ),
                    "assigned_quantity": str(
                        sum((r.quantity for r in siblings), Decimal("0"))
                    ),
                    "agents": sorted(
                        {
                            r.sales_agent_id
                            for r in siblings
                            if r.sales_agent_id is not None
                        }
                    ),
                }
            )

        scoped = rows.filter(lot_id=item_param) if item_param else rows
        assignments = [
            {
                "assignment": row.pk,
                "lot": row.lot_id,
                "item_name": row.lot.item.name,
                "voucher_number": row.lot.voucher_number,
                "agent": row.sales_agent_id,
                "agent_name": (
                    f"{row.sales_agent.first_name} {row.sales_agent.last_name}".strip()
                    if row.sales_agent
                    else ""
                ),
                "assigned_quantity": str(row.quantity),
                "unit_price": str(
                    (row.unit_sales_at_assignment + row.changing_amount).quantize(
                        Decimal("0.01")
                    )
                ),
                "expected_amount": str(
                    (
                        (row.unit_sales_at_assignment + row.changing_amount)
                        * row.quantity
                    ).quantize(Decimal("0.01"))
                ),
                "date": row.request.date,
            }
            for row in scoped
        ]
        return Response({"data": {"items": items, "assignments": assignments}})
