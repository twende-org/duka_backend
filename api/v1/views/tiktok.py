"""TikTok OAuth + posting endpoints, mirroring the legacy Facebook Cloud Functions.

- ``tiktokCallback`` (open, TikTok redirects the browser here),
- ``getTikTokSession`` (authed, returns the sanitized account blob),
- ``saveTikTokConnection`` (authed, owner/manager only, returns success flags),
- ``postProductToTikTok`` (authed, owner/manager only, publishes a product video).

The authorize URL itself is built in the frontend dialog (the Facebook flow
does the same), so the callback only has to exchange the code, stash the
account in a session row, and bounce back to the app.
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
from apps.social import posting, services, tiktok
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.shops.permissions import MANAGEMENT_ROLES, assert_shop_access

logger = logging.getLogger(__name__)


def _bool_param(data, *keys):
    """Read a boolean option sent as a real bool or a string; ``None`` if absent."""
    for key in keys:
        if key not in data or data[key] is None:
            continue
        value = data[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ('true', '1', 'yes', 'on'):
                return True
            if lowered in ('false', '0', 'no', 'off'):
                return False
        return None
    return None


class TikTokCallbackView(APIView):
    """GET /api/v1/social/tiktok/callback?code=...&state=..."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        code = request.query_params.get('code')
        parsed = services.parse_oauth_state(request.query_params.get('state'))
        shop_id = parsed['shop_id']
        if not code or not shop_id:
            return HttpResponse('Missing code or shopId', status=400, content_type='text/plain')
        try:
            tokens = tiktok.exchange_code_for_token(code, parsed['code_verifier'])
            user = tiktok.fetch_tiktok_user(tokens['access_token'])
            session_id = tiktok.create_tiktok_session(shop_id, tokens, user)
        except tiktok.TikTokApiError:
            logger.exception('TikTok OAuth callback failed')
            return HttpResponse(
                'TikTok authentication failed', status=500, content_type='text/plain'
            )
        query = urlencode({'tt_session_id': session_id, 'shopId': shop_id})
        return HttpResponseRedirect(f'{parsed["origin"]}{parsed["return_path"]}?{query}')


class TikTokSessionView(APIView):
    """GET /api/v1/social/tiktok/sessions/<session_id>"""

    permission_classes = [IsAuthenticated]

    def get(self, request, session_id):
        try:
            account = tiktok.get_tiktok_session_user(session_id)
        except tiktok.TikTokSessionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(account)


class TikTokConnectionView(APIView):
    """POST /api/v1/social/tiktok/connections"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        shop_id = first_param(data, 'shopId', 'shop_id')
        session_id = first_param(data, 'sessionId', 'session_id')
        if not shop_id or not session_id:
            return Response(
                {'detail': 'shopId or sessionId is missing.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = tiktok.save_tiktok_connection(
                request.user, str(shop_id), str(session_id)
            )
        except tiktok.TikTokSessionError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(result)


class TikTokCreatorInfoView(APIView):
    """GET /api/v1/social/tiktok/creator-info?shopId=...

    Proxies TikTok's creator_info query for the shop's connected account so
    the pre-publish sheet can render per the Content Sharing Guidelines:
    creator nickname, selectable privacy levels, disabled interactions and
    the account's maximum video duration.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        shop_id = first_param(request.query_params, 'shopId', 'shop_id')
        shop_pk = resolve_legacy_pk(Shop, str(shop_id)) if shop_id else None
        shop = Shop.objects.filter(pk=shop_pk).first() if shop_pk else None
        if shop is None:
            return Response({'detail': 'Shop not found.'}, status=status.HTTP_404_NOT_FOUND)
        assert_shop_access(request.user, shop.id, roles=MANAGEMENT_ROLES)

        from apps.social.models import SocialIntegration

        integration = SocialIntegration.objects.filter(
            shop=shop, platform='tiktok', is_connected=True,
        ).first()
        if integration is None:
            return Response(
                {'detail': 'tiktok-not-connected'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            access_token = tiktok.ensure_fresh_access_token(integration)
            info = tiktok.fetch_creator_info(access_token)
        except tiktok.TikTokApiError as exc:
            logger.warning('TikTok creator info failed for shop %s: %s', shop.pk, exc)
            return Response(
                {'detail': f'TikTok creator info failed: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(info)


class TikTokPostView(APIView):
    """POST /api/v1/social/tiktok/posts

    Publishes one product to the shop's connected TikTok account as a video
    (the same reel pipeline the Facebook reel format uses). The pre-publish
    sheet forwards the user's own privacy/interaction/disclosure choices; an
    unknown privacy_level is rejected before any work is done.
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

        post_options = self._parse_post_options(data)
        if post_options is None:
            return Response(
                {'detail': 'Unsupported privacyLevel.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        shop_pk = resolve_legacy_pk(Shop, str(shop_id))
        shop = Shop.objects.filter(pk=shop_pk).first() if shop_pk else None
        if shop is None:
            return Response({'detail': 'Shop not found.'}, status=status.HTTP_404_NOT_FOUND)
        assert_shop_access(request.user, shop.id, roles=MANAGEMENT_ROLES)

        try:
            result = posting.perform_tiktok_post(
                shop.pk,
                [str(product_id)],
                first_param(data, 'message'),
                post_options=post_options or None,
            )
        except posting.TikTokNotConnected as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except (posting.TikTokVideoTooLong, posting.TikTokPrivacyUnavailable) as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except posting.ProductNotFound as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except posting.SocialPostFailed as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)

    @staticmethod
    def _parse_post_options(data):
        """Build the video-init kwargs from the request, or ``None`` if invalid."""
        privacy_level = first_param(data, 'privacyLevel', 'privacy_level')
        if privacy_level and privacy_level not in tiktok.ALLOWED_PRIVACY_LEVELS:
            return None
        options = {}
        if privacy_level:
            options['privacy_level'] = privacy_level
        for request_key, kwarg in (
            ('disableComment', 'disable_comment'),
            ('disableDuet', 'disable_duet'),
            ('disableStitch', 'disable_stitch'),
            ('brandContent', 'brand_content'),
            ('brandOrganic', 'brand_organic'),
        ):
            value = _bool_param(data, request_key, kwarg)
            if value is not None:
                options[kwarg] = value
        return options
