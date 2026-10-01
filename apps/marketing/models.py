from django.db import models
# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop

class DiscountCode(CoreModel):
    DISCOUNT_TYPES = (
        ('percentage', 'Percentage'),
        ('fixed', 'Fixed Amount'),
    )
    
    STATUS_CHOICES = (
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('disabled', 'Disabled'),
    )

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='discount_codes')
    code = models.CharField(max_length=50)
    type = models.CharField(max_length=20, choices=DISCOUNT_TYPES, default='percentage')
    value = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    used_count = models.IntegerField(default=0)

    class Meta:
        unique_together = ('shop', 'code')

    def __str__(self):
        return f"{self.code} ({self.shop.name})"


class Campaign(CoreModel):
    STATUS_CHOICES = (
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('draft', 'Draft'),
    )
    
    CHANNEL_CHOICES = (
        ('sms', 'SMS'),
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
    )

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='campaigns')
    name = models.CharField(max_length=255)
    source = models.CharField(max_length=100, default='Dashboard')
    platform = models.CharField(max_length=100, default='biashara')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='completed')
    reach = models.IntegerField(default=0)
    engagement = models.CharField(max_length=100, blank=True, null=True)
    spend = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    roi = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    start_date = models.DateTimeField(blank=True, null=True)
    audience_filter = models.CharField(max_length=100, blank=True, null=True)
    channel = models.CharField(max_length=50, choices=CHANNEL_CHOICES, blank=True, null=True)
    promo_code = models.ForeignKey(DiscountCode, on_delete=models.SET_NULL, blank=True, null=True, related_name='campaigns')

    def __str__(self):
        return f"{self.name} ({self.shop.name})"
