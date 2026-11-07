from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .access import (
    PORTAL_ROLES,
    SUBSCRIPTION_WARNING_DAYS,
    TRIAL_WARNING_DAYS,
    period_status,
    prospective_cover_until,
    tenant_access,
)
from .models import SubscriptionPayment, TenantBilling

CHANNELS = {"TELEBIRR", "BANK", "CASH", "OTHER"}


def _denied(message, code=status.HTTP_403_FORBIDDEN):
    return Response({"errors": {"message": message}}, status=code)


def _tenant_of(request):
    profile = getattr(request.user, "profile", None)
    return (profile.tenant if profile else None), (profile.role if profile else "")


def _iso(value):
    return value.isoformat() if value else None


class TenantSubscriptionView(APIView):
    """Real yearly billing snapshot for the signed-in wholesaler."""

    def get(self, request):
        tenant, role = _tenant_of(request)
        if not tenant:
            return _denied(
                "No wholesaler linked to this login.", status.HTTP_404_NOT_FOUND
            )

        row = TenantBilling.objects.filter(pk=tenant.pk).first()
        pending = (
            SubscriptionPayment.objects.filter(tenant_id=tenant.pk, status="PENDING")
            .order_by("-id")
            .first()
        )
        today = timezone.localdate()
        state = period_status(row, today, payment_pending=pending is not None)
        access = tenant_access(tenant.pk, role)

        modules = ["Credentials(Common)"]
        if tenant.module_inventory:
            modules.append("Inventory")
        if tenant.module_hr:
            modules.append("HR Module")

        annual_fee = row.annual_fee if row else Decimal("0.00")
        setup_fee = (row.setup_fee if row else None) or Decimal("0.00")
        trial_end = row.free_trial_ends_on if row else None
        paid_until = row.subscription_expires_on if row else None
        # A free-trial payment charges the setup fee; every other state asks
        # for the yearly fee. Mirrors the portal page's amount logic.
        trial_fee_applies = state in ("trial", "trial_ending", "trial_expired")
        amount_due = (
            setup_fee
            if trial_fee_applies and setup_fee > 0
            else annual_fee
        )

        return Response(
            {
                "data": {
                    "modules": modules,
                    "setupFeeETB": float(setup_fee),
                    "quarterlyFeeETB": 0,
                    "annualFeeETB": float(annual_fee),
                    "setupFeeApproved": True,
                    "createdAt": tenant.created_at.isoformat()
                    if tenant.created_at
                    else None,
                    "billingStartedAt": None,
                    "billingHold": bool(row and row.billing_hold),
                    "isIllustrationTenant": bool(row and row.is_illustration),
                    "accountStatus": row.account_status if row else "ACTIVE",
                    "freeTrialEndsAt": _iso(trial_end),
                    "subscriptionPaidUntil": _iso(paid_until),
                    "subscriptionPaymentApproved": True,
                    "paidQuartersCount": 0,
                    "awaitingSelfSignupSetup": False,
                    "paymentTransactionRef": (
                        row.subscription_transaction_id if row else ""
                    )
                    or None,
                    "paymentPending": pending is not None,
                    "periodStatus": state,
                    "accessMode": access["access_mode"],
                    "trialWarningDays": TRIAL_WARNING_DAYS,
                    "subscriptionWarningDays": SUBSCRIPTION_WARNING_DAYS,
                    "trialDaysLeft": (trial_end - today).days if trial_end else None,
                    "subscriptionDaysLeft": (paid_until - today).days
                    if paid_until
                    else None,
                    "amountDueETB": float(amount_due),
                    "pendingPayment": {
                        "id": pending.id,
                        "transactionRef": pending.transaction_id,
                        "channel": pending.channel,
                        "amount": float(pending.amount),
                        "years": pending.years,
                        "paidOn": _iso(pending.paid_on),
                        "coversUntil": _iso(pending.covers_until),
                        "submittedAt": pending.submitted_at.isoformat()
                        if pending.submitted_at
                        else None,
                    }
                    if pending
                    else None,
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


class TenantPaymentSubmitView(APIView):
    """Manager/owner reports a yearly payment; Apex verifies it in the console."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tenant, role = _tenant_of(request)
        if not tenant:
            return _denied(
                "No wholesaler linked to this login.", status.HTTP_404_NOT_FOUND
            )
        if role not in PORTAL_ROLES:
            return _denied("Only the manager can submit a subscription payment.")

        existing = SubscriptionPayment.objects.filter(
            tenant_id=tenant.pk, status="PENDING"
        ).first()
        if existing:
            return Response(
                {
                    "data": {
                        "payment": existing.id,
                        "status": existing.status,
                        "amount": float(existing.amount),
                        "coversUntil": _iso(existing.covers_until),
                    }
                }
            )

        channel = str(request.data.get("channel") or "OTHER").strip().upper()
        if channel not in CHANNELS:
            return _denied("Pick a payment channel.", status.HTTP_400_BAD_REQUEST)

        reference = str(request.data.get("transaction_ref") or "").strip()[:60]
        if len(reference) < 4:
            return _denied(
                "Enter the transaction reference from your receipt.",
                status.HTTP_400_BAD_REQUEST,
            )

        try:
            years = int(request.data.get("years", 1))
        except (TypeError, ValueError):
            return _denied("Years must be a whole number.", status.HTTP_400_BAD_REQUEST)
        if not 1 <= years <= 5:
            return _denied("Pay for 1 to 5 years.", status.HTTP_400_BAD_REQUEST)

        row = TenantBilling.objects.filter(pk=tenant.pk).first()
        annual_fee = row.annual_fee if row else Decimal("0.00")
        setup_fee = (row.setup_fee if row else None) or Decimal("0.00")
        today = timezone.localdate()
        # The fee is the console's price, never a number the browser supplies.
        # A free-trial payment is the one-time setup fee (years do not
        # multiply it); any other state charges the yearly fee.
        state = period_status(row, today)
        if state in ("trial", "trial_ending", "trial_expired") and setup_fee > 0:
            amount = setup_fee
            years = 1
        else:
            amount = (annual_fee * years).quantize(Decimal("0.01"))
        if amount <= 0:
            return _denied(
                "No fee is set for this wholesaler yet. Contact Apex.",
                status.HTTP_400_BAD_REQUEST,
            )

        payment = SubscriptionPayment.objects.create(
            tenant_id=tenant.pk,
            transaction_id=reference,
            channel=channel,
            status="PENDING",
            amount=amount,
            years=years,
            paid_on=today,
            covers_until=prospective_cover_until(row, years, today),
            note=str(request.data.get("note") or "").strip()[:255],
            submitted_by=request.user,
            submitted_at=timezone.now(),
        )

        return Response(
            {
                "data": {
                    "payment": payment.id,
                    "status": payment.status,
                    "amount": float(payment.amount),
                    "coversUntil": _iso(payment.covers_until),
                }
            },
            status=status.HTTP_201_CREATED,
        )