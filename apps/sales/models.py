from django.db import models
from django.utils import timezone
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch
# pyrefly: ignore [missing-import]
from apps.products.models import Product
from django.conf import settings
# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel

class Sale(CoreModel):
    PAYMENT_METHODS = (
        ('cash', 'Cash'),
        ('mpesa', 'M-Pesa'),
        ('airtel_money', 'Airtel Money'),
        ('tigopesa', 'Tigo Pesa'),
        ('halopesa', 'HaloPesa'),
        ('bank', 'Bank Transfer'),
        ('card', 'Card'),
        ('credit', 'Credit'),
    )
    SALE_STATUS = (
        ('completed', 'Completed'),
        ('draft', 'Draft'),
    )

    legacy_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='sales')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
    attendant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    shift = models.ForeignKey('Shift', on_delete=models.SET_NULL, null=True, blank=True, related_name='sales')
    
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    profit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=50, choices=PAYMENT_METHODS)
    
    customer_id = models.CharField(max_length=255, blank=True, null=True)
    customer_name = models.CharField(max_length=255, blank=True, null=True)
    customer_phone = models.CharField(max_length=20, blank=True, null=True)
    #: Buyer's portal account. Firebase kept this as ``customerUserId`` on the
    #: sale document; the portal queries filter on it, so imports and POS writes
    #: must keep it populated for receipts to be visible.
    customer_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='portal_sales')
    notes = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=50, choices=SALE_STATUS, default='completed')

class SaleItem(CoreModel):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    #: Snapshot of the product name: receipts must survive product deletion.
    product_name = models.CharField(max_length=255, blank=True)
    quantity = models.IntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    total_price = models.DecimalField(max_digits=12, decimal_places=2)
    profit = models.DecimalField(max_digits=12, decimal_places=2, default=0)

class DailySalesSummary(CoreModel):
    """
    Caches daily totals to power the dashboard without needing to run aggregate SQL on thousands of rows.
    Mirrors Firebase shops/{shopId}/sales_days/{YYYY-MM-DD}
    """
    date = models.DateField()
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='daily_summaries')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='daily_summaries')
    
    total_sales = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    transactions = models.IntegerField(default=0)
    profit = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    net_profit = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total_expenses = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    
    class Meta:
        unique_together = ('shop', 'branch', 'date')
        ordering = ['-date']

class Shift(CoreModel):
    """
    Cash-drawer shift for a shop, mirrors Firebase shops/{shopId}/shifts.
    Shifts are shop-level (not branch-level) to match the frontend contract.
    """
    STATUS_CHOICES = (
        ('OPEN', 'Open'),
        ('CLOSED', 'Closed'),
    )
    APPROVAL_STATUS = (
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('DISPUTED', 'Disputed'),
    )

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='shifts')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN')

    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='shifts_opened')
    opened_by_name = models.CharField(max_length=255, blank=True)
    opened_at = models.DateTimeField(default=timezone.now)
    opening_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='shifts_closed')
    closed_by_name = models.CharField(max_length=255, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    cash_sales_total = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    cash_expenses_total = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    expected_closing_cash = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    actual_closing_cash = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    cash_left_for_next_day = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    cash_submitted_to_owner = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    discrepancy = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)

    notes = models.TextField(blank=True, null=True)
    owner_approval_status = models.CharField(max_length=50, choices=APPROVAL_STATUS, default='PENDING')

    class Meta:
        ordering = ['-opened_at']

class Order(CoreModel):
    #: The app's lifecycle (``Orders.tsx`` tabs, ``Fulfillment.tsx`` pipeline)
    #: drives transitions through every one of these, so the whole union is valid.
    ORDER_STATUS = (
        ('draft', 'Draft'),
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('awaiting_shipment', 'Awaiting Shipment'),
        ('confirmed', 'Confirmed'),
        ('allocated', 'Allocated'),
        ('picking', 'Picking'),
        ('packed', 'Packed'),
        ('ready_for_delivery', 'Ready for Delivery'),
        ('out_for_delivery', 'Out for Delivery'),
        ('in_transit', 'In Transit'),
        ('delivered', 'Delivered'),
        ('paid', 'Paid'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    )
    APPROVAL_STATUS = (
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('pending_approval', 'Pending Approval'),
    )
    SOURCE_CHOICES = (
        ('public_storefront', 'Public Storefront'),
        ('in_app', 'In App'),
        # Customer wishlist checkout wrote this marker straight to Firestore; the
        # merchant's Orders page shows it as "Wishlist Order".
        ('wishlist', 'Wishlist'),
    )
    
    legacy_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='orders')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
    idempotency_key = models.CharField(max_length=255, unique=True, null=True, blank=True)
    
    # Financials
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    profit_estimate = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=50, default='Cash')
    #: Stamped by ``pay_order``; Firestore wrote ``paidAt`` when the order settled.
    paid_at = models.DateTimeField(null=True, blank=True)
    
    # Status & Lifecycle
    status = models.CharField(max_length=50, choices=ORDER_STATUS, default='pending')
    approval_status = models.CharField(max_length=50, choices=APPROVAL_STATUS, blank=True, null=True)
    source = models.CharField(max_length=50, choices=SOURCE_CHOICES, default='in_app')
    
    # Customer Details
    customer_id = models.CharField(max_length=255, blank=True, null=True) # Will link to CRM Customer later
    customer_name = models.CharField(max_length=255, blank=True, null=True)
    customer_phone = models.CharField(max_length=20, blank=True, null=True)
    customer_type = models.CharField(max_length=50, blank=True, null=True)
    customer_po_number = models.CharField(max_length=100, blank=True, null=True)
    required_delivery_date = models.DateField(null=True, blank=True)
    #: Buyer's portal account (Firestore ``customerUserId``); drives the portal's
    #: "my orders" query, so it must be set by every checkout path.
    customer_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='portal_orders')
    
    # Metadata & Tracking
    salesperson_id = models.CharField(max_length=255, blank=True, null=True)
    internal_notes = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    fulfillment_details = models.JSONField(default=dict, blank=True)

    class Meta:
        # Newest first, and a stable order for the paginated list the app walks.
        ordering = ['-created_at']

class OrderItem(CoreModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    #: Snapshot of the product name: portal order history must survive deletion.
    product_name = models.CharField(max_length=255, blank=True)
    quantity = models.IntegerField(default=1)
    picked_qty = models.IntegerField(default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
