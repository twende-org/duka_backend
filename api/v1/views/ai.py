"""AI endpoints for the helpers that used to call OpenRouter from the browser.

``src/lib/ai.ts`` shipped ``VITE_OPENROUTER_API_KEY`` inside the frontend
bundle; these views run the same prompts from Django (``apps.core.assistant``)
so the key stays server-side. The frontend adapters translate the answers back
to the shapes the widgets already render. The storefront, marketplace and
search-parse endpoints are anonymous by design (they back public widgets) and
are throttled per-IP; the copy generator requires a signed-in merchant.
"""
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

# pyrefly: ignore [missing-import]
from apps.core import assistant
# pyrefly: ignore [missing-import]
from apps.core.utils import first_param

#: Longest single user message the chat endpoints forward to the model.
MAX_CHAT_MESSAGE_CHARS = 2000
#: Search query forwarded to the intent parser.
MAX_SEARCH_QUERY_CHARS = 500
#: Marketing-copy prompt forwarded to the model.
MIN_COPY_PROMPT_CHARS = 10
MAX_COPY_PROMPT_CHARS = 8000


class AIAnonThrottle(AnonRateThrottle):
    """Per-IP throttle for the anonymous AI endpoints (scope ``ai_anon``)."""

    scope = 'ai_anon'


class AICopyThrottle(UserRateThrottle):
    """Per-user throttle for the marketing copy generator (scope ``ai_copy``)."""

    scope = 'ai_copy'


def _category_hints(data):
    """The shop's own category names from the body (``categoryNames`` list)."""
    names = first_param(data, 'categoryNames', 'category_names')
    if not isinstance(names, list):
        return None
    return [name for name in (str(n).strip() for n in names) if name]


class AIAssistantView(APIView):
    """``POST /api/v1/ai/assistant/`` -> ``{"reply": "..."}``.

    Body: ``{message, shopName?, context?}`` where ``context`` is the dashboard
    snapshot the widget used to inline into the prompt. Mirrors the legacy
    ``askBusinessAssistant``: the answer may contain a ``[NAVIGATE:path]`` tag
    for the caller to act on.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        message = first_param(data, 'message')
        if not isinstance(message, str) or not message.strip():
            return Response(
                {'detail': 'message is required.'}, status=status.HTTP_400_BAD_REQUEST,
            )
        context = first_param(data, 'context') or {}
        if not isinstance(context, dict):
            return Response(
                {'detail': 'context must be an object.'}, status=status.HTTP_400_BAD_REQUEST,
            )
        shop_name = str(first_param(data, 'shopName', 'shop_name') or 'Twende Duka')
        try:
            reply = assistant.ask_assistant(shop_name, message, context)
        except assistant.AssistantError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        return Response({'reply': reply})


class AIProductExtractionView(APIView):
    """``POST /api/v1/ai/extract-product/`` -> ``{"details": {...}}``.

    Body: ``{image, categoryNames?}`` where ``image`` is the compressed data
    URL the camera capture produced. ``categoryNames`` is the shop's own
    category list, injected into the prompt so the returned ``category``
    matches the product form's dropdown. Only the known product fields come
    back, as non-empty strings, ready to prefill the product form.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        image = first_param(data, 'image')
        if not isinstance(image, str) or not image.strip():
            return Response(
                {'detail': 'image is required.'}, status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            details = assistant.extract_product_details(image, category_hints=_category_hints(data))
        except assistant.AssistantError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        return Response({'details': details})


class AIProductListExtractionView(APIView):
    """``POST /api/v1/ai/extract-products/`` -> ``{"details": [{...}, ...]}``.

    Body: ``{image, categoryNames?}`` where the photo may hold several
    distinct products and ``categoryNames`` is the shop's own category list
    (steers the returned ``category`` values to the form's dropdown).
    Distinct entries come back most prominent first, nameless ones dropped,
    capped at 20 — ready to queue into the product form.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        image = first_param(data, 'image')
        if not isinstance(image, str) or not image.strip():
            return Response(
                {'detail': 'image is required.'}, status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            details = assistant.extract_product_list(image, category_hints=_category_hints(data))
        except assistant.AssistantError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        return Response({'details': details})


def _context_or_400(data):
    """The optional ``context`` body field as a dict, or a 400 Response."""
    context = first_param(data, 'context') or {}
    if not isinstance(context, dict):
        return None, Response(
            {'detail': 'context must be an object.'}, status=status.HTTP_400_BAD_REQUEST,
        )
    return context, None


class AIPublicAssistantView(APIView):
    """``POST /api/v1/ai/public-assistant/`` -> ``{"reply": "..."}``.

    Anonymous endpoint backing the storefront widget (legacy
    ``askPublicAssistant``). Body: ``{message, shopName?, context?}`` where
    ``context`` is the public shop snapshot (products, prices, contact
    details) the prompt inlines. The reply may carry a
    ``[NAVIGATE:?productId=ID]`` tag for the caller to act on.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AIAnonThrottle]

    def post(self, request):
        data = request.data or {}
        message = first_param(data, 'message')
        if not isinstance(message, str) or not message.strip():
            return Response(
                {'detail': 'message is required.'}, status=status.HTTP_400_BAD_REQUEST,
            )
        message = message[:MAX_CHAT_MESSAGE_CHARS]
        context, error = _context_or_400(data)
        if error:
            return error
        shop_name = str(first_param(data, 'shopName', 'shop_name') or 'Twende Duka')
        try:
            reply = assistant.ask_public_assistant(shop_name, message, context)
        except assistant.AssistantError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        return Response({'reply': reply})


class AIMarketplaceAssistantView(APIView):
    """``POST /api/v1/ai/marketplace-assistant/`` -> ``{"reply": "..."}``.

    Anonymous endpoint backing the marketplace concierge widget (legacy
    ``askMarketplaceAssistant``). Body: ``{messages: [{role, content}], context}``
    where ``messages`` is the multi-turn history (``ai``/``assistant`` roles
    accepted) and ``context`` is the public shop directory snapshot. The reply
    may carry a ``[NAVIGATE:/shop/SLUG]`` tag after explicit user confirmation.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AIAnonThrottle]

    def post(self, request):
        data = request.data or {}
        messages = first_param(data, 'messages')
        if not isinstance(messages, list):
            return Response(
                {'detail': 'messages must be a list.'}, status=status.HTTP_400_BAD_REQUEST,
            )
        context, error = _context_or_400(data)
        if error:
            return error
        try:
            reply = assistant.ask_marketplace_assistant(messages, context)
        except assistant.AssistantError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        return Response({'reply': reply})


class AISearchParseView(APIView):
    """``POST /api/v1/ai/parse-search/`` -> structured search filters.

    Anonymous endpoint for the directory natural-language search (legacy
    ``parseQueryWithAI``). Body: ``{query}``; answers with the parsed fields
    (``cleanQuery``, ``category``, ``brand``, ``minPrice``, ``maxPrice``).
    Any failure — including a missing server key (503) — is reported as an
    error so the caller falls back to its local basic parser, exactly like
    the browser helper.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AIAnonThrottle]

    def post(self, request):
        data = request.data or {}
        query = first_param(data, 'query')
        if not isinstance(query, str) or not query.strip():
            return Response(
                {'detail': 'query is required.'}, status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            parsed = assistant.parse_search_query(query[:MAX_SEARCH_QUERY_CHARS])
        except assistant.AssistantError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        return Response(parsed)


class AIGenerateCopyView(APIView):
    """``POST /api/v1/ai/generate-copy/`` -> ``{"text": "..."}``.

    Authenticated endpoint for the marketing dialogs (ad generator, feed
    caption, sold-out caption) that used to call OpenRouter with the bundled
    key. Body: ``{prompt}`` — the fully built prompt; the raw completion
    comes back and each dialog strips/parses it locally as before.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [AICopyThrottle]

    def post(self, request):
        data = request.data or {}
        prompt = first_param(data, 'prompt')
        if not isinstance(prompt, str) or not (
            MIN_COPY_PROMPT_CHARS <= len(prompt.strip()) <= MAX_COPY_PROMPT_CHARS
        ):
            return Response(
                {'detail': f'prompt must be between {MIN_COPY_PROMPT_CHARS} and '
                           f'{MAX_COPY_PROMPT_CHARS} characters.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            text = assistant.generate_copy(prompt)
        except assistant.AssistantError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        return Response({'text': text})
