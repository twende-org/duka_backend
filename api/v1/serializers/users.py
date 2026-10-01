from rest_framework import serializers

# pyrefly: ignore [missing-import]
from apps.core.legacy import is_uuid
# pyrefly: ignore [missing-import]
from apps.users.models import CustomerAddress, Subscription, User, WishlistItem


def app_visible_user_id(user) -> str:
    """The id the app knows the user by: Firestore uid when imported, else uuid.

    Legacy admin pages join shops to people by the Firebase uid (``users/{uid}``
    document id), so any payload that gets matched against Firestore-era data
    must use this instead of the Django pk.
    """
    return user.firebase_uid or str(user.pk)


def resolve_app_user(value):
    """Look a user up by Django uuid or by Firebase uid; None when not found."""
    if value in (None, ''):
        return None
    value = str(value)
    if is_uuid(value):
        user = User.objects.filter(pk=value).first()
        if user is not None:
            return user
    return User.objects.filter(firebase_uid=value).first()


class AdminUserSerializer(serializers.ModelSerializer):
    """Staff-only view of a platform user (legacy ``users/{uid}`` documents).

    Only three admin toggles are writable: ``isSuspended``, ``businessProfile``
    (wholesale application decisions) and ``corporateProfile`` (company
    membership). The last two merge partial maps in ``update``, mirroring the
    legacy dot-path writes that changed a single key (e.g.
    ``businessProfile.status``).
    """

    id = serializers.SerializerMethodField()
    displayName = serializers.CharField(source='display_name', read_only=True)
    accountType = serializers.CharField(source='account_type', read_only=True)
    isStaff = serializers.SerializerMethodField()
    isSuspended = serializers.BooleanField(source='is_suspended', required=False)
    # None (not {}) when the user never applied, because the admin page filters
    # wholesale applications on the truthiness of this field.
    businessProfile = serializers.JSONField(source='business_profile', required=False, allow_null=True)
    # Corporate membership is staff-assigned: ``GET`` shows the map, ``PATCH``
    # merges the given keys (same partial-write semantics as businessProfile).
    corporateProfile = serializers.JSONField(source='corporate_profile', required=False, allow_null=True)
    # The legacy ``createdAt``; Django users are timestamped by ``date_joined``.
    createdAt = serializers.DateTimeField(source='date_joined', read_only=True)

    class Meta:
        model = User
        fields = (
            'id', 'email', 'displayName', 'phone', 'accountType',
            'isStaff', 'isSuspended', 'businessProfile', 'corporateProfile', 'createdAt',
        )

    def get_id(self, obj):
        return app_visible_user_id(obj)

    def get_isStaff(self, obj):
        return bool(obj.is_staff or obj.is_superuser)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['businessProfile'] = instance.business_profile or None
        data['corporateProfile'] = instance.corporate_profile or None
        return data

    def update(self, instance, validated_data):
        for field in ('business_profile', 'corporate_profile'):
            profile = validated_data.get(field, serializers.empty)
            if isinstance(profile, dict) and profile:
                merged = dict(getattr(instance, field) or {})
                merged.update(profile)
                validated_data[field] = merged
        return super().update(instance, validated_data)


class SubscriptionSerializer(serializers.ModelSerializer):
    """One row per user; ``id`` and ``userId`` are both the app-visible user id
    (the legacy Firestore document id was the user id)."""

    id = serializers.SerializerMethodField()
    userId = serializers.SerializerMethodField()
    userEmail = serializers.CharField(source='user_email', required=False, allow_blank=True)
    userName = serializers.CharField(source='user_name', required=False, allow_blank=True)
    startDate = serializers.DateField(source='start_date', required=False, allow_null=True)
    endDate = serializers.DateField(source='end_date', required=False, allow_null=True)
    paymentMethod = serializers.CharField(source='payment_method', required=False, allow_blank=True)
    paymentReference = serializers.CharField(source='payment_reference', required=False, allow_blank=True)
    confirmedBy = serializers.CharField(source='confirmed_by', read_only=True)
    confirmedAt = serializers.DateTimeField(source='confirmed_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Subscription
        fields = (
            'id', 'userId', 'userEmail', 'userName', 'plan', 'status',
            'startDate', 'endDate', 'paymentMethod', 'paymentReference',
            'amount', 'confirmedBy', 'confirmedAt', 'createdAt', 'updatedAt',
        )

    def get_id(self, obj):
        return app_visible_user_id(obj.user)

    def get_userId(self, obj):
        return app_visible_user_id(obj.user)


class WishlistItemSerializer(serializers.ModelSerializer):
    """Saved product; ``productId`` is the app-visible product id."""

    productId = serializers.CharField(source='product_id')
    shopId = serializers.CharField(source='shop_id', required=False, allow_blank=True)
    shopName = serializers.CharField(source='shop_name', required=False, allow_blank=True)
    wholesalePrice = serializers.DecimalField(
        source='wholesale_price', max_digits=14, decimal_places=2,
        required=False, allow_null=True,
    )
    addedAt = serializers.DateTimeField(source='created_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = WishlistItem
        fields = (
            'id', 'productId', 'name', 'price', 'shopId', 'shopName',
            'wholesalePrice', 'moq', 'addedAt', 'createdAt', 'updatedAt',
        )
        read_only_fields = ('id', 'createdAt', 'updatedAt')
        # ``productId`` carries the requirement; the auto-generated ``product_id``
        # field exists only so snake_case callers stay accepted.
        extra_kwargs = {'product_id': {'required': False}}


class AddressSerializer(serializers.ModelSerializer):
    """Saved delivery address (legacy ``users/{uid}/addresses`` documents)."""

    isDefault = serializers.BooleanField(source='is_default', required=False)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = CustomerAddress
        fields = (
            'id', 'tag', 'name', 'phone', 'street', 'city',
            'isDefault', 'createdAt', 'updatedAt',
        )
        read_only_fields = ('id', 'createdAt', 'updatedAt')
