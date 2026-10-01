"""Corporate procurement serializers (departments, buyers, purchase orders).

``companyId`` is always read-only: it comes from the caller's ``corporateProfile``
server-side, never from the request body, so a member cannot post into another
company. Decimal money fields answer as strings (DRF's
``COERCE_DECIMAL_TO_STRING``); the frontend corporate adapter coerces them back
to numbers the same way the other domains do.
"""
from rest_framework import serializers

# pyrefly: ignore [missing-import]
from apps.corporate.models import CorporateDepartment, CorporatePurchaseOrder
# pyrefly: ignore [missing-import]
from apps.users.models import User
# pyrefly: ignore [missing-import]
from api.v1.serializers.users import app_visible_user_id


class CorporateDepartmentSerializer(serializers.ModelSerializer):
    """Company department (legacy ``companies/{id}/departments`` documents)."""

    companyId = serializers.CharField(source='company_id', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = CorporateDepartment
        fields = ('id', 'companyId', 'name', 'budget', 'spent', 'createdAt', 'updatedAt')
        read_only_fields = ('id', 'spent', 'createdAt', 'updatedAt')


class CorporateBuyerSerializer(serializers.ModelSerializer):
    """A colleague in the same company (legacy ``users`` where companyId matches)."""

    id = serializers.SerializerMethodField()
    displayName = serializers.CharField(source='display_name', read_only=True)
    corporateProfile = serializers.JSONField(source='corporate_profile', read_only=True)

    class Meta:
        model = User
        fields = ('id', 'email', 'displayName', 'phone', 'corporateProfile')

    def get_id(self, obj):
        return app_visible_user_id(obj)


class CorporatePurchaseOrderSerializer(serializers.ModelSerializer):
    """Company purchase order (legacy ``companies/{id}/purchase_orders``).

    ``approvalStatus``/``approverId``/``approvedAt`` are stamped by the approve
    and reject actions, mirroring the legacy writes that only the approver UI did.
    """

    companyId = serializers.CharField(source='company_id', read_only=True)
    shopId = serializers.CharField(source='shop_id', required=False, allow_blank=True)
    shopName = serializers.CharField(source='shop_name', required=False, allow_blank=True)
    buyerId = serializers.CharField(source='buyer_id', required=False, allow_blank=True)
    buyerName = serializers.CharField(source='buyer_name', required=False, allow_blank=True)
    buyerPhone = serializers.CharField(source='buyer_phone', required=False, allow_blank=True)
    departmentId = serializers.CharField(source='department_id', required=False, allow_blank=True)
    departmentName = serializers.CharField(source='department_name', required=False, allow_blank=True)
    totalAmount = serializers.DecimalField(
        source='total_amount', max_digits=14, decimal_places=2, required=False
    )
    approvalStatus = serializers.CharField(source='approval_status', read_only=True)
    approverId = serializers.CharField(source='approver_id', read_only=True)
    approvedAt = serializers.DateTimeField(source='approved_at', read_only=True)
    rejectedAt = serializers.DateTimeField(source='rejected_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = CorporatePurchaseOrder
        fields = (
            'id', 'companyId', 'shopId', 'shopName', 'buyerId', 'buyerName', 'buyerPhone',
            'departmentId', 'departmentName', 'items', 'totalAmount',
            'approvalStatus', 'approverId', 'approvedAt', 'rejectedAt', 'createdAt', 'updatedAt',
        )
        read_only_fields = ('id', 'createdAt', 'updatedAt')
