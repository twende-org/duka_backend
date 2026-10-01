from rest_framework import serializers

# pyrefly: ignore [missing-import]
from apps.core.models import Announcement, SupportTicket


class AnnouncementSerializer(serializers.ModelSerializer):
    """camelCase aliases keep the legacy document shape for the admin page;
    snake_case keys stay writable for internal callers."""

    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)
    createdBy = serializers.CharField(source='created_by', required=False, allow_blank=True)
    createdByEmail = serializers.CharField(source='created_by_email', required=False, allow_blank=True)

    class Meta:
        model = Announcement
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class SupportTicketSerializer(serializers.ModelSerializer):
    """The submitter's identity is taken from the request user server-side, so
    only the ticket body is writable; ``resolved`` can be patched by admins."""

    userId = serializers.CharField(source='user_id', read_only=True)
    userEmail = serializers.CharField(source='user_email', read_only=True)
    userName = serializers.CharField(source='user_name', read_only=True)
    shopId = serializers.CharField(source='shop_id', required=False, allow_blank=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = SupportTicket
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        extra_kwargs = {'user': {'read_only': True}}

    def validate_message(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Message cannot be empty.')
        return value
