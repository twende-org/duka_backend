from decimal import Decimal

from rest_framework import serializers

# pyrefly: ignore [missing-import]
from apps.core.legacy import LegacyPrimaryKeyRelatedField
# pyrefly: ignore [missing-import]
from apps.expenses.models import Expense
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch
# pyrefly: ignore [missing-import]
from apps.sales.models import Shift


class ExpenseSerializer(serializers.ModelSerializer):
    """camelCase aliases accept the frontend payload; snake_case keys stay writable for internal callers."""

    # Shops/branches imported from Firestore are still addressed by legacy id in the app.
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    branchId = LegacyPrimaryKeyRelatedField(source='branch', queryset=Branch.objects.all(), required=False, allow_null=True)
    #: Shifts are Django-native (no Firestore id), so only uuids resolve here.
    shiftId = serializers.PrimaryKeyRelatedField(source='shift', queryset=Shift.objects.all(), required=False, allow_null=True)
    paymentMethod = serializers.CharField(source='payment_method', required=False, max_length=50)
    paidTo = serializers.CharField(source='paid_to', required=False, allow_blank=True, allow_null=True)
    isRecurring = serializers.BooleanField(source='is_recurring', required=False)
    recordedBy = LegacyPrimaryKeyRelatedField(source='recorded_by', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))

    class Meta:
        model = Expense
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {'shop': {'required': False}}

    def validate(self, attrs):
        if self.instance is None and 'shop' not in attrs:
            raise serializers.ValidationError({'shopId': 'This field is required.'})
        return attrs
