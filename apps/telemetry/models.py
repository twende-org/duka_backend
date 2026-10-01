from django.db import models

# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel

# Telemetry rows are historical records: they are never updated, and they must
# accept whatever an old client sends without rejecting the write. That is why
# shop_id/user_id are plain strings (the legacy Firestore ids) and the
# open-ended category/event_type fields carry no ``choices``.


class ActivityLog(CoreModel):
    """Audit trail of user actions (legacy ``activity_logs`` collection)."""

    user_id = models.CharField(max_length=128, blank=True, default='', db_index=True)
    user_email = models.CharField(max_length=255, blank=True, default='')
    user_name = models.CharField(max_length=255, blank=True, default='')
    role = models.CharField(max_length=64, blank=True, default='')
    shop_id = models.CharField(max_length=128, blank=True, default='', db_index=True)
    action = models.CharField(max_length=255, blank=True, default='')
    category = models.CharField(max_length=64, blank=True, default='', db_index=True)
    details = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.action} by {self.user_email or self.user_id}"


class AnalyticsEvent(CoreModel):
    """Product / storefront event stream (legacy ``analytics_events`` collection).

    The legacy emitter spread arbitrary payload keys into the document; the
    named columns cover every event the app actually sends today and anything
    new lands in ``payload`` so a write can never fail on schema drift.
    """

    event_type = models.CharField(max_length=64, db_index=True)
    device_id = models.CharField(max_length=128, blank=True, default='', db_index=True)
    user_id = models.CharField(max_length=128, blank=True, default='')
    url = models.CharField(max_length=500, blank=True, default='')
    shop_id = models.CharField(max_length=128, blank=True, default='', db_index=True)
    shop_name = models.CharField(max_length=255, blank=True, default='')
    product_id = models.CharField(max_length=128, blank=True, default='')
    product_name = models.CharField(max_length=255, blank=True, default='')
    source = models.CharField(max_length=64, blank=True, default='')
    query = models.CharField(max_length=255, blank=True, default='')
    category = models.CharField(max_length=128, blank=True, default='')
    is_ai_mode = models.BooleanField(null=True, blank=True)
    context = models.CharField(max_length=128, blank=True, default='')
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.event_type} @ {self.created_at:%Y-%m-%d %H:%M}"


class ErrorEvent(CoreModel):
    """Client-side error reports (legacy ``error_events`` collection)."""

    user_id = models.CharField(max_length=128, blank=True, default='', db_index=True)
    user_email = models.CharField(max_length=255, blank=True, default='')
    user_name = models.CharField(max_length=255, blank=True, default='')
    shop_id = models.CharField(max_length=128, blank=True, default='', db_index=True)
    action = models.CharField(max_length=255, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    error_code = models.CharField(max_length=128, blank=True, default='')
    route = models.CharField(max_length=500, blank=True, default='')
    category = models.CharField(max_length=64, blank=True, default='', db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.category}: {self.error_message[:60]}"
