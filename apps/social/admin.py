from django.contrib import admin

from apps.social.models import (
    FacebookOAuthSession, SocialIntegration, Conversation, Message, SocialLog,
)


@admin.register(SocialIntegration)
class SocialIntegrationAdmin(admin.ModelAdmin):
    # Tokens are excluded: never editable or visible in the admin.
    exclude = ('access_token', 'refresh_token')
    list_display = ('shop', 'platform', 'is_connected', 'page_name', 'auto_reply_enabled', 'sync_status', 'last_sync_at')
    list_filter = ('platform', 'is_connected', 'sync_status')
    search_fields = ('shop__name', 'page_name', 'page_id')
    raw_id_fields = ('shop',)
    readonly_fields = ('created_at', 'updated_at', 'last_sync_at')


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('customer_name', 'shop', 'platform', 'status', 'last_message_at', 'created_at')
    list_filter = ('platform', 'status', 'shop')
    search_fields = ('customer_name', 'shop__name')
    raw_id_fields = ('shop',)
    inlines = [MessageInline]


@admin.register(FacebookOAuthSession)
class FacebookOAuthSessionAdmin(admin.ModelAdmin):
    # Both columns hold credentials; keep them out of the admin entirely.
    exclude = ('user_token', 'pages')
    list_display = ('id', 'shop', 'created_at')
    readonly_fields = ('id', 'shop', 'created_at')


@admin.register(SocialLog)
class SocialLogAdmin(admin.ModelAdmin):
    list_display = ('shop', 'type', 'action', 'status', 'facebook_post_id', 'created_at')
    list_filter = ('type', 'action', 'status', 'shop')
    search_fields = ('shop__name', 'facebook_post_id', 'instagram_post_id')
    raw_id_fields = ('shop',)
    readonly_fields = ('created_at', 'updated_at')
