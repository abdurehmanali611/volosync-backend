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

    @property
    def family_logo_url(self):
        """One shared logo per family: the main branch's, else any member's.

        Every satellite resolves the company logo so its top bar, sidebar,
        receipts and PDF prints match the main branch even when the branch row
        itself never stored a logo.
        """
        root = self
        seen = {self.pk}
        while root.parent_id and root.parent_id not in seen:
            parent = Tenant.objects.filter(pk=root.parent_id).first()
            if parent is None:
                break
            seen.add(parent.pk)
            root = parent
        members = [root, *root.branches.all()]
        main = next((member for member in members if member.is_main), None)
        if main and main.logo_url:
            return main.logo_url
        if self.logo_url:
            return self.logo_url
        return next((member.logo_url for member in members if member.logo_url), "")
