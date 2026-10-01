"""Facebook OAuth endpoints mirroring the legacy Cloud Functions.

- ``facebookCallback`` (open, Facebook redirects the browser here),
- ``getFacebookPagesSession`` (authed, returns a bare array of safe pages),
- ``saveFacebookConnection`` (authed, owner/manager only, returns success flags),
- ``postProductToFacebook`` (authed, owner/manager only, publishes a product),
- ``facebookWebhook`` (open, Facebook calls it: GET handshake + POST events),
- ``testMarketingDrip`` (authed, owner/manager only, runs the peak-hours drip).
"""
import logging
from urllib.parse import urlencode

from django.http import HttpResponse, HttpResponseRedirect
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

# pyrefly: ignore [missing-import]
from apps.core.legacy import resolve_legacy_pk
# pyrefly: ignore [missing-import]
from apps.core.utils import first_param
# pyrefly: ignore [missing-import]
from apps.social import posting, services, webhooks
# pyrefly: ignore [missing-import]
from apps.social.tasks import run_marketing_drip
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.shops.permissions import MANAGEMENT_ROLES, assert_shop_access

logger = logging.getLogger(__name__)


class FacebookCallbackView(APIView):
    """GET /api/v1/social/facebook/callback?code=...&state=..."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        code = request.query_params.get('code')
        parsed = services.parse_oauth_state(request.query_params.get('state'))
        shop_id = parsed['shop_id']
        if not code or not shop_id:
            return HttpResponse('Missing code or shopId', status=400, content_type='text/plain')
        try:
            user_token = services.exchange_code_for_long_lived_token(code)
            pages = services.fetch_user_pages(user_token)
            session_id = services.create_oauth_session(shop_id, user_token, pages)
        except services.FacebookGraphError:
            logger.exception('Facebook OAuth callback failed')
            return HttpResponse(
                'Facebook authentication failed', status=500, content_type='text/plain'
            )
        query = urlencode({'fb_session_id': session_id, 'shopId': shop_id})
        return HttpResponseRedirect(f'{parsed["origin"]}{parsed["return_path"]}?{query}')


class FacebookPagesSessionView(APIView):
    """GET /api/v1/social/facebook/sessions/<session_id>"""

    permission_classes = [IsAuthenticated]

    def get(self, request, session_id):
        try:
            pages = services.get_session_safe_pages(session_id)
        except services.FacebookSessionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(pages)


class FacebookConnectionView(APIView):
    """POST /api/v1/social/facebook/connections"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        shop_id = first_param(data, 'shopId', 'shop_id')
        page_id = first_param(data, 'pageId', 'page_id')
        session_id = first_param(data, 'sessionId', 'session_id')
        if not shop_id or not page_id or not session_id:
            return Response(
                {'detail': 'shopId, pageId, or sessionId is missing.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = services.save_page_connection(
                request.user, str(shop_id), str(page_id), str(session_id)
            )
        except services.FacebookSessionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(result)


class FacebookPostView(APIView):
    """POST /api/v1/social/facebook/posts

    Mirror of the ``postProductToFacebook`` callable: publishes one product to
    the shop's connected page (and Instagram when a business account is linked).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        shop_id = first_param(data, 'shopId', 'shop_id')
        product_id = first_param(data, 'productId', 'product_id')
        if not shop_id or not product_id:
            return Response(
                {'detail': 'shopId or productId is missing.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        shop_pk = resolve_legacy_pk(Shop, str(shop_id))
        shop = Shop.objects.filter(pk=shop_pk).first() if shop_pk else None
        if shop is None:
            return Response({'detail': 'Shop not found.'}, status=status.HTTP_404_NOT_FOUND)
        assert_shop_access(request.user, shop.id, roles=MANAGEMENT_ROLES)

        try:
            result = posting.perform_facebook_post(
                shop.pk,
                [str(product_id)],
                first_param(data, 'message'),
                bool(first_param(data, 'includeImage', 'include_image')),
            )
        except posting.FacebookNotConnected as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except posting.ProductNotFound as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except posting.SocialPostFailed as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)


class FacebookWebhookView(APIView):
    """GET/POST /api/v1/social/facebook/webhook

    Mirror of the ``facebookWebhook`` Cloud Function: the GET handshake proves
    endpoint ownership, the POST receives page feed events and auto-replies to
    new comments. Answers are plain text (no auth, no CSRF) because Facebook,
    not a browser, is the caller.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        status_code, body = webhooks.verify_subscription(request.query_params)
        return HttpResponse(body, status=status_code, content_type='text/plain')

    def post(self, request):
        signature = request.headers.get('X-Hub-Signature-256')
        if not webhooks.signature_is_valid(request.body, signature):
            logger.warning('Facebook webhook: rejected a payload with an invalid signature.')
            return HttpResponse('Forbidden', status=403, content_type='text/plain')
        status_code = webhooks.process_event(request.data)
        body = {200: 'EVENT_RECEIVED', 404: 'Not Found', 500: 'Error processing event'}
        return HttpResponse(
            body[status_code], status=status_code, content_type='text/plain'
        )


class MarketingDripTestView(APIView):
    """POST /api/v1/social/marketing/test-drip

    Mirror of the legacy ``testMarketingDrip`` callable, which was an
    unauthenticated HTTP function that posted to *every* shop on the platform.
    Pass ``shopId`` to run the drip for one shop (owner/manager only); the
    platform-wide run requires a superuser.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        shop_id = first_param(data, 'shopId', 'shop_id')
        if shop_id:
            shop_pk = resolve_legacy_pk(Shop, str(shop_id))
            shop = Shop.objects.filter(pk=shop_pk).first() if shop_pk else None
            if shop is None:
                return Response({'detail': 'Shop not found.'}, status=status.HTTP_404_NOT_FOUND)
            assert_shop_access(request.user, shop.id, roles=MANAGEMENT_ROLES)
            results = run_marketing_drip(shop_ids=[shop.pk])
        else:
            if not request.user.is_superuser:
                return Response(
                    {'detail': 'shopId is missing.'}, status=status.HTTP_403_FORBIDDEN
                )
            results = run_marketing_drip()
        return Response({
            'success': True,
            'message': 'Marketing Drip Triggered Successfully. Check logs to verify.',
            'results': results,
        })
