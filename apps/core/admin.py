from django.contrib import admin

from apps.core.models import Announcement, SupportTicket


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ('title', 'type', 'active', 'created_by_email', 'created_at')
    list_filter = ('type', 'active')
    search_fields = ('title', 'message', 'created_by_email')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ('category', 'user_email', 'shop_id', 'resolved', 'created_at')
    list_filter = ('category', 'resolved')
    search_fields = ('message', 'user_email', 'user_name', 'shop_id', 'route')
    raw_id_fields = ('user',)
    readonly_fields = ('created_at', 'updated_at')
