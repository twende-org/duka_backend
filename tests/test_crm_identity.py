"""Identity resolution: legacy ``resolveIdentity`` and the ``linkIdentity`` trigger.

The endpoint runs once per login with the profile's phone/email, so these tests
pin down who gets linked (unclaimed rows only), what the count reports
(every match, Firestore parity), and that historical sales/orders follow.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.crm.models import Customer
# pyrefly: ignore [missing-import]
from apps.sales.models import Order, Sale
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop
# pyrefly: ignore [missing-import]
from apps.users.models import User

RESOLVE_URL = '/api/v1/identity/resolve/'
PROFILE_URL = '/api/users/me/'


class IdentityBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='buyer', email='buyer@test.com', password='password', phone='0712345678')
        self.client.force_authenticate(user=self.user)
        self.other = User.objects.create_user(
            username='other', email='other@test.com', password='password', phone='0655999999')

        self.shop = Shop.objects.create(name='Duka A')
        self.branch = Branch.objects.create(shop=self.shop, name='Main', is_main=True)
        self.other_shop = Shop.objects.create(name='Duka B')
        self.other_branch = Branch.objects.create(shop=self.other_shop, name='Main', is_main=True)

    def resolve(self, **payload):
        return self.client.post(RESOLVE_URL, payload, format='json')

    def make_sale(self, shop=None, branch=None, **overrides):
        data = dict(shop=shop or self.shop, branch=branch or self.branch,
                    payment_method='cash', total_amount=Decimal('5000.00'), status='completed')
        data.update(overrides)
        return Sale.objects.create(**data)

    def make_order(self, shop=None, branch=None, **overrides):
        data = dict(shop=shop or self.shop, branch=branch or self.branch,
                    total_amount=Decimal('12000.00'), status='pending')
        data.update(overrides)
        return Order.objects.create(**data)


class ResolveIdentityAccessTests(IdentityBase):
    def test_requires_authentication(self):
        response = APIClient().post(RESOLVE_URL, {'phone': '0712345678'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_requires_phone_or_email(self):
        for payload in ({}, {'phone': ''}, {'phone': '  ', 'email': None}):
            response = self.resolve(**payload)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(
                response.json()['detail'],
                ['Must provide either phone or email to resolve identity.'],
            )

    def test_response_shape(self):
        self.assertEqual(self.resolve(phone='0712345678').json(),
                         {'success': True, 'linkedCount': 0})


class ResolveIdentityLinkingTests(IdentityBase):
    def test_links_unclaimed_row_and_backfills_history(self):
        customer = Customer.objects.create(shop=self.shop, name='Buyer', phone='255712345678',
                                           user_id='', legacy_id='cust-1')
        sale = self.make_sale(customer_id='cust-1')
        order = self.make_order(customer_id='cust-1')

        response = self.resolve(phone='0712345678')

        self.assertEqual(response.json(), {'success': True, 'linkedCount': 1})
        customer.refresh_from_db()
        self.assertEqual(customer.user_id, str(self.user.pk))
        self.assertIsNotNone(customer.linked_at)
        sale.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(sale.customer_user, self.user)
        self.assertEqual(order.customer_user, self.user)

    def test_matching_is_shop_agnostic(self):
        Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678',
                                user_id='', legacy_id='cust-a')
        Customer.objects.create(shop=self.other_shop, name='Buyer', phone='+255712345678',
                                user_id='', legacy_id='cust-b')
        self.assertEqual(self.resolve(phone='0712345678').json()['linkedCount'], 2)
        self.assertEqual(Customer.objects.filter(user_id=str(self.user.pk)).count(), 2)

    def test_email_match_ignores_case(self):
        customer = Customer.objects.create(shop=self.shop, name='Buyer',
                                           email='Buyer@Test.com', user_id='')
        self.assertEqual(self.resolve(email='buyer@test.com').json()['linkedCount'], 1)
        customer.refresh_from_db()
        self.assertEqual(customer.user_id, str(self.user.pk))

    def test_row_matched_by_phone_and_email_counts_once(self):
        Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678',
                                email='buyer@test.com', user_id='')
        response = self.resolve(phone='0712345678', email='buyer@test.com')
        self.assertEqual(response.json()['linkedCount'], 1)

    def test_row_owned_by_another_account_is_counted_but_not_touched(self):
        customer = Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678',
                                           user_id=str(self.other.pk), legacy_id='cust-2')
        sale = self.make_sale(customer_id='cust-2')

        response = self.resolve(phone='0712345678')

        self.assertEqual(response.json()['linkedCount'], 1)  # Firestore parity
        customer.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(customer.user_id, str(self.other.pk))
        self.assertIsNone(sale.customer_user)

    def test_row_already_owned_by_caller_is_backfilled_not_restamped(self):
        customer = Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678',
                                           user_id=str(self.user.pk), legacy_id='cust-3')
        original_linked_at = customer.linked_at
        sale = self.make_sale(customer_id='cust-3')

        response = self.resolve(phone='0712345678')

        self.assertEqual(response.json()['linkedCount'], 1)
        customer.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(customer.linked_at, original_linked_at)
        self.assertEqual(sale.customer_user, self.user)

    def test_firebase_uid_link_is_recognised_as_owned_by_caller(self):
        self.user.firebase_uid = 'fb-uid-1'
        self.user.save(update_fields=['firebase_uid'])
        customer = Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678',
                                           user_id='fb-uid-1', legacy_id='cust-4')
        sale = self.make_sale(customer_id='cust-4')

        self.resolve(phone='0712345678')

        customer.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(customer.user_id, 'fb-uid-1')
        self.assertEqual(sale.customer_user, self.user)

    def test_backfill_repairs_django_uuid_customer_id(self):
        customer = Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678',
                                           user_id='')
        sale = self.make_sale(customer_id=str(customer.pk))
        self.resolve(phone='0712345678')
        sale.refresh_from_db()
        self.assertEqual(sale.customer_user, self.user)

    def test_second_call_is_idempotent(self):
        Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678', user_id='')
        self.assertEqual(self.resolve(phone='0712345678').json()['linkedCount'], 1)
        self.assertEqual(self.resolve(phone='0712345678').json()['linkedCount'], 1)

    def test_unrelated_phones_are_not_linked(self):
        Customer.objects.create(shop=self.shop, name='Someone', phone='0788888888', user_id='')
        self.assertEqual(self.resolve(phone='0712345678').json()['linkedCount'], 0)


class ProfilePhoneChangeTests(IdentityBase):
    def test_phone_change_links_matching_rows(self):
        customer = Customer.objects.create(shop=self.shop, name='Buyer', phone='0755111222',
                                           user_id='', legacy_id='cust-9')
        sale = self.make_sale(customer_id='cust-9')

        response = self.client.patch(PROFILE_URL, {'phone': '0755111222'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        customer.refresh_from_db()
        sale.refresh_from_db()
        self.assertEqual(customer.user_id, str(self.user.pk))
        self.assertEqual(sale.customer_user, self.user)

    def test_other_profile_fields_do_not_trigger_linking(self):
        Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678', user_id='')
        response = self.client.patch(PROFILE_URL, {'display_name': 'New Name'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(Customer.objects.filter(user_id=str(self.user.pk)).exists())

    def test_clearing_phone_does_not_link(self):
        Customer.objects.create(shop=self.shop, name='Buyer', phone='0712345678', user_id='')
        response = self.client.patch(PROFILE_URL, {'phone': ''}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(Customer.objects.filter(user_id=str(self.user.pk)).exists())
