from decimal import Decimal

from django.conf import settings
from django.db import models


class TenantBilling(models.Model):
    """The billing columns the Apex console owns on tenants_tenant.

    Unmanaged and migration-free on purpose: the console emits the DDL, this
    app only reads and writes the columns that already exist.
    """

    subscription_expires_on = models.DateField(null=True, blank=True)
    subscription_transaction_id = models.CharField(max_length=60, blank=True)
    annual_fee = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    # One-time onboarding fee the free-trial payment asks for; NULL/zero means
    # the console has not set one and the portal falls back to the yearly fee.
    setup_fee = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    account_status = models.CharField(max_length=20, default="ACTIVE")
    is_illustration = models.BooleanField(default=False)
    billing_hold = models.BooleanField(default=False)
    free_trial_ends_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "tenants_tenant"


class SubscriptionPayment(models.Model):
    """Mirror of the console's yearly payment queue (tenants_subscriptionpayment).

    A wholesaler's submission lands PENDING and changes nothing on the tenant;
    Apex approves it in the console, which extends subscription_expires_on.
    """

    tenant_id = models.IntegerField()
    transaction_id = models.CharField(max_length=60)
    channel = models.CharField(max_length=20, default="OTHER")
    status = models.CharField(max_length=20, default="PENDING")
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    years = models.PositiveSmallIntegerField(default=1)
    paid_on = models.DateField()
    covers_until = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "tenants_subscriptionpayment"