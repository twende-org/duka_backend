from django.contrib import admin

from apps.products.models import (
    B2BStockTransfer, B2BStockTransferItem, Category, Inventory, InventoryMovement,
    Product, StockTransfer,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'shop', 'created_at')
    search_fields = ('name', 'shop__name')
    raw_id_fields = ('shop',)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'shop', 'sku', 'status', 'buying_price', 'selling_price', 'is_active', 'created_at')
    list_filter = ('status', 'is_active', 'condition', 'shop')
    search_fields = ('name', 'sku', 'barcode', 'shop__name')
    raw_id_fields = ('shop', 'category', 'branch')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ('product', 'branch', 'quantity', 'allocated_qty', 'low_stock_threshold')
    search_fields = ('product__name', 'branch__name')
    raw_id_fields = ('product', 'branch')
    list_filter = ('branch',)


@admin.register(InventoryMovement)
class InventoryMovementAdmin(admin.ModelAdmin):
    list_display = ('product', 'branch', 'movement_type', 'quantity_changed', 'previous_qty', 'new_qty', 'created_at')
    list_filter = ('movement_type', 'branch')
    search_fields = ('product__name', 'reason')
    raw_id_fields = ('product', 'branch', 'user')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(StockTransfer)
class StockTransferAdmin(admin.ModelAdmin):
    list_display = ('shop', 'from_branch', 'to_branch', 'product', 'quantity', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('shop__name', 'product__name')
    raw_id_fields = ('shop', 'from_branch', 'to_branch', 'product', 'created_by')


class B2BStockTransferItemInline(admin.TabularInline):
    model = B2BStockTransferItem
    extra = 0
    raw_id_fields = ('product', 'received_product')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(B2BStockTransfer)
class B2BStockTransferAdmin(admin.ModelAdmin):
    list_display = ('from_shop', 'to_shop', 'status', 'reference', 'completed_at', 'created_at')
    list_filter = ('status', 'from_shop', 'to_shop')
    search_fields = ('from_shop__name', 'to_shop__name', 'reference')
    raw_id_fields = ('from_shop', 'to_shop', 'from_branch', 'to_branch', 'created_by', 'completed_by')
    inlines = (B2BStockTransferItemInline,)
    readonly_fields = ('created_at', 'updated_at')
