from rest_framework import serializers

from apps.core.legacy import LegacyPrimaryKeyRelatedField
from apps.intake.models import IntakeBatch, ProductDraft
from apps.shops.models import Shop


class ProductDraftSerializer(serializers.ModelSerializer):
    """One staged line item; everything the merchant may fix up is writable.

    ``batch`` itself is not editable (drafts move with their batch); the row
    freezes once the batch is applied, enforced in the view layer.
    """
    batchId = LegacyPrimaryKeyRelatedField(source='batch', queryset=IntakeBatch.objects.all())
    nameEn = serializers.CharField(source='name_en', max_length=255)
    nameSw = serializers.CharField(source='name_sw', required=False, allow_blank=True, max_length=255)
    unit = serializers.CharField(required=False, allow_blank=True, max_length=100)
    quantity = serializers.IntegerField(required=False, min_value=0)
    buyingPrice = serializers.DecimalField(source='buying_price', max_digits=12, decimal_places=2, required=False)
    sellingPrice = serializers.DecimalField(source='selling_price', max_digits=12, decimal_places=2, required=False)
    categoryName = serializers.CharField(source='category_name', required=False, allow_blank=True, max_length=255)
    aiConfidenceScore = serializers.FloatField(source='ai_confidence_score', required=False, min_value=0, max_value=1)
    traItemCode = serializers.CharField(source='tra_item_code', required=False, allow_blank=True, max_length=20)
    taxRatePercent = serializers.DecimalField(source='tax_rate_percent', max_digits=5, decimal_places=2, required=False)
    appliedProductId = serializers.PrimaryKeyRelatedField(source='applied_product', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = ProductDraft
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {'batch': {'read_only': True}}


class IntakeBatchSerializer(serializers.ModelSerializer):
    """Staging batch with its drafts inlined (the review workspace needs both)."""
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all())
    createdById = serializers.PrimaryKeyRelatedField(source='created_by', read_only=True)
    status = serializers.ChoiceField(choices=IntakeBatch.STATUS_CHOICES, read_only=True)
    sourceType = serializers.CharField(source='source_type', read_only=True)
    engineUsed = serializers.CharField(source='engine_used', read_only=True)
    sources = serializers.JSONField(read_only=True)
    sourceNote = serializers.CharField(source='source_note', required=False, allow_blank=True)
    errorMessage = serializers.CharField(source='error_message', read_only=True)
    itemCount = serializers.IntegerField(source='item_count', read_only=True)
    aiUsage = serializers.JSONField(source='ai_usage', read_only=True)
    drafts = ProductDraftSerializer(many=True, read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = IntakeBatch
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {'shop': {'required': False}}
