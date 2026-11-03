from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.models import StockLot

from .models import Tenant
from .serializers import TenantSerializer

User = get_user_model()


class TenantMeView(APIView):
    def get(self, request):
        profile = getattr(request.user, "profile", None)
        if not profile or not profile.tenant:
            return Response({"data": None})
        return Response({"data": TenantSerializer(profile.tenant).data})


class TenantSubscriptionView(APIView):
    """Subscription snapshot for the signed-in tenant.

    Volosync stores module access as two booleans on the tenant row, so the
    frontend's canonical module list is derived here instead of being persisted
    separately.
    """

    def get(self, request):
        profile = getattr(request.user, "profile", None)
        tenant = profile.tenant if profile else None
        if not tenant:
            return Response(
                {"errors": {"message": "No property linked to this login."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        modules = ["Credentials(Common)"]
        if tenant.module_inventory:
            modules.append("Inventory")
        if tenant.module_hr:
            modules.append("HR Module")

        paid_until = (
            tenant.subscription_expires_on.isoformat()
            if tenant.subscription_expires_on
            else None
        )
        return Response(
            {
                "data": {
                    "modules": modules,
                    "setupFeeETB": 0,
                    "quarterlyFeeETB": 0,
                    "setupFeeApproved": True,
                    "createdAt": tenant.created_at.isoformat()
                    if tenant.created_at
                    else None,
                    "billingStartedAt": None,
                    "billingHold": False,
                    # Tenant billing is managed outside this app; treat it as
                    # exempt so the module flags above are the real gate.
                    "isIllustrationTenant": True,
                    "freeTrialEndsAt": None,
                    "subscriptionPaidUntil": paid_until,
                    "subscriptionPaymentApproved": True,
                    "paidQuartersCount": 0,
                    "awaitingSelfSignupSetup": False,
                    "paymentTransactionRef": tenant.subscription_transaction_id
                    or None,
                    "cafeOrderMode": "digital",
                    "cafeOrderModeHistory": [],
                    "cashierCancelOrdersEnabled": False,
                    "receptionCmPortalEnabled": False,
                    "waiterOrderingEnabled": False,
                    "waiterPaymentApprovalEnabled": False,
                    "hrSoloManagerEnabled": False,
                    "hrBiometricsEnabled": False,
                }
            }
        )


class TenantListView(APIView):
    """Platform list: every wholesaler plus its branches."""

    def get(self, request):
        tenants = Tenant.objects.annotate(
            staff_count=Count("staff", distinct=True),
            branch_count=Count("branches", distinct=True),
            lot_count=Count("lots", distinct=True),
        ).prefetch_related("branches")
        payload = []
        for tenant in tenants:
            payload.append(
                {
                    **TenantSerializer(tenant).data,
                    "staff_count": tenant.staff_count,
                    "branch_count": tenant.branch_count,
                    "lot_count": tenant.lot_count,
                    "is_branch": tenant.parent_id is not None,
                    "stock_value": str(
                        sum(
                            (
                                lot.remaining_quantity * lot.unit_cost
                                for lot in StockLot.objects.filter(tenant=tenant)
                            ),
                            0,
                        )
                    ),
                }
            )
        return Response({"data": payload})


class TenantDetailView(APIView):
    def patch(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if not tenant:
            return Response({"errors": {"message": "Not found."}}, status=404)
        allowed = [
            "name",
            "phone",
            "address",
            "vat_rate",
            "module_inventory",
            "module_hr",
            "sales_role_enabled",
            "subscription_expires_on",
        ]
        for field in allowed:
            if field in request.data:
                setattr(tenant, field, request.data[field])
        tenant.save()
        return Response({"data": TenantSerializer(tenant).data})


class BranchCreateView(APIView):
    """Owner/Apex create a branch as a child tenant with manager credentials."""

    def post(self, request, pk):
        parent = Tenant.objects.filter(pk=pk).first()
        if not parent:
            return Response({"errors": {"message": "Not found."}}, status=404)

        name = (request.data.get("name") or "").strip()
        tin = (request.data.get("tin") or "").strip()
        if not name or not tin:
            return Response(
                {"errors": {"message": "Branch name and TIN are required."}}, status=400
            )
        if tin == parent.tin:
            return Response(
                {"errors": {"message": "A branch must have its own TIN."}}, status=400
            )
        if Tenant.objects.filter(tin=tin).exists():
            return Response(
                {"errors": {"message": "That TIN already exists."}}, status=400
            )

        branch = Tenant.objects.create(
            name=name,
            tin=tin,
            phone=request.data.get("phone", ""),
            address=request.data.get("address", ""),
            vat_rate=parent.vat_rate,
            module_inventory=parent.module_inventory,
            module_hr=parent.module_hr,
            sales_role_enabled=parent.sales_role_enabled,
            parent=parent,
        )

        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        if username and password:
            if User.objects.filter(username=username).exists():
                return Response(
                    {"errors": {"message": "That username is taken."}}, status=400
                )
            user = User.objects.create_user(
                username=username,
                password=password,
                first_name=request.data.get("manager_first_name", "Branch"),
                last_name=request.data.get("manager_last_name", "Manager"),
            )
            from user.models import Profile

            Profile.objects.create(user=user, tenant=branch, role="MANAGER")

        return Response({"data": TenantSerializer(branch).data}, status=201)
