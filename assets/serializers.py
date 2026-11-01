from rest_framework import serializers

from .models import Asset, AssetAssignment


class AssetAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetAssignment
        fields = [
            "id",
            "assignee_name",
            "assigned_at",
            "expected_return_date",
            "is_active",
            "removed_at",
        ]


class AssetSerializer(serializers.ModelSerializer):
    assignee_name = serializers.CharField(read_only=True)
    book_value = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    assignments = AssetAssignmentSerializer(many=True, read_only=True)
    photo = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = [
            "id",
            "asset_tag",
            "name",
            "category",
            "serial_number",
            "quantity",
            "brand_model",
            "photo",
            "unit_cost",
            "condition_at_registration",
            "useful_life_years",
            "status",
            "assignee_name",
            "book_value",
            "created_at",
            "assignments",
        ]

    def get_photo(self, obj):
        if not obj.photo:
            return None
        name = obj.photo.name
        if name.startswith(("http://", "https://")):
            return name
        url = obj.photo.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url


class AssetInputSerializer(serializers.ModelSerializer):
    """Manager registration / edit input with the enums validated.

    status is deliberately absent: assets only move through the Finance
    assign/remove endpoints, never a direct write.
    """

    photo = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Asset
        fields = [
            "asset_tag",
            "name",
            "category",
            "serial_number",
            "quantity",
            "brand_model",
            "photo",
            "unit_cost",
            "condition_at_registration",
            "useful_life_years",
        ]
        extra_kwargs = {"asset_tag": {"required": False}}

    def validate_category(self, value):
        valid = [code for code, _ in Asset.CATEGORIES]
        if value not in valid:
            raise serializers.ValidationError(
                f"Choose one of: {', '.join(valid)}."
            )
        return value

    def validate_condition_at_registration(self, value):
        valid = [code for code, _ in Asset.CONDITIONS]
        if value not in valid:
            raise serializers.ValidationError(
                f"Choose one of: {', '.join(valid)}."
            )
        return value
