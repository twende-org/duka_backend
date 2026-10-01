from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

# pyrefly: ignore [missing-import]
from apps.core.models import Announcement, SupportTicket
# pyrefly: ignore [missing-import]
from apps.core.permissions import IsPlatformAdmin
# pyrefly: ignore [missing-import]
from api.v1.serializers.support import AnnouncementSerializer, SupportTicketSerializer


def is_platform_admin(user):
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


class AnnouncementViewSet(viewsets.ModelViewSet):
    """In-app announcements.

    Reads are open to every signed-in user (the banner is part of the app
    shell); ``?active=true`` narrows to the live ones. Only platform admins may
    create, edit or delete and only they see the inactive rows.
    """

    serializer_class = AnnouncementSerializer
    http_method_names = ['get', 'post', 'patch', 'put', 'delete', 'head', 'options']

    def get_queryset(self):
        qs = Announcement.objects.all()
        if not is_platform_admin(self.request.user):
            qs = qs.filter(active=True)
        active = self.request.query_params.get('active')
        if active is not None:
            qs = qs.filter(active=active.strip().lower() in ('true', '1', 'yes'))
        return qs

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsPlatformAdmin()]

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(
            created_by=str(user.pk),
            created_by_email=user.email or '',
        )


class SupportTicketViewSet(viewsets.ModelViewSet):
    """Support messages filed from the layout.

    Any signed-in user can file one (create); listing, filtering by
    ``?resolved=`` and flipping ``resolved`` are platform-admin actions.
    """

    serializer_class = SupportTicketSerializer
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        qs = SupportTicket.objects.all()
        resolved = self.request.query_params.get('resolved')
        if resolved is not None:
            qs = qs.filter(resolved=resolved.strip().lower() in ('true', '1', 'yes'))
        return qs

    def get_permissions(self):
        if self.action == 'create':
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsPlatformAdmin()]

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(
            user=user,
            user_email=user.email or '',
            user_name=user.display_name or '',
        )
