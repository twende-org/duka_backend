from rest_framework import serializers
from rest_framework.validators import UniqueTogetherValidator
# pyrefly: ignore [missing-import]
from apps.core.legacy import LegacyPrimaryKeyRelatedField
# pyrefly: ignore [missing-import]
from apps.marketing.models import DiscountCode, Campaign
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop


class DiscountCodeSerializer(serializers.ModelSerializer):
    """camelCase aliases accept the dashboard payload; snake_case keys stay writable for internal callers."""

    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    usedCount = serializers.IntegerField(source='used_count', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = DiscountCode
        fields = '__all__'
        read_only_fields = ['used_count', 'created_at', 'updated_at']
        extra_kwargs = {'shop': {'required': False}}
        #: DRF cannot build the auto validator because ``shopId`` and ``shop``
        #: share a source; the same constraint is declared explicitly instead.
        validators = [
            UniqueTogetherValidator(
                queryset=DiscountCode.objects.all(),
                fields=['shopId', 'code'],
                message='This discount code already exists for the shop.',
            )
        ]

    def validate(self, attrs):
        if self.instance is None and attrs.get('shop') is None:
            raise serializers.ValidationError({'shopId': 'This field is required.'})
        return attrs


class CampaignSerializer(serializers.ModelSerializer):
    """camelCase aliases accept the dashboard payload; snake_case keys stay writable for internal callers."""

    promo_code_detail = DiscountCodeSerializer(source='promo_code', read_only=True)
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    promoCodeId = LegacyPrimaryKeyRelatedField(source='promo_code', queryset=DiscountCode.objects.all(), required=False, allow_null=True)
    audienceFilter = serializers.CharField(source='audience_filter', required=False, allow_null=True, allow_blank=True)
    startDate = serializers.DateTimeField(source='start_date', required=False, allow_null=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Campaign
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']
        extra_kwargs = {'shop': {'required': False}}

    def validate(self, attrs):
        shop = attrs.get('shop') or (self.instance.shop if self.instance else None)
        if self.instance is None and shop is None:
            raise serializers.ValidationError({'shopId': 'This field is required.'})

        promo_code = attrs.get('promo_code', self.instance.promo_code if self.instance else None)
        if shop and promo_code and promo_code.shop_id != shop.id:
            raise serializers.ValidationError({'promoCodeId': 'Discount code does not belong to this shop.'})
        return attrs
