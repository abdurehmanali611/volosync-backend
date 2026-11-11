from decimal import Decimal

from rest_framework import serializers

from .models import (
    Item,
    PromotionLine,
    PromotionRequest,
    RegistrationLine,
    RegistrationRequest,
    StockAssignment,
    StockLot,
    StockMovement,
    StockOutRequest,
)


class CloudinaryImageField(serializers.ImageField):
    """Accept a Cloudinary URL on write and return it untouched on read."""

    def to_internal_value(self, data):
        if isinstance(data, str) and data.startswith(("http://", "https://")):
            return data
        return super().to_internal_value(data)

    def to_representation(self, value):
        if not value:
            return None
        name = value.name if hasattr(value, "name") else str(value)
        if name.startswith(("http://", "https://")):
            return name
        url = value.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url


class ItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ["id", "name", "category", "unit", "note"]


class StockLotSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="item.name", read_only=True)
    category = serializers.CharField(source="item.category", read_only=True)
    unit = serializers.CharField(source="item.unit", read_only=True)
    price_with_vat = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    paid_amount = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    stocked_out_quantity = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)
    image = serializers.SerializerMethodField()

    class Meta:
        model = StockLot
        fields = [
            "id",
            "voucher_number",
            "item",
            "item_name",
            "category",
            "unit",
            "quantity",
            "remaining_quantity",
            "stocked_out_quantity",
            "unit_cost",
            "unit_sales",
            "vat_rate",
            "price_with_vat",
            "paid_amount",
            "registration_date",
            "expiry_date",
            "supplier_name",
            "supplier_phone",
            "supplier_address",
            "supplier_tin",
            "note",
            "image",
        ]

    def get_image(self, obj):
        if not obj.image:
            return None
        name = obj.image.name
        if name.startswith(("http://", "https://")):
            return name
        url = obj.image.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url


class RegistrationLineSerializer(serializers.ModelSerializer):
    price_with_vat = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    paid_amount = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    image = CloudinaryImageField(required=False, allow_null=True)

    class Meta:
        model = RegistrationLine
        fields = [
            "id",
            "item_name",
            "category",
            "unit",
            "quantity",
            "unit_cost",
            "unit_sales",
            "vat_rate",
            "price_with_vat",
            "paid_amount",
            "registration_date",
            "expiry_date",
            "note",
            "image",
        ]
        extra_kwargs = {"vat_rate": {"required": False}, "image": {"required": False}}

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        return value


class RegistrationSerializer(serializers.ModelSerializer):
    lines = RegistrationLineSerializer(many=True)
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True)
    total_value = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)

    class Meta:
        model = RegistrationRequest
        fields = [
            "id",
            "voucher_number",
            "status",
            "supplier_name",
            "supplier_phone",
            "supplier_address",
            "supplier_tin",
            "note",
            "created_at",
            "created_by_name",
            "total_value",
            "lines",
        ]
        read_only_fields = ["voucher_number", "status", "created_at"]

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("At least one line is required.")
        return lines


class StockAssignmentSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="lot.item.name", read_only=True)
    voucher_number = serializers.CharField(source="lot.voucher_number", read_only=True)
    agent_name = serializers.CharField(source="sales_agent.full_name", read_only=True)

    class Meta:
        model = StockAssignment
        fields = [
            "id",
            "lot",
            "voucher_number",
            "item_name",
            "sales_agent",
            "agent_name",
            "quantity",
            "changing_amount",
            "change_reason",
            "unit_sales_at_assignment",
        ]
        read_only_fields = ["unit_sales_at_assignment"]
        extra_kwargs = {
            "sales_agent": {"required": True},
        }

    def validate(self, attrs):
        lot = attrs.get("lot")
        quantity = attrs.get("quantity") or Decimal("0")
        changing = attrs.get("changing_amount") or Decimal("0")
        reason = attrs.get("change_reason", "")
        sales_agent = attrs.get("sales_agent")

        if quantity <= 0:
            raise serializers.ValidationError({"quantity": "Quantity must be greater than zero."})
        if lot and quantity > lot.remaining_quantity:
            raise serializers.ValidationError(
                {"quantity": f"{lot.voucher_number} only has {lot.remaining_quantity} available."}
            )
        if changing != 0 and not reason.strip():
            raise serializers.ValidationError(
                {"change_reason": "A reason is required when the changing amount is not zero."}
            )
        if lot and lot.unit_sales + changing <= 0:
            raise serializers.ValidationError(
                {"changing_amount": "The effective price must stay above zero."}
            )
        if not sales_agent:
            raise serializers.ValidationError({"sales_agent": "A sales agent must be selected."})
        if sales_agent.tenant != lot.tenant:
            raise serializers.ValidationError({"sales_agent": "Sales agent does not belong to this tenant."})
        if not sales_agent.is_active:
            raise serializers.ValidationError({"sales_agent": "Selected sales agent is not active."})
        return attrs


class StockOutSerializer(serializers.ModelSerializer):
    assignments = StockAssignmentSerializer(many=True)
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True)

    class Meta:
        model = StockOutRequest
        fields = [
            "id",
            "voucher_number",
            "mode",
            "date",
            "status",
            "note",
            "created_at",
            "created_by_name",
            "assignments",
        ]
        read_only_fields = ["voucher_number", "status", "created_at"]

    def validate_assignments(self, assignments):
        if not assignments:
            raise serializers.ValidationError("At least one assignment is required.")
        return assignments


class MovementSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="lot.item.name", read_only=True)
    voucher_number = serializers.CharField(source="lot.voucher_number", read_only=True)
    actor_name = serializers.CharField(source="actor.username", read_only=True)

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "type",
            "lot",
            "voucher_number",
            "item_name",
            "quantity_delta",
            "actor_name",
            "role",
            "ref_type",
            "ref_id",
            "note",
            "at",
        ]


class LotEditSerializer(serializers.ModelSerializer):
    """Manager-only inventory edit (spec §4.2a / R5).

    Quantity is the remaining stock and is simply replaced, like renaming.
    unit_sales and unit_cost are editable here; price_with_vat and paid_amount
    re-derive from them (paid_amount always multiplies the REGISTERED quantity,
    per R4). voucher_number is immutable, no approval chain, and no
    StockMovement rows are written for edits (A3).
    """

    item_name = serializers.CharField(source="item.name", required=False)
    category = serializers.CharField(source="item.category", required=False)
    unit = serializers.CharField(source="item.unit", required=False)
    image = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = StockLot
        fields = [
            "id",
            "voucher_number",
            "item_name",
            "category",
            "unit",
            "remaining_quantity",
            "unit_sales",
            "unit_cost",
            "expiry_date",
            "note",
            "image",
            "supplier_name",
            "supplier_phone",
            "supplier_address",
            "supplier_tin",
        ]
        read_only_fields = ["voucher_number"]

    def validate_remaining_quantity(self, value):
        if value < 0:
            raise serializers.ValidationError("Quantity cannot be negative.")
        return value

    def validate_unit_sales(self, value):
        if value <= 0:
            raise serializers.ValidationError("Unit sales must be greater than zero.")
        return value

    def validate_unit_cost(self, value):
        if value < 0:
            raise serializers.ValidationError("Unit cost cannot be negative.")
        return value

    def update(self, instance, validated_data):
        item_data = validated_data.pop("item", {})
        for field, value in item_data.items():
            setattr(instance.item, field, value)
        if item_data:
            instance.item.save(update_fields=list(item_data.keys()))
        return super().update(instance, validated_data)


class PromotionLineSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source="lot.item.name", read_only=True)
    voucher_number = serializers.CharField(source="lot.voucher_number", read_only=True)
    unit_sales = serializers.DecimalField(
        source="lot.unit_sales", read_only=True, max_digits=18, decimal_places=2
    )
    manufacturer = serializers.CharField(source="lot.supplier_name", read_only=True)
    remaining = serializers.DecimalField(
        source="lot.remaining_quantity", read_only=True, max_digits=14, decimal_places=3
    )
    printed_price = serializers.SerializerMethodField()
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = PromotionLine
        fields = [
            "id",
            "lot",
            "item_name",
            "voucher_number",
            "unit_sales",
            "manufacturer",
            "remaining",
            "quantity",
            "changing_amount",
            "change_reason",
            "printed_price",
            "line_total",
        ]

    def get_printed_price(self, obj):
        return str((obj.lot.unit_sales + obj.changing_amount).quantize(Decimal("0.01")))

    def get_line_total(self, obj):
        total = (obj.lot.unit_sales + obj.changing_amount) * obj.quantity
        return str(total.quantize(Decimal("0.01")))


class PromotionSerializer(serializers.ModelSerializer):
    lines = PromotionLineSerializer(many=True)
    created_by_name = serializers.CharField(
        source="created_by.get_full_name", read_only=True
    )
    manufacturers = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    class Meta:
        model = PromotionRequest
        fields = [
            "id",
            "voucher_number",
            "mode",
            "date",
            "status",
            "note",
            "created_at",
            "created_by_name",
            "manufacturers",
            "total",
            "lines",
        ]
        read_only_fields = ["voucher_number", "status", "created_at"]

    def get_manufacturers(self, obj):
        names = sorted(
            {line.lot.supplier_name for line in obj.lines.all() if line.lot.supplier_name}
        )
        return names

    def get_total(self, obj):
        total = Decimal("0")
        for line in obj.lines.all():
            total += (line.lot.unit_sales + line.changing_amount) * line.quantity
        return str(total.quantize(Decimal("0.01")))

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("At least one promotion line is required.")
        return lines
