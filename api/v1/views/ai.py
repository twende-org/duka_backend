"""AI endpoints for the helpers that used to call OpenRouter from the browser.

``src/lib/ai.ts`` shipped ``VITE_OPENROUTER_API_KEY`` inside the frontend
bundle; these views run the same prompts from Django (``apps.core.assistant``)
so the key stays server-side. The frontend adapters translate the answers back
to the shapes the widgets already render.
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

# pyrefly: ignore [missing-import]
from apps.core import assistant
# pyrefly: ignore [missing-import]
from apps.core.utils import first_param


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

    Body: ``{image}`` where ``image`` is the compressed data URL the camera
    capture produced. Only the known product fields come back, as non-empty
    strings, ready to prefill the product form.
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
            details = assistant.extract_product_details(image)
        except assistant.AssistantError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        return Response({'details': details})


class AIProductListExtractionView(APIView):
    """``POST /api/v1/ai/extract-products/`` -> ``{"details": [{...}, ...]}``.

    Body: ``{image}`` where the photo may hold several distinct products.
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
            details = assistant.extract_product_list(image)
        except assistant.AssistantError as exc:
            return Response({'detail': str(exc)}, status=exc.status_code)
        return Response({'details': details})
