from django.contrib import admin

from apps.telemetry.models import ActivityLog, AnalyticsEvent, ErrorEvent


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ('action', 'category', 'user_name', 'user_email', 'shop_id', 'created_at')
    list_filter = ('category', 'role')
    search_fields = ('user_email', 'user_name', 'user_id', 'shop_id', 'action', 'details')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AnalyticsEvent)
class AnalyticsEventAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'shop_name', 'query', 'source', 'device_id', 'created_at')
    list_filter = ('event_type', 'source')
    search_fields = ('event_type', 'shop_id', 'shop_name', 'query', 'device_id', 'url')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ErrorEvent)
class ErrorEventAdmin(admin.ModelAdmin):
    list_display = ('category', 'action', 'error_message', 'route', 'user_email', 'created_at')
    list_filter = ('category',)
    search_fields = ('error_message', 'route', 'user_email', 'user_id', 'shop_id', 'action')
    readonly_fields = ('created_at', 'updated_at')
