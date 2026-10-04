"""Social posting + webhook chat schema (Django port of the legacy Firestore
collections: ``social_integrations``, ``oauth_sessions``, ``social_logs``).

Storage contracts enforced here:
- Token columns hold CIPHERTEXT, never plaintext — the crypto-js-compatible
  AES-256-CBC envelope from ``apps.core.crypto`` (``encrypt_token`` /
  ``decrypt_token``). Callers that already wrap/unwrap keep working unchanged;
  the model helpers are the clean envelope interface for new code.
- ``SocialLog.status`` values are lowercase strings because ``posting.py``
  writes ``'success'`` / ``'failure'`` literally and existing rows + tests
  depend on those values. The UPPERCASE Python names satisfy the SUCCESS /
  FAILED / PARTIAL enum contract.
"""
from django.db import models

# pyrefly: ignore [missing-import]
from apps.core.crypto import decrypt_token, encrypt_token
# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop


class SocialIntegration(CoreModel):
    """A shop's connected social account (Facebook Page today; the platform
    column keeps the multi-platform door open the way PLATFORM_CHOICES did)."""

    PLATFORM_CHOICES = (
        ('meta', 'Meta/Facebook'),
        ('facebook', 'Facebook Page'),
        ('instagram', 'Instagram'),
        ('tiktok', 'TikTok'),
    )
    SYNC_STATUS_CHOICES = (
        ('idle', 'Idle'),
        ('syncing', 'Syncing'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    )

    shop = models.ForeignKey(
        Shop, on_delete=models.CASCADE, related_name='social_integrations'
    )
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    is_connected = models.BooleanField(default=False)
    # Indexed: the comment webhook does an exact-match lookup on page_id for
    # EVERY incoming comment event — this must not scan.
    page_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    page_name = models.CharField(max_length=255, blank=True, null=True)
    instagram_id = models.CharField(max_length=255, blank=True, null=True)
    connected_by = models.CharField(max_length=128, blank=True, null=True)
    auto_reply_enabled = models.BooleanField(default=False)
    # Ciphertext columns (AES-256-CBC OpenSSL envelope via apps.core.crypto).
    # Use set_access_token()/get_access_token() — never assign plaintext.
    access_token = models.CharField(max_length=1000, blank=True, null=True)
    refresh_token = models.CharField(max_length=1000, blank=True, null=True)
    last_sync_at = models.DateTimeField(blank=True, null=True)
    sync_status = models.CharField(
        max_length=20, choices=SYNC_STATUS_CHOICES, default='idle'
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['shop', 'platform'],
                name='socialint_shop_platform_uniq',
            ),
        ]

    def __str__(self):
        return f"{self.platform} for {self.shop.name}"

    # --- crypto envelope wrapper (apps.core.crypto) -------------------------
    def set_access_token(self, plaintext):
        self.access_token = encrypt_token(plaintext)

    def get_access_token(self):
        return decrypt_token(self.access_token)

    def set_refresh_token(self, plaintext):
        self.refresh_token = encrypt_token(plaintext)

    def get_refresh_token(self):
        return decrypt_token(self.refresh_token)


class FacebookOAuthSession(models.Model):
    """Short-lived handoff between the OAuth callback and the page picker.

    The callback has no auth context, so the user token is parked here
    (encrypted) instead of the redirect URL. Mirrors the legacy Firestore
    ``oauth_sessions`` documents. Required by ``services.py`` — kept even
    though it is not part of the posting schema proper.
    """

    id = models.CharField(max_length=64, primary_key=True)
    shop = models.ForeignKey(
        Shop,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='facebook_oauth_sessions',
    )
    user_token = models.TextField()
    pages = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Facebook OAuth session {self.id}"


class SocialLog(CoreModel):
    """Immutable outcome of a social post / AI reply (port of the Firestore
    ``shops/{shopId}/social_logs`` documents written by the Cloud Functions).

    Rows are written only by the posting pipeline; the dashboard and metrics
    polling read heavily by (shop, recency) and (status, recency), which the
    indexes below are shaped for.
    """

    SUCCESS = 'success'
    FAILED = 'failure'
    PARTIAL = 'partial'
    STATUS_CHOICES = (
        (SUCCESS, 'Success'),
        (FAILED, 'Failure'),
        (PARTIAL, 'Partial'),
    )
    TYPE_CHOICES = (
        ('social_post', 'Social Post'),
        ('ai_reply', 'AI Reply'),
    )
    ACTION_CHOICES = (
        ('post', 'Post'),
        ('reply', 'Reply'),
    )

    shop = models.ForeignKey(
        Shop, on_delete=models.CASCADE, related_name='social_logs'
    )
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='social_post')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, default='post')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    product_ids = models.JSONField(default=list, blank=True)
    facebook_post_id = models.CharField(max_length=255, blank=True, null=True)
    instagram_post_id = models.CharField(max_length=255, blank=True, null=True)
    error = models.TextField(blank=True, null=True)
    video_fallback_reason = models.TextField(blank=True, null=True)
    instagram_error = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # created_at is inherited from TimeStampedModel (abstract), so a
            # per-field db_index cannot be redeclared here — Meta.indexes is
            # the correct mechanism for the "index created_at + status" spec.
            models.Index(fields=['-created_at'], name='sociallog_created_idx'),
            models.Index(
                fields=['status', '-created_at'], name='sociallog_status_created_idx'
            ),
            models.Index(
                fields=['shop', '-created_at'], name='sociallog_shop_created_idx'
            ),
        ]

    def __str__(self):
        return f"{self.type} ({self.status}) for {self.shop.name}"


class Conversation(CoreModel):
    """A customer DM thread on one platform, per shop (stub for the
    interactive DM processing layer)."""

    PLATFORM_CHOICES = (
        ('whatsapp', 'WhatsApp'),
        ('instagram', 'Instagram DM'),
        ('facebook', 'Facebook Messenger'),
        ('sms', 'SMS'),
    )
    STATUS_CHOICES = (
        ('open', 'Open'),
        ('resolved', 'Resolved'),
        ('spam', 'Spam'),
    )

    shop = models.ForeignKey(
        Shop, on_delete=models.CASCADE, related_name='social_conversations'
    )
    customer_name = models.CharField(max_length=255)
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='open'
    )
    # Indexed: inbox lists order by recency and every new message updates it.
    last_message_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-last_message_at']

    def __str__(self):
        return f"{self.customer_name} via {self.platform} on {self.shop.name}"


class Message(CoreModel):
    """One message inside a Conversation (stub for future DM processing)."""

    SENDER_CHOICES = (
        ('customer', 'Customer'),
        ('shop', 'Shop'),
    )

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='messages'
    )
    sender_type = models.CharField(max_length=20, choices=SENDER_CHOICES)
    content = models.TextField()

    class Meta:
        ordering = ['created_at']
        indexes = [
            # The history endpoint paginates per conversation in created order.
            models.Index(
                fields=['conversation', 'created_at'], name='socialmsg_conv_created_idx'
            ),
        ]

    def __str__(self):
        return f"Message in {self.conversation.id} by {self.sender_type}"
