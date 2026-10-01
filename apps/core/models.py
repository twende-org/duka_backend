import uuid
from django.db import models

class UUIDModel(models.Model):
    """
    Abstract base class that provides a UUID as the primary key.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    """
    Abstract base class that provides self-updating
    'created_at' and 'updated_at' fields.
    """
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class CoreModel(UUIDModel, TimeStampedModel):
    """
    Abstract base class combining UUID primary keys and timestamps.
    Most domain models should inherit from this.
    """
    class Meta:
        abstract = True


class Announcement(CoreModel):
    """Platform-wide banner shown inside the app (legacy ``announcements`` collection)."""

    TYPE_CHOICES = (
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('success', 'Success'),
        ('error', 'Alert'),
    )

    title = models.CharField(max_length=255)
    message = models.TextField()
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='info')
    active = models.BooleanField(default=True)
    # The legacy documents stamped the author's Firebase uid + email; both stay
    # as plain strings so rows survive the author being deleted.
    created_by = models.CharField(max_length=128, blank=True, default='')
    created_by_email = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({'active' if self.active else 'inactive'})"


class SupportTicket(CoreModel):
    """In-app support message submitted from the layout (legacy ``support_tickets``)."""

    user = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='support_tickets'
    )
    user_email = models.CharField(max_length=255, blank=True, default='')
    user_name = models.CharField(max_length=255, blank=True, default='')
    # App-visible (legacy) shop id, recorded for context when the ticket is filed.
    shop_id = models.CharField(max_length=128, blank=True, default='')
    message = models.TextField()
    category = models.CharField(max_length=50, default='Bug')
    route = models.CharField(max_length=255, blank=True, default='')
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.category}: {self.message[:40]}"
