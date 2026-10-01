from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
# pyrefly: ignore [missing-import]
from apps.users.models import User
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.social.models import SocialIntegration, Conversation, Message

class SocialModuleTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='testuser', email='test@example.com', password='password')
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Test Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')

    def test_social_integration_creation(self):
        integration = SocialIntegration.objects.create(
            shop=self.shop,
            platform='meta',
            is_connected=False
        )
        self.assertEqual(integration.shop, self.shop)
        self.assertEqual(integration.platform, 'meta')

    def test_connect_account_endpoint(self):
        integration = SocialIntegration.objects.create(
            shop=self.shop,
            platform='meta',
            is_connected=False
        )
        url = reverse('social-integration-connect-account', args=[integration.id])
        data = {
            'page_id': '12345',
            'page_name': 'My FB Page'
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        integration.refresh_from_db()
        self.assertTrue(integration.is_connected)
        self.assertEqual(integration.page_id, '12345')
        self.assertEqual(integration.page_name, 'My FB Page')

    def test_conversation_and_messages(self):
        conversation = Conversation.objects.create(
            shop=self.shop,
            customer_name='John Doe',
            platform='whatsapp'
        )
        message = Message.objects.create(
            conversation=conversation,
            sender_type='customer',
            content='Hello, do you have this in size 42?'
        )
        self.assertEqual(conversation.customer_name, 'John Doe')
        self.assertEqual(message.content, 'Hello, do you have this in size 42?')

    def test_shop_social_links(self):
        self.shop.social_links = {
            'facebook': 'https://facebook.com/testshop',
            'whatsapp': 'https://wa.me/255123456789'
        }
        self.shop.save()
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.social_links.get('facebook'), 'https://facebook.com/testshop')
