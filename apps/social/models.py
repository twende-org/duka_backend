from django.db import models
# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel
from apps.shops.models import Shop

class SocialIntegration(CoreModel):
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
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='social_integrations')
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    is_connected = models.BooleanField(default=False)
    page_id = models.CharField(max_length=255, blank=True, null=True)
    page_name = models.CharField(max_length=255, blank=True, null=True)
    instagram_id = models.CharField(max_length=255, blank=True, null=True)
    connected_by = models.CharField(max_length=128, blank=True, null=True)
    auto_reply_enabled = models.BooleanField(default=False)
    # Encrypted at rest with the crypto-js scheme in apps.core.crypto: the page
    # token and the long-lived user token respectively.
    access_token = models.CharField(max_length=1000, blank=True, null=True)
    refresh_token = models.CharField(max_length=1000, blank=True, null=True)
    last_sync_at = models.DateTimeField(blank=True, null=True)
    sync_status = models.CharField(max_length=20, choices=SYNC_STATUS_CHOICES, default='idle')
    
    class Meta:
        unique_together = ('shop', 'platform')
        
    def __str__(self):
        return f"{self.platform} for {self.shop.name}"


class FacebookOAuthSession(models.Model):
    """Short-lived handoff between the OAuth callback and the page picker.

    The callback has no auth context, so tokens are parked here (encrypted)
    instead of putting them in the redirect URL. Mirrors the legacy Firestore
    ``oauth_sessions`` collection.
    """
    id = models.CharField(max_length=64, primary_key=True)
    shop = models.ForeignKey(
        Shop, on_delete=models.SET_NULL, null=True, blank=True, related_name='facebook_oauth_sessions'
    )
    user_token = models.TextField()
    pages = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Facebook OAuth session {self.id}"


class SocialLog(CoreModel):
    """Outcome of a social post / auto-reply, mirroring the Firestore
    ``shops/{shopId}/social_logs`` documents written by the Cloud Functions."""

    TYPE_CHOICES = (
        ('social_post', 'Social Post'),
        ('ai_reply', 'AI Reply'),
    )
    ACTION_CHOICES = (
        ('post', 'Post'),
        ('reply', 'Reply'),
    )
    STATUS_CHOICES = (
        ('success', 'Success'),
        ('failure', 'Failure'),
    )

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='social_logs')
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

    def __str__(self):
        return f"{self.type} ({self.status}) for {self.shop.name}"


class Conversation(CoreModel):
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
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='social_conversations')
    customer_name = models.CharField(max_length=255)
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    last_message_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.customer_name} via {self.platform} on {self.shop.name}"

class Message(CoreModel):
    SENDER_CHOICES = (
        ('customer', 'Customer'),
        ('shop', 'Shop'),
    )
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender_type = models.CharField(max_length=20, choices=SENDER_CHOICES)
    content = models.TextField()
    
    class Meta:
        ordering = ['created_at']
        
    def __str__(self):
        return f"Message in {self.conversation.id} by {self.sender_type}"
