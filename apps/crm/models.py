from django.db import models
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel

class Customer(CoreModel):
    CUSTOMER_TYPES = (
        ('retail', 'Retail'),
        ('wholesale', 'Wholesale'),
        ('corporate', 'Corporate'),
        ('reseller', 'Reseller'),
        ('distributor', 'Distributor'),
    )
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='customers')
    # Firestore document id from the pre-Django era; sale/order ``customer_id``
    # columns keep pointing at it, so the portal links rows through this.
    legacy_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    customer_type = models.CharField(max_length=50, choices=CUSTOMER_TYPES, default='retail')
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # B2B & Additional Fields
    business_name = models.CharField(max_length=255, blank=True)
    contact_person = models.CharField(max_length=255, blank=True)
    registration_number = models.CharField(max_length=255, blank=True)
    commercial_settings = models.JSONField(default=dict, blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    
    # Analytics Fields
    total_purchases = models.IntegerField(default=0)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    last_purchase_date = models.DateField(null=True, blank=True)
    
    # Linking Fields
    user_id = models.CharField(max_length=255, null=True, blank=True, help_text="Linked Platform User ID")
    linked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.shop.name})"

class CustomerInvoice(CoreModel):
    INVOICE_STATUS = (
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('overdue', 'Overdue'),
        ('cancelled', 'Cancelled')
    )
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='invoices')
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='customer_invoices')
    
    # We will use a generic string for order_id here temporarily to avoid circular imports with sales.Order
    # Alternatively, we could import it locally or use 'sales.Order' string reference.
    order = models.ForeignKey('sales.Order', on_delete=models.SET_NULL, null=True, related_name='invoices')
    
    amount_due = models.DecimalField(max_digits=12, decimal_places=2)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=INVOICE_STATUS, default='pending')
    
    def __str__(self):
        return f"Invoice {self.id} - {self.customer.name}"

class CustomerPayment(CoreModel):
    PAYMENT_METHODS = (
        ('Cash', 'Cash'),
        ('Bank', 'Bank'),
        ('Mobile Money', 'Mobile Money'),
    )
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='customer_payments')
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='payments')
    
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=50, choices=PAYMENT_METHODS)
    reference = models.CharField(max_length=255, blank=True)
    date = models.DateField()
    notes = models.TextField(blank=True)
    
    # Optional link to specific invoices
    invoices = models.ManyToManyField(CustomerInvoice, blank=True, related_name='payments')
    
    def __str__(self):
        return f"Payment {self.id} - {self.customer.name} - {self.amount}"


class Supplier(CoreModel):
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='suppliers')
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    
    # If this is a platform B2B supplier, store their twende duka shop ID
    platform_shop_id = models.CharField(max_length=255, blank=True, null=True)
    
    products = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    owner_id = models.CharField(max_length=255, blank=True, null=True)
    
    # AP Fields
    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_purchases = models.IntegerField(default=0)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    def __str__(self):
        return f"{self.name} ({self.shop.name})"


class SupplierInvoice(CoreModel):
    INVOICE_STATUS = (
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('overdue', 'Overdue'),
        ('cancelled', 'Cancelled')
    )
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name='invoices')
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='supplier_invoices')
    
    # We will use a generic string for po_id here temporarily to avoid circular imports with purchases.PurchaseOrder
    purchase_order = models.ForeignKey('purchases.PurchaseOrder', on_delete=models.SET_NULL, null=True, related_name='invoices')
    
    amount_due = models.DecimalField(max_digits=12, decimal_places=2)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=50, choices=INVOICE_STATUS, default='pending')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Supplier Invoice {self.id} - {self.supplier.name}"

class SupplierPayment(CoreModel):
    PAYMENT_METHODS = (
        ('Cash', 'Cash'),
        ('Bank', 'Bank'),
        ('Mobile Money', 'Mobile Money'),
    )
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='supplier_payments')
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name='payments')
    
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=50, choices=PAYMENT_METHODS)
    reference = models.CharField(max_length=255, blank=True)
    date = models.DateField()
    notes = models.TextField(blank=True)
    
    # Optional link to specific invoices
    invoices = models.ManyToManyField(SupplierInvoice, blank=True, related_name='payments')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Supplier Payment {self.id} - {self.supplier.name} - {self.amount}"
