from django.conf import settings
from django.db import models

# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch
# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel


class Expense(CoreModel):
    """Shop expense, mirrors the Firebase `expenses` docs written by addExpenseWithSummary."""

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='expenses')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses')
    shift = models.ForeignKey('sales.Shift', on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses')

    category = models.CharField(max_length=100)
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField()
    payment_method = models.CharField(max_length=50, default='Taslimu')

    reference = models.CharField(max_length=255, blank=True, default='')
    paid_to = models.CharField(max_length=255, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    is_recurring = models.BooleanField(default=False)

    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses'
    )

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"{self.description} ({self.amount})"
