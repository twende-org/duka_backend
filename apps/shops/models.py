from django.db import models
from django.conf import settings
# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel

class Shop(CoreModel):
    name = models.CharField(max_length=255)
    # Firestore document id from the pre-Django era; the app keeps using it as the
    # visible id so mirrors, inventory docs and public readers stay keyed the same.
    legacy_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)
    description = models.TextField(blank=True)
    country = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=100, blank=True)
    district = models.CharField(max_length=100, blank=True)
    
    # Basic & Contact
    slug = models.SlugField(max_length=255, unique=True, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    whatsapp = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    website = models.URLField(blank=True, null=True)
    slogan = models.CharField(max_length=255, blank=True, null=True)
    operatingHours = models.TextField(blank=True, null=True)
    businessType = models.CharField(max_length=100, blank=True, null=True)
    productCondition = models.CharField(max_length=50, blank=True, null=True)
    instagramUrl = models.URLField(blank=True, null=True)
    facebookUrl = models.URLField(blank=True, null=True)
    tiktokUrl = models.URLField(blank=True, null=True)
    inventoryModel = models.CharField(max_length=50, blank=True, null=True)
    language = models.CharField(max_length=50, blank=True, null=True)
    
    # Legal Fields
    tin_number = models.CharField(max_length=50, blank=True, null=True)
    vrn_number = models.CharField(max_length=50, blank=True, null=True)
    license_number = models.CharField(max_length=100, blank=True, null=True)
    registration_number = models.CharField(max_length=100, blank=True, null=True)

    # Booleans
    keepsStock = models.BooleanField(default=False)
    isPublic = models.BooleanField(default=False)
    trackInventory = models.BooleanField(default=False)
    isWholesaleSupplier = models.BooleanField(default=False)
    
    # Location
    address = models.TextField(blank=True, null=True)
    location = models.CharField(max_length=255, blank=True, null=True)
    lat = models.FloatField(blank=True, null=True)
    lon = models.FloatField(blank=True, null=True)
    
    # Arrays
    shopTypes = models.JSONField(default=list, blank=True, null=True)
    productCategories = models.JSONField(default=list, blank=True, null=True)
    customerTypes = models.JSONField(default=list, blank=True, null=True)
    salesChannels = models.JSONField(default=list, blank=True, null=True)
    pricingModels = models.JSONField(default=list, blank=True, null=True)
    stockLocations = models.JSONField(default=list, blank=True, null=True)
    fulfillmentMethods = models.JSONField(default=list, blank=True, null=True)
    serviceCoverage = models.JSONField(default=list, blank=True, null=True)
    businessCategories = models.JSONField(default=list, blank=True, null=True)
    
    # Nested Objects
    productCapabilities = models.JSONField(default=dict, blank=True, null=True)
    online_store_settings = models.JSONField(default=dict, blank=True, null=True)
    store_policies = models.JSONField(default=dict, blank=True, null=True)
    ai_marketing_settings = models.JSONField(default=dict, blank=True, null=True)
    social_links = models.JSONField(default=dict, blank=True, null=True)
    # Payout target collected by the business setup wizard (provider, account name,
    # account number, bank name, branch code). Firestore kept it in the
    # shops/{id}/settings/default doc; here it is a shop column.
    payout_details = models.JSONField(default=dict, blank=True, null=True)
    
    # Media — TextField because the shop form stores base64 data URLs directly
    # (Firebase accepted any string; URLField would reject them on write).
    imageUrl = models.TextField(blank=True, null=True)
    coverImage = models.TextField(blank=True, null=True)
    
    currency = models.CharField(max_length=10, default='TZS')
    timezone = models.CharField(max_length=50, default='Africa/Dar_es_Salaam')

    # Setup wizard / moderation state. Firestore wrote these through the generic
    # shop update path; the wizard gate (setupStatus !== 'completed') and the
    # admin verification badge both read them back, so they must round-trip.
    setupStatus = models.CharField(max_length=20, default='pending')
    setupProgress = models.PositiveIntegerField(default=0)
    verificationStatus = models.CharField(max_length=20, default='unverified')

    # Public storefront social proof. Firestore allowed any authenticated user to
    # bump this one field (affectedKeys().hasOnly(['followerCount'])), so it is
    # exposed via follow/unfollow actions rather than the general update path.
    follower_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

class Branch(CoreModel):
    legacy_id = models.CharField(max_length=64, blank=True, null=True, unique=True, db_index=True)
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='branches')
    name = models.CharField(max_length=255) # E.g. Main Branch
    location = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    is_main = models.BooleanField(default=False)
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    branch_type = models.CharField(max_length=100, default='Storefront')
    timezone = models.CharField(max_length=50, blank=True, null=True)
    operating_hours = models.TextField(blank=True, null=True)
    features = models.JSONField(default=dict, blank=True, null=True)
    
    def __str__(self):
        return f"{self.name} - {self.shop.name}"

class UserRole(CoreModel):
    ROLE_CHOICES = (
        ('owner', 'Owner'),
        ('manager', 'Manager'),
        ('attendant', 'Attendant'),
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='shop_roles')
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='user_roles')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    
    class Meta:
        unique_together = ('user', 'shop')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.role} at {self.shop.name}"

class Invitation(CoreModel):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('declined', 'Declined'),
        ('cancelled', 'Cancelled'),
    )
    email = models.EmailField()
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='invitations')
    role = models.CharField(max_length=20, choices=UserRole.ROLE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_invitations')
    
    class Meta:
        unique_together = ('email', 'shop')
        ordering = ['-created_at']

    def __str__(self):
        return f"Invite {self.email} to {self.shop.name} as {self.role}"
