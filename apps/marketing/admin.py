from django.contrib import admin

from apps.marketing.models import DiscountCode, Campaign


@admin.register(DiscountCode)
class DiscountCodeAdmin(admin.ModelAdmin):
    list_display = ('code', 'shop', 'type', 'value', 'status', 'used_count', 'created_at')
    list_filter = ('type', 'status', 'shop')
    search_fields = ('code', 'shop__name')
    raw_id_fields = ('shop',)


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ('name', 'shop', 'source', 'platform', 'channel', 'status', 'reach', 'spend', 'revenue', 'roi', 'start_date')
    list_filter = ('source', 'platform', 'channel', 'status', 'shop')
    search_fields = ('name', 'shop__name')
    raw_id_fields = ('shop',)
