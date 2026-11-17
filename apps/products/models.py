from django.db import models
from django.conf import settings
from django.db.models import Q
from django.db.models.functions import Lower
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch
# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel

class Category(CoreModel):
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(max_length=255)
    legacy_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)

    class Meta:
        verbose_name_plural = 'categories'
        unique_together = ('shop', 'name')
        constraints = [
            models.UniqueConstraint('shop', Lower('name'), name='uniq_category_shop_lower_name'),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.shop.name})"

class MerchantCategory(CoreModel):
    """Merchant-defined category shown in the setup wizard.

    Mirrors the Firestore shops/{shopId}/merchant_categories subcollection.
    """
    STATUS_CHOICES = (('active', 'Active'), ('inactive', 'Inactive'))
    
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='merchant_categories')
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, blank=True)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children')
    description = models.TextField(blank=True, null=True)
    sort_order = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    legacy_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)
    
    class Meta:
        ordering = ['sort_order']
        verbose_name_plural = 'merchant categories'
        constraints = [
            models.UniqueConstraint(fields=['shop', 'parent', 'name'], name='uniq_merchantcat_shop_parent_name'),
            models.UniqueConstraint(fields=['shop', 'name'], condition=Q(parent__isnull=True), name='uniq_merchantcat_root_name'),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.shop.name})"

class MasterProduct(CoreModel):
    """Platform-wide catalogue row shared by every shop (Phase 1 tap-to-add).

    Carries identity only — name, brand, barcode, warranty — never price or
    stock: each shop keeps its own cost and margin on its own ``Product``.
    No ``legacy_id``: these rows have no Firestore history, so ``app_id()``
    falls back to the uuid and the legacy helpers still work on them.
    """

    name = models.CharField(max_length=255, db_index=True)
    brand = models.CharField(max_length=100, blank=True, db_index=True)
    # Free text, not a FK: the shop-side ``Category`` rows are per-shop, so a
    # global taxonomy FK would either force a new shared table or break that
    # isolation. The tap-to-add service resolves this name into the shop's own
    # category, the same way the AI intake apply step does.
    category_name = models.CharField(max_length=100, db_index=True)
    niche = models.CharField(max_length=50, default='electronics', db_index=True)
    barcode = models.CharField(max_length=100, blank=True, db_index=True)
    # TextField, matching Product.image_url: the Firestore-era rows this mirrors
    # held base64 data URLs, which URLField would reject on write.
    image_url = models.TextField(blank=True, null=True)
    default_warranty = models.CharField(max_length=100, blank=True)
    requires_imei = models.BooleanField(default=False)
    attributes = models.JSONField(default=dict, blank=True, null=True)
    is_verified = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'master products'

    def __str__(self):
        return f"{self.brand} {self.name}".strip()


class Product(CoreModel):
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='products')
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=255)
    legacy_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)
    sku = models.CharField(max_length=100, blank=True)
    barcode = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    brand = models.CharField(max_length=100, blank=True)
    
    # Categorization & Hierarchy
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True)
    marketplace_categories = models.JSONField(default=list, blank=True, null=True)
    marketplace_category_id = models.CharField(max_length=255, blank=True, null=True)
    merchant_category_id = models.CharField(max_length=255, blank=True, null=True)
    
    # Physical & Operational Specs
    condition = models.CharField(max_length=50, blank=True, null=True, choices=(
        ('new', 'New'), ('used', 'Used'), ('refurbished', 'Refurbished'), 
        ('rental', 'Rental'), ('digital', 'Digital'), ('service', 'Service')
    ))
    unit = models.CharField(max_length=100, blank=True, null=True)
    weight = models.CharField(max_length=100, blank=True, null=True)
    size = models.CharField(max_length=100, blank=True, null=True)
    color = models.CharField(max_length=100, blank=True, null=True)
    store_location = models.CharField(max_length=100, blank=True, null=True)
    moq = models.IntegerField(default=1)
    expiry_date = models.DateField(blank=True, null=True)
    warranty = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=50, default='active', choices=(
        ('active', 'Active'), ('inactive', 'Inactive'), ('discontinued', 'Discontinued')
    ))

    # Financials
    buying_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    wholesale_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    prices = models.JSONField(default=list, blank=True, null=True)
    
    # Rich Media & Social Proof
    # TextField: Firestore held both Storage URLs and base64 data URLs here.
    image_url = models.TextField(blank=True, null=True)
    image_urls = models.JSONField(default=list, blank=True, null=True)
    rating = models.FloatField(default=0.0)
    review_count = models.IntegerField(default=0)
    
    # Channel Distribution Toggles
    publish_to_facebook = models.BooleanField(default=False)
    publish_to_directory = models.BooleanField(default=False)
    publish_to_delivery_app = models.BooleanField(default=False)
    # Round-robin key for the daily social poster (legacy Firestore lastPostedAt).
    last_posted_at = models.DateTimeField(blank=True, null=True)
    
    # Complex Nested Data
    attributes = models.JSONField(default=dict, blank=True, null=True)
    variants = models.JSONField(default=list, blank=True, null=True)
    tags = models.JSONField(default=list, blank=True, null=True)
    
    # Shared Master Catalog (Phase 1). Nullable, so every pre-catalog product is
    # untouched; only the tap-to-add service writes it, and the API serializer
    # keeps it read-only so a generic product PATCH cannot repoint a shop row.
    master_product = models.ForeignKey(
        MasterProduct, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='shop_products',
    )
    requires_imei = models.BooleanField(default=False)

    # B2B & Sourcing
    supplier = models.CharField(max_length=255, blank=True, null=True)
    source_product_id = models.CharField(max_length=255, blank=True, null=True)
    supplier_shop_id = models.CharField(max_length=255, blank=True, null=True)
    
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return self.name

class Inventory(CoreModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='inventory')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='inventory')
    quantity = models.IntegerField(default=0)
    allocated_qty = models.IntegerField(default=0)
    low_stock_threshold = models.IntegerField(default=5)
    location = models.CharField(max_length=255, blank=True, null=True)
    
    class Meta:
        unique_together = ('product', 'branch')

class InventoryMovement(CoreModel):
    MOVEMENT_TYPES = (
        ('in', 'Stock In'),
        ('out', 'Stock Out'),
        ('sale', 'Sale'),
        ('transfer', 'Transfer'),
        ('adjustment', 'Adjustment'),
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES)
    quantity_changed = models.IntegerField()
    previous_qty = models.IntegerField(default=0)
    new_qty = models.IntegerField(default=0)
    reason = models.CharField(max_length=255, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')

class InventorySerialItem(CoreModel):
    """One physical unit, tracked by IMEI or serial number, inside one shop.

    Shop-scoped rather than catalogue-scoped: the same ``MasterProduct`` has
    different devices in different shops, and stock itself is already keyed by
    (product, branch) in :class:`Inventory`. The POS checkout links a unit to its
    sale line (``sale_item``) when the cashier scans it; a voided sale puts the
    unit back on the shelf.
    """

    STATUS_CHOICES = (
        ('in_stock', 'In Stock'),
        ('sold', 'Sold'),
        ('reserved', 'Reserved'),
        ('returned', 'Returned'),
        ('damaged', 'Damaged'),
    )

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='serial_items')
    # PROTECT, not CASCADE: a sold unit is the warranty and theft record for a
    # device that left the shop months ago. Deleting the product row must not
    # take that history with it — the API soft-deletes products instead (see
    # ``ProductViewSet.perform_destroy``), and this constraint is what makes a
    # hard delete fail loudly rather than silently erase the audit trail.
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='serial_items')
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='serial_items'
    )
    imei_number = models.CharField(max_length=100, db_index=True)
    serial_number = models.CharField(max_length=100, blank=True, db_index=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='in_stock', db_index=True
    )
    #: Set only once a future checkout links the unit to a sale line; the
    #: product itself stays named because SaleItem already snapshots its name.
    sale_item = models.ForeignKey(
        'sales.SaleItem', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='serial_items',
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # A device that is on the shelf cannot be registered twice. Blank
            # IMEIs (serial-only items) are excluded or every one of them would
            # collide with the next.
            models.UniqueConstraint(
                fields=['shop', 'imei_number'],
                condition=Q(status='in_stock') & ~Q(imei_number=''),
                name='uniq_serial_imei_in_stock_per_shop',
            ),
        ]

    def __str__(self):
        return f"{self.imei_number or self.serial_number} ({self.get_status_display()})"


class StockTransfer(CoreModel):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    )
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='stock_transfers')
    from_branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='transfers_out')
    to_branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='transfers_in')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.IntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    completed_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"Transfer {self.quantity} of {self.product.name} from {self.from_branch.name} to {self.to_branch.name}"


class B2BStockTransfer(CoreModel):
    """Wholesale→retail stock transfer between two DIFFERENT shops (spec Part 2).

    In-shop branch moves stay on :class:`StockTransfer`. Here the buyer's stock
    is quarantined in ``pending`` until they confirm; confirmation (complete) or
    cancellation (restock) is the only way out — handled with row locks in
    ``apps.products.b2b_services``.
    """
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    )
    SOURCE_CHOICES = (
        ('manual', 'Manual dispatch'),
        ('sale', 'Point-of-sale delivery'),
    )
    from_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='b2b_transfers_out')
    to_shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='b2b_transfers_in')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='manual', db_index=True)
    sale = models.ForeignKey(
        'sales.Sale', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='b2b_delivery_manifests',
        help_text='Set when the manifest was staged automatically from a POS sale; stock was already deducted by that sale.',
    )
    from_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='b2b_transfers_out')
    to_branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='b2b_transfers_in',
        help_text='Optional; the buyer defaults to their main (or first) branch.',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    reference = models.CharField(max_length=128, blank=True, default='')
    note = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='b2b_transfers_created')
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='b2b_transfers_completed')
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(from_shop=models.F('to_shop')),
                name='b2b_transfer_shops_differ',
            ),
        ]

    def __str__(self):
        return f"B2B {self.get_status_display()}: {self.from_shop.name} -> {self.to_shop.name}"


class B2BStockTransferItem(CoreModel):
    """One line on a B2B transfer.

    Sender-side product is nullable so history survives a deleted catalogue row;
    ``product_name``/``sku``/``barcode``/``unit`` snapshot the sender's labels at
    dispatch time. Manual transfers are mapped by the buyer before completion;
    sale-sourced manifests may instead be accepted in one tap, which auto-matches
    these snapshots against the buyer's catalogue and creates missing products.
    """
    transfer = models.ForeignKey(B2BStockTransfer, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name='b2b_transfer_items_sent')
    product_name = models.CharField(max_length=255)
    sku = models.CharField(max_length=100, blank=True, default='')
    barcode = models.CharField(max_length=100, blank=True, default='')
    unit = models.CharField(max_length=100, blank=True, default='pcs')
    quantity = models.IntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    mapped_product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='b2b_transfer_items_mapped',
        help_text='Buyer-side choice: the receiving product this line lands on at completion.',
    )
    received_product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name='b2b_transfer_items_received')

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.quantity} x {self.product_name}"
