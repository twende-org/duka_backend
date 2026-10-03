"""Twende Duka AI intake endpoints (spec Part 1).

``POST /api/v1/inventory/intake/`` accepts compressed invoice images (multipart
``images``), media file URLs (``imageUrls``) or raw QR payloads (``qrPayloads``)
— never a wholesaler software integration. Creation answers 202 immediately;
parsing happens on a background thread and the client polls the batch.
"""
from django.core.exceptions import ValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError as DRFValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api.v1.serializers.intake import IntakeBatchSerializer, ProductDraftSerializer
from apps.core.legacy import LegacyLookupMixin, filter_by_ref
from apps.core.utils import first_param
from apps.intake.models import IntakeBatch, ProductDraft
from apps.intake.quota import ai_images_used, get_shop_limit
from apps.intake.services import apply_intake_batch, create_intake_batch, dispatch_intake_processing
from apps.shops.models import Shop
from apps.shops.permissions import ShopScopedQuerysetMixin, assert_shop_access

MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/jpg', 'image/png', 'image/webp', 'image/gif'}


def _as_list(value):
    """One spelling for multipart (QueryDict) and JSON (list) payloads."""
    if value is None:
        return []
    if hasattr(value, 'getlist'):
        return value.getlist('qrPayloads') or value.getlist('qr_payloads') or value.getlist('qrPayload[]') or []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _image_files(request):
    files = []
    for key in ('images', 'images[]', 'image'):
        files.extend(request.FILES.getlist(key))
    return files


def _url_values(request):
    data = request.data
    values = []
    for key in ('imageUrls', 'image_urls', 'imageUrl'):
        raw = data.get(key) if hasattr(data, 'get') else None
        values.extend(_as_list(raw))
    return values


def _qr_values(request):
    data = request.data
    if hasattr(data, 'getlist'):
        return data.getlist('qrPayloads') or data.getlist('qr_payloads') or data.getlist('qrPayload')
    return _as_list(data.get('qrPayloads') or data.get('qrPayload'))


class IntakeBatchViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    queryset = IntakeBatch.objects.all()
    serializer_class = IntakeBatchSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    # Batches are created, polled and applied; edits happen on the drafts.
    http_method_names = ['get', 'post', 'head', 'options']
    shop_paths = ('shop_id',)

    def get_queryset(self):
        queryset = self.scope_queryset(
            super().get_queryset().select_related('shop', 'created_by').prefetch_related('drafts'))
        shop_id = first_param(self.request.query_params, 'shop_id', 'shopId')
        if shop_id:
            queryset = filter_by_ref(queryset, 'shop_id', shop_id, Shop)
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        shop = serializer.validated_data['shop']
        assert_shop_access(request.user, shop.id)

        images = _image_files(request)
        for upload in images:
            if (upload.content_type or '').lower() not in ALLOWED_IMAGE_TYPES:
                raise DRFValidationError(
                    {'images': [f'{upload.name}: unsupported type; use JPEG, PNG, WEBP or GIF.']})
            if upload.size > MAX_IMAGE_BYTES:
                raise DRFValidationError(
                    {'images': [f'{upload.name}: larger than 8MB.']})

        try:
            batch = create_intake_batch(
                shop,
                request.user if request.user.is_authenticated else None,
                images=images,
                image_urls=_url_values(request),
                qr_payloads=_qr_values(request),
                note=serializer.validated_data.get('source_note', ''),
            )
        except ValidationError as exc:
            raise DRFValidationError(exc.message_dict if hasattr(exc, 'message_dict') else str(exc))
        dispatch_intake_processing(batch.id)
        return Response(self.get_serializer(batch).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'])
    def apply(self, request, pk=None):
        """Push the completed drafts into the live product + inventory tables."""
        batch = self.get_object()
        assert_shop_access(request.user, batch.shop_id)
        try:
            summary = apply_intake_batch(batch, request.user)
        except ValidationError as exc:
            raise DRFValidationError(str(exc))
        batch.refresh_from_db()
        return Response({
            'summary': summary,
            'batch': self.get_serializer(batch).data,
        })


class IntakeQuotaView(APIView):
    """Monthly AI photo allowance for one shop (merchant badge + admin editor).

    Staff/superusers may read any shop; everyone else needs a role in the shop.
    ``limit``/``remaining`` are null when the shop is unlimited.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        shop_ref = first_param(request.query_params, 'shop_id', 'shopId')
        if not shop_ref:
            raise DRFValidationError({'shop_id': ['This query parameter is required.']})
        shop = filter_by_ref(Shop.objects.all(), 'id', shop_ref, Shop).first()
        if shop is None:
            raise NotFound('Shop not found.')
        if not (request.user.is_staff or request.user.is_superuser):
            assert_shop_access(request.user, shop.id)
        limit = get_shop_limit(shop)
        used = ai_images_used(shop)
        return Response({
            'limit': limit,
            'used': used,
            'remaining': None if limit is None else max(0, limit - used),
        })


class ProductDraftViewSet(LegacyLookupMixin, ShopScopedQuerysetMixin, viewsets.ModelViewSet):
    """Merchant corrections on staged rows before the batch is applied."""
    queryset = ProductDraft.objects.all()
    serializer_class = ProductDraftSerializer
    permission_classes = [IsAuthenticated]
    # Drafts are reviewed (list/read), corrected (patch) or discarded (delete).
    http_method_names = ['get', 'patch', 'delete', 'head', 'options']
    shop_paths = ('batch__shop_id',)

    def get_queryset(self):
        queryset = self.scope_queryset(
            super().get_queryset().select_related('batch', 'applied_product'))
        batch_id = first_param(self.request.query_params, 'batch_id', 'batchId')
        if batch_id:
            queryset = filter_by_ref(queryset, 'batch_id', batch_id, IntakeBatch)
        return queryset

    def _assert_editable(self, batch):
        if batch.status == 'applied':
            raise DRFValidationError({'detail': 'This batch has been applied; drafts are frozen.'})
        if batch.status == 'processing':
            raise DRFValidationError({'detail': 'Extraction is still running; try again shortly.'})

    def perform_update(self, serializer):
        self._assert_editable(serializer.instance.batch)
        serializer.save()

    def perform_destroy(self, instance):
        self._assert_editable(instance.batch)
        instance.delete()
