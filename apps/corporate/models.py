"""Corporate procurement (legacy ``companies/{companyId}`` subcollections).

The legacy app kept departments and purchase orders under
``companies/{companyId}/departments`` and ``companies/{companyId}/purchase_orders``
Firestore documents, while buyers were plain ``users`` carrying a
``corporateProfile.companyId``. There was never a reader of the company document
itself (name/credit fields live on the member's profile), so Django models only
the two subcollections and scopes them by the app-visible company id.

Ids stay the app-visible strings: ``company_id`` is the value the profile holds
and ``department_id`` on an order is the document id the buyer picked from the
department list. The order keeps ``department_name`` snapshotted, exactly like
the legacy document did, so an order renders even if the department is gone.
"""
from django.db import models

# pyrefly: ignore [missing-import]
from apps.core.models import CoreModel


class CorporateDepartment(CoreModel):
    """A budgeted unit inside a company (legacy ``companies/{id}/departments``)."""

    # App-visible company id: what ``corporateProfile.companyId`` carries.
    company_id = models.CharField(max_length=128, db_index=True)
    name = models.CharField(max_length=255)
    budget = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    spent = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.company_id})"


class CorporatePurchaseOrder(CoreModel):
    """A company purchase order awaiting approval (legacy ``purchase_orders``)."""

    APPROVAL_STATUS_CHOICES = (
        ('pending_approval', 'Pending approval'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    )

    company_id = models.CharField(max_length=128, db_index=True)
    # Storefront snapshot: the shop the buyer picked, addressed by app-visible id.
    shop_id = models.CharField(max_length=128, blank=True, default='')
    shop_name = models.CharField(max_length=255, blank=True, default='')
    buyer_id = models.CharField(max_length=128, blank=True, default='')
    buyer_name = models.CharField(max_length=255, blank=True, default='')
    buyer_phone = models.CharField(max_length=32, blank=True, default='')
    department_id = models.CharField(max_length=128, blank=True, default='')
    department_name = models.CharField(max_length=255, blank=True, default='')
    # ``[{productId, productName, quantity, price, subtotal}]`` as the checkout sent it.
    items = models.JSONField(default=list, blank=True)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    approval_status = models.CharField(
        max_length=20, choices=APPROVAL_STATUS_CHOICES, default='pending_approval'
    )
    approver_id = models.CharField(max_length=128, blank=True, default='')
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.department_name or 'PO'} — {self.total_amount} ({self.approval_status})"
