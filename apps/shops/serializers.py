from rest_framework import serializers
# pyrefly: ignore [missing-import]
from apps.core.legacy import app_id
from .models import Shop

# Merchant settings keys -> Shop legal columns. The setup wizard posts the KYC
# step as one ``businessInfo`` blob while the columns are flat on the model.
BUSINESS_INFO_FIELDS = {
    'tin': 'tin_number',
    'vat': 'vrn_number',
    'registrationNumber': 'registration_number',
    'licenseNumber': 'license_number',
}

class ShopSettingsSerializer(serializers.ModelSerializer):
    # Merchant settings were a ``shops/{id}/settings/default`` Firestore doc and
    # the pages still speak those camelCase keys, so the JSON columns are aliased
    # back to them here rather than exposing the snake_case model spellings.
    shopId = serializers.SerializerMethodField()
    categories = serializers.ListField(
        child=serializers.CharField(),
        source='productCategories',  # assuming we map it to productCategories
        required=False
    )
    onlineStore = serializers.JSONField(source='online_store_settings', required=False)
    storePolicies = serializers.JSONField(source='store_policies', required=False)
    socialLinks = serializers.JSONField(source='social_links', required=False)
    aiMarketing = serializers.JSONField(source='ai_marketing_settings', required=False)
    payoutDetails = serializers.JSONField(source='payout_details', required=False)
    # Write-only blob; to_representation assembles the read shape from the columns.
    businessInfo = serializers.DictField(required=False, write_only=True)

    class Meta:
        model = Shop
        fields = [
            'id', 'shopId', 'name', 'phone', 'whatsapp', 'description',
            'location', 'lat', 'lon', 'productCondition',
            'isWholesaleSupplier', 'categories', 'imageUrl', 'coverImage',
            'onlineStore', 'storePolicies', 'socialLinks', 'aiMarketing',
            'payoutDetails', 'businessInfo',
        ]
        read_only_fields = ['id']

    def get_shopId(self, obj):
        return app_id(obj)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['businessInfo'] = {
            key: getattr(instance, column) or ''
            for key, column in BUSINESS_INFO_FIELDS.items()
        }
        return data

    def update(self, instance, validated_data):
        business = validated_data.pop('businessInfo', None)
        if business:
            for key, column in BUSINESS_INFO_FIELDS.items():
                if key in business:
                    setattr(instance, column, business[key])
        return super().update(instance, validated_data)
