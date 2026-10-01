from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

# pyrefly: ignore [missing-import]
from apps.products.models import Category, Inventory, InventoryMovement, Product
# pyrefly: ignore [missing-import]
from apps.sales.models import Order, OrderItem
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop

User = get_user_model()


class PublicStorefrontTests(TestCase):
    """Anonymous storefront surface: no token, no membership, read-only."""

    def setUp(self):
        self.client = APIClient()
        self.public_shop = Shop.objects.create(
            name='Mama Shop', legacy_id='shop-public-1', slug='mama-shop',
            isPublic=True, isWholesaleSupplier=True, phone='0712000000',
            location='Kariakoo, Dar es Salaam', whatsapp='0712000001',
        )
        self.private_shop = Shop.objects.create(
            name='Hidden Shop', legacy_id='shop-private-1', slug='hidden-shop',
            isPublic=False,
        )
        self.category = Category.objects.create(shop=self.public_shop, name='Drinks')
        self.product = Product.objects.create(
            shop=self.public_shop, category=self.category, legacy_id='prod-1',
            name='Soda Crate', selling_price='12000.00', wholesale_price='11000.00',
            buying_price='9000.00', supplier='Crate Supplier', supplier_shop_id='shop-9',
            publish_to_directory=True, status='active',
        )
        self.hidden_product = Product.objects.create(
            shop=self.public_shop, name='Hidden Product', selling_price='500.00',
            publish_to_directory=False, status='active',
        )
        self.inactive_product = Product.objects.create(
            shop=self.public_shop, name='Discontinued Product', selling_price='500.00',
            publish_to_directory=True, status='discontinued',
        )

    # --- shops ---

    def test_anonymous_list_returns_paginated_shops(self):
        response = self.client.get('/api/v1/public/shops/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        names = {row['name'] for row in response.data['results']}
        self.assertEqual(names, {'Mama Shop', 'Hidden Shop'})

    def test_list_filters_public_and_wholesale(self):
        public = self.client.get('/api/v1/public/shops/', {'is_public': 'true'})
        self.assertEqual(public.data['count'], 1)
        self.assertEqual(public.data['results'][0]['legacyId'], 'shop-public-1')

        wholesale = self.client.get('/api/v1/public/shops/', {'isWholesaleSupplier': 'true'})
        self.assertEqual(wholesale.data['count'], 1)

        searched = self.client.get('/api/v1/public/shops/', {'search': 'kariakoo'})
        self.assertEqual(searched.data['count'], 1)

    def test_detail_by_slug_legacy_id_and_uuid(self):
        for identifier in ('mama-shop', 'shop-public-1', str(self.public_shop.id)):
            response = self.client.get(f'/api/v1/public/shops/{identifier}/')
            self.assertEqual(response.status_code, status.HTTP_200_OK, identifier)
            self.assertEqual(response.data['name'], 'Mama Shop')
            self.assertEqual(response.data['whatsappNumber'], '0712000001')

    def test_detail_falls_back_to_frontend_slug_of_name(self):
        Shop.objects.create(name="Mama's Shop", isPublic=True)

        response = self.client.get("/api/v1/public/shops/mama-s-shop/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], "Mama's Shop")

    def test_unknown_detail_is_404(self):
        response = self.client.get('/api/v1/public/shops/no-such-shop/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_shop_payload_carries_address_fallback_fields(self):
        # supplierMapping.ts falls back to district/region/country when the
        # shop's free-text location is empty (DiscoverSuppliers link dialog).
        Shop.objects.filter(pk=self.public_shop.pk).update(
            country='Tanzania', region='Dar es Salaam', district='Ilala')

        response = self.client.get('/api/v1/public/shops/mama-shop/')

        self.assertEqual(response.data['country'], 'Tanzania')
        self.assertEqual(response.data['region'], 'Dar es Salaam')
        self.assertEqual(response.data['district'], 'Ilala')

    def test_non_public_shop_is_readable_by_direct_link(self):
        # Firestore allowed ``read: if true`` on shops, so a shared link to a
        # non-public shop still renders; only directory listings filter it out.
        response = self.client.get('/api/v1/public/shops/hidden-shop/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_invalid_token_does_not_block_public_reads(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer not-a-real-token')
        response = self.client.get('/api/v1/public/shops/mama-shop/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # --- products ---

    def test_shop_products_are_public_and_filtered(self):
        # ``publishToDirectory !== false`` on Firestore, no status filter: the
        # storefront pages filter status client-side, so a discontinued row must
        # still come through (ShopProductGrid renders it today).
        response = self.client.get('/api/v1/public/shops/shop-public-1/products/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        rows = {row['name']: row for row in response.data['results']}
        self.assertEqual(set(rows), {'Soda Crate', 'Discontinued Product'})
        soda = rows['Soda Crate']
        self.assertEqual(soda['legacyId'], 'prod-1')
        self.assertEqual(soda['shopId'], 'shop-public-1')
        self.assertEqual(soda['category'], 'Drinks')
        self.assertEqual(soda['sellingPrice'], '12000.00')

    def test_products_hidden_until_shop_is_approved(self):
        # The AdminShops "Approve" toggle (``isPublic``) is the last check: an
        # unapproved shop serves no products on the public surface, then flips
        # visible the moment the platform admin approves it.
        Product.objects.create(
            shop=self.private_shop, name='Private Stock', selling_price='900.00',
            publish_to_directory=True, status='active',
        )
        hidden = self.client.get('/api/v1/public/shops/shop-private-1/products/')
        self.assertEqual(hidden.status_code, status.HTTP_200_OK)
        self.assertEqual(hidden.data['count'], 0)

        Shop.objects.filter(pk=self.private_shop.pk).update(isPublic=True)
        approved = self.client.get('/api/v1/public/shops/shop-private-1/products/')
        self.assertEqual(approved.status_code, status.HTTP_200_OK)
        self.assertEqual(approved.data['count'], 1)
        self.assertEqual(approved.data['results'][0]['name'], 'Private Stock')

    def test_shop_products_hide_merchant_only_fields(self):
        response = self.client.get('/api/v1/public/shops/mama-shop/products/')

        for row in response.data['results']:
            for key in ('buyingPrice', 'buying_price', 'supplier', 'supplierShopId',
                        'supplier_shop_id', 'sourceProductId', 'attributes', 'variants'):
                self.assertNotIn(key, row)

    def test_shop_products_carry_stock_and_min_stock(self):
        # The storefront used to merge a separate inventory read client-side;
        # these two fields are that read, folded into the product payload.
        branch = Branch.objects.create(shop=self.public_shop, name='Main Branch', is_main=True)
        second = Branch.objects.create(shop=self.public_shop, name='Kariakoo', is_main=False)
        Inventory.objects.create(product=self.product, branch=branch, quantity=7,
                                 low_stock_threshold=3)
        Inventory.objects.create(product=self.product, branch=second, quantity=5,
                                 low_stock_threshold=9)

        response = self.client.get('/api/v1/public/shops/mama-shop/products/')

        rows = {row['name']: row for row in response.data['results']}
        self.assertEqual(rows['Soda Crate']['stock'], 12)
        self.assertEqual(rows['Soda Crate']['minStock'], 3)
        # No inventory row at all reads as out of stock, exactly like the
        # ``stockMap.get(id) ?? 0`` the pages used to compute.
        self.assertEqual(rows['Discontinued Product']['stock'], 0)
        self.assertEqual(rows['Discontinued Product']['minStock'], 5)

    def test_shop_payload_carries_store_policies_and_online_store(self):
        Shop.objects.filter(pk=self.public_shop.pk).update(
            store_policies={'returnsPolicy': '7 days', 'shippingPolicy': 'Dar only'},
            online_store_settings={'enabled': True, 'themeColor': '#0f766e',
                                   'pickupAvailable': True},
        )

        response = self.client.get('/api/v1/public/shops/mama-shop/')

        self.assertEqual(response.data['storePolicies']['returnsPolicy'], '7 days')
        self.assertEqual(response.data['storePolicies']['shippingPolicy'], 'Dar only')
        self.assertEqual(response.data['onlineStore']['themeColor'], '#0f766e')
        self.assertTrue(response.data['onlineStore']['pickupAvailable'])

    def test_shop_products_paginate(self):
        for index in range(3):
            Product.objects.create(
                shop=self.public_shop, name=f'Extra {index}', selling_price='100.00',
                publish_to_directory=True, status='active',
            )

        response = self.client.get('/api/v1/public/shops/mama-shop/products/', {'page_size': 2})

        self.assertEqual(response.data['count'], 5)
        self.assertEqual(len(response.data['results']), 2)
        self.assertTrue(response.data['next'])

    def test_unknown_shop_products_are_404(self):
        response = self.client.get('/api/v1/public/shops/no-such-shop/products/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PublicShopFollowTests(TestCase):
    """Follow/unfollow mirror the Firestore rule that let any signed-in user
    bump ``followerCount`` and nothing else."""

    def setUp(self):
        self.client = APIClient()
        self.shop = Shop.objects.create(
            name='Mama Shop', legacy_id='shop-public-1', slug='mama-shop',
            isPublic=True, follower_count=7,
        )
        self.user = User.objects.create_user(username='follower', password='pw-123456')

    def authenticate(self):
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def test_follower_count_is_part_of_the_public_payload(self):
        response = self.client.get('/api/v1/public/shops/mama-shop/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['followerCount'], 7)

    def test_follow_requires_authentication(self):
        anonymous = self.client.post('/api/v1/public/shops/mama-shop/follow/')
        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)

        self.client.credentials(HTTP_AUTHORIZATION='Bearer not-a-real-token')
        invalid = self.client.post('/api/v1/public/shops/mama-shop/follow/')
        self.assertEqual(invalid.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_follow_and_unfollow_adjust_counter(self):
        self.authenticate()

        followed = self.client.post('/api/v1/public/shops/mama-shop/follow/')
        self.assertEqual(followed.status_code, status.HTTP_200_OK)
        self.assertEqual(followed.data['followerCount'], 8)

        followed_again = self.client.post('/api/v1/public/shops/shop-public-1/follow/')
        self.assertEqual(followed_again.data['followerCount'], 9)

        unfollowed = self.client.post('/api/v1/public/shops/mama-shop/unfollow/')
        self.assertEqual(unfollowed.data['followerCount'], 8)

        self.shop.refresh_from_db()
        self.assertEqual(self.shop.follower_count, 8)

    def test_unfollow_never_goes_below_zero(self):
        Shop.objects.filter(pk=self.shop.pk).update(follower_count=0)
        self.authenticate()

        response = self.client.post('/api/v1/public/shops/mama-shop/unfollow/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['followerCount'], 0)

    def test_follow_unknown_shop_is_404(self):
        self.authenticate()
        response = self.client.post('/api/v1/public/shops/no-such-shop/follow/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PublicWishlistOrderTests(TestCase):
    """Wishlist checkout: the Firestore ``shops/{id}/orders`` append is now a POST.

    Firestore accepted the write from any visitor and trusted the client prices;
    the Django endpoint keeps the visitor rule but re-prices every line from the
    product rows, and moves no stock until the merchant confirms on WhatsApp.
    """

    PAYLOAD = {
        'orderId': 'ORD-123456',
        'customerName': 'Asha Juma',
        'customerPhone': '0712000000',
        'customerAddress': 'Sinza, Dar es Salaam',
        'notes': 'Deliver in the morning',
        'items': [{'productId': 'prod-1', 'productName': 'Soda Crate',
                   'quantity': 2, 'price': 1}],
    }

    def setUp(self):
        self.client = APIClient()
        self.shop = Shop.objects.create(
            name='Mama Shop', legacy_id='shop-public-1', slug='mama-shop',
            isPublic=True, whatsapp='0712000001',
        )
        self.branch = Branch.objects.create(shop=self.shop, name='Main Branch', is_main=True)
        self.product = Product.objects.create(
            shop=self.shop, legacy_id='prod-1', name='Soda Crate',
            selling_price='12000.00', wholesale_price='11000.00', moq=5,
            buying_price='9000.00', publish_to_directory=True, status='active',
        )

    def post_order(self, payload=None):
        return self.client.post('/api/v1/public/shops/mama-shop/orders/',
                                payload or dict(self.PAYLOAD), format='json')

    def authenticate(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def test_anonymous_checkout_prices_from_the_product_row(self):
        response = self.post_order()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['orderId'], 'ORD-123456')
        self.assertEqual(response.data['legacyId'], 'ORD-123456')
        self.assertEqual(response.data['source'], 'wishlist')
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['customerAddress'], 'Sinza, Dar es Salaam')
        # The client sent ``price: 1``; the server re-read 12000 from the product.
        self.assertEqual(response.data['items'][0]['unitPrice'], '12000.00')
        self.assertEqual(response.data['items'][0]['price'], '12000.00')
        self.assertEqual(response.data['totalAmount'], '24000.00')

        order = Order.objects.get(legacy_id='ORD-123456')
        self.assertEqual(order.shop_id, self.shop.pk)
        self.assertEqual(order.branch_id, self.branch.pk)
        self.assertEqual(order.customer_name, 'Asha Juma')
        self.assertEqual(order.customer_phone, '0712000000')
        self.assertEqual(order.customer_user_id, None)
        self.assertEqual(order.fulfillment_details['deliveryMethod'], 'pickup')
        self.assertEqual(order.fulfillment_details['deliveryAddress'], 'Sinza, Dar es Salaam')

    def test_wholesale_price_applies_at_the_moq(self):
        payload = dict(self.PAYLOAD)
        payload['items'] = [{**self.PAYLOAD['items'][0], 'quantity': 5}]

        response = self.post_order(payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['items'][0]['unitPrice'], '11000.00')
        self.assertEqual(response.data['totalAmount'], '55000.00')

    def test_replaying_the_same_order_id_does_not_duplicate(self):
        first = self.post_order()
        second = self.post_order()

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(first.data['id'], second.data['id'])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)

    def test_order_number_reuse_by_another_shop_is_rejected(self):
        other = Shop.objects.create(name='Other Shop', legacy_id='shop-other',
                                    slug='other-shop', isPublic=True)
        Branch.objects.create(shop=other, name='Main Branch', is_main=True)
        self.post_order()

        response = self.client.post('/api/v1/public/shops/other-shop/orders/',
                                    dict(self.PAYLOAD), format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.count(), 1)

    def test_signed_in_shopper_sees_the_order_in_the_portal(self):
        user = User.objects.create_user(username='asha', password='pw-123456',
                                        phone='0712000000')
        self.authenticate(user)

        response = self.post_order()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = Order.objects.get(legacy_id='ORD-123456')
        self.assertEqual(order.customer_user_id, user.pk)

        portal = self.client.get('/api/v1/portal/orders/')
        self.assertEqual(portal.status_code, status.HTTP_200_OK)
        self.assertEqual(portal.data['count'], 1)
        self.assertEqual(portal.data['results'][0]['orderId'], 'ORD-123456')

    def test_guest_phone_links_to_a_platform_account(self):
        user = User.objects.create_user(username='asha', password='pw-123456',
                                        phone='0712000000')

        self.post_order()

        order = Order.objects.get(legacy_id='ORD-123456')
        self.assertEqual(order.customer_user_id, user.pk)

    def test_stock_is_not_moved_until_the_merchant_confirms(self):
        self.post_order()

        self.assertFalse(Inventory.objects.exists())
        self.assertFalse(InventoryMovement.objects.exists())

    def test_missing_details_are_rejected(self):
        for field in ('orderId', 'customerName', 'customerPhone'):
            payload = {key: value for key, value in self.PAYLOAD.items() if key != field}
            response = self.post_order(payload)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, field)
            self.assertIn(field, response.data, field)

        self.assertFalse(Order.objects.exists())

    def test_empty_items_are_rejected(self):
        response = self.post_order({**self.PAYLOAD, 'items': []})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('items', response.data)

    def test_unknown_shop_is_404(self):
        response = self.client.post('/api/v1/public/shops/no-such-shop/orders/',
                                    dict(self.PAYLOAD), format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_token_is_rejected(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer not-a-real-token')
        response = self.post_order()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_branch_is_provisioned_when_the_shop_has_none(self):
        """Imported shops that never sold in Firestore have no branch row.

        A shopper cannot create one, so the checkout derives a Main Branch the
        same way ``import_firestore._main_branch`` does instead of dead-ending.
        """
        bare = Shop.objects.create(
            name='Branchless Shop', legacy_id='shop-public-2', slug='branchless-shop',
            isPublic=True,
        )
        Product.objects.create(
            shop=bare, legacy_id='prod-2', name='Unga',
            selling_price='3500.00', moq=1, status='active',
        )

        response = self.client.post(
            '/api/v1/public/shops/branchless-shop/orders/',
            {**self.PAYLOAD, 'orderId': 'ORD-654321',
             'items': [{'productId': 'prod-2', 'productName': 'Unga',
                        'quantity': 3, 'price': 1}]},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = Order.objects.get(legacy_id='ORD-654321')
        self.assertEqual(order.branch.shop_id, bare.pk)
        self.assertTrue(order.branch.is_main)
        self.assertEqual(order.branch.name, 'Main Branch')
        self.assertEqual(order.total_amount, Decimal('10500.00'))
