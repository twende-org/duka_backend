from django.contrib.auth.models import AbstractUser
from django.db import models
import uuid

class User(AbstractUser):
    ACCOUNT_TYPE_CHOICES = (
        ('merchant', 'Merchant'),
        ('staff', 'Staff'),
        ('customer', 'Customer'),
        ('unassigned', 'Unassigned'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    """
    Custom User model representing a Platform User.
    Users can have multiple roles across different shops (handled by UserRole model later),
    but they have global capabilities determining their workspace access.
    """
    # Firebase Auth uid (the Firestore users/{uid} document id). The app still
    # joins shops to people by uid, so the importer keeps the mapping.
    firebase_uid = models.CharField(max_length=128, blank=True, null=True, db_index=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    display_name = models.CharField(max_length=255, blank=True, null=True)
    account_type = models.CharField(
        max_length=20, choices=ACCOUNT_TYPE_CHOICES, default='merchant'
    )
    # Workspace preference a dual-role user picked ("merchant"/"customer"/"ask").
    # Blank means "never chose" so the post-login redirect can still decide.
    default_workspace = models.CharField(max_length=20, blank=True, default='')
    
    # Global Capabilities
    can_manage_business = models.BooleanField(default=False)
    can_shop = models.BooleanField(default=True)
    can_buy_for_business = models.BooleanField(default=False)

    # We use email as the primary login field in the original app, but Django AbstractUser defaults to username.
    # To keep things simple, we'll keep username but encourage using email for authentication in the custom backend,
    # or just make email unique.
    email = models.EmailField(unique=True)

    # Admin "suspend" toggle. Deliberately separate from ``is_active`` so a
    # suspended user can still be re-activated and their data stays intact.
    is_suspended = models.BooleanField(default=False)

    # Legacy ``businessProfile`` map (companyName, tin, vrn, status, category,
    # creditLimit, creditBalance, approvedAt). JSON because the shape is owned
    # by the client and empty means "no wholesale application".
    business_profile = models.JSONField(default=dict, blank=True)

    # Legacy ``corporateProfile`` map (companyId, companyName, tin, departmentId,
    # role buyer|approver|admin, creditLimit, creditBalance, status). Membership
    # is granted by staff, so self-service writes must not touch this map.
    corporate_profile = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.email or self.username


class Subscription(models.Model):
    """Platform plan for one user (legacy ``subscriptions/{uid}`` documents).

    The Firestore document id was the user id, so the user *is* the primary key.
    """

    PLAN_CHOICES = (
        ('free', 'Free'),
        ('basic', 'Basic'),
        ('business', 'Business'),
        ('enterprise', 'Enterprise'),
    )
    STATUS_CHOICES = (
        ('active', 'Active'),
        ('pending', 'Pending'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
    )

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, primary_key=True, related_name='subscription'
    )
    user_email = models.EmailField(blank=True, default='')
    user_name = models.CharField(max_length=255, blank=True, default='')
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='free')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    # The app writes plain ``YYYY-MM-DD`` strings (AdminPayments).
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    payment_method = models.CharField(max_length=64, blank=True, default='')
    payment_reference = models.CharField(max_length=128, blank=True, default='')
    amount = models.IntegerField(default=0)
    confirmed_by = models.CharField(max_length=128, blank=True, default='')
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.plan} ({self.status}) for {self.user_email or self.user_id}"


class WishlistItem(models.Model):
    """Saved product, one row per (user, product) (legacy ``users/{uid}/wishlist``).

    ``product_id`` is the app-visible product id (legacy Firestore id), matching
    how the rest of the storefront addresses products.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='wishlist_items')
    product_id = models.CharField(max_length=128)
    name = models.CharField(max_length=255, blank=True, default='')
    price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    shop_id = models.CharField(max_length=128, blank=True, default='')
    shop_name = models.CharField(max_length=255, blank=True, default='')
    wholesale_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    moq = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('user', 'product_id')

    def __str__(self):
        return f"{self.name or self.product_id} for {self.user_id}"


class CustomerAddress(models.Model):
    """Saved delivery address (legacy ``users/{uid}/addresses`` documents).

    The portal page addresses rows by id and sends the whole snapshot on create,
    so the model mirrors the legacy document fields one-to-one.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='addresses')
    tag = models.CharField(max_length=32, blank=True, default='Nyumbani')
    name = models.CharField(max_length=255, blank=True, default='')
    phone = models.CharField(max_length=32, blank=True, default='')
    street = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=128, blank=True, default='')
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.tag} — {self.name or self.phone} for {self.user_id}"
