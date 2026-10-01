from django.db import models
from django.conf import settings
# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.products.models import Product

class PurchaseOrder(CoreModel):
    STATUS_CHOICES = (
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('supplier_reviewing', 'Supplier Reviewing'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
        ('awaiting_shipment', 'Awaiting Shipment'),
        ('partially_received', 'Partially Received'),
        ('completed', 'Completed'),
        ('closed_short', 'Closed Short'),
    )
    
    buyer_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='purchase_orders')
    supplier_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='sales_orders_received')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft')
    
    # Financials
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=10, default='TZS')
    
    payment_terms = models.CharField(max_length=255, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    timeline = models.JSONField(default=list, blank=True, null=True)
    # Snapshot of the supplier shop's name, the legacy document carried `supplierName`.
    supplier_name = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"PO-{self.id} ({self.buyer_shop.name} -> {self.supplier_shop.name})"


class PurchaseOrderItem(CoreModel):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    source_product_id = models.CharField(max_length=255, blank=True, null=True)
    product_name = models.CharField(max_length=255) # Snapshot
    
    expected_qty = models.IntegerField(default=0)
    received_qty = models.IntegerField(default=0)
    
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.product_name} x {self.expected_qty}"


class PurchaseShipment(CoreModel):
    STATUS_CHOICES = (
        ('preparing', 'Preparing'),
        ('packed', 'Packed'),
        ('dispatched', 'Dispatched'),
        ('in_transit', 'In Transit'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    )
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='shipments')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='preparing')
    
    # Logistics
    tracking_number = models.CharField(max_length=100, blank=True, null=True)
    carrier = models.CharField(max_length=100, blank=True, null=True)
    driver_name = models.CharField(max_length=100, blank=True, null=True)
    vehicle_number = models.CharField(max_length=100, blank=True, null=True)
    dispatch_date = models.DateTimeField(blank=True, null=True)
    estimated_delivery = models.DateField(blank=True, null=True)
    
    supplier_notes = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    timeline = models.JSONField(default=list, blank=True, null=True)

    def __str__(self):
        return f"Shipment for PO-{self.purchase_order.id}"

class PurchaseShipmentItem(CoreModel):
    shipment = models.ForeignKey(PurchaseShipment, on_delete=models.CASCADE, related_name='items')
    product_id = models.CharField(max_length=255)
    source_product_id = models.CharField(max_length=255, blank=True, null=True)
    product_name = models.CharField(max_length=255)
    shipped_qty = models.IntegerField(default=0)
    
    def __str__(self):
        return f"{self.product_name} x {self.shipped_qty} (Shipment {self.shipment.id})"


class GRN(CoreModel):
    STATUS_CHOICES = (
        ('draft', 'Draft'),
        ('completed', 'Completed'),
    )
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='grns')
    shipment = models.ForeignKey(PurchaseShipment, on_delete=models.SET_NULL, null=True, blank=True, related_name='grns')
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='buyer_grns') # Buyer
    supplier = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='supplier_grns') # Supplier
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    notes = models.TextField(blank=True, null=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"GRN for PO-{self.purchase_order.id}"


class GRNItem(CoreModel):
    grn = models.ForeignKey(GRN, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    product_name = models.CharField(max_length=255)
    
    expected_qty = models.IntegerField(default=0)
    accepted_qty = models.IntegerField(default=0)
    damaged_qty = models.IntegerField(default=0)
    missing_qty = models.IntegerField(default=0)
    rejected_qty = models.IntegerField(default=0)
    
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"GRN Item {self.product_name} - Accepted: {self.accepted_qty}"


class B2BConnection(CoreModel):
    """Trade link between a buyer shop and a platform supplier shop (legacy ``b2b_connections``)."""

    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    )
    PAYMENT_TYPE_CHOICES = (
        ('CASH', 'Cash'),
        ('CREDIT', 'Credit'),
    )
    PRICING_TIER_CHOICES = (
        ('wholesale', 'Wholesale'),
        ('corporate', 'Corporate'),
        ('reseller', 'Reseller'),
    )

    buyer_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='b2b_connections_as_buyer')
    supplier_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='b2b_connections_as_supplier')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    credit_limit = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    payment_type = models.CharField(max_length=10, choices=PAYMENT_TYPE_CHOICES, blank=True, null=True)
    credit_days = models.IntegerField(null=True, blank=True)
    pricing_tier = models.CharField(max_length=20, choices=PRICING_TIER_CHOICES, blank=True, null=True)

    class Meta:
        unique_together = ('buyer_shop', 'supplier_shop')

    def __str__(self):
        return f"{self.buyer_shop.name} -> {self.supplier_shop.name} ({self.status})"


class B2BSupplierBalance(CoreModel):
    """
    Accounts-payable balance the buyer keeps for one of its platform suppliers.

    The legacy document id was ``{buyerShopId}_{supplierShopId}``; the pair stays
    unique per buyer/supplier here.
    """

    buyer_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='b2b_supplier_balances')
    supplier_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='b2b_buyer_balances')
    supplier_name = models.CharField(max_length=255, blank=True, default='')
    total_purchases = models.IntegerField(default=0)
    received_goods_value = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    outstanding_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        unique_together = ('buyer_shop', 'supplier_shop')

    def __str__(self):
        return f"{self.buyer_shop.name} / {self.supplier_shop.name}: {self.outstanding_balance}"


class B2BSupplierInvoice(CoreModel):
    """Supplier invoice raised by the buyer against a B2B PO (legacy ``b2b_supplier_invoices``)."""

    STATUS_CHOICES = (
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('under_review', 'Under Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('paid', 'Paid'),
    )

    buyer_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='b2b_supplier_invoices')
    supplier_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='b2b_buyer_invoices')
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name='b2b_invoices')
    grn_ids = models.JSONField(default=list, blank=True)
    invoice_number = models.CharField(max_length=100, blank=True, default='')
    invoice_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=10, default='TZS')
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    attachment_url = models.URLField(max_length=500, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')

    def __str__(self):
        return f"{self.invoice_number or 'INV'} ({self.status})"


class B2BSupplierPayment(CoreModel):
    """Payment the buyer records against a B2B supplier balance (legacy ``b2b_supplier_payments``)."""

    PAYMENT_METHODS = ('Cash', 'Bank', 'Mobile Money')

    buyer_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='b2b_supplier_payments')
    supplier_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='b2b_buyer_payments')
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    method = models.CharField(max_length=30, choices=[(m, m) for m in PAYMENT_METHODS], default='Cash')
    reference = models.CharField(max_length=255, blank=True, null=True)
    invoice_ids = models.JSONField(default=list, blank=True)
    date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.amount} -> {self.supplier_shop.name}"
