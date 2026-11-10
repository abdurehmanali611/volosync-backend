"""Self-signup: a wholesaler registers, Apex approves, then access unlocks.

Public DRF endpoints (no GraphQL — this is the Django equivalent of the
hotcol signup flow):

- GET  signup/agents/    — active platform sales agents for the combobox
- GET  signup/pricing/   — server-resolved fees for the chosen module mix
- POST signup/           — create the pending tenant + manager + setup payment
- GET  signup/status/    — pending/approved/rejected, polled by the site
- POST signup/payment/   — re-submit a payment reference after a rejection

Until Apex approves, `billing.access` denies the login for every role
(`setup_pending`); approval stamps a paid year on the tenant, which unlocks it.
"""

from decimal import InvalidOperation

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from sales.models import SalesAgent

from .models import Tenant
from .signup import (
    PlatformSalesAgent,
    SetupPayment,
    latest_setup_payment,
    quoted_fees,
)

User = get_user_model()

PAYMENT_CHANNELS = {
    "TELEBIRR": "Telebirr",
    "BANK": "Commercial Bank of Ethiopia (CBE)",
    "OTHER": "Other",
}

SETUP_WAIT_COPY = (
    "Your registration is pending Apex approval. Login is disabled until your "
    "setup payment is verified. For help, contact Apex on WhatsApp: "
    "+251935000642 or +251930272975."
)


class SignupThrottle(AnonRateThrottle):
    """Public endpoint — keep abuse to a crawl."""

    scope = "signup"


def _denied(message, code=http_status.HTTP_400_BAD_REQUEST):
    return Response({"errors": {"message": message}}, status=code)


def _today():
    return timezone.localdate()


def _clean(value, limit=255):
    return str(value or "").strip()[:limit]


class SignupAgentsView(APIView):
    """Active platform sales agents for the signup combobox."""

    permission_classes = [AllowAny]
    throttle_classes = [SignupThrottle]

    def get(self, request):
        rows = PlatformSalesAgent.objects.filter(active=True).order_by("name")
        return Response(
            {
                "data": [
                    {"id": row.id, "name": row.name, "phone": row.phone, "city": row.city}
                    for row in rows
                ]
            }
        )


class SignupPricingView(APIView):
    """Fees for a module mix, resolved from the catalog server-side."""

    permission_classes = [AllowAny]
    throttle_classes = [SignupThrottle]

    def get(self, request):
        inventory = str(request.query_params.get("inventory", "1")) not in ("0", "false")
        hr = str(request.query_params.get("hr", "1")) not in ("0", "false")
        sales = str(request.query_params.get("sales", "0")) not in ("0", "false")
        setup_fee, yearly_fee, plan = quoted_fees(inventory, hr, sales)
        if setup_fee is None or plan is None:
            return _denied(
                "The pricing catalog is empty. Contact Apex to have one applied.",
                http_status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "data": {
                    "setupFeeETB": float(setup_fee),
                    "yearlyFeeETB": float(yearly_fee),
                    "planName": plan.name,
                }
            }
        )


def _validate_registration(payload):
    """Common validation for signup + resubmit. Returns (values, error)."""
    channel = _clean(payload.get("channel")).upper()
    if channel not in PAYMENT_CHANNELS:
        return None, "Pick a payment channel."
    reference = _clean(payload.get("transaction_ref"), 60)
    if len(reference) < 4:
        return None, "Enter the transaction reference from your receipt."
    return {"channel": channel, "reference": reference}, None


class SignupView(APIView):
    """Create a pending wholesaler: tenant + manager + setup payment queue row."""

    permission_classes = [AllowAny]
    throttle_classes = [SignupThrottle]

    def post(self, request):
        payload = request.data or {}
        name = _clean(payload.get("name"))
        tin = _clean(payload.get("tin"), 50)
        logo_url = _clean(payload.get("logo_url"), 400)
        username = _clean(payload.get("username"), 150)
        password = str(payload.get("password") or "")
        sales_agent_id = payload.get("sales_agent_id")
        inventory = bool(payload.get("module_inventory", True))
        hr = bool(payload.get("module_hr", True))
        sales = bool(payload.get("sales_role_enabled", False))

        if len(name) < 2:
            return _denied("Enter the registered business name.")
        if not tin:
            return _denied("Enter the business TIN number.")
        if not username:
            return _denied("Choose a manager username.")
        if len(password) < 6:
            return _denied("The manager password must be at least 6 characters.")

        fees, error = _validate_registration(payload)
        if error:
            return _denied(error)
        channel, reference = fees["channel"], fees["reference"]

        if User.objects.filter(username__iexact=username).exists() or (
            SalesAgent.objects.filter(login_username__iexact=username).exists()
        ):
            return _denied("That username is already taken.")
        if Tenant.objects.filter(tin__iexact=tin).exists():
            return _denied("That TIN is already registered.")

        # The Sales Role is an add-on to Inventory: it cannot be checked
        # without it (the mirror of the console's tenant-creation form).
        if sales and not inventory:
            return _denied("The Sales Role requires the Inventory module.")

        setup_fee, yearly_fee, plan = quoted_fees(inventory, hr, sales)
        if setup_fee is None or plan is None:
            return _denied(
                "The pricing catalog is empty. Contact Apex to have one applied.",
                http_status.HTTP_404_NOT_FOUND,
            )
        if setup_fee <= 0:
            return _denied(
                "Apex has not set a setup fee for new signups yet. Contact Apex."
            )

        sales_agent = None
        if sales_agent_id not in (None, "", "null"):
            sales_agent = PlatformSalesAgent.objects.filter(
                pk=sales_agent_id, active=True
            ).first()
            if sales_agent is None:
                return _denied("That sales agent is not available.")

        now = timezone.now()
        today = timezone.localdate()
        try:
            with transaction.atomic():
                # Lock the identities before anything is written.
                if User.objects.filter(username__iexact=username).exists():
                    return _denied("That username is already taken.")
                tenant = Tenant.objects.create(
                    name=name,
                    tin=tin,
                    logo_url=logo_url,
                    module_inventory=inventory,
                    module_hr=hr,
                    sales_role_enabled=sales,
                    is_main=True,
                )
                # Console-owned columns volosync's Tenant model does not carry:
                # attribution + the catalog-priced fees (fees stay auto-priced).
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE tenants_tenant SET sales_agent_id = %s, "
                        "annual_fee = %s, setup_fee = %s, fees_manually_set = 0 "
                        "WHERE id = %s",
                        [
                            sales_agent.id if sales_agent else None,
                            yearly_fee,
                            setup_fee,
                            tenant.pk,
                        ],
                    )
                user = User.objects.create_user(
                    username=username,
                    email="",
                    password=password,
                    first_name=_clean(payload.get("first_name"), 80),
                    last_name=_clean(payload.get("last_name"), 80),
                )
                user.profile  # noqa: B018 — Profile is created by the console-side signal if any
                from user.models import Profile

                Profile.objects.create(user=user, tenant=tenant, role="MANAGER")
                SetupPayment.objects.create(
                    tenant_id=tenant.pk,
                    amount=setup_fee,
                    transaction_ref=reference,
                    channel=channel,
                    status="PENDING",
                    submitted_by_id=user.id,
                    submitted_at=now,
                    created_at=now,
                )
        except InvalidOperation:
            return _denied("Could not register with the supplied details.")
        except Exception as exc:  # noqa: BLE001 — surfaced through the envelope
            message = str(exc)
            if "unique" in message.lower() or "duplicate" in message.lower():
                return _denied("That username or TIN is already registered.")
            raise

        return Response(
            {
                "data": {
                    "tenant": tenant.name,
                    "tin": tenant.tin,
                    "username": username,
                    "status": "pending",
                    "message": SETUP_WAIT_COPY,
                }
            },
            status=http_status.HTTP_201_CREATED,
        )


class SignupStatusView(APIView):
    """Public standing of a signup, polled by the pending-approval screen."""

    permission_classes = [AllowAny]
    throttle_classes = [SignupThrottle]

    def get(self, request):
        username = _clean(request.query_params.get("username"), 150)
        if not username:
            return _denied("Username is required.")
        profile = (
            get_user_model()
            .objects.filter(username__iexact=username, profile__role="MANAGER")
            .select_related("profile__tenant")
            .first()
        )
        if profile is None or profile.profile.tenant is None:
            return _denied("No registration found for that username.", 404)
        tenant = profile.profile.tenant
        row = latest_setup_payment(tenant.pk)
        # An active paid year means Apex approved (and stamped) the signup.
        if tenant.subscription_expires_on and tenant.subscription_expires_on >= _today():
            standing = "approved"
        elif row is None:
            standing = "pending"
        else:
            standing = row.status.lower()
        return Response(
            {
                "data": {
                    "status": standing,
                    "business_name": tenant.name,
                    "note": row.decision_note if standing == "rejected" else "",
                }
            }
        )


class SignupResubmitView(APIView):
    """Re-submit a payment reference after a rejection."""

    permission_classes = [AllowAny]
    throttle_classes = [SignupThrottle]

    def post(self, request):
        payload = request.data or {}
        username = _clean(payload.get("username"), 150)
        if not username:
            return _denied("Username is required.")
        fees, error = _validate_registration(payload)
        if error:
            return _denied(error)

        profile = (
            get_user_model()
            .objects.filter(username__iexact=username, profile__role="MANAGER")
            .select_related("profile__tenant")
            .first()
        )
        if profile is None or profile.profile.tenant is None:
            return _denied("No registration found for that username.", 404)
        tenant = profile.profile.tenant

        row = latest_setup_payment(tenant.pk)
        if row is not None and row.status == "APPROVED":
            return _denied("This registration is already approved.")
        if row is not None and row.status == "PENDING":
            return Response(
                {
                    "data": {
                        "status": "pending",
                        "message": SETUP_WAIT_COPY,
                    }
                }
            )

        # The quoted fee may have changed since the signup; price from the
        # catalog again so the queue row matches what Apex expects.
        setup_fee, _yearly, _plan = quoted_fees(
            tenant.module_inventory,
            tenant.module_hr,
            tenant.sales_role_enabled,
        )
        if setup_fee is None or _plan is None:
            return _denied(
                "The pricing catalog is empty. Contact Apex to have one applied."
            )
        now = timezone.now()
        SetupPayment.objects.create(
            tenant_id=tenant.pk,
            amount=setup_fee,
            transaction_ref=fees["reference"],
            channel=fees["channel"],
            status="PENDING",
            submitted_by_id=profile.user_id,
            submitted_at=now,
            created_at=now,
        )
        return Response(
            {
                "data": {
                    "status": "pending",
                    "message": SETUP_WAIT_COPY,
                }
            },
            status=http_status.HTTP_201_CREATED,
        )
