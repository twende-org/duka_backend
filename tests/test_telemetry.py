from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status

# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.social.models import SocialLog
# pyrefly: ignore [missing-import]
from apps.telemetry.models import ActivityLog, AnalyticsEvent, ErrorEvent
# pyrefly: ignore [missing-import]
from apps.users.models import CustomerAddress, Subscription, User, WishlistItem


def rows(response):
    """List payload rows, tolerating both the paginated envelope and a bare list."""
    data = response.json()
    return data['results'] if isinstance(data, dict) else data


class ActivityLogTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='owner', email='owner@example.com', password='password'
        )
        self.other = User.objects.create_user(
            username='other', email='other@example.com', password='password'
        )
        self.shop = Shop.objects.create(name='Test Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')

    def test_create_stamps_identity_and_accepts_null_fields(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(reverse('activity-log-list'), {
            'action': 'Update product',
            'category': 'products',
            'details': 'Changed the price',
            'role': None,
            'metadata': None,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        log = ActivityLog.objects.get()
        self.assertEqual(log.user_id, str(self.user.pk))
        self.assertEqual(log.user_email, 'owner@example.com')
        self.assertEqual(log.role, '')
        self.assertEqual(log.metadata, {})

    def test_create_requires_authentication(self):
        response = self.client.post(
            reverse('activity-log-list'), {'action': 'x'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_shop_list_is_member_scoped(self):
        ActivityLog.objects.create(shop_id=str(self.shop.pk), action='mine')
        other_shop = Shop.objects.create(name='Other Shop')
        ActivityLog.objects.create(shop_id=str(other_shop.pk), action='theirs')
        # Shops migrated from Firestore are addressed by legacy id in the app.
        self.shop.legacy_id = 'firestore-shop-1'
        self.shop.save(update_fields=['legacy_id'])

        self.client.force_authenticate(user=self.user)
        for ref in (str(self.shop.pk), 'firestore-shop-1'):
            response = self.client.get(reverse('activity-log-list'), {'shop_id': ref})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual([row['action'] for row in rows(response)], ['mine'])

        self.client.force_authenticate(user=self.other)
        response = self.client.get(
            reverse('activity-log-list'), {'shop_id': str(self.shop.pk)}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unscoped_list_is_staff_only(self):
        ActivityLog.objects.create(shop_id=str(self.shop.pk), action='mine')

        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse('activity-log-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(rows(response), [])

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        response = self.client.get(reverse('activity-log-list'))
        self.assertEqual(len(rows(response)), 1)

    def test_user_filter_narrows_the_shop_stream(self):
        ActivityLog.objects.create(
            shop_id=str(self.shop.pk), user_id=str(self.user.pk), action='mine'
        )
        ActivityLog.objects.create(
            shop_id=str(self.shop.pk), user_id=str(self.other.pk), action='theirs'
        )

        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse('activity-log-list'), {
            'shop_id': str(self.shop.pk),
            'user_id': str(self.user.pk),
        })
        self.assertEqual([row['action'] for row in rows(response)], ['mine'])


class EventIngestTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='shopper', email='shopper@example.com', password='password'
        )

    def test_analytics_event_accepts_guests_and_sweeps_unknown_keys(self):
        response = self.client.post(reverse('analytics-event-list'), {
            'eventType': 'search_query',
            'query': 'shoes',
            'deviceId': None,
            'isAiMode': None,
            'unexpectedKey': {'anything': True},
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        event = AnalyticsEvent.objects.get()
        self.assertEqual(event.event_type, 'search_query')
        self.assertEqual(event.device_id, '')
        self.assertIsNone(event.is_ai_mode)
        self.assertEqual(event.payload, {'unexpectedKey': {'anything': True}})

    def test_analytics_event_requires_event_type(self):
        response = self.client.post(
            reverse('analytics-event-list'), {'query': 'shoes'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_analytics_event_stamps_signed_in_user(self):
        self.client.force_authenticate(user=self.user)
        self.client.post(
            reverse('analytics-event-list'), {'eventType': 'product_view'}, format='json'
        )
        self.assertEqual(AnalyticsEvent.objects.get().user_id, str(self.user.pk))

    def test_analytics_list_is_staff_only(self):
        AnalyticsEvent.objects.create(event_type='shop_visit')
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse('analytics-event-list'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        response = self.client.get(
            reverse('analytics-event-list'), {'eventType': 'shop_visit'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(rows(response)), 1)

    def test_error_event_accepts_guests(self):
        response = self.client.post(reverse('error-event-list'), {
            'errorMessage': 'Cannot read properties of undefined',
            'errorCode': 'TypeError',
            'route': '/shop/1',
            'category': 'render',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ErrorEvent.objects.get().error_message.startswith('Cannot'), True)

    def test_error_event_stamps_identity_when_signed_in(self):
        self.client.force_authenticate(user=self.user)
        self.client.post(
            reverse('error-event-list'), {'errorMessage': 'boom'}, format='json'
        )
        event = ErrorEvent.objects.get()
        self.assertEqual(event.user_id, str(self.user.pk))
        self.assertEqual(event.user_email, 'shopper@example.com')

    def test_error_list_is_staff_only(self):
        ErrorEvent.objects.create(error_message='boom')
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse('error-event-list'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AnalyticsSummaryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='owner', email='owner@example.com', password='password'
        )
        self.other = User.objects.create_user(
            username='other', email='other@example.com', password='password'
        )
        self.shop = Shop.objects.create(name='Test Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')

        for event_type in ('shop_visit', 'shop_visit', 'product_view',
                           'whatsapp_click', 'whatsapp_click', 'whatsapp_click',
                           'shop_follow'):
            AnalyticsEvent.objects.create(
                event_type=event_type, shop_id=str(self.shop.pk)
            )

    def test_shop_summary_counts(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            reverse('analytics-shop-summary'), {'shop_id': str(self.shop.pk)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {
            'visits': 2,
            'productViews': 1,
            'whatsappClicks': 3,
            'followers': 1,
        })

    def test_shop_summary_requires_shop_access(self):
        self.client.force_authenticate(user=self.other)
        response = self.client.get(
            reverse('analytics-shop-summary'), {'shop_id': str(self.shop.pk)}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_shop_summary_requires_shop_id(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse('analytics-shop-summary'))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_global_summary_is_staff_only(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse('analytics-global-summary'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_global_summary_totals_and_top_searches(self):
        for query in ('shoes', 'shoes', 'bags'):
            AnalyticsEvent.objects.create(event_type='search_query', query=query)
        AnalyticsEvent.objects.create(event_type='search_query', query='')

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse('analytics-global-summary'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data['totalSearches'], 4)
        self.assertEqual(data['totalWhatsAppClicks'], 3)
        self.assertEqual(data['totalShopVisits'], 2)
        self.assertEqual(data['topSearches'], [
            {'query': 'shoes', 'count': 2},
            {'query': 'bags', 'count': 1},
        ])


class SubscriptionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username='admin', email='admin@example.com', password='password',
            is_staff=True,
        )
        self.user = User.objects.create_user(
            username='merchant', email='merchant@example.com', password='password',
            display_name='Merchant One',
        )
        self.url = reverse('subscription-detail', args=[str(self.user.pk)])

    def test_retrieve_answers_null_when_no_row(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # DRF renders a None payload as an empty 200 body; the app reads that as
        # "no subscription row" and falls back to the free tier.
        self.assertEqual(response.content, b'')

    def test_put_upserts_and_fills_identity_from_the_user(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.put(self.url, {
            'plan': 'business',
            'status': 'pending',
            'amount': 45000,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        subscription = Subscription.objects.get(user=self.user)
        self.assertEqual(subscription.plan, 'business')
        self.assertEqual(subscription.user_email, 'merchant@example.com')
        self.assertEqual(subscription.user_name, 'Merchant One')

    def test_patch_merges_into_the_existing_row(self):
        Subscription.objects.create(user=self.user, plan='free', status='pending')
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(self.url, {'status': 'active'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        subscription = Subscription.objects.get(user=self.user)
        self.assertEqual(subscription.status, 'active')
        self.assertEqual(subscription.plan, 'free')

    def test_confirm_stamps_the_staff_actor(self):
        Subscription.objects.create(user=self.user, plan='basic', status='pending')
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            reverse('subscription-confirm', args=[str(self.user.pk)])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        subscription = Subscription.objects.get(user=self.user)
        self.assertEqual(subscription.status, 'active')
        self.assertEqual(subscription.confirmed_by, str(self.staff.pk))
        self.assertIsNotNone(subscription.confirmed_at)

    def test_non_staff_cannot_confirm_or_update(self):
        Subscription.objects.create(user=self.user, plan='basic', status='pending')
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            reverse('subscription-confirm', args=[str(self.user.pk)])
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.patch(self.url, {'status': 'active'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_retrieve_resolves_either_id_space(self):
        self.user.firebase_uid = 'firebase-uid-1'
        self.user.save(update_fields=['firebase_uid'])
        Subscription.objects.create(user=self.user, plan='basic', status='active')

        self.client.force_authenticate(user=self.user)
        for ref in ('firebase-uid-1', str(self.user.pk)):
            response = self.client.get(reverse('subscription-detail', args=[ref]))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.json()['plan'], 'basic')


class WishlistTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='shopper', email='shopper@example.com', password='password'
        )
        self.other = User.objects.create_user(
            username='other', email='other@example.com', password='password'
        )
        self.client.force_authenticate(user=self.user)

    def payload(self, name='Shoes'):
        return {
            'productId': 'prod-1',
            'name': name,
            'price': 15000,
            'shopId': 'shop-1',
            'shopName': 'Test Shop',
            'wholesalePrice': 12000,
            'moq': 6,
        }

    def test_create_then_resubmit_upserts(self):
        response = self.client.post(
            reverse('wishlist-list'), self.payload(), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        response = self.client.post(
            reverse('wishlist-list'), self.payload(name='Shoes v2'), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(WishlistItem.objects.filter(user=self.user).count(), 1)
        self.assertEqual(WishlistItem.objects.get().name, 'Shoes v2')

    def test_toggle_adds_then_removes(self):
        response = self.client.post(
            reverse('wishlist-toggle'), self.payload(), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json(), {'added': True})

        response = self.client.post(
            reverse('wishlist-toggle'), self.payload(), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {'added': False})
        self.assertFalse(WishlistItem.objects.filter(user=self.user).exists())

    def test_bulk_delete(self):
        for product_id in ('prod-1', 'prod-2'):
            WishlistItem.objects.create(user=self.user, product_id=product_id)

        response = self.client.post(reverse('wishlist-bulk-delete'), {
            'productIds': ['prod-1', 'prod-2', 'missing'],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {'deleted': 2})
        self.assertFalse(WishlistItem.objects.filter(user=self.user).exists())

        response = self.client.post(
            reverse('wishlist-bulk-delete'), {'productIds': 'prod-1'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_detail_is_addressed_by_product_id(self):
        WishlistItem.objects.create(user=self.user, product_id='prod-1', name='Shoes')

        response = self.client.get(reverse('wishlist-detail', args=['prod-1']))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['productId'], 'prod-1')

        response = self.client.delete(reverse('wishlist-detail', args=['prod-1']))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(WishlistItem.objects.filter(user=self.user).exists())

    def test_rows_are_private_to_the_caller(self):
        WishlistItem.objects.create(user=self.other, product_id='prod-1')

        response = self.client.get(reverse('wishlist-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(rows(response), [])

        response = self.client.get(reverse('wishlist-detail', args=['prod-1']))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class AddressTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='shopper', email='shopper@example.com', password='password'
        )
        self.other = User.objects.create_user(
            username='other', email='other@example.com', password='password'
        )
        self.client.force_authenticate(user=self.user)

    def payload(self, **overrides):
        data = {
            'tag': 'Ofisini',
            'name': 'Asha Mwangi',
            'phone': '+255700000001',
            'street': 'Samora Ave 12',
            'city': 'Dar es Salaam',
            'isDefault': True,
        }
        data.update(overrides)
        return data

    def test_create_returns_camel_case_row(self):
        response = self.client.post(
            reverse('address-list'), self.payload(), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        self.assertEqual(body['tag'], 'Ofisini')
        self.assertEqual(body['isDefault'], True)
        self.assertIn('createdAt', body)

        address = CustomerAddress.objects.get(user=self.user)
        self.assertEqual(address.street, 'Samora Ave 12')
        self.assertTrue(address.is_default)

    def test_create_defaults_is_default_to_false(self):
        response = self.client.post(
            reverse('address-list'), self.payload(isDefault=False), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(CustomerAddress.objects.get().is_default)

    def test_list_and_delete(self):
        CustomerAddress.objects.create(user=self.user, tag='Nyumbani', name='Home')
        response = self.client.get(reverse('address-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        listed = rows(response)
        self.assertEqual(len(listed), 1)

        response = self.client.delete(
            reverse('address-detail', args=[listed[0]['id']])
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(CustomerAddress.objects.filter(user=self.user).exists())

    def test_rows_are_private_to_the_caller(self):
        CustomerAddress.objects.create(user=self.other, tag='Kazi', name='Theirs')

        response = self.client.get(reverse('address-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(rows(response), [])

        response = self.client.delete(
            reverse('address-detail', args=[str(CustomerAddress.objects.get().pk)])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse('address-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class StaffUserTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username='admin', email='admin@example.com', password='password',
            is_staff=True,
        )
        self.target = User.objects.create_user(
            username='merchant', email='merchant@example.com', password='password',
            display_name='Merchant One', phone='255700000001',
            business_profile={'companyName': 'Acme', 'status': 'pending'},
        )
        self.superuser = User.objects.create_superuser(
            username='root', email='root@example.com', password='password'
        )

    def test_list_requires_staff(self):
        self.client.force_authenticate(user=self.target)
        response = self.client.get(reverse('platform-user-list'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(reverse('platform-user-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(rows(response)), 3)

    def test_search_matches_email_name_or_phone(self):
        self.client.force_authenticate(user=self.staff)
        for term in ('merchant@example.com', 'Merchant One', '255700000001'):
            response = self.client.get(
                reverse('platform-user-list'), {'search': term}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(
                [row['email'] for row in rows(response)], ['merchant@example.com']
            )

    def test_business_profile_merges_on_patch(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            reverse('platform-user-detail', args=[str(self.target.pk)]),
            {'businessProfile': {'status': 'approved'}},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.target.refresh_from_db()
        self.assertEqual(self.target.business_profile, {
            'companyName': 'Acme', 'status': 'approved',
        })
        self.assertEqual(response.json()['businessProfile']['companyName'], 'Acme')

    def test_business_status_filter_drives_the_applications_queue(self):
        User.objects.create_user(
            username='approved', email='approved@example.com', password='password',
            business_profile={'companyName': 'Beta', 'status': 'approved'},
        )
        User.objects.create_user(
            username='plain', email='plain@example.com', password='password',
        )

        self.client.force_authenticate(user=self.staff)
        for param in ('business_status', 'businessStatus'):
            response = self.client.get(
                reverse('platform-user-list'), {param: 'PENDING'}
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(
                [row['email'] for row in rows(response)], ['merchant@example.com']
            )

    def test_corporate_profile_merges_on_patch(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            reverse('platform-user-detail', args=[str(self.target.pk)]),
            {'corporateProfile': {
                'companyId': 'company-acme', 'companyName': 'Acme Ltd',
                'role': 'approver', 'creditLimit': 5000000,
            }},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()['corporateProfile']
        self.assertEqual(body['companyId'], 'company-acme')
        self.assertEqual(body['role'], 'approver')

        self.target.refresh_from_db()
        self.assertEqual(self.target.corporate_profile['companyName'], 'Acme Ltd')

        # A later partial patch must not wipe the stored membership.
        response = self.client.patch(
            reverse('platform-user-detail', args=[str(self.target.pk)]),
            {'corporateProfile': {'creditBalance': 120000}},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.target.refresh_from_db()
        self.assertEqual(self.target.corporate_profile, {
            'companyId': 'company-acme', 'companyName': 'Acme Ltd',
            'role': 'approver', 'creditLimit': 5000000, 'creditBalance': 120000,
        })

    def test_detail_resolves_firebase_uid_and_echoes_it(self):
        self.target.firebase_uid = 'firebase-uid-1'
        self.target.save(update_fields=['firebase_uid'])

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            reverse('platform-user-detail', args=['firebase-uid-1'])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['id'], 'firebase-uid-1')

    def test_system_admin_toggle(self):
        self.client.force_authenticate(user=self.staff)
        url = reverse('platform-user-system-admin', args=[str(self.target.pk)])

        response = self.client.post(url, {'isSystemAdmin': True}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()['isStaff'])
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_staff)

        response = self.client.post(url, {'isSystemAdmin': False}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_staff)

    def test_delete_rejects_self_and_superusers(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.delete(
            reverse('platform-user-detail', args=[str(self.staff.pk)])
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.delete(
            reverse('platform-user-detail', args=[str(self.superuser.pk)])
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_delete_removes_the_user(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.delete(
            reverse('platform-user-detail', args=[str(self.target.pk)])
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(User.objects.filter(pk=self.target.pk).exists())

    def test_create_is_not_supported(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            reverse('platform-user-list'), {'email': 'new@example.com'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class SocialLogTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='owner', email='owner@example.com', password='password'
        )
        self.shop = Shop.objects.create(name='Test Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        SocialLog.objects.create(
            shop=self.shop, type='social_post', action='post', status='success',
            product_ids=['prod-1'], facebook_post_id='fb-1',
        )
        self.other_shop = Shop.objects.create(name='Other Shop')
        SocialLog.objects.create(
            shop=self.other_shop, type='social_post', action='post', status='failure',
            error='boom',
        )
        self.client.force_authenticate(user=self.user)

    def test_list_is_scoped_to_the_users_shops(self):
        response = self.client.get(reverse('social-log-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        logs = rows(response)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['status'], 'success')
        self.assertEqual(logs[0]['productIds'], ['prod-1'])
        self.assertEqual(logs[0]['facebookPostId'], 'fb-1')

    def test_list_accepts_the_shop_filter(self):
        response = self.client.get(
            reverse('social-log-list'), {'shop_id': str(self.other_shop.pk)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(rows(response), [])

    def test_read_only(self):
        response = self.client.post(
            reverse('social-log-list'), {'status': 'success'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
