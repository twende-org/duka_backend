from django.contrib import admin

from apps.purchases.models import (
    PurchaseOrder, PurchaseOrderItem,
    PurchaseShipment, PurchaseShipmentItem,
    GRN, GRNItem,
)


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 0
    raw_id_fields = ('product',)


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'buyer_shop', 'supplier_shop', 'status', 'total_amount', 'currency', 'created_at')
    list_filter = ('status', 'currency')
    search_fields = ('buyer_shop__name', 'supplier_shop__name')
    raw_id_fields = ('buyer_shop', 'supplier_shop')
    inlines = [PurchaseOrderItemInline]
    readonly_fields = ('created_at', 'updated_at')


class PurchaseShipmentItemInline(admin.TabularInline):
    model = PurchaseShipmentItem
    extra = 0


@admin.register(PurchaseShipment)
class PurchaseShipmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'purchase_order', 'status', 'carrier', 'tracking_number', 'dispatch_date', 'created_at')
    list_filter = ('status',)
    search_fields = ('tracking_number', 'carrier', 'purchase_order__buyer_shop__name')
    raw_id_fields = ('purchase_order',)
    inlines = [PurchaseShipmentItemInline]
    readonly_fields = ('created_at', 'updated_at')


class GRNItemInline(admin.TabularInline):
    model = GRNItem
    extra = 0
    raw_id_fields = ('product',)


@admin.register(GRN)
class GRNAdmin(admin.ModelAdmin):
    list_display = ('id', 'purchase_order', 'shop', 'supplier', 'status', 'completed_at', 'created_at')
    list_filter = ('status', 'shop')
    search_fields = ('shop__name', 'purchase_order__buyer_shop__name')
    raw_id_fields = ('purchase_order', 'shipment', 'shop', 'supplier')
    inlines = [GRNItemInline]
    readonly_fields = ('created_at', 'updated_at')
