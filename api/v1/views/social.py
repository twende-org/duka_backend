from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from django.utils import timezone
# pyrefly: ignore [missing-import]
from apps.core.legacy import filter_by_ref, resolve_legacy_pk
# pyrefly: ignore [missing-import]
from apps.core.utils import first_param
# pyrefly: ignore [missing-import]
from apps.social.models import SocialIntegration, Conversation, Message, SocialLog
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.shops.permissions import MANAGEMENT_ROLES, ShopScopedQuerysetMixin, assert_shop_access
# pyrefly: ignore [missing-import]
from api.v1.serializers.social import (
    SocialIntegrationSerializer, ConversationSerializer, MessageSerializer, SocialLogSerializer,
)

class SocialIntegrationViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = SocialIntegrationSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(SocialIntegration.objects.all())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            return filter_by_ref(qs, 'shop_id', shop_id, Shop)
        return qs

    def perform_create(self, serializer):
        # The frontend posts Firebase shop ids, so resolve before access checks.
        shop = serializer.validated_data.get('shop')
        assert_shop_access(self.request.user, shop.id, roles=MANAGEMENT_ROLES)
        serializer.save()

    @action(detail=True, methods=['post'])
    def connect_account(self, request, pk=None):
        """Mock endpoint to connect an account."""
        integration = self.get_object()
        page_id = request.data.get('page_id')
        page_name = request.data.get('page_name')
        
        integration.is_connected = True
        if page_id:
            integration.page_id = page_id
        if page_name:
            integration.page_name = page_name
            
        integration.save()
        serializer = self.get_serializer(integration)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def disconnect_account(self, request, pk=None):
        """Mock endpoint to disconnect an account."""
        integration = self.get_object()
        integration.is_connected = False
        integration.page_id = None
        integration.page_name = None
        integration.save()
        serializer = self.get_serializer(integration)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def sync_catalog(self, request, pk=None):
        """Mock endpoint to trigger catalog sync."""
        integration = self.get_object()
        integration.last_sync_at = timezone.now()
        integration.sync_status = 'success'
        integration.save()
        serializer = self.get_serializer(integration)
        return Response(serializer.data)

class SocialLogViewSet(ShopScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    """Outcome log of social posts / AI replies, read by the social dashboard.

    Rows are produced by the posting tasks, so the endpoint is read-only and
    scoped to the shops the caller belongs to.
    """

    serializer_class = SocialLogSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(SocialLog.objects.all())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            return filter_by_ref(qs, 'shop_id', shop_id, Shop)
        return qs


class ConversationViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = ConversationSerializer
    shop_paths = ('shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(Conversation.objects.all())
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            return filter_by_ref(qs, 'shop_id', shop_id, Shop)
        return qs

    def perform_create(self, serializer):
        shop = serializer.validated_data.get('shop')
        assert_shop_access(self.request.user, shop.id)
        serializer.save()

class MessageViewSet(ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = MessageSerializer
    shop_paths = ('conversation__shop_id',)

    def get_queryset(self):
        qs = self.scope_queryset(Message.objects.all())
        conversation_id = first_param(self.request.query_params, 'conversation_id', 'conversationId')
        if conversation_id:
            return filter_by_ref(qs, 'conversation_id', conversation_id, Conversation)
        return qs

    def perform_create(self, serializer):
        # conversation is read-only on the serializer, so it arrives as a body
        # field, possibly as a Firebase legacy id.
        conversation_ref = first_param(self.request.data, 'conversation_id', 'conversationId')
        conversation = None
        if conversation_ref:
            pk = resolve_legacy_pk(Conversation, conversation_ref)
            conversation = Conversation.objects.filter(pk=pk).first() if pk else None
        if conversation is None:
            raise ValidationError({'conversationId': 'Unknown conversation.'})
        assert_shop_access(self.request.user, conversation.shop_id)
        Conversation.objects.filter(id=conversation.id).update(last_message_at=timezone.now())
        serializer.save(conversation=conversation)
