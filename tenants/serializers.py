from rest_framework import serializers

from .models import Tenant


class TenantSerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            "id",
            "name",
            "tin",
            "phone",
            "address",
            "logo_url",
            "vat_rate",
            "module_inventory",
            "module_hr",
            "sales_role_enabled",
            "parent",
            "is_main",
            "subscription_expires_on",
            "subscription_transaction_id",
            "subscription_paid_on",
        ]

    def get_logo_url(self, obj):
        return obj.family_logo_url
