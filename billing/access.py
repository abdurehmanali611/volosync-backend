"""Yearly billing rules shared by the login gate and the subscription snapshot.

Mirrors lib/subscriptionQuarter.ts on the frontend: a free trial warns 10 days
out, a paid year warns 7 days out, and either one ending blocks the login. There
is no grace period — an ended subscription stops access the next day.
"""

from datetime import timedelta

from django.utils import timezone

from .models import SubscriptionPayment, TenantBilling

TRIAL_WARNING_DAYS = 10
SUBSCRIPTION_WARNING_DAYS = 7
YEAR_DAYS = 365

#: Only these roles may reach the payment portal; everyone else is just denied.
PORTAL_ROLES = {"MANAGER", "OWNER"}

ALLOWED_STATUSES = {"exempt", "on_hold", "trial", "trial_ending", "active", "warning"}

BLOCK_MESSAGES = {
    "restricted": "This wholesaler account is restricted. Contact Apex to restore access.",
    "trial_expired": "Your subscription is ended.",
    "expired": "Your subscription is ended.",
    "pending_approval": "Your payment is awaiting Apex verification.",
}


def prospective_cover_until(row, years, today=None):
    today = today or timezone.localdate()
    start = row.subscription_expires_on if row else None
    if not start or start < today:
        start = today
    return start + timedelta(days=YEAR_DAYS * years)


def period_status(row, today=None, payment_pending=False):
    today = today or timezone.localdate()
    if row is None or row.is_illustration:
        return "exempt"
    if row.account_status != "ACTIVE":
        return "restricted"
    # A hold freezes renewals without revoking access — that is what
    # distinguishes it from a suspension.
    if row.billing_hold:
        return "on_hold"

    # An active free trial is the current billing window: it governs access and
    # its own 10-day warning, even when the row also carries the yearly renewal
    # date the console stamps at signup. Mirrors lib/subscriptionQuarter.ts and
    # the console's billing_state.
    if row.free_trial_ends_on:
        trial_left = (row.free_trial_ends_on - today).days
        if trial_left >= 0:
            return "trial_ending" if trial_left <= TRIAL_WARNING_DAYS else "trial"

    if row.subscription_expires_on:
        left = (row.subscription_expires_on - today).days
        if left < 0:
            return "pending_approval" if payment_pending else "expired"
        return "warning" if left <= SUBSCRIPTION_WARNING_DAYS else "active"

    if row.free_trial_ends_on:
        # Trial ended with no paid year on record. An approved renewal writes a
        # future subscription_expires_on, so it never reaches this branch.
        return "pending_approval" if payment_pending else "trial_expired"

    # Neither a trial nor a paid year on record: nothing to enforce yet.
    return "exempt"


def tenant_access(tenant_id, role):
    """Login verdict for a staff role on a tenant."""
    if not tenant_id:
        return {
            "allowed": True,
            "status": "exempt",
            "access_mode": "full",
            "payment_kind": None,
            "message": "",
        }

    row = TenantBilling.objects.filter(pk=tenant_id).first()
    pending = SubscriptionPayment.objects.filter(
        tenant_id=tenant_id, status="PENDING"
    ).exists()
    status = period_status(row, payment_pending=pending)

    if status in ALLOWED_STATUSES:
        return {
            "allowed": True,
            "status": status,
            "access_mode": "full",
            "payment_kind": None,
            "message": "",
        }

    message = BLOCK_MESSAGES.get(status, "Your subscription is ended.")
    if status != "restricted" and role in PORTAL_ROLES:
        return {
            "allowed": True,
            "status": status,
            "access_mode": "payment_portal",
            "payment_kind": "yearly",
            "message": message,
        }
    return {
        "allowed": False,
        "status": status,
        "access_mode": "full",
        "payment_kind": None,
        "message": message,
    }