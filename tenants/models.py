from decimal import Decimal

from django.db import models


class Tenant(models.Model):
    MODULE_CHOICES = (("INVENTORY", "Inventory"), ("HR", "HR"))

    name = models.CharField(max_length=255)
    tin = models.CharField(max_length=50, unique=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.CharField(max_length=255, blank=True)
    logo_url = models.URLField(blank=True)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("15.00"))
    module_inventory = models.BooleanField(default=True)
    module_hr = models.BooleanField(default=True)
    sales_role_enabled = models.BooleanField(default=False)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="branches"
    )
    is_main = models.BooleanField(default=False)
    subscription_expires_on = models.DateField(null=True, blank=True)
    subscription_transaction_id = models.CharField(max_length=60, blank=True)
    subscription_paid_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_branch(self):
        return self.parent_id is not None
