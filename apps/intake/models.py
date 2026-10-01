from django.db import models

# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel
# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop


class IntakeBatch(CoreModel):
    """Staging container for one AI/QR ingestion run.

    Batches and their :class:`ProductDraft` rows are deliberately decoupled from
    the live ``Product``/``Inventory`` tables: nothing here is sellable stock
    until the merchant reviews the drafts and triggers :meth:`apply`.
    """

    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('applied', 'Applied'),
    )
    SOURCE_CHOICES = (
        ('qr', 'QR code'),
        ('image', 'Image'),
        ('url', 'Media URL'),
    )
    #: Which parser produced the drafts: ``qr`` (local, deterministic) or ``ai``.
    ENGINE_CHOICES = (
        ('qr', 'QR'),
        ('ai', 'AI vision'),
    )

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='intake_batches')
    created_by = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='intake_batches'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    source_type = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='image')
    engine_used = models.CharField(max_length=20, choices=ENGINE_CHOICES, blank=True, default='')
    # Ordered source descriptors: [{kind: 'qr'|'image'|'url', ref, name?, content_type?}].
    # ``ref`` is a storage path for images, the raw QR payload, or a media URL.
    sources = models.JSONField(default=list, blank=True)
    source_note = models.TextField(blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    item_count = models.IntegerField(default=0)
    # AI cost telemetry: {'model', 'models_tried', 'images', 'prompt_tokens',
    # 'completion_tokens'}; null for QR batches (no AI call was made).
    ai_usage = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Intake {self.id} ({self.status}) for {self.shop.name}"


class ProductDraft(CoreModel):
    """One proposed inventory row, fully editable before it hits production."""

    batch = models.ForeignKey(IntakeBatch, on_delete=models.CASCADE, related_name='drafts')
    name_en = models.CharField(max_length=255)
    name_sw = models.CharField(max_length=255, blank=True, default='')
    unit = models.CharField(max_length=100, blank=True, default='pcs')
    quantity = models.IntegerField(default=0)
    buying_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    category_name = models.CharField(max_length=255, blank=True, default='')
    ai_confidence_score = models.FloatField(default=0.0)
    # TRA (VFD) staging values: resolved by the keyword mapper, overridable by
    # the merchant, and copied onto Product.tax_rate when the draft is applied.
    tra_item_code = models.CharField(max_length=20, blank=True, default='')
    tax_rate_percent = models.DecimalField(max_digits=5, decimal_places=2, default=18)
    applied_product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True, related_name='intake_drafts'
    )

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.name_en} x{self.quantity} (batch {self.batch_id})"
