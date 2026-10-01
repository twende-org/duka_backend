from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from unittest.mock import patch
from google.auth import exceptions as google_exceptions
from apps.users.models import User

@override_settings(GOOGLE_CLIENT_ID='test-google-client-id')
class GoogleLoginViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('google_login')

    @patch('apps.users.views.id_token.verify_oauth2_token')
    def test_google_login_success_new_user(self, mock_verify):
        # Mock Google token payload
        mock_verify.return_value = {
            "email": "testuser@gmail.com",
            "given_name": "Test",
            "family_name": "User"
        }

        data = {"idToken": "fake-google-id-token"}
        response = self.client.post(self.url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('access' in response.data)
        self.assertTrue('refresh' in response.data)
        self.assertTrue(response.data['is_new_user'])
        
        # Verify user was created
        user = User.objects.get(email="testuser@gmail.com")
        self.assertEqual(user.first_name, "Test")
        self.assertEqual(user.last_name, "User")

    @patch('apps.users.views.id_token.verify_oauth2_token')
    def test_google_login_success_existing_user(self, mock_verify):
        User.objects.create(username="testuser@gmail.com", email="testuser@gmail.com")
        
        mock_verify.return_value = {
            "email": "testuser@gmail.com",
            "given_name": "Test",
            "family_name": "User"
        }

        data = {"idToken": "fake-google-id-token"}
        response = self.client.post(self.url, data, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['is_new_user'])

    def test_google_login_no_token(self):
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('apps.users.views.id_token.verify_oauth2_token')
    def test_google_login_invalid_token(self, mock_verify):
        mock_verify.side_effect = ValueError("Invalid token")
        
        data = {"idToken": "invalid-token"}
        response = self.client.post(self.url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_settings(GOOGLE_CLIENT_ID='')
    def test_google_login_not_configured(self):
        response = self.client.post(self.url, {"idToken": "any-token"}, format='json')
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch('apps.users.views.id_token.verify_oauth2_token')
    def test_google_login_backfills_firebase_uid_from_sub(self, mock_verify):
        mock_verify.return_value = {
            "email": "legacy@gmail.com",
            "sub": "firebase-uid-123",
        }

        response = self.client.post(self.url, {"idToken": "fake-google-id-token"}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user = User.objects.get(email="legacy@gmail.com")
        self.assertEqual(user.firebase_uid, "firebase-uid-123")

    @patch('apps.users.views.id_token.verify_oauth2_token')
    def test_google_login_keeps_existing_firebase_uid(self, mock_verify):
        user = User.objects.create(
            username="keeper@gmail.com",
            email="keeper@gmail.com",
            firebase_uid="existing-uid",
        )
        mock_verify.return_value = {
            "email": "keeper@gmail.com",
            "sub": "different-uid",
        }

        response = self.client.post(self.url, {"idToken": "fake-google-id-token"}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.firebase_uid, "existing-uid")


class RegisterViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('register')

    def test_register_merchant(self):
        payload = {
            'email': 'Merchant@Example.com',
            'password': 'secret123',
            'displayName': 'Asha Mwangi',
            'phone': '+255700000001',
            'accountType': 'merchant',
        }
        response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['is_new_user'])
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)
        self.assertEqual(response.data['user']['account_type'], 'merchant')
        self.assertEqual(response.data['user']['default_workspace'], '')
        self.assertEqual(response.data['user']['roles'], [])

        user = User.objects.get(email='merchant@example.com')
        self.assertEqual(user.username, 'merchant@example.com')
        self.assertEqual(user.display_name, 'Asha Mwangi')
        self.assertEqual(user.first_name, 'Asha')
        self.assertEqual(user.phone, '+255700000001')
        self.assertTrue(user.can_manage_business)
        self.assertTrue(user.can_buy_for_business)
        self.assertTrue(user.check_password('secret123'))

    def test_register_defaults_to_customer(self):
        response = self.client.post(
            self.url, {'email': 'shopper@example.com', 'password': 'secret123'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email='shopper@example.com')
        self.assertEqual(user.account_type, 'customer')
        self.assertFalse(user.can_manage_business)

    def test_register_duplicate_email_rejected(self):
        User.objects.create_user(username='taken@example.com', email='taken@example.com', password='secret123')
        response = self.client.post(
            self.url, {'email': 'TAKEN@example.com', 'password': 'secret123'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_short_password_rejected(self):
        response = self.client.post(
            self.url, {'email': 'new@example.com', 'password': '123'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_then_login_roundtrip(self):
        self.client.post(
            self.url,
            {'email': 'roundtrip@example.com', 'password': 'secret123', 'accountType': 'staff'},
            format='json',
        )
        response = self.client.post(
            reverse('email_login'),
            {'email': 'RoundTrip@example.com', 'password': 'secret123'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['is_new_user'])
        self.assertEqual(response.data['user']['email'], 'roundtrip@example.com')


class EmailLoginViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('email_login')
        self.user = User.objects.create_user(
            username='owner@example.com', email='owner@example.com', password='secret123'
        )

    def test_login_returns_tokens_and_roles(self):
        from apps.shops.models import Shop, UserRole
        shop = Shop.objects.create(name='Login Shop')
        UserRole.objects.create(user=self.user, shop=shop, role='owner')

        response = self.client.post(
            self.url, {'email': 'owner@example.com', 'password': 'secret123'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)
        self.assertEqual(response.data['user']['default_workspace'], '')
        self.assertEqual(len(response.data['user']['roles']), 1)
        self.assertEqual(response.data['user']['roles'][0]['role'], 'owner')
        self.assertEqual(response.data['user']['roles'][0]['shop_id'], str(shop.id))

    def test_login_echoes_the_profile_maps_the_app_reads(self):
        corporate = {
            'companyId': 'company-acme', 'companyName': 'Acme Ltd',
            'role': 'buyer', 'status': 'APPROVED',
        }
        business = {'companyName': 'Acme Wholesale', 'tin': '123-456', 'status': 'APPROVED'}
        self.user.corporate_profile = corporate
        self.user.business_profile = business
        self.user.save()

        response = self.client.post(
            self.url, {'email': 'owner@example.com', 'password': 'secret123'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['user']['corporate_profile'], corporate)
        self.assertEqual(response.data['user']['business_profile'], business)

    def test_login_answers_none_for_a_plain_shopper(self):
        response = self.client.post(
            self.url, {'email': 'owner@example.com', 'password': 'secret123'}, format='json'
        )
        self.assertIsNone(response.data['user']['corporate_profile'])
        self.assertIsNone(response.data['user']['business_profile'])

    def test_login_unknown_email_rejected(self):
        response = self.client.post(
            self.url, {'email': 'nobody@example.com', 'password': 'secret123'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_wrong_password_rejected(self):
        response = self.client.post(
            self.url, {'email': 'owner@example.com', 'password': 'wrong'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_inactive_user_rejected(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.post(
            self.url, {'email': 'owner@example.com', 'password': 'secret123'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class UserProfileViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('user_profile')
        self.user = User.objects.create_user(
            username='profile@example.com', email='profile@example.com', password='secret123',
            display_name='Profile Owner',
        )
        self.client.force_authenticate(user=self.user)

    def test_patch_sets_default_workspace(self):
        response = self.client.patch(self.url, {'default_workspace': 'merchant'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['default_workspace'], 'merchant')
        self.user.refresh_from_db()
        self.assertEqual(self.user.default_workspace, 'merchant')

    def test_patch_updates_display_name_and_phone(self):
        response = self.client.patch(
            self.url, {'display_name': 'New Name', 'phone': '+255700000009'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, 'New Name')
        self.assertEqual(self.user.phone, '+255700000009')

    def test_email_is_read_only(self):
        response = self.client.patch(self.url, {'email': 'hacked@example.com'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'profile@example.com')

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.patch(self.url, {'default_workspace': 'customer'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class FirebaseLoginViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('firebase_login')

    def test_missing_token_rejected(self):
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('apps.users.views.verify_firebase_token')
    def test_bridge_creates_user_with_unusable_password(self, mock_verify):
        mock_verify.return_value = {'email': 'FireBase@Gmail.com', 'name': 'Fire Base'}

        response = self.client.post(self.url, {'idToken': 'fake'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['is_new_user'])
        self.assertIn('access', response.data)
        user = User.objects.get(email='firebase@gmail.com')
        self.assertEqual(user.account_type, 'unassigned')
        self.assertEqual(user.display_name, 'Fire Base')
        self.assertFalse(user.has_usable_password())

    @patch('apps.users.views.verify_firebase_token')
    def test_bridge_reuses_existing_user_by_email(self, mock_verify):
        existing = User.objects.create_user(
            username='firebase@gmail.com', email='firebase@gmail.com', password='secret123'
        )
        mock_verify.return_value = {'email': 'firebase@gmail.com', 'name': 'Fire Base'}

        response = self.client.post(self.url, {'idToken': 'fake'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['is_new_user'])
        self.assertEqual(response.data['user']['id'], str(existing.id))
        self.assertEqual(User.objects.count(), 1)

    @patch('apps.users.views.verify_firebase_token')
    def test_bridge_rejects_invalid_token(self, mock_verify):
        mock_verify.side_effect = ValueError('bad token')
        response = self.client.post(self.url, {'idToken': 'bad'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch('apps.users.views.verify_firebase_token')
    def test_bridge_rejects_google_auth_error(self, mock_verify):
        mock_verify.side_effect = google_exceptions.InvalidValue('wrong audience')
        response = self.client.post(self.url, {'idToken': 'bad'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
