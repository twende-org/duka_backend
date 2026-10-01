from rest_framework.permissions import BasePermission


class IsPlatformAdmin(BasePermission):
    """Django ``is_staff`` is the platform-admin flag (the Firestore ``admins``
    collection in the legacy app); only these users see the admin API surface."""

    message = 'Platform admin access required.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))
