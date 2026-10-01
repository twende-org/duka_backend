"""Tests for the media-upload endpoint that replaced Firebase Storage."""
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from api.v1.views.uploads import MAX_UPLOAD_BYTES, safe_folder

User = get_user_model()

# A 1x1 transparent PNG — enough for the storage backend, which does not decode.
PNG_BYTES = bytes.fromhex(
    '89504e470d0a1a0a0000000d494844520000000100000001080600000'
    '01f15c4890000000a49444154789c6300010000050001'
    '0d0a2db40000000049454e44ae426082'
)


class MediaUploadViewTest(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix='biashara-uploads-test-')
        self.addCleanup(shutil.rmtree, self.media_root, True)
        self.media_override = override_settings(MEDIA_ROOT=self.media_root)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.client = APIClient()
        self.url = '/api/v1/uploads/'
        self.user = User.objects.create_user(
            username='uploader@example.com', email='uploader@example.com', password='secret123',
        )

    def _post(self, **overrides):
        payload = {
            'file': SimpleUploadedFile('foto.png', PNG_BYTES, content_type='image/png'),
        }
        payload.update(overrides)
        return self.client.post(self.url, payload, format='multipart')

    def test_requires_authentication(self):
        response = self._post()
        self.assertEqual(response.status_code, 401)

    def test_upload_returns_media_url(self):
        self.client.force_authenticate(user=self.user)
        response = self._post(folder='products')
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data['url'].endswith('.png'))
        self.assertTrue(response.data['path'].startswith('uploads/products/'))

        from django.core.files.storage import default_storage

        self.assertTrue(default_storage.exists(response.data['path']))

    def test_unknown_folder_falls_back_to_products(self):
        self.client.force_authenticate(user=self.user)
        response = self._post(folder='../../etc')
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data['path'].startswith('uploads/products/'))

    def test_rejects_missing_file(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {}, format='multipart')
        self.assertEqual(response.status_code, 400)

    def test_rejects_unsupported_content_type(self):
        self.client.force_authenticate(user=self.user)
        response = self._post(
            file=SimpleUploadedFile('evil.svg', b'<svg/>', content_type='image/svg+xml')
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_oversized_file(self):
        self.client.force_authenticate(user=self.user)
        oversized = SimpleUploadedFile(
            'big.jpg', b'\xff' * (MAX_UPLOAD_BYTES + 1), content_type='image/jpeg'
        )
        response = self._post(file=oversized)
        self.assertEqual(response.status_code, 413)


class SafeFolderTest(TestCase):
    def test_keeps_whitelisted_kind(self):
        self.assertEqual(safe_folder('shops'), 'shops')
        self.assertEqual(safe_folder('products/1e2a-uuid'), 'products')

    def test_blocks_traversal_and_unknown_kinds(self):
        self.assertEqual(safe_folder('..\\..\\windows'), 'products')
        self.assertEqual(safe_folder('../../etc'), 'products')
        self.assertEqual(safe_folder(None), 'products')
