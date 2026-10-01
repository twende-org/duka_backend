from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import User

ACCOUNT_TYPE_CAPABILITIES = {
    'merchant': {'can_manage_business': True, 'can_shop': True, 'can_buy_for_business': True},
    'staff': {'can_manage_business': False, 'can_shop': True, 'can_buy_for_business': False},
    'customer': {'can_manage_business': False, 'can_shop': True, 'can_buy_for_business': False},
    'unassigned': {'can_manage_business': False, 'can_shop': True, 'can_buy_for_business': False},
}


def find_user_by_email(email):
    """Case-insensitive lookup; falls back to username for admin-created accounts."""
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.filter(username__iexact=email).first()
    return user


def user_auth_payload(user):
    """Serializable user object shared by all auth endpoints."""
    return {
        'id': str(user.id),
        'email': user.email,
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'display_name': user.display_name,
        'phone': user.phone,
        'account_type': user.account_type,
        'default_workspace': user.default_workspace,
        # Both profile maps ride along so the session the app stores at sign-in
        # matches what ``/api/users/me/`` answers (the app only re-reads the
        # profile on reload, not after a fresh login).
        'business_profile': user.business_profile or None,
        'corporate_profile': user.corporate_profile or None,
        # Platform-admin flag (Django ``is_staff``; the legacy Firestore
        # ``admins/{uid}`` collection) gates the admin area in the app shell.
        'is_staff': bool(user.is_staff or user.is_superuser),
        'capabilities': {
            'can_manage_business': user.can_manage_business,
            'can_shop': user.can_shop,
            'can_buy_for_business': user.can_buy_for_business,
        },
        'roles': [
            {
                'id': str(role.id),
                'user_id': str(role.user_id),
                'shop_id': str(role.shop_id),
                'role': role.role,
            }
            for role in user.shop_roles.all()
        ],
    }


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(min_length=6, write_only=True)
    displayName = serializers.CharField(required=False, allow_blank=True, default='')
    phone = serializers.CharField(required=False, allow_blank=True, default='')
    accountType = serializers.ChoiceField(
        choices=list(ACCOUNT_TYPE_CAPABILITIES.keys()), required=False, default='customer'
    )

    def validate_email(self, value):
        value = value.strip().lower()
        existing = find_user_by_email(value)
        if existing is not None:
            raise serializers.ValidationError('A user with this email already exists.')
        return value

    def create(self, validated_data):
        account_type = validated_data['accountType']
        capabilities = ACCOUNT_TYPE_CAPABILITIES[account_type]
        display_name = validated_data['displayName']
        return User.objects.create_user(
            username=validated_data['email'],
            email=validated_data['email'],
            password=validated_data['password'],
            display_name=display_name,
            phone=validated_data.get('phone') or None,
            account_type=account_type,
            first_name=display_name.split(' ')[0] if display_name else '',
            **capabilities,
        )


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    """TokenObtainPairSerializer that authenticates with email + password."""

    username_field = 'email'

    def validate(self, attrs):
        email = attrs[self.username_field]
        password = attrs['password']
        user = find_user_by_email(email)
        if user is None or not user.is_active or not user.check_password(password):
            raise AuthenticationFailed(
                self.error_messages['no_active_account'], 'no_active_account'
            )
        self.user = user
        refresh = self.get_token(user)
        return {
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'is_new_user': False,
            'user': user_auth_payload(user),
        }


class UserProfileSerializer(serializers.ModelSerializer):
    is_staff = serializers.SerializerMethodField()
    # Self-service view of the legacy ``businessProfile`` map, so the customer
    # pages can read (and the wholesale/corporate flows can write) their own
    # application without going through the staff-only admin endpoint.
    businessProfile = serializers.JSONField(source='business_profile', required=False, allow_null=True)
    # Read-only self-service view of ``corporateProfile``: company membership is
    # granted by staff (admin API or Django admin), never by the member.
    corporateProfile = serializers.JSONField(source='corporate_profile', read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'email', 'display_name', 'phone', 'first_name', 'last_name',
            'account_type', 'default_workspace', 'date_joined', 'is_staff',
            'businessProfile', 'corporateProfile',
        ]
        read_only_fields = ['id', 'email', 'date_joined', 'is_staff']

    def get_is_staff(self, obj):
        return bool(obj.is_staff or obj.is_superuser)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # None (not {}) when the user never applied: the customer pages filter
        # wholesale applications on the truthiness of this field.
        data['businessProfile'] = instance.business_profile or None
        data['corporateProfile'] = instance.corporate_profile or None
        return data

    def update(self, instance, validated_data):
        # Partial merge, mirroring the legacy dot-path writes that changed only
        # ``businessProfile.status`` without dropping the rest of the map.
        profile = validated_data.get('business_profile', serializers.empty)
        if isinstance(profile, dict) and profile:
            merged = dict(instance.business_profile or {})
            merged.update(profile)
            validated_data['business_profile'] = merged
        return super().update(instance, validated_data)


class PasswordUpdateSerializer(serializers.Serializer):
    current_password = serializers.CharField(required=True)
    new_password = serializers.CharField(required=True, min_length=6)

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value
