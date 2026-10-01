from rest_framework import serializers
from apps.core.legacy import LegacyPrimaryKeyRelatedField
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.social.models import SocialIntegration, Conversation, Message, SocialLog

class SocialIntegrationSerializer(serializers.ModelSerializer):
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    pageId = serializers.CharField(source='page_id', required=False, allow_null=True, allow_blank=True)
    pageName = serializers.CharField(source='page_name', required=False, allow_null=True, allow_blank=True)
    instagramId = serializers.CharField(source='instagram_id', required=False, allow_null=True, allow_blank=True)
    autoReplyEnabled = serializers.BooleanField(source='auto_reply_enabled', required=False)
    isConnected = serializers.BooleanField(source='is_connected', read_only=True)
    connectedBy = serializers.CharField(source='connected_by', read_only=True)
    syncStatus = serializers.CharField(source='sync_status', read_only=True)
    lastSyncAt = serializers.DateTimeField(source='last_sync_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = SocialIntegration
        fields = '__all__'
        read_only_fields = (
            'id', 'created_at', 'updated_at', 'is_connected', 'access_token',
            'refresh_token', 'last_sync_at', 'sync_status',
        )
        extra_kwargs = {'shop': {'required': False}}
        # ``shopId`` and the auto-generated ``shop`` field share a source, which
        # breaks DRF's unique-together validator; (shop, platform) is enforced
        # in validate() instead.
        validators = []

    def validate(self, attrs):
        shop = attrs.get('shop', getattr(self.instance, 'shop', None))
        platform = attrs.get('platform', getattr(self.instance, 'platform', None))
        if self.instance is None and shop is None:
            raise serializers.ValidationError({'shopId': 'This field is required.'})
        if shop and platform:
            clash = SocialIntegration.objects.filter(shop=shop, platform=platform)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {'platform': 'This integration already exists for this shop.'})
        return attrs

class MessageSerializer(serializers.ModelSerializer):
    conversationId = serializers.CharField(source='conversation_id', read_only=True)
    senderType = serializers.CharField(source='sender_type', required=False)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Message
        fields = '__all__'
        read_only_fields = ('conversation', 'created_at')

class ConversationSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)
    shopId = LegacyPrimaryKeyRelatedField(source='shop', queryset=Shop.objects.all(), required=False)
    customerName = serializers.CharField(source='customer_name', required=False)
    lastMessageAt = serializers.DateTimeField(source='last_message_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Conversation
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at', 'last_message_at')
        extra_kwargs = {'shop': {'required': False}}
        validators = []

    def validate(self, attrs):
        if self.instance is None and 'shop' not in attrs:
            raise serializers.ValidationError({'shopId': 'This field is required.'})
        return attrs


class SocialLogSerializer(serializers.ModelSerializer):
    """Read-only outcome log of a social post / AI reply.

    Rows are written by the posting tasks, never by clients, so every field
    here is read-only and the payload shape matches the legacy
    ``social_logs`` documents the dashboard already renders.
    """

    productIds = serializers.ListField(source='product_ids', read_only=True)
    facebookPostId = serializers.CharField(source='facebook_post_id', read_only=True)
    instagramPostId = serializers.CharField(source='instagram_post_id', read_only=True)
    videoFallbackReason = serializers.CharField(source='video_fallback_reason', read_only=True)
    instagramError = serializers.CharField(source='instagram_error', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = SocialLog
        # Explicit list: every field is read-only, so no snake_case aliases are
        # needed and the shop is addressed by the query filter, not the payload.
        fields = (
            'id', 'type', 'action', 'status', 'facebookPostId', 'instagramPostId',
            'error', 'productIds', 'videoFallbackReason', 'instagramError', 'createdAt',
        )
