"""Self-signup support: unmanaged mirrors of the console-owned tables.

The Apex console (volosync-admin) owns the DDL for the pricing catalog, the
platform sales agents and the setup-payment queue. The tenant app only reads
the catalog and agents, and appends PENDING rows to the setup queue — the
console decides every payment.
"""

from decimal import Decimal

from django.db import models


class PlatformSalesAgent(models.Model):
    """Mirror of the console's field sales team for the signup combobox."""

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    city = models.CharField(max_length=120, blank=True)
    sex = models.CharField(max_length=10, blank=True)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "tenants_platformsalesagent"

    def __str__(self):
        return self.name


class PricingPlan(models.Model):
    """Read-only mirror of the console's pricing catalog.

    Yearly only. The default active plan drives the fees a signup is quoted
    and charged — resolved server-side, never from the browser.
    """

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=100)
    setup_fee = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    yearly_base_fee = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    inventory_yearly_fee = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    hr_yearly_fee = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    sales_role_yearly_fee = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    description = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_default = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "tenants_pricingplan"

    def __str__(self):
        return self.name


class SetupPayment(models.Model):
    """Mirror of the console's onboarding (setup) fee queue.

    The signup appends PENDING rows; only the console decides them. Approval
    activates the tenant — the console stamps the paid year on the tenant row.
    """

    STATUS = (("PENDING", "Pending"), ("APPROVED", "Approved"), ("REJECTED", "Rejected"))

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.IntegerField()
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    transaction_ref = models.CharField(max_length=60, blank=True)
    channel = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=20, default="PENDING")
    note = models.CharField(max_length=255, blank=True)
    submitted_by_id = models.IntegerField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by_id = models.IntegerField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "tenants_setuppayment"

    def __str__(self):
        return f"{self.tenant_id} · setup {self.amount}"


def default_pricing_plan():
    """The plan fees are quoted from: default AND active."""
    return PricingPlan.objects.filter(is_default=True, active=True).first()


def quoted_fees(inventory: bool, hr: bool, sales: bool = False):
    """(setup_fee, yearly_fee) from the default active plan.

    Mirrors the console's suggest_fees: base + each switched-on module fee.
    Returns (None, None) when there is no default active plan.
    """
    plan = default_pricing_plan()
    if plan is None:
        return None, None
    yearly = plan.yearly_base_fee
    if inventory:
        yearly += plan.inventory_yearly_fee
    if hr:
        yearly += plan.hr_yearly_fee
    if sales:
        yearly += plan.sales_role_yearly_fee
    return plan.setup_fee, yearly


def latest_setup_payment(tenant_id):
    """The queue row that decides the signup's standing.

    An APPROVED row wins (the console activated the tenant); then any PENDING
    row; otherwise the latest REJECTED row — whose note the signup screen
    shows.
    """
    rows = SetupPayment.objects.filter(tenant_id=tenant_id).order_by("-id")
    for candidate in rows:
        if candidate.status == "APPROVED":
            return candidate
    pending = rows.filter(status="PENDING").first()
    if pending:
        return pending
    return rows.first()


def signup_awaiting_approval(tenant_id) -> bool:
    """True when a self-signup is waiting on (or was rejected by) Apex.

    Only meaningful for a tenant with no active paid year and no trial — the
    console stamps the paid year at approval, so anything else means the
    signup still gates the login.
    """
    return latest_setup_payment(tenant_id) is not None
