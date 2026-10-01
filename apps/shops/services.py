from django.db import transaction
from rest_framework.exceptions import ValidationError
from .models import Shop, Branch, UserRole, Invitation
# pyrefly: ignore [missing-import]
from apps.users.models import User

def create_shop(*, user: User, name: str, **kwargs) -> Shop:
    """
    Service to create a new Shop.
    Automatically assigns the creator as the 'owner' and creates a Main Branch.
    """
    with transaction.atomic():
        shop = Shop.objects.create(name=name, **kwargs)
        UserRole.objects.create(user=user, shop=shop, role='owner')
        Branch.objects.create(shop=shop, name="Main Branch", is_main=True)
    return shop

def update_shop(*, shop: Shop, **data) -> Shop:
    """
    Service to update shop details.
    """
    for field, value in data.items():
        setattr(shop, field, value)
    shop.save()
    return shop

def ensure_main_branch(shop: Shop) -> Branch:
    """
    The branch shop-floor writes land on, derived on demand.

    Firestore had no branch concept — orders and sales simply hung off the shop —
    so shops imported from it may have none. ``import_firestore._main_branch``
    creates a "Main Branch" the first time one is needed; this keeps that rule
    for later arrivals instead of failing the write outright.
    """
    branch = shop.branches.filter(is_main=True).first() or shop.branches.first()
    if branch is None:
        branch = Branch.objects.create(shop=shop, name="Main Branch", is_main=True)
    return branch

@transaction.atomic
def accept_invitation(*, user: User, invitation: Invitation):
    """
    Mark an invitation accepted and grant the invited role to the user.

    Firebase's acceptInvitation() wrote the composite user_roles/{userId}_{shopId}
    document with tx.set, so a pre-existing role was overwritten rather than
    rejected — update_or_create keeps that upsert behavior.
    """
    invitation = Invitation.objects.select_for_update().get(id=invitation.id)
    if invitation.status != 'pending':
        raise ValidationError('This invitation is no longer pending.')

    role, _ = UserRole.objects.update_or_create(
        user=user, shop=invitation.shop, defaults={'role': invitation.role}
    )
    invitation.status = 'accepted'
    invitation.save(update_fields=['status', 'updated_at'])
    return invitation, role


@transaction.atomic
def decline_invitation(*, invitation: Invitation) -> Invitation:
    """Mark an invitation declined; no role is granted."""
    invitation = Invitation.objects.select_for_update().get(id=invitation.id)
    if invitation.status != 'pending':
        raise ValidationError('This invitation is no longer pending.')

    invitation.status = 'declined'
    invitation.save(update_fields=['status', 'updated_at'])
    return invitation


@transaction.atomic
def upsert_invitation(*, shop: Shop, email: str, role: str, invited_by: User) -> Invitation:
    """
    Send (or re-send) an invitation.

    (email, shop) is unique, so re-inviting an address that was already invited —
    and accepted or declined — reuses the existing row and resets it to pending.
    Firebase had no such constraint and simply allowed repeated invitations.
    """
    invitation = Invitation.objects.select_for_update().filter(shop=shop, email__iexact=email).first()
    if invitation is None:
        return Invitation.objects.create(shop=shop, email=email, role=role, invited_by=invited_by)

    invitation.email = email
    invitation.role = role
    invitation.status = 'pending'
    invitation.invited_by = invited_by
    invitation.save(update_fields=['email', 'role', 'status', 'invited_by', 'updated_at'])
    return invitation
