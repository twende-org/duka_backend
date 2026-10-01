from rest_framework import serializers

# pyrefly: ignore [missing-import]
from apps.telemetry.models import ActivityLog, AnalyticsEvent, ErrorEvent

# Legacy clients send ``null`` for optional telemetry values (the Firestore SDK
# stored whatever it was handed). The columns here are non-nullable, so these
# two field types coerce a null into the empty value instead of rejecting the
# write with a 400 -- telemetry must never fail the caller.


class NullToBlankCharField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault('required', False)
        kwargs.setdefault('allow_blank', True)
        super().__init__(**kwargs)

    def run_validation(self, data=serializers.empty):
        if data is None or data is serializers.empty:
            return ''
        return super().run_validation(data)


class NullToEmptyJSONField(serializers.JSONField):
    def __init__(self, **kwargs):
        kwargs.setdefault('required', False)
        super().__init__(**kwargs)

    def run_validation(self, data=serializers.empty):
        if data is None or data is serializers.empty:
            return {}
        return super().run_validation(data)


class ActivityLogSerializer(serializers.ModelSerializer):
    """camelCase aliases keep the legacy document shape for the admin page.

    Rows are write-only from the app's side and never edited, so timestamps are
    read-only and identity is stamped server-side by the view.
    """

    userId = NullToBlankCharField(source='user_id')
    userEmail = NullToBlankCharField(source='user_email')
    userName = NullToBlankCharField(source='user_name')
    role = NullToBlankCharField()
    shopId = NullToBlankCharField(source='shop_id')
    metadata = NullToEmptyJSONField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = ActivityLog
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class AnalyticsEventSerializer(serializers.ModelSerializer):
    """Storefront event stream; every field except ``eventType`` is optional so
    an old client can never be rejected mid-funnel."""

    eventType = serializers.CharField(source='event_type')
    deviceId = NullToBlankCharField(source='device_id')
    userId = NullToBlankCharField(source='user_id')
    url = NullToBlankCharField()
    shopId = NullToBlankCharField(source='shop_id')
    shopName = NullToBlankCharField(source='shop_name')
    productId = NullToBlankCharField(source='product_id')
    productName = NullToBlankCharField(source='product_name')
    source = NullToBlankCharField()
    query = NullToBlankCharField()
    category = NullToBlankCharField()
    context = NullToBlankCharField()
    isAiMode = serializers.BooleanField(source='is_ai_mode', required=False, allow_null=True)
    payload = NullToEmptyJSONField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = AnalyticsEvent
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
        # ``eventType`` carries the requirement; the auto-generated ``event_type``
        # field exists only so snake_case callers stay accepted.
        extra_kwargs = {'event_type': {'required': False}}

    def to_internal_value(self, data):
        """Sweep keys with no column into ``payload``.

        The legacy emitter spread arbitrary payload keys into the document, so
        an event type added later must not start failing the write.
        """
        validated = super().to_internal_value(data)
        if isinstance(data, dict):
            known = set(self.fields)
            extras = {key: value for key, value in data.items() if key not in known}
            if extras:
                payload = dict(validated.get('payload') or {})
                payload.update(extras)
                validated['payload'] = payload
        return validated


class ErrorEventSerializer(serializers.ModelSerializer):
    """Crash reports; mirrors the ``error_events`` documents the admin page reads."""

    userId = NullToBlankCharField(source='user_id')
    userEmail = NullToBlankCharField(source='user_email')
    userName = NullToBlankCharField(source='user_name')
    shopId = NullToBlankCharField(source='shop_id')
    action = NullToBlankCharField()
    errorMessage = NullToBlankCharField(source='error_message')
    errorCode = NullToBlankCharField(source='error_code')
    route = NullToBlankCharField()
    category = NullToBlankCharField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = ErrorEvent
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')
