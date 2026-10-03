from rest_framework import serializers
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch, UserRole, Invitation
from django.contrib.auth import get_user_model
# pyrefly: ignore [missing-import]
from apps.core.legacy import LegacyPrimaryKeyRelatedField, app_id, resolve_legacy_pk

User = get_user_model()

class BranchSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source='branch_type', required=False, allow_blank=True)
    isActive = serializers.BooleanField(source='is_active', required=False, default=True)
    operatingHours = serializers.CharField(source='operating_hours', required=False, allow_blank=True, allow_null=True)
    managerId = LegacyPrimaryKeyRelatedField(
        source='manager', queryset=User.objects.all(), required=False, allow_null=True
    )
    managerName = serializers.SerializerMethodField()
    # Accepts/answers the app-visible shop id (legacy Firestore id or uuid), so
    # branches keep joining to shops by the id the merchant dashboard holds.
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all())
    shop = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Branch
        fields = '__all__'
        read_only_fields = ('shop',)

    def get_managerName(self, obj):
        if obj.manager:
            return obj.manager.get_full_name() or obj.manager.username
        return None

class ShopSerializer(serializers.ModelSerializer):
    branches = BranchSerializer(many=True, read_only=True)
    ownerId = serializers.SerializerMethodField()
    categories = serializers.JSONField(source='productCategories', required=False)
    whatsappNumber = serializers.CharField(source='whatsapp', required=False, allow_blank=True)
    legacyId = serializers.CharField(source='legacy_id', read_only=True)
    # Read-only: follower_count only moves through the public follow/unfollow
    # actions, mirroring the Firestore rule that allowed just this key to change.
    followerCount = serializers.IntegerField(source='follower_count', read_only=True)
    # Platform admin's per-shop AI intake allowance override (null = global
    # default); non-staff writes are stripped in the viewset.
    aiIntakeMonthlyLimit = serializers.IntegerField(
        source='ai_intake_monthly_limit', required=False, allow_null=True,
        min_value=0, max_value=32767,
    )
    # The merchant dashboard shows this stat; it used to be a Firestore counter
    # field, now derived from the real product rows on read.
    productCount = serializers.SerializerMethodField()
    # Same story for the admin list's sales column: the Firestore shop doc kept
    # an all-time counter, recomputed here from the daily summary rows.
    salesTotal = serializers.SerializerMethodField()

    class Meta:
        model = Shop
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')

    def get_ownerId(self, obj):
        owner_role = obj.user_roles.filter(role='owner').first()
        return app_id(owner_role.user) if owner_role else None

    def get_productCount(self, obj):
        return obj.products.count()

    def get_salesTotal(self, obj):
        total = getattr(obj, 'sales_total', None)
        return float(total) if total is not None else 0.0

class UserRoleSerializer(serializers.ModelSerializer):
    # Read ids mirror back the app-visible id (legacy Firestore id when the row
    # came from Firestore) so admin pages keep joining roles by the ids they hold.
    userId = serializers.SerializerMethodField()
    shopId = serializers.SerializerMethodField()
    email = serializers.EmailField(source='user.email', read_only=True)
    displayName = serializers.SerializerMethodField()
    # Write-only: role assignment posts the member as userId or email.
    user = serializers.CharField(write_only=True, required=False)
    
    class Meta:
        model = UserRole
        fields = ('id', 'userId', 'shopId', 'role', 'email', 'displayName', 'user')
        read_only_fields = ('id', 'userId', 'shopId')

    def get_userId(self, obj):
        return app_id(obj.user)

    def get_shopId(self, obj):
        return app_id(obj.shop)

    def get_displayName(self, obj):
        name = obj.user.get_full_name()
        return name if name else obj.user.username

class InvitationSerializer(serializers.ModelSerializer):
    # ``shopId`` is the app's spelling; it writes the FK and, being declared,
    # DRF would normally require it. The viewset looks the shop up itself
    # (permission check), so the field is optional here.
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    invitedBy = serializers.SerializerMethodField()
    shopName = serializers.CharField(source='shop.name', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Invitation
        fields = ('id', 'email', 'role', 'status', 'shopId', 'invitedBy', 'shopName',
                  'createdAt', 'updatedAt', 'created_at', 'updated_at')
        read_only_fields = ('id', 'status', 'invitedBy', 'shopName',
                            'createdAt', 'updatedAt', 'created_at', 'updated_at')
        # Re-invites are handled by the upsert_invitation service, so the
        # auto-generated (email, shop) UniqueTogetherValidator is disabled.
        validators = []

    def get_invitedBy(self, obj):
        return app_id(obj.invited_by) if obj.invited_by else None

    def validate_email(self, value):
        # Firebase's sendInvitation() lowercased/trimmed the address, which is what
        # makes the invitee lookup (and accept matching) case-insensitive.
        return value.lower().strip()
