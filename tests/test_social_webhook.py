import hashlib
import hmac
import json
from unittest.mock import Mock, patch

import requests
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.core.crypto import encrypt_token
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.social import webhooks
# pyrefly: ignore [missing-import]
from apps.social.models import SocialIntegration

PAGE_ID = 'page-100'
PAGE_TOKEN = 'EAAG-page-token'
VERIFY_TOKEN = 'biashara-connect-webhook-secret-2024'


def graph_response(payload=None):
    response = Mock()
    response.json.return_value = payload or {}
    response.raise_for_status.return_value = None
    return response


class WebhookVerificationTests(TestCase):
    """GET handshake (legacy ``WEBHOOK_VERIFIED`` flow)."""

    def setUp(self):
        self.client = APIClient()
        self.url = reverse('facebook-webhook')

    def test_matching_token_echoes_the_challenge(self):
        response = self.client.get(self.url, {
            'hub.mode': 'subscribe',
            'hub.verify_token': VERIFY_TOKEN,
            'hub.challenge': '1158201444',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'1158201444')

    def test_wrong_token_is_forbidden(self):
        response = self.client.get(self.url, {
            'hub.mode': 'subscribe', 'hub.verify_token': 'nope', 'hub.challenge': '1',
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content, b'Forbidden')

    def test_non_subscribe_mode_is_forbidden(self):
        response = self.client.get(self.url, {
            'hub.mode': 'unsubscribe', 'hub.verify_token': VERIFY_TOKEN, 'hub.challenge': '1',
        })
        self.assertEqual(response.status_code, 403)

    def test_missing_parameters_are_a_bad_request(self):
        self.assertEqual(self.client.get(self.url).status_code, 400)
        self.assertEqual(
            self.client.get(self.url, {'hub.mode': 'subscribe'}).status_code, 400
        )

    @override_settings(FACEBOOK_WEBHOOK_VERIFY_TOKEN='rotated-token')
    def test_configured_token_overrides_the_legacy_default(self):
        response = self.client.get(self.url, {
            'hub.mode': 'subscribe', 'hub.verify_token': 'rotated-token', 'hub.challenge': '42',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'42')
        self.assertEqual(self.client.get(self.url, {
            'hub.mode': 'subscribe', 'hub.verify_token': VERIFY_TOKEN, 'hub.challenge': '42',
        }).status_code, 403)


class WebhookEventTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('facebook-webhook')
        self.shop = Shop.objects.create(
            name='Mama Shop', legacy_id='hook-shop-1', whatsapp='+255 712 345 678',
        )
        self.integration = SocialIntegration.objects.create(
            shop=self.shop, platform='facebook', is_connected=True,
            page_id=PAGE_ID, page_name='Mama Shop', auto_reply_enabled=True,
            access_token=encrypt_token(PAGE_TOKEN),
        )

    def event(self, page_id=PAGE_ID, **value_overrides):
        value = {
            'item': 'comment',
            'verb': 'add',
            'comment_id': 'comment-1',
            'message': 'Bei gani?',
            'from': {'id': 'customer-1', 'name': 'Juma'},
            **value_overrides,
        }
        return {
            'object': 'page',
            'entry': [{'id': page_id, 'changes': [{'field': 'feed', 'value': value}]}],
        }

    def post_event(self, payload):
        return self.client.post(self.url, payload, format='json')

    def graph_calls(self, mock_post):
        return [
            call for call in mock_post.call_args_list
            if str(call.args[0]).startswith('https://graph.facebook.com/')
        ]


class WebhookEventTests(WebhookEventTestCase):
    @override_settings(OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_comment_gets_the_fallback_reply(self, mock_post):
        mock_post.return_value = graph_response()

        response = self.post_event(self.event())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'EVENT_RECEIVED')
        self.assertEqual(mock_post.call_count, 1)
        calls = self.graph_calls(mock_post)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], 'https://graph.facebook.com/v18.0/comment-1/comments')
        self.assertEqual(calls[0].kwargs['data']['message'], webhooks.FALLBACK_REPLY)
        self.assertEqual(calls[0].kwargs['data']['access_token'], PAGE_TOKEN)

    @override_settings(OPENROUTER_API_KEY='test-key')
    @patch('apps.social.webhooks.requests.post')
    def test_ai_reply_is_posted_and_prompt_carries_the_whatsapp_link(self, mock_post):
        mock_post.side_effect = [
            graph_response({'choices': [{'message': {'content': '  Bei ni 5,000. Wasiliana WhatsApp: https://wa.me/255712345678  '}}]}),
            graph_response(),
        ]

        response = self.post_event(self.event())

        self.assertEqual(response.status_code, 200)
        ai_call, graph_call = mock_post.call_args_list
        self.assertEqual(ai_call.args[0], 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(ai_call.kwargs['json']['model'], 'google/gemini-2.5-flash-lite')
        self.assertEqual(ai_call.kwargs['json']['temperature'], 0.7)
        self.assertEqual(ai_call.kwargs['json']['max_tokens'], 150)
        prompt = ai_call.kwargs['json']['messages'][0]['content']
        self.assertIn('Mama Shop', prompt)
        self.assertIn('Bei gani?', prompt)
        self.assertIn('https://wa.me/255712345678', prompt)
        self.assertEqual(
            graph_call.kwargs['data']['message'],
            'Bei ni 5,000. Wasiliana WhatsApp: https://wa.me/255712345678',
        )

    @override_settings(OPENROUTER_API_KEY='test-key')
    @patch('apps.social.webhooks.requests.post')
    def test_ai_failure_still_replies_with_the_fallback(self, mock_post):
        mock_post.side_effect = [requests.ConnectionError('openrouter down'), graph_response()]

        response = self.post_event(self.event())

        self.assertEqual(response.status_code, 200)
        graph_call = self.graph_calls(mock_post)[0]
        self.assertEqual(graph_call.kwargs['data']['message'], webhooks.FALLBACK_REPLY)

    @override_settings(OPENROUTER_API_KEY='test-key')
    @patch('apps.social.webhooks.requests.post')
    def test_empty_ai_reply_falls_back(self, mock_post):
        mock_post.side_effect = [graph_response({'choices': []}), graph_response()]

        self.post_event(self.event())

        self.assertEqual(
            self.graph_calls(mock_post)[0].kwargs['data']['message'], webhooks.FALLBACK_REPLY
        )

    @override_settings(OPENROUTER_API_KEY='test-key')
    @patch('apps.social.webhooks.requests.post')
    def test_dm_us_fallback_without_a_shop_phone(self, mock_post):
        mock_post.side_effect = [graph_response({'choices': []}), graph_response()]
        self.shop.whatsapp = ''
        self.shop.phone = ''
        self.shop.save(update_fields=['whatsapp', 'phone'])

        self.post_event(self.event())

        ai_call = mock_post.call_args_list[0]
        self.assertIn('DM us!', ai_call.kwargs['json']['messages'][0]['content'])

    @override_settings(OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_page_reply_is_skipped_to_avoid_looping(self, mock_post):
        self.post_event(self.event(**{'from': {'id': PAGE_ID}}))
        self.assertFalse(self.graph_calls(mock_post))

    @override_settings(OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_other_feed_events_are_ignored(self, mock_post):
        self.post_event(self.event(item='like'))
        self.post_event(self.event(verb='remove'))
        payload = self.event()
        payload['entry'][0]['changes'][0]['field'] = 'ratings'
        self.post_event(payload)
        self.assertFalse(mock_post.called)

    @override_settings(OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_auto_reply_disabled_means_no_reply(self, mock_post):
        self.integration.auto_reply_enabled = False
        self.integration.save(update_fields=['auto_reply_enabled'])

        self.post_event(self.event())

        self.assertFalse(mock_post.called)

    @override_settings(OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_unknown_page_is_ignored(self, mock_post):
        self.post_event(self.event(page_id='page-unknown'))
        self.assertFalse(mock_post.called)

    @override_settings(OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_integration_without_a_token_does_not_reply(self, mock_post):
        self.integration.access_token = None
        self.integration.save(update_fields=['access_token'])

        self.post_event(self.event())

        self.assertFalse(mock_post.called)

    @override_settings(OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_graph_failure_is_logged_not_raised(self, mock_post):
        mock_post.side_effect = requests.ConnectionError('graph down')

        response = self.post_event(self.event())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'EVENT_RECEIVED')

    def test_non_page_payloads_get_a_not_found(self):
        response = self.post_event({'object': 'instagram', 'entry': []})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.content, b'Not Found')


class WebhookSignatureTests(WebhookEventTestCase):
    """The unauthenticated POST is signed once an app secret is configured."""

    def setUp(self):
        super().setUp()
        self.secret = 'app-secret-123'
        self.body = json.dumps(self.event()).encode('utf-8')

    def sign(self, body, secret=None):
        digest = hmac.new((secret or self.secret).encode('utf-8'), body, hashlib.sha256)
        return f'sha256={digest.hexdigest()}'

    @override_settings(FACEBOOK_APP_SECRET='app-secret-123', OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_valid_signature_is_accepted(self, mock_post):
        mock_post.return_value = graph_response()

        response = self.client.generic(
            'POST', self.url, data=self.body, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=self.sign(self.body),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'EVENT_RECEIVED')
        self.assertTrue(self.graph_calls(mock_post))

    @override_settings(FACEBOOK_APP_SECRET='app-secret-123', OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_tampered_body_is_rejected(self, mock_post):
        response = self.client.generic(
            'POST', self.url, data=self.body, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=self.sign(b'{"object": "page"}'),
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(mock_post.called)

    @override_settings(FACEBOOK_APP_SECRET='app-secret-123', OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_missing_signature_is_rejected(self, mock_post):
        response = self.client.generic(
            'POST', self.url, data=self.body, content_type='application/json',
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(mock_post.called)

    @override_settings(FACEBOOK_APP_SECRET='', OPENROUTER_API_KEY='')
    @patch('apps.social.webhooks.requests.post')
    def test_unsigned_events_are_fine_without_a_secret(self, mock_post):
        mock_post.return_value = graph_response()

        response = self.post_event(self.event())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.graph_calls(mock_post))

    @override_settings(
        FACEBOOK_APP_SECRET='app-secret-123',
        FACEBOOK_WEBHOOK_VERIFY_SIGNATURE=False,
        OPENROUTER_API_KEY='',
    )
    @patch('apps.social.webhooks.requests.post')
    def test_signature_check_can_be_switched_off(self, mock_post):
        mock_post.return_value = graph_response()

        response = self.post_event(self.event())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.graph_calls(mock_post))


class WebhookUnitTests(TestCase):
    def test_process_event_returns_500_when_handling_blows_up(self):
        with patch('apps.social.webhooks.handle_new_comment', side_effect=RuntimeError('boom')):
            status_code = webhooks.process_event({
                'object': 'page',
                'entry': [{
                    'id': PAGE_ID,
                    'changes': [{
                        'field': 'feed',
                        'value': {'item': 'comment', 'verb': 'add', 'comment_id': 'c1'},
                    }],
                }],
            })
        self.assertEqual(status_code, 500)

    def test_whatsapp_link_strips_non_digits(self):
        shop = Shop(name='S', legacy_id='s-1', phone='(0712) 345-678')
        self.assertEqual(webhooks.whatsapp_link(shop), 'https://wa.me/0712345678')
        self.assertEqual(webhooks.whatsapp_link(Shop(name='S')), 'DM us!')
