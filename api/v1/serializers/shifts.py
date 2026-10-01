from decimal import Decimal

from rest_framework import serializers

# pyrefly: ignore [missing-import]
from apps.core.legacy import LegacyPrimaryKeyRelatedField
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.sales.models import Shift


class ShiftSerializer(serializers.ModelSerializer):
    """camelCase aliases accept the frontend payload; closed-shift fields are response-only."""

    # Shops imported from Firestore are still addressed by their legacy id in the app.
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    openedBy = LegacyPrimaryKeyRelatedField(source='opened_by', read_only=True)
    openedByName = serializers.CharField(source='opened_by_name', required=False, allow_blank=True, allow_null=True)
    openedAt = serializers.DateTimeField(source='opened_at', required=False)
    openingCash = serializers.DecimalField(source='opening_cash', max_digits=12, decimal_places=2, required=False)
    closedBy = LegacyPrimaryKeyRelatedField(source='closed_by', read_only=True)
    closedByName = serializers.CharField(source='closed_by_name', read_only=True)
    closedAt = serializers.DateTimeField(source='closed_at', read_only=True)
    cashSalesTotal = serializers.DecimalField(source='cash_sales_total', max_digits=15, decimal_places=2, read_only=True)
    cashExpensesTotal = serializers.DecimalField(source='cash_expenses_total', max_digits=15, decimal_places=2, read_only=True)
    expectedClosingCash = serializers.DecimalField(source='expected_closing_cash', max_digits=15, decimal_places=2, read_only=True)
    actualClosingCash = serializers.DecimalField(source='actual_closing_cash', max_digits=15, decimal_places=2, read_only=True)
    cashLeftForNextDay = serializers.DecimalField(source='cash_left_for_next_day', max_digits=15, decimal_places=2, read_only=True)
    cashSubmittedToOwner = serializers.DecimalField(source='cash_submitted_to_owner', max_digits=15, decimal_places=2, read_only=True)
    discrepancy = serializers.DecimalField(max_digits=15, decimal_places=2, read_only=True)
    ownerApprovalStatus = serializers.CharField(source='owner_approval_status', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Shift
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at', 'status')
        extra_kwargs = {'shop': {'required': False}}

    def validate(self, attrs):
        if self.instance is None and 'shop' not in attrs:
            raise serializers.ValidationError({'shopId': 'This field is required.'})
        return attrs


class CloseShiftInputSerializer(serializers.Serializer):
    """Mirrors the closingData payload sent by the Sales page."""

    actualClosingCash = serializers.DecimalField(source='actual_closing_cash', max_digits=15, decimal_places=2)
    cashLeftForNextDay = serializers.DecimalField(source='cash_left_for_next_day', max_digits=15, decimal_places=2, required=False, default=Decimal('0.00'))
    cashSubmittedToOwner = serializers.DecimalField(source='cash_submitted_to_owner', max_digits=15, decimal_places=2, required=False, default=Decimal('0.00'))
    closedByName = serializers.CharField(source='closed_by_name', required=False, allow_blank=True, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, allow_null=True)
