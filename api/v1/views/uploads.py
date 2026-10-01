"""Authenticated media uploads (replaces Firebase Storage from ``imageUtils.ts``).

The browser compresses an image, posts it here, and stores the returned URL in
``imageUrl``/``imageUrls`` fields — the same contract the Firebase SDK had, so
nothing downstream (storefront, social posting, reels) needs to change.

Files land under ``MEDIA_ROOT`` and are served in development by Django's
static view; production serves the same paths from the web server.
"""
import re
import uuid

from django.core.files.storage import default_storage
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

UPLOAD_DIRECTORY = 'uploads'
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# Content type -> stored extension. Anything else is rejected; the extension is
# chosen from this table rather than the client's filename.
ALLOWED_IMAGE_TYPES = {
    'image/jpeg': '.jpg',
    'image/jpg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
    'image/gif': '.gif',
}

# Upload kinds the UI actually uses; a stray value falls back to 'products'.
ALLOWED_KINDS = {'products', 'shops', 'expenses', 'customers', 'avatars'}


def safe_folder(raw: object) -> str:
    """Reduce the client's ``folder`` to a single whitelisted path segment."""
    for candidate in str(raw or '').lower().split('/'):
        cleaned = re.sub(r'[^a-z0-9_-]', '', candidate)[:32]
        if cleaned in ALLOWED_KINDS:
            return cleaned
    return 'products'


class MediaUploadView(APIView):
    """``POST /api/v1/uploads/`` (multipart: ``file``, optional ``folder``).

    Answers ``{"url": ..., "path": ...}`` with the absolute URL the browser
    should persist.
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        upload = request.FILES.get('file')
        if upload is None:
            return Response(
                {'detail': ['No file was submitted.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if upload.size > MAX_UPLOAD_BYTES:
            return Response(
                {'detail': ['Image is larger than 8MB.']},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        extension = ALLOWED_IMAGE_TYPES.get((upload.content_type or '').lower())
        if extension is None:
            return Response(
                {'detail': ['Unsupported image type. Use JPEG, PNG, WEBP or GIF.']},
                status=status.HTTP_400_BAD_REQUEST,
            )

        stored_name = default_storage.save(
            f'{UPLOAD_DIRECTORY}/{safe_folder(request.data.get("folder"))}/'
            f'{uuid.uuid4().hex}{extension}',
            upload,
        )
        return Response(
            {'url': request.build_absolute_uri(default_storage.url(stored_name)), 'path': stored_name},
            status=status.HTTP_201_CREATED,
        )
