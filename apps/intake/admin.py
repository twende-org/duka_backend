from django.contrib import admin

# pyrefly: ignore [missing-import]
from apps.intake.models import IntakeBatch, ProductDraft


class ProductDraftInline(admin.TabularInline):
    model = ProductDraft
    extra = 0
    readonly_fields = ('created_at', 'updated_at')


@admin.register(IntakeBatch)
class IntakeBatchAdmin(admin.ModelAdmin):
    list_display = ('id', 'shop', 'status', 'source_type', 'engine_used', 'item_count', 'created_at')
    list_filter = ('status', 'source_type', 'engine_used', 'shop')
    search_fields = ('shop__name', 'source_note', 'error_message')
    raw_id_fields = ('shop', 'created_by')
    readonly_fields = ('created_at', 'updated_at')
    inlines = (ProductDraftInline,)


@admin.register(ProductDraft)
class ProductDraftAdmin(admin.ModelAdmin):
    list_display = ('name_en', 'batch', 'quantity', 'buying_price', 'selling_price',
                    'ai_confidence_score', 'tra_item_code', 'applied_product', 'created_at')
    list_filter = ('tra_item_code', 'batch__status')
    search_fields = ('name_en', 'name_sw', 'batch__shop__name')
    raw_id_fields = ('batch', 'applied_product')
    readonly_fields = ('created_at', 'updated_at')
