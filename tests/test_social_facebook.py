import base64
import json
from unittest.mock import Mock, patch

import requests
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.core.crypto import decrypt_token, encrypt_token
# pyrefly: ignore [missing-import]
from apps.social.models import FacebookOAuthSession, SocialIntegration
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.users.models import User


def graph_response(payload):
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


LONG_LIVED_TOKEN = 'EAAG-long-lived-user-token'
PAGE_TOKEN = 'EAAG-page-token'


class FacebookOAuthTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='owner', email='owner@example.com', password='password',
            firebase_uid='firebase-uid-1',
        )
        self.shop = Shop.objects.create(name='Test Shop', legacy_id='fb-shop-1')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.callback_url = reverse('facebook-callback')
        self.connections_url = reverse('facebook-connections')

    def state(self, **overrides):
        payload = {
            'shopId': self.shop.legacy_id,
            'origin': 'https://duka.twendedigital.tech',
            'returnPath': '/dashboard/shops',
            **overrides,
        }
        return base64.b64encode(json.dumps(payload).encode('utf-8')).decode('ascii')

    def pages(self):
        return [{'id': 'page-1', 'name': 'Page One', 'category': 'Retail', 'access_token': PAGE_TOKEN}]

    def session(self, session_id='a' * 32, user_token=None, pages=None):
        return FacebookOAuthSession.objects.create(
            id=session_id,
            shop=self.shop,
            user_token=user_token if user_token is not None else encrypt_token(LONG_LIVED_TOKEN),
            pages=pages if pages is not None else self.pages(),
        )


class FacebookCallbackTests(FacebookOAuthTests):
    @patch('apps.social.services.requests.get')
    def test_callback_redirects_with_session(self, mock_get):
        mock_get.side_effect = [
            graph_response({'access_token': 'short-lived'}),
            graph_response({'access_token': LONG_LIVED_TOKEN}),
            graph_response({'data': self.pages()}),
        ]
        response = self.client.get(self.callback_url, {'code': 'code-1', 'state': self.state()})

        self.assertEqual(response.status_code, 302)
        session = FacebookOAuthSession.objects.get()
        self.assertRegex(session.id, r'^[0-9a-f]{32}$')
        self.assertEqual(session.shop, self.shop)
        self.assertEqual(decrypt_token(session.user_token), LONG_LIVED_TOKEN)
        self.assertEqual(
            response['Location'],
            f'https://duka.twendedigital.tech/dashboard/shops?fb_session_id={session.id}&shopId=fb-shop-1',
        )

    @patch('apps.social.services.requests.get')
    def test_callback_accepts_raw_legacy_state(self, mock_get):
        mock_get.side_effect = [
            graph_response({'access_token': 'short-lived'}),
            graph_response({'access_token': LONG_LIVED_TOKEN}),
            graph_response({'data': self.pages()}),
        ]
        response = self.client.get(self.callback_url, {'code': 'code-1', 'state': 'fb-shop-1'})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('https://duka.twendedigital.tech/dashboard/shops?'))
        self.assertIn('shopId=fb-shop-1', response['Location'])

    @patch('apps.social.services.requests.get')
    def test_callback_ignores_foreign_redirect_origin(self, mock_get):
        mock_get.side_effect = [
            graph_response({'access_token': 'short-lived'}),
            graph_response({'access_token': LONG_LIVED_TOKEN}),
            graph_response({'data': self.pages()}),
        ]
        state = self.state(origin='https://evil.example', returnPath='//evil.example/steal')
        response = self.client.get(self.callback_url, {'code': 'code-1', 'state': state})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('https://duka.twendedigital.tech/dashboard/shops?'))

    def test_callback_without_code_is_plain_400(self):
        response = self.client.get(self.callback_url, {'state': self.state()})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, b'Missing code or shopId')

    def test_callback_without_shop_is_plain_400(self):
        response = self.client.get(self.callback_url, {'code': 'code-1'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, b'Missing code or shopId')

    @patch('apps.social.services.requests.get')
    def test_callback_graph_failure_is_plain_500(self, mock_get):
        mock_get.return_value = graph_response({'error': {'message': 'bad code'}})
        mock_get.return_value.raise_for_status.side_effect = requests.HTTPError('400')
        response = self.client.get(self.callback_url, {'code': 'code-1', 'state': self.state()})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.content, b'Facebook authentication failed')

    @patch('apps.social.services.requests.get')
    def test_callback_uses_configured_graph_version(self, mock_get):
        mock_get.side_effect = [
            graph_response({'access_token': 'short-lived'}),
            graph_response({'access_token': LONG_LIVED_TOKEN}),
            graph_response({'data': self.pages()}),
        ]
        self.client.get(self.callback_url, {'code': 'code-1', 'state': self.state()})
        self.assertEqual(mock_get.call_count, 3)
        self.assertIn('/v18.0/oauth/access_token', mock_get.call_args_list[0].args[0])


class FacebookSessionPagesTests(FacebookOAuthTests):
    def test_pages_require_authentication(self):
        session = self.session()
        response = self.client.get(reverse('facebook-session-pages', args=[session.id]))
        self.assertEqual(response.status_code, 401)

    def test_pages_are_a_bare_safe_array(self):
        session = self.session()
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse('facebook-session-pages', args=[session.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            [{'id': 'page-1', 'name': 'Page One', 'category': 'Retail'}],
        )

    def test_unknown_session_is_404(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse('facebook-session-pages', args=['f' * 32]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.data['detail'], 'Session expired or invalid. Please connect again.'
        )


class FacebookConnectionSaveTests(FacebookOAuthTests):
    @patch('apps.social.services.requests.get')
    def test_save_connection_stores_encrypted_tokens(self, mock_get):
        mock_get.return_value = graph_response({'instagram_business_account': {'id': 'ig-1'}})
        session = self.session()
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.connections_url, {
            'shopId': self.shop.legacy_id,
            'pageId': 'page-1',
            'sessionId': session.id,
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'success': True, 'instagramLinked': True})
        integration = SocialIntegration.objects.get(shop=self.shop, platform='facebook')
        self.assertTrue(integration.is_connected)
        self.assertEqual(integration.page_id, 'page-1')
        self.assertEqual(integration.page_name, 'Page One')
        self.assertEqual(integration.instagram_id, 'ig-1')
        self.assertEqual(integration.connected_by, 'firebase-uid-1')
        self.assertEqual(decrypt_token(integration.access_token), PAGE_TOKEN)
        self.assertEqual(decrypt_token(integration.refresh_token), LONG_LIVED_TOKEN)
        self.assertFalse(FacebookOAuthSession.objects.filter(id=session.id).exists())

    @patch('apps.social.services.requests.get')
    def test_save_without_instagram_link_reports_false(self, mock_get):
        mock_get.return_value = graph_response({})
        session = self.session()
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.connections_url, {
            'shopId': self.shop.legacy_id,
            'pageId': 'page-1',
            'sessionId': session.id,
        }, format='json')

        self.assertEqual(response.data, {'success': True, 'instagramLinked': False})
        self.assertIsNone(SocialIntegration.objects.get().instagram_id)

    def test_save_requires_management_role(self):
        staff = User.objects.create_user(username='staff', email='staff@example.com', password='password')
        UserRole.objects.create(user=staff, shop=self.shop, role='staff')
        session = self.session()
        self.client.force_authenticate(user=staff)

        response = self.client.post(self.connections_url, {
            'shopId': self.shop.legacy_id,
            'pageId': 'page-1',
            'sessionId': session.id,
        }, format='json')

        self.assertEqual(response.status_code, 403)
        self.assertFalse(SocialIntegration.objects.exists())

    def test_save_with_missing_fields_is_400(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.connections_url, {'shopId': self.shop.legacy_id}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_save_with_unknown_session_is_404(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.connections_url, {
            'shopId': self.shop.legacy_id,
            'pageId': 'page-1',
            'sessionId': 'f' * 32,
        }, format='json')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['detail'], 'Session expired or invalid. Please reconnect.')

    def test_save_with_page_missing_from_session_is_404(self):
        session = self.session()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.connections_url, {
            'shopId': self.shop.legacy_id,
            'pageId': 'other-page',
            'sessionId': session.id,
        }, format='json')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['detail'], 'Selected page not found in session.')

    @patch('apps.social.services.requests.get')
    def test_save_accepts_snake_case_keys(self, mock_get):
        mock_get.return_value = graph_response({})
        session = self.session()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.connections_url, {
            'shop_id': self.shop.legacy_id,
            'page_id': 'page-1',
            'session_id': session.id,
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SocialIntegration.objects.filter(platform='facebook').exists())
