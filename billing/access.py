"""Yearly billing rules shared by the login gate and the subscription snapshot.

Mirrors lib/subscriptionQuarter.ts on the frontend: a free trial warns 10 days
out, a paid year warns 7 days out, and either one ending blocks the login. There
is no grace period — a yearly subscription stops access on its final day.
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
    "trial_expired": "Your free trial has ended.",
    "expired": "Your subscription is ended.",
    "pending_approval": "Your payment is awaiting Apex verification.",
    "setup_pending": (
        "Your registration is pending Apex approval. Login is disabled until "
        "your setup payment is verified. For help, contact Apex on WhatsApp: "
        "+251935000642 or +251930272975."
    ),
}


def prospective_cover_until(row, years, today=None):
    today = today or timezone.localdate()
    start = row.subscription_expires_on if row else None
    if not start or start < today:
        start = today
    return start + timedelta(days=YEAR_DAYS * years)


def family_is_illustration(row):
    """Illustration is a company-wide identity: any family member exempts all.

    Branches of an illustration wholesaler never owe a payment, even when the
    flag sits on the main/root rather than on the branch row itself.
    """
    if row is None:
        return False
    if row.is_illustration:
        return True
    root = row
    seen = {row.pk}
    while root.parent_id and root.parent_id not in seen:
        parent = TenantBilling.objects.filter(pk=root.parent_id).first()
        if parent is None:
            break
        if parent.is_illustration:
            return True
        seen.add(parent.pk)
        root = parent
    if root.is_illustration:
        return True
    frontier = [root.pk]
    visited = {root.pk}
    while frontier:
        children = TenantBilling.objects.filter(parent_id__in=frontier)
        if children.filter(is_illustration=True).exists():
            return True
        ids = list(children.values_list("pk", flat=True))
        frontier = [pk for pk in ids if pk not in visited]
        visited.update(frontier)
    return False


def period_status(row, today=None, payment_pending=False):
    today = today or timezone.localdate()
    if row is None or family_is_illustration(row):
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
        if left <= 0:
            return "pending_approval" if payment_pending else "expired"
        return "warning" if left <= SUBSCRIPTION_WARNING_DAYS else "active"

    if row.free_trial_ends_on:
        # Trial ended with no paid year on record. An approved renewal writes a
        # future subscription_expires_on, so it never reaches this branch.
        return "pending_approval" if payment_pending else "trial_expired"

    # Neither a trial nor a paid year on record: nothing to enforce yet.
    return "exempt"


def setup_submission_outstanding(tenant_id) -> bool:
    """True when the console's setup queue holds a row for this tenant.

    Imported lazily — the signup mirrors live in the tenants app and must not
    create an import cycle with billing.
    """
    from tenants.signup import latest_setup_payment

    return latest_setup_payment(tenant_id) is not None


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

    # A self-signup waits for Apex before anyone can log in. It has no active
    # paid year and no trial, so without a setup submission in the console's
    # queue the status would read "exempt" and slip straight through.
    # Illustration families never wait on setup approval and never owe a
    # payment, so the override must not pull them back into a gate.
    if (
        status == "exempt"
        and not family_is_illustration(row)
        and setup_submission_outstanding(tenant_id)
    ):
        status = "setup_pending"

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