import os
from functools import wraps
from decimal import Decimal
from uuid import uuid4

from django.core.files.storage import default_storage
from django.db.models import ProtectedError, Q, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from approvals.services import decide, initial_status
from sales.models import SalesAgent

from .models import (
    Item,
    PromotionLine,
    PromotionRequest,
    RegistrationLine,
    RegistrationRequest,
    StockAssignment,
    StockLot,
    StockMovement,
    StockOutRequest,
    next_voucher,
)
from .serializers import (
    LotEditSerializer,
    MovementSerializer,
    PromotionSerializer,
    RegistrationSerializer,
    StockLotSerializer,
    StockOutSerializer,
)


def agent_login(user):
    """The sales agent record behind a login, if this user is one.

    Agents sign in with their own credentials against the tenant's shared Sales
    username, so the record — not the Access profile — says who they are.
    """
    return SalesAgent.objects.filter(user=user).select_related("tenant").first()


def role_of(user):
    role = getattr(getattr(user, "profile", None), "role", None)
    if role:
        return role
    return "SALES" if hasattr(user, "sales_agent") else None


def tenant_of(user):
    tenant = getattr(getattr(user, "profile", None), "tenant", None)
    if tenant:
        return tenant
    agent = agent_login(user)
    return agent.tenant if agent else None


def _create_registration_lines(doc, tenant, lines):
    for line_data in lines:
        RegistrationLine.objects.create(
            request=doc,
            vat_rate=line_data.get("vat_rate", tenant.vat_rate),
            **{key: value for key, value in line_data.items() if key != "vat_rate"},
        )


def _create_stock_assignments(doc, rows):
    for row in rows:
        lot = row["lot"]
        StockAssignment.objects.create(
            request=doc,
            lot=lot,
            sales_agent=row.get("sales_agent"),
            quantity=row["quantity"],
            changing_amount=row.get("changing_amount", 0),
            change_reason=row.get("change_reason", ""),
            unit_sales_at_assignment=lot.unit_sales,
        )


def _store_draft(model, request, pk):
    """A STORE draft the caller may still edit, send or delete."""
    return model.objects.filter(
        tenant=tenant_of(request.user),
        pk=pk,
        status="DRAFT",
        created_by=request.user,
    ).first()


def acting_role(user):
    """Role used for web authorisation: the owner runs the manager workspace."""
    role = role_of(user)
    return "MANAGER" if role == "OWNER" else role


def require_roles(*roles):
    allowed = set(roles)
    if "MANAGER" in allowed:
        allowed.add("OWNER")

    def decorator(view):
        original = view

        @wraps(view)
        def wrapper(self, request, *args, **kwargs):
            if role_of(request.user) not in allowed:
                raise PermissionDenied(f"Allowed roles: {', '.join(roles)}.")
            return original(self, request, *args, **kwargs)

        return wrapper

    return decorator


class LotListView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = StockLot.objects.filter(tenant=tenant).select_related("item")
        search = request.query_params.get("search")
        if search:
            qs = qs.filter(Q(item__name__icontains=search) | Q(voucher_number__icontains=search))
        only_active = request.query_params.get("active")
        if only_active == "1":
            qs = qs.filter(remaining_quantity__gt=0)
        return Response(
            {"data": StockLotSerializer(qs, many=True, context={"request": request}).data}
        )


class LotDetailView(APIView):
    """Manager-only inventory edit. Quantity is a plain field edit (spec §4.2a)."""

    @require_roles("MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        lot = StockLot.objects.filter(tenant=tenant, pk=pk).first()
        if not lot:
            return Response({"errors": {"message": "Not found."}}, status=404)
        serializer = LotEditSerializer(lot, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"data": StockLotSerializer(lot, context={"request": request}).data}
        )

    @require_roles("MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        lot = StockLot.objects.filter(tenant=tenant, pk=pk).first()
        if not lot:
            return Response({"errors": {"message": "Not found."}}, status=404)
        # Stock-out and promotion lines protect their lot: a lot with history
        # stays in inventory so the movement trail cannot dangle.
        if lot.assignments.exists() or lot.promotions.exists():
            return Response(
                {
                    "errors": {
                        "message": "This lot has stock-outs and cannot be deleted."
                    }
                },
                status=400,
            )
        try:
            lot.delete()
        except ProtectedError:
            return Response(
                {"errors": {"message": "This lot is referenced by other records."}},
                status=400,
            )
        return Response({"data": {"id": pk, "deleted": True}})


class InventorySummaryView(APIView):
    """Payment & tax: registered vs in store vs stocked out, VAT split.

    Serves both the Store and the Finance Payment & tax screens.
    ``?tab=all|with_vat|without_vat`` filters on the computed input VAT per
    row: with_vat = lots where vat_amount > 0, without_vat = lots registered
    at a 0% rate (registration already accepts a per-line vat_rate override,
    so zero-rated lots exist as soon as one is registered).
    Output VAT is the VAT on the stocked-out portion, estimated at the lot's
    current unit_sales price.
    """

    def get(self, request):
        tenant = tenant_of(request.user)
        lots = StockLot.objects.filter(tenant=tenant).select_related("item")
        rows = []
        for lot in lots:
            registered_value = (lot.quantity * lot.unit_cost).quantize(Decimal("0.01"))
            in_store_value = (lot.remaining_quantity * lot.unit_cost).quantize(Decimal("0.01"))
            stocked_out_quantity = lot.quantity - lot.remaining_quantity
            stocked_out_value = registered_value - in_store_value
            paid_amount = lot.paid_amount
            base_value = registered_value
            vat_amount = paid_amount - base_value
            output_vat = (
                stocked_out_quantity * lot.unit_sales * lot.vat_rate / 100
            ).quantize(Decimal("0.01"))
            rows.append(
                {
                    "id": lot.id,
                    "voucher_number": lot.voucher_number,
                    "item_name": lot.item.name,
                    "category": lot.item.category,
                    "unit": lot.item.unit,
                    "quantity": str(lot.quantity),
                    "remaining_quantity": str(lot.remaining_quantity),
                    "stocked_out_quantity": str(lot.stocked_out_quantity),
                    "unit_cost": str(lot.unit_cost),
                    "paid_amount": str(paid_amount),
                    "price_with_vat": str(lot.price_with_vat),
                    "registered_qty": str(lot.quantity),
                    "in_store_qty": str(lot.remaining_quantity),
                    "stocked_out_qty": str(stocked_out_quantity),
                    "registered_value": str(registered_value),
                    "in_store_value": str(in_store_value),
                    "stocked_out_value": str(stocked_out_value),
                    "base_value": str(base_value),
                    "vat_amount": str(vat_amount),
                    "inclusive_amount": str(paid_amount),
                    "output_vat": str(output_vat),
                    "vat": str(vat_amount),
                    "vat_rate": str(lot.vat_rate),
                    # Decimal mirrors, dropped below — used to filter and total.
                    "_registered_value": registered_value,
                    "_in_store_value": in_store_value,
                    "_stocked_out_value": stocked_out_value,
                    "_base_value": base_value,
                    "_vat_amount": vat_amount,
                    "_inclusive_amount": paid_amount,
                    "_output_vat": output_vat,
                }
            )
        tab = request.query_params.get("tab", "all")
        if tab == "with_vat":
            rows = [row for row in rows if row["_vat_amount"] > 0]
        elif tab == "without_vat":
            rows = [row for row in rows if row["_vat_amount"] == 0]
        totals = {
            "registered_value": Decimal("0"),
            "in_store_value": Decimal("0"),
            "stocked_out_value": Decimal("0"),
            "base_value": Decimal("0"),
            "vat": Decimal("0"),
            "inclusive_amount": Decimal("0"),
            "output_vat": Decimal("0"),
        }
        # Totals follow the filtered rows so the panel always reconciles.
        for row in rows:
            totals["registered_value"] += row.pop("_registered_value")
            totals["in_store_value"] += row.pop("_in_store_value")
            totals["stocked_out_value"] += row.pop("_stocked_out_value")
            totals["base_value"] += row.pop("_base_value")
            totals["vat"] += row.pop("_vat_amount")
            totals["inclusive_amount"] += row.pop("_inclusive_amount")
            totals["output_vat"] += row.pop("_output_vat")
        totals["total_input_vat"] = totals["vat"]
        totals["total_output_vat"] = totals["output_vat"]
        totals["net_vat"] = totals["output_vat"] - totals["vat"]
        return Response(
            {
                "data": {
                    "tab": tab,
                    "rows": rows,
                    "totals": {key: str(value) for key, value in totals.items()},
                }
            }
        )


class LotImageUploadView(APIView):
    """Optional item photo: the browser uploads first, then sends the path with the line."""

    @require_roles("STORE", "FINANCE", "MANAGER")
    def post(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"errors": {"message": "Attach an image file."}}, status=400
            )
        if upload.size > 5 * 1024 * 1024:
            return Response(
                {"errors": {"message": "Keep the image under 5 MB."}}, status=400
            )
        extension = os.path.splitext(upload.name)[1].lower() or ".jpg"
        path = default_storage.save(f"lots/{uuid4().hex}{extension}", upload)
        return Response(
            {
                "data": {
                    "path": path,
                    "url": request.build_absolute_uri(default_storage.url(path)),
                }
            },
            status=201,
        )


class RegistrationListCreateView(APIView):
    @require_roles("STORE", "FINANCE", "MANAGER", "OWNER")
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = RegistrationRequest.objects.filter(tenant=tenant).prefetch_related("lines")
        if request.query_params.get("queue") == "1":
            role = acting_role(request.user)
            wanted = {
                "FINANCE": "PENDING_FINANCE",
                "MANAGER": "PENDING_MANAGER",
            }.get(role)
            qs = qs.filter(status=wanted) if wanted else qs.none()
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
            # Drafts are private to the store keeper who saved them.
            if status_filter == "DRAFT":
                qs = qs.filter(created_by=request.user)
        else:
            # Combined lists only carry documents that have been sent on.
            qs = qs.exclude(status="DRAFT")
        return Response({"data": RegistrationSerializer(qs, many=True).data})

    @require_roles("STORE", "FINANCE", "MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role = role_of(request.user)
        # Store registrations stay private drafts until the store keeper sends
        # them from Review; other roles create straight into their approval step.
        draft = role == "STORE"
        doc = RegistrationRequest.objects.create(
            tenant=tenant,
            voucher_number=next_voucher(RegistrationRequest, tenant, "REG"),
            created_by=request.user,
            submitted_at=None if draft else timezone.now(),
            status="DRAFT" if draft else initial_status(role),
            supplier_name=serializer.validated_data.get("supplier_name", ""),
            supplier_phone=serializer.validated_data.get("supplier_phone", ""),
            supplier_address=serializer.validated_data.get("supplier_address", ""),
            supplier_tin=serializer.validated_data.get("supplier_tin", ""),
            note=serializer.validated_data.get("note", ""),
        )
        _create_registration_lines(doc, tenant, serializer.validated_data["lines"])
        if doc.status == "APPROVED":
            doc.apply_on_approval()
        return Response({"data": RegistrationSerializer(doc).data}, status=201)


class RegistrationDraftView(APIView):
    """Edit or discard a store draft before it is sent to Finance."""

    @require_roles("STORE")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        doc = _store_draft(RegistrationRequest, request, pk)
        if not doc:
            return Response({"errors": {"message": "Draft not found."}}, status=404)
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        fields = [
            field
            for field in (
                "supplier_name",
                "supplier_phone",
                "supplier_address",
                "supplier_tin",
                "note",
            )
            if field in data
        ]
        for field in fields:
            setattr(doc, field, data[field])
        doc.save(update_fields=fields)
        doc.lines.all().delete()
        _create_registration_lines(doc, tenant, serializer.validated_data["lines"])
        return Response({"data": RegistrationSerializer(doc).data})

    @require_roles("STORE")
    def delete(self, request, pk):
        doc = _store_draft(RegistrationRequest, request, pk)
        if not doc:
            return Response({"errors": {"message": "Draft not found."}}, status=404)
        doc.delete()
        return Response({"data": {"id": pk, "deleted": True}})


class RegistrationSubmitView(APIView):
    """Send a store draft to Finance: DRAFT -> PENDING_FINANCE."""

    @require_roles("STORE")
    def post(self, request, pk):
        doc = _store_draft(RegistrationRequest, request, pk)
        if not doc:
            return Response({"errors": {"message": "Draft not found."}}, status=404)
        if not doc.lines.exists():
            return Response(
                {"errors": {"message": "Add at least one line before sending."}},
                status=400,
            )
        doc.status = "PENDING_FINANCE"
        doc.submitted_at = timezone.now()
        doc.save(update_fields=["status", "submitted_at"])
        return Response({"data": RegistrationSerializer(doc).data})


class RegistrationDecisionView(APIView):
    @require_roles("FINANCE", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        doc = RegistrationRequest.objects.filter(tenant=tenant, pk=pk).first()
        if not doc:
            return Response({"errors": {"message": "Not found."}}, status=404)
        decide(doc, request.user, request.data.get("decision"), request.data.get("note", ""))
        return Response({"data": RegistrationSerializer(doc).data})


class StockOutListCreateView(APIView):
    @require_roles("STORE", "FINANCE", "MANAGER", "SALES", "OWNER")
    def get(self, request):
        tenant = tenant_of(request.user)
        if role_of(request.user) == "SALES" and (
            not tenant or not tenant.sales_role_enabled
        ):
            return Response(
                {"errors": {"message": "Sales role is disabled for this tenant."}},
                status=status.HTTP_403_FORBIDDEN,
            )
        qs = StockOutRequest.objects.filter(tenant=tenant).prefetch_related(
            "assignments__lot__item", "assignments__sales_agent"
        )
        # Sales only ever sees approved documents. The shared Sales login sees
        # the whole agent team; a legacy per-agent login sees its own lines.
        if role_of(request.user) == "SALES":
            qs = qs.filter(status="APPROVED")
            agent_id = (
                SalesAgent.objects.filter(user=request.user)
                .values_list("id", flat=True)
                .first()
            )
            if agent_id:
                qs = qs.filter(assignments__sales_agent_id=agent_id).distinct()
        if request.query_params.get("queue") == "1":
            wanted = {
                "FINANCE": "PENDING_FINANCE",
                "MANAGER": "PENDING_MANAGER",
            }.get(acting_role(request.user))
            qs = qs.filter(status=wanted) if wanted else qs.none()
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
            if status_filter == "DRAFT":
                qs = qs.filter(created_by=request.user)
        else:
            # Combined lists only carry documents that have been sent on.
            qs = qs.exclude(status="DRAFT")
        return Response({"data": StockOutSerializer(qs, many=True).data})

    @require_roles("STORE")
    def post(self, request):
        tenant = tenant_of(request.user)
        serializer = StockOutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        doc = StockOutRequest.objects.create(
            tenant=tenant,
            voucher_number=next_voucher(StockOutRequest, tenant, "SO"),
            mode=serializer.validated_data.get("mode", "SINGLE"),
            date=serializer.validated_data["date"],
            note=serializer.validated_data.get("note", ""),
            created_by=request.user,
            # Store stock outs wait for review before reaching Finance.
            status="DRAFT",
        )
        _create_stock_assignments(doc, serializer.validated_data["assignments"])
        return Response({"data": StockOutSerializer(doc).data}, status=201)


class StockOutDraftView(APIView):
    """Edit or discard a store stock out draft before it is sent."""

    @require_roles("STORE")
    def patch(self, request, pk):
        doc = _store_draft(StockOutRequest, request, pk)
        if not doc:
            return Response({"errors": {"message": "Draft not found."}}, status=404)
        serializer = StockOutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        doc.mode = data.get("mode", doc.mode)
        doc.date = data.get("date", doc.date)
        doc.note = data.get("note", doc.note)
        doc.save(update_fields=["mode", "date", "note"])
        doc.assignments.all().delete()
        _create_stock_assignments(doc, data["assignments"])
        return Response({"data": StockOutSerializer(doc).data})

    @require_roles("STORE")
    def delete(self, request, pk):
        doc = _store_draft(StockOutRequest, request, pk)
        if not doc:
            return Response({"errors": {"message": "Draft not found."}}, status=404)
        doc.delete()
        return Response({"data": {"id": pk, "deleted": True}})


class StockOutSubmitView(APIView):
    """Send a stock out draft to Finance: DRAFT -> PENDING_FINANCE."""

    @require_roles("STORE")
    def post(self, request, pk):
        doc = _store_draft(StockOutRequest, request, pk)
        if not doc:
            return Response({"errors": {"message": "Draft not found."}}, status=404)
        assignments = list(doc.assignments.select_related("lot"))
        if not assignments:
            return Response(
                {"errors": {"message": "Add at least one line before sending."}},
                status=400,
            )
        for assignment in assignments:
            if assignment.quantity > assignment.lot.remaining_quantity:
                return Response(
                    {
                        "errors": {
                            "message": (
                                f"{assignment.lot.voucher_number} only has "
                                f"{assignment.lot.remaining_quantity} available."
                            )
                        }
                    },
                    status=400,
                )
        # Two rows can point at the same lot (batch mode), so compare the
        # combined quantity per lot as well.
        needed = {}
        for assignment in assignments:
            needed[assignment.lot_id] = (
                needed.get(assignment.lot_id, Decimal("0")) + assignment.quantity
            )
        for assignment in assignments:
            lot = assignment.lot
            if needed[lot.pk] > lot.remaining_quantity:
                return Response(
                    {
                        "errors": {
                            "message": (
                                f"{lot.voucher_number} only has "
                                f"{lot.remaining_quantity} available."
                            )
                        }
                    },
                    status=400,
                )
        doc.status = "PENDING_FINANCE"
        doc.save(update_fields=["status"])
        return Response({"data": StockOutSerializer(doc).data})


class StockOutDecisionView(APIView):
    @require_roles("FINANCE", "MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        doc = StockOutRequest.objects.filter(tenant=tenant, pk=pk).first()
        if not doc:
            return Response({"errors": {"message": "Not found."}}, status=404)
        decide(doc, request.user, request.data.get("decision"), request.data.get("note", ""))
        return Response({"data": StockOutSerializer(doc).data})


class MovementListView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        qs = StockMovement.objects.filter(tenant=tenant).select_related("lot__item", "actor")[:200]
        return Response({"data": MovementSerializer(qs, many=True).data})


class AgentOptionsView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        agents = SalesAgent.objects.filter(tenant=tenant, is_active=True)
        return Response(
            {
                "data": [
                    {"id": agent.id, "name": agent.full_name, "phone": agent.phone}
                    for agent in agents
                ]
            }
        )


class DashboardView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        lots = StockLot.objects.filter(tenant=tenant)
        stock_value = sum(
            (lot.remaining_quantity * lot.unit_cost for lot in lots), Decimal("0")
        )
        pending = {
            "registrations": RegistrationRequest.objects.filter(
                tenant=tenant, status__in=["PENDING_FINANCE", "PENDING_MANAGER"]
            ).count(),
            "stockouts": StockOutRequest.objects.filter(
                tenant=tenant, status__in=["PENDING_FINANCE", "PENDING_MANAGER"]
            ).count(),
        }
        return Response(
            {
                "data": {
                    "tenant": {
                        "name": tenant.name if tenant else "",
                        "tin": tenant.tin if tenant else "",
                        "vat_rate": str(tenant.vat_rate) if tenant else "15.00",
                        "sales_role_enabled": tenant.sales_role_enabled if tenant else False,
                    },
                    "tenant_id": tenant.pk if tenant else None,
                    "role": role_of(request.user),
                    "lots": lots.count(),
                    "stock_value": str(stock_value.quantize(Decimal("0.01"))),
                    "pending": pending,
                }
            }
        )


class PromotionListCreateView(APIView):
    """Promotion documents. Finance raises them; only the Manager decides."""

    def get(self, request):
        tenant = tenant_of(request.user)
        qs = PromotionRequest.objects.filter(tenant=tenant).prefetch_related(
            "lines__lot__item"
        )
        date_from = request.query_params.get("from")
        date_to = request.query_params.get("to")
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if request.query_params.get("queue") == "1":
            qs = (
                qs.filter(status="PENDING_MANAGER")
                if acting_role(request.user) == "MANAGER"
                else qs.none()
            )
        return Response({"data": PromotionSerializer(qs, many=True).data})

    @require_roles("FINANCE")
    def post(self, request):
        tenant = tenant_of(request.user)
        serializer = PromotionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mode = serializer.validated_data.get("mode", "SINGLE")
        if mode == "SINGLE" and len(serializer.validated_data["lines"]) != 1:
            raise PermissionDenied("A single promotion carries exactly one line.")
        doc = PromotionRequest.objects.create(
            tenant=tenant,
            voucher_number=next_voucher(PromotionRequest, tenant, "PR"),
            mode=mode,
            date=serializer.validated_data["date"],
            note=serializer.validated_data.get("note", ""),
            created_by=request.user,
            status=initial_status(role_of(request.user)),
        )
        for row in serializer.validated_data["lines"]:
            PromotionLine.objects.create(
                request=doc,
                lot=row["lot"],
                quantity=row["quantity"],
                changing_amount=row.get("changing_amount", 0),
                change_reason=row.get("change_reason", ""),
            )
        return Response({"data": PromotionSerializer(doc).data}, status=201)


class PromotionDecisionView(APIView):
    @require_roles("MANAGER")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        doc = PromotionRequest.objects.filter(tenant=tenant, pk=pk).first()
        if not doc:
            return Response({"errors": {"message": "Not found."}}, status=404)
        decide(doc, request.user, request.data.get("decision"), request.data.get("note", ""))
        return Response({"data": PromotionSerializer(doc).data})
