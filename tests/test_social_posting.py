import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import requests
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.core.crypto import encrypt_token
# pyrefly: ignore [missing-import]
from apps.marketing.models import Campaign
# pyrefly: ignore [missing-import]
from apps.products.models import Inventory, Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.social import posting, tasks
# pyrefly: ignore [missing-import]
from apps.social.models import SocialIntegration, SocialLog
# pyrefly: ignore [missing-import]
from apps.social.reels import ReelGenerationError
# pyrefly: ignore [missing-import]
from apps.users.models import User

PAGE_ID = 'page-1'
PAGE_TOKEN = 'EAAG-page-token'


def graph_response(payload):
    response = Mock()
    response.status_code = 200
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def graph_error(payload, status_code=400):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    response.text = json.dumps(payload)
    error = requests.HTTPError(f'{status_code} Error')
    error.response = response
    return error


# Pin the AI caption off so results do not depend on whether the environment
# carries a real OPENROUTER_API_KEY; the two AI tests override it back to a
# dummy key at method level (which wins over this class-level value).
@override_settings(OPENROUTER_API_KEY='')
class PostingTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='owner', email='owner@example.com', password='password',
        )
        self.shop = Shop.objects.create(
            name='Test Shop', legacy_id='post-shop-1', slug='test-shop',
            phone='+255700000000',
        )
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.integration = SocialIntegration.objects.create(
            shop=self.shop, platform='facebook', is_connected=True,
            page_id=PAGE_ID, page_name='Page One', access_token=encrypt_token(PAGE_TOKEN),
        )
        self.product = Product.objects.create(
            shop=self.shop, name='Sukari', legacy_id='prod-1',
            selling_price=Decimal('1200.00'), description='Sukari nzuri',
            image_urls=['https://cdn.example/one.jpg', 'https://cdn.example/two.jpg'],
        )


class CaptionStrategyTests(TestCase):
    def test_price_formatting_matches_locale_output(self):
        self.assertEqual(posting.format_price(Decimal('1200.00')), '1,200')
        self.assertEqual(posting.format_price(Decimal('1234.50')), '1,234.5')
        self.assertEqual(posting.format_price(Decimal('999.00')), '999')

    def test_strategy_windows(self):
        self.assertEqual(posting.caption_strategy(5), posting.STRATEGY_DEFAULT)
        self.assertEqual(posting.caption_strategy(6), posting.STRATEGY_MORNING)
        self.assertEqual(posting.caption_strategy(10), posting.STRATEGY_MORNING)
        self.assertEqual(posting.caption_strategy(11), posting.STRATEGY_MIDDAY)
        self.assertEqual(posting.caption_strategy(15), posting.STRATEGY_MIDDAY)
        self.assertEqual(posting.caption_strategy(16), posting.STRATEGY_EVENING)
        # EAT = UTC + 3 without wrapping: hour 22 UTC is 25 EAT, still evening.
        self.assertEqual(posting.caption_strategy(25), posting.STRATEGY_EVENING)


class PerformFacebookPostTests(PostingTestCase):
    def test_missing_connection_raises_legacy_code(self):
        self.integration.delete()
        with self.assertRaises(posting.FacebookNotConnected) as ctx:
            posting.perform_facebook_post(self.shop.legacy_id, [self.product.legacy_id])
        self.assertEqual(str(ctx.exception), 'facebook-not-connected')

    def test_unknown_product_raises_legacy_code(self):
        with self.assertRaises(posting.ProductNotFound) as ctx:
            posting.perform_facebook_post(self.shop.legacy_id, ['missing-product'])
        self.assertEqual(str(ctx.exception), 'product-not-found')

    @patch('apps.social.posting.requests.post')
    def test_text_only_post_uses_feed_with_contact_links(self, mock_post):
        mock_post.return_value = graph_response({'id': 'fb-9'})
        product = Product.objects.create(
            shop=self.shop, name='Mchele', legacy_id='prod-2', selling_price=Decimal('0'),
        )

        result = posting.perform_facebook_post(
            self.shop.legacy_id, [product.legacy_id], include_image=False,
        )

        self.assertEqual(
            result, {'success': True, 'facebookPostId': 'fb-9', 'instagramPostId': None},
        )
        self.assertEqual(mock_post.call_count, 1)
        self.assertIn(f'{PAGE_ID}/feed', mock_post.call_args.args[0])
        message = mock_post.call_args.kwargs['data']['message']
        self.assertIn('Mchele - Bei Nafuu', message)
        self.assertIn('https://wa.me/255700000000?text=Nahitaji%20kununua%20bidhaa', message)
        self.assertIn('https://duka.twendedigital.tech/shop/test-shop', message)

    @patch('apps.social.posting.requests.post')
    def test_photo_post_uses_first_image_and_fallback_caption(self, mock_post):
        mock_post.return_value = graph_response({'post_id': 'fb-photo-1'})

        result = posting.perform_facebook_post(self.shop.legacy_id, [self.product.legacy_id])

        self.assertEqual(result['facebookPostId'], 'fb-photo-1')
        self.assertIn(f'{PAGE_ID}/photos', mock_post.call_args.args[0])
        payload = mock_post.call_args.kwargs['data']
        self.assertEqual(payload['url'], 'https://cdn.example/one.jpg')
        self.assertIn('Sukari - TZS 1,200', payload['message'])
        self.assertIn('Sukari nzuri', payload['message'])

    @patch('apps.social.posting.requests.post')
    def test_reel_post_sends_video_url(self, mock_post):
        mock_post.return_value = graph_response({'id': 'fb-vid-1'})

        with patch(
            'apps.social.posting.generate_reel_from_images',
            return_value='https://api.example/media/generated_reels/reel.mp4',
        ):
            result = posting.perform_facebook_post(
                self.shop.legacy_id, [self.product.legacy_id], include_image=True,
                post_format='reel',
            )

        self.assertEqual(result['facebookPostId'], 'fb-vid-1')
        self.assertIn(f'{PAGE_ID}/videos', mock_post.call_args.args[0])
        payload = mock_post.call_args.kwargs['data']
        self.assertEqual(payload['file_url'], 'https://api.example/media/generated_reels/reel.mp4')
        self.assertIn('💬 DM us to order!', payload['description'])
        self.assertIn('📞 Contact: +255700000000', payload['description'])
        # Reels avoid outbound links to protect organic reach.
        self.assertNotIn('wa.me', payload['description'])

    @patch('apps.social.posting.requests.post')
    def test_reel_failure_falls_back_to_photo_and_is_logged(self, mock_post):
        mock_post.return_value = graph_response({'id': 'fb-photo-2'})

        with patch(
            'apps.social.posting.generate_reel_from_images',
            side_effect=ReelGenerationError('ffmpeg is not installed on this host.'),
        ):
            result = posting.perform_facebook_post(
                self.shop.legacy_id, [self.product.legacy_id], post_format='reel',
            )

        self.assertEqual(result['facebookPostId'], 'fb-photo-2')
        self.assertIn(f'{PAGE_ID}/photos', mock_post.call_args.args[0])
        log = SocialLog.objects.get(status='success')
        self.assertEqual(log.video_fallback_reason, 'ffmpeg is not installed on this host.')
        self.assertEqual(log.product_ids, ['prod-1'])

    @override_settings(OPENROUTER_API_KEY='test-key')
    @patch('apps.social.posting.requests.post')
    def test_ai_caption_is_used_and_uses_evening_strategy(self, mock_post):
        mock_post.side_effect = [
            graph_response({'choices': [{'message': {'content': '```json\nKaribu dukani! 🎉```'}}]}),
            graph_response({'id': 'fb-ai-1'}),
        ]

        posting.perform_facebook_post(
            self.shop.legacy_id, [self.product.legacy_id], include_image=False,
            now=datetime(2026, 9, 29, 13, 0, tzinfo=dt_timezone.utc),
        )

        prompt = mock_post.call_args_list[0].kwargs['json']['messages'][0]['content']
        self.assertIn(posting.STRATEGY_EVENING, prompt)
        message = mock_post.call_args_list[1].kwargs['data']['message']
        self.assertTrue(message.startswith('Karibu dukani! 🎉'))

    @override_settings(OPENROUTER_API_KEY='test-key')
    @patch('apps.social.posting.requests.post')
    def test_ai_failure_uses_fallback_caption(self, mock_post):
        mock_post.side_effect = [
            requests.RequestException('boom'),
            requests.RequestException('boom'),
            requests.RequestException('boom'),
            graph_response({'id': 'fb-fallback-1'}),
        ]

        posting.perform_facebook_post(
            self.shop.legacy_id, [self.product.legacy_id], include_image=False,
        )

        message = mock_post.call_args.kwargs['data']['message']
        self.assertTrue(message.startswith('Sukari - TZS 1,200'))

    @patch('apps.social.posting.requests.post')
    def test_graph_failure_records_log_and_raises(self, mock_post):
        mock_post.side_effect = graph_error({'error': {'message': 'Invalid OAuth token'}})

        with self.assertRaises(posting.SocialPostFailed) as ctx:
            posting.perform_facebook_post(self.shop.legacy_id, [self.product.legacy_id])

        self.assertTrue(str(ctx.exception).startswith('Social posting failed: '))
        log = SocialLog.objects.get(status='failure')
        self.assertEqual(json.loads(log.error), {'error': {'message': 'Invalid OAuth token'}})
        self.assertFalse(Campaign.objects.exists())

    @patch('apps.social.posting.requests.post')
    def test_success_records_log_and_campaign(self, mock_post):
        mock_post.return_value = graph_response({'id': 'fb-ok-1'})

        posting.perform_facebook_post(self.shop.legacy_id, [self.product.legacy_id])

        log = SocialLog.objects.get(status='success')
        self.assertEqual(log.type, 'social_post')
        self.assertEqual(log.action, 'post')
        self.assertEqual(log.facebook_post_id, 'fb-ok-1')
        self.assertIsNone(log.instagram_post_id)
        self.assertIsNone(log.instagram_error)
        campaign = Campaign.objects.get()
        self.assertEqual(campaign.name, 'Social Post: Sukari')
        self.assertEqual(campaign.source, 'manual')
        self.assertEqual(campaign.platform, 'facebook')
        self.assertEqual(campaign.status, 'running')


class InstagramPostingTests(PostingTestCase):
    def setUp(self):
        super().setUp()
        self.integration.instagram_id = 'ig-9'
        self.integration.save(update_fields=['instagram_id'])

    @patch('apps.social.posting.requests.post')
    def test_instagram_skipped_without_images(self, mock_post):
        mock_post.return_value = graph_response({'id': 'fb-text-1'})
        product = Product.objects.create(
            shop=self.shop, name='Bila Picha', legacy_id='prod-3', selling_price=Decimal('100'),
        )

        result = posting.perform_facebook_post(
            self.shop.legacy_id, [product.legacy_id], include_image=False,
        )

        self.assertEqual(mock_post.call_count, 1)
        self.assertIsNone(result['instagramPostId'])

    @patch('apps.social.posting.time.sleep')
    @patch('apps.social.posting.generate_reel_from_images')
    @patch('apps.social.posting.requests.get')
    @patch('apps.social.posting.requests.post')
    def test_instagram_reel_published_after_processing(
        self, mock_post, mock_get, mock_reel, mock_sleep,
    ):
        mock_reel.return_value = 'https://api.example/media/generated_reels/reel.mp4'
        mock_post.side_effect = [
            graph_response({'id': 'fb-vid-2'}),
            graph_response({'id': 'container-1'}),
            graph_response({'id': 'ig-post-1'}),
        ]
        mock_get.return_value = graph_response({'status_code': 'FINISHED'})

        result = posting.perform_facebook_post(
            self.shop.legacy_id, [self.product.legacy_id], post_format='reel',
        )

        self.assertEqual(result['instagramPostId'], 'ig-post-1')
        reel_call = mock_post.call_args_list[1]
        self.assertIn('ig-9/media', reel_call.args[0])
        self.assertEqual(reel_call.kwargs['data']['media_type'], 'REELS')
        self.assertEqual(reel_call.kwargs['data']['video_url'], mock_reel.return_value)
        self.assertIn('ig-9/media_publish', mock_post.call_args_list[2].args[0])
        log = SocialLog.objects.get(status='success')
        self.assertEqual(log.instagram_post_id, 'ig-post-1')
        self.assertIsNone(log.video_fallback_reason)

    @patch('apps.social.posting.time.sleep')
    @patch('apps.social.posting.generate_reel_from_images')
    @patch('apps.social.posting.requests.get')
    @patch('apps.social.posting.requests.post')
    def test_instagram_video_timeout_falls_back_to_image(
        self, mock_post, mock_get, mock_reel, mock_sleep,
    ):
        mock_reel.return_value = 'https://api.example/media/generated_reels/reel.mp4'
        mock_get.return_value = graph_response({'status_code': 'IN_PROGRESS'})
        mock_post.side_effect = [
            graph_response({'id': 'fb-vid-3'}),
            graph_response({'id': 'container-1'}),
            graph_response({'id': 'container-2'}),
            graph_response({'id': 'ig-image-1'}),
        ]

        result = posting.perform_facebook_post(
            self.shop.legacy_id, [self.product.legacy_id], post_format='reel',
        )

        self.assertEqual(result['instagramPostId'], 'ig-image-1')
        self.assertEqual(mock_sleep.call_count, 36)
        self.assertIn('image_url', mock_post.call_args_list[2].kwargs['data'])
        log = SocialLog.objects.get(status='success')
        self.assertEqual(
            log.video_fallback_reason,
            'Instagram video container processing timed out after 3 minutes.',
        )

    @patch('apps.social.posting.requests.post')
    def test_instagram_failure_does_not_fail_the_post(self, mock_post):
        mock_post.side_effect = [
            graph_response({'id': 'fb-vid-4'}),
            graph_error({'error': {'message': 'video too long'}}),
            graph_error({'error': {'message': 'image broken'}}),
        ]

        with patch(
            'apps.social.posting.generate_reel_from_images',
            return_value='https://api.example/media/generated_reels/reel.mp4',
        ):
            result = posting.perform_facebook_post(
                self.shop.legacy_id, [self.product.legacy_id], post_format='reel',
            )

        self.assertEqual(result['success'], True)
        self.assertEqual(result['facebookPostId'], 'fb-vid-4')
        self.assertIsNone(result['instagramPostId'])
        log = SocialLog.objects.get(status='success')
        self.assertEqual(json.loads(log.instagram_error), {'error': {'message': 'image broken'}})
        self.assertEqual(log.video_fallback_reason, '400 Error')


class FacebookPostViewTests(PostingTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('facebook-posts')
        self.payload = {
            'shopId': self.shop.legacy_id,
            'productId': self.product.legacy_id,
            'includeImage': True,
        }

    def test_requires_authentication(self):
        response = self.client.post(self.url, self.payload, format='json')
        self.assertEqual(response.status_code, 401)

    def test_requires_management_role(self):
        staff = User.objects.create_user(username='staff', email='staff@example.com', password='password')
        UserRole.objects.create(user=staff, shop=self.shop, role='attendant')
        self.client.force_authenticate(user=staff)
        response = self.client.post(self.url, self.payload, format='json')
        self.assertEqual(response.status_code, 403)

    def test_missing_product_id_is_400(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'shopId': self.shop.legacy_id}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['detail'], 'shopId or productId is missing.')

    def test_unknown_shop_is_404(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {**self.payload, 'shopId': 'nope'}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_not_connected_is_400_with_legacy_code(self):
        self.integration.delete()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, self.payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['detail'], 'facebook-not-connected')

    def test_unknown_product_is_404_with_legacy_code(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {**self.payload, 'productId': 'nope'}, format='json')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['detail'], 'product-not-found')

    @patch('apps.social.posting.perform_facebook_post')
    def test_success_returns_callable_payload(self, mock_perform):
        mock_perform.return_value = {
            'success': True, 'facebookPostId': 'fb-1', 'instagramPostId': None,
        }
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, mock_perform.return_value)
        mock_perform.assert_called_once_with(
            self.shop.pk, [self.product.legacy_id], None, True,
        )

    @patch('apps.social.posting.perform_facebook_post')
    def test_graph_failure_is_502(self, mock_perform):
        mock_perform.side_effect = posting.SocialPostFailed('Social posting failed: {"error": "x"}')
        self.client.force_authenticate(user=self.user)

        response = self.client.post(self.url, self.payload, format='json')

        self.assertEqual(response.status_code, 502)
        self.assertTrue(response.data['detail'].startswith('Social posting failed: '))


class PostProductTaskTests(PostingTestCase):
    @patch('apps.social.tasks.perform_facebook_post')
    def test_task_returns_failure_instead_of_raising(self, mock_perform):
        mock_perform.side_effect = posting.SocialPostFailed('Social posting failed: boom')

        result = tasks.post_product_to_facebook(self.shop.legacy_id, ['prod-1'])

        self.assertEqual(result['success'], False)
        self.assertIn('boom', result['error'])

    @patch('apps.social.tasks.perform_facebook_post')
    def test_task_forwards_format_and_include_image(self, mock_perform):
        mock_perform.return_value = {'success': True}

        tasks.post_product_to_facebook('shop-1', ['prod-1'], 'reel', False)

        mock_perform.assert_called_once_with('shop-1', ['prod-1'], None, False, 'reel')


class DailySocialPosterTests(PostingTestCase):
    def setUp(self):
        super().setUp()
        self.branch = Branch.objects.create(shop=self.shop, name='Main', is_main=True)
        Inventory.objects.create(product=self.product, branch=self.branch, quantity=5)

    @patch('apps.social.tasks.perform_facebook_post')
    def test_posts_least_recently_posted_product_as_reel(self, mock_perform):
        stale = Product.objects.create(
            shop=self.shop, name='Stale', legacy_id='prod-stale',
            selling_price=Decimal('500'), image_urls=['https://cdn.example/s.jpg'],
            last_posted_at=datetime(2020, 1, 1, tzinfo=dt_timezone.utc),
        )
        Inventory.objects.create(product=stale, branch=self.branch, quantity=3)
        self.product.last_posted_at = timezone.now()
        self.product.save(update_fields=['last_posted_at'])
        mock_perform.return_value = {
            'success': True, 'facebookPostId': 'fb-1', 'instagramPostId': None,
        }

        results = tasks.daily_social_poster()

        self.assertEqual(mock_perform.call_count, 1)
        self.assertEqual(mock_perform.call_args.args[0], self.shop.pk)
        self.assertEqual(mock_perform.call_args.args[1], ['prod-stale'])
        self.assertEqual(mock_perform.call_args.args[4], 'reel')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['success'], True)
        stale.refresh_from_db()
        self.assertIsNotNone(stale.last_posted_at)

    @patch('apps.social.tasks.perform_facebook_post')
    def test_skips_products_without_images(self, mock_perform):
        self.product.image_urls = []
        self.product.image_url = ''
        self.product.save()

        results = tasks.daily_social_poster()

        self.assertEqual(results, [])
        mock_perform.assert_not_called()

    @patch('apps.social.tasks.perform_facebook_post')
    def test_skips_shops_without_a_connection(self, mock_perform):
        self.integration.delete()

        results = tasks.daily_social_poster()

        self.assertEqual(results, [])
        mock_perform.assert_not_called()

    def test_out_of_stock_products_are_ignored(self):
        Inventory.objects.update(quantity=0)
        self.assertIsNone(tasks.next_product_for_shop(self.shop))

    @patch('apps.social.tasks.perform_facebook_post')
    def test_failures_are_collected_per_shop(self, mock_perform):
        mock_perform.side_effect = posting.SocialPostFailed('Social posting failed: boom')

        results = tasks.daily_social_poster()

        self.assertEqual(results[0]['success'], False)
        self.assertIn('boom', results[0]['error'])


class AutoPostSignalTests(PostingTestCase):
    @patch('apps.social.signals.post_product_to_facebook')
    def test_create_with_publish_flag_dispatches_after_commit(self, mock_task):
        with self.captureOnCommitCallbacks(execute=True):
            product = Product.objects.create(
                shop=self.shop, name='Auto', legacy_id='prod-auto', publish_to_facebook=True,
            )

        mock_task.delay.assert_called_once_with(str(self.shop.pk), ['prod-auto'])
        self.assertIsNotNone(product.pk)

    @patch('apps.social.signals.post_product_to_facebook')
    def test_create_without_flag_does_not_dispatch(self, mock_task):
        with self.captureOnCommitCallbacks(execute=True):
            Product.objects.create(shop=self.shop, name='Quiet', legacy_id='prod-quiet')

        mock_task.delay.assert_not_called()

    @patch('apps.social.signals.post_product_to_facebook')
    def test_false_to_true_update_dispatches_once(self, mock_task):
        product = Product.objects.create(
            shop=self.shop, name='Later', legacy_id='prod-later', publish_to_facebook=False,
        )
        with self.captureOnCommitCallbacks(execute=True):
            product.publish_to_facebook = True
            product.save()

        mock_task.delay.assert_called_once_with(str(self.shop.pk), ['prod-later'])

    @patch('apps.social.signals.post_product_to_facebook')
    def test_plain_update_does_not_dispatch(self, mock_task):
        product = Product.objects.create(
            shop=self.shop, name='Steady', legacy_id='prod-steady', publish_to_facebook=True,
        )
        with self.captureOnCommitCallbacks(execute=True):
            product.name = 'Steady renamed'
            product.save()

        mock_task.delay.assert_not_called()

    @patch('apps.social.signals.post_product_to_facebook')
    def test_broker_outage_never_breaks_the_product_write(self, mock_task):
        mock_task.delay.side_effect = ConnectionError('redis is down')

        with self.captureOnCommitCallbacks(execute=True):
            product = Product.objects.create(
                shop=self.shop, name='Resilient', legacy_id='prod-resilient',
                publish_to_facebook=True,
            )

        self.assertTrue(Product.objects.filter(pk=product.pk).exists())
