from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.products.models import Category, Inventory, Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop
# pyrefly: ignore [missing-import]
from apps.telemetry.models import AnalyticsEvent


class PublicSearchAPITests(TestCase):
    """Anonymous cross-shop search surface (/public/products/search/).

    Typo-fuzzy recall is Postgres-only (trigram candidates); these tests run on
    SQLite, so they assert the exact/substring ladder the API guarantees on
    every backend and leave the fuzzy ladder to tests.test_search.
    """

    def setUp(self):
        self.client = APIClient()
        self.public_shop = Shop.objects.create(
            name='Mama Electronics', legacy_id='shop-search-api-1', slug='mama-electronics',
            isPublic=True, location='Kariakoo, Dar es Salaam', phone='0713000000',
        )
        self.private_shop = Shop.objects.create(
            name='Secret Shop', legacy_id='shop-search-api-2', slug='secret-shop',
            isPublic=False,
        )
        self.phones = Category.objects.create(shop=self.public_shop, name='Phones')
        self.drinks = Category.objects.create(shop=self.public_shop, name='Drinks')

        self.samsung = Product.objects.create(
            shop=self.public_shop, category=self.phones, legacy_id='search-prod-1',
            name='Samsung Galaxy A15', brand='Samsung', selling_price='450000.00',
            publish_to_directory=True, status='active',
        )
        self.kettle = Product.objects.create(
            shop=self.public_shop, category=self.drinks, legacy_id='search-prod-2',
            name='Electric Kettle', barcode='1234567890123', selling_price='35000.00',
            publish_to_directory=True, status='active',
        )
        self.kettle_twin = Product.objects.create(
            shop=self.public_shop, category=self.drinks, legacy_id='search-prod-3',
            name='Kettle 1234567890123', selling_price='32000.00',
            publish_to_directory=True, status='active',
        )
        self.soda = Product.objects.create(
            shop=self.public_shop, category=self.drinks, legacy_id='search-prod-4',
            name='Soda Crate', sku='AB-100', selling_price='12000.00',
            publish_to_directory=True, status='active',
        )
        # Excluded by the two gates search shares with every storefront read:
        # the draft is unpublished and the phone sits in an unapproved shop.
        self.hidden_draft = Product.objects.create(
            shop=self.public_shop, name='Hidden Draft', selling_price='1000.00',
            publish_to_directory=False, status='active',
        )
        self.secret_phone = Product.objects.create(
            shop=self.private_shop, name='Secret Phone', selling_price='1000.00',
            publish_to_directory=True, status='active',
        )

    def test_anonymous_search_returns_matching_cards(self):
        branch = Branch.objects.create(shop=self.public_shop, name='Main Branch', is_main=True)
        Inventory.objects.create(product=self.samsung, branch=branch, quantity=9,
                                 low_stock_threshold=3)

        response = self.client.get('/api/v1/public/products/search/', {'q': 'samsung'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        row = response.data['results'][0]
        self.assertEqual(row['name'], 'Samsung Galaxy A15')
        self.assertEqual(row['legacyId'], 'search-prod-1')
        self.assertEqual(row['shopId'], 'shop-search-api-1')
        self.assertEqual(row['category'], 'Phones')
        self.assertEqual(row['sellingPrice'], '450000.00')
        # stock/minStock replace the separate inventory read the storefront
        # pages used to merge client-side; they must survive the ranked path.
        self.assertEqual(row['stock'], 9)
        self.assertEqual(row['minStock'], 3)

        alias = self.client.get('/api/v1/public/products/search/', {'search': 'samsung'})
        self.assertEqual(alias.data['count'], 1)

    def test_exact_barcode_outranks_name_occurrence(self):
        # The same digits live in one product's barcode column and inside
        # another's name; barcode carries the higher weight, so an exact code
        # lookup lands first.
        response = self.client.get('/api/v1/public/products/search/', {'q': '1234567890123'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(
            [row['name'] for row in response.data['results']],
            ['Electric Kettle', 'Kettle 1234567890123'],
        )

    def test_all_terms_must_match(self):
        both = self.client.get('/api/v1/public/products/search/', {'q': 'samsung phone'})
        self.assertEqual(both.data['count'], 1)
        self.assertEqual(both.data['results'][0]['name'], 'Samsung Galaxy A15')

        none = self.client.get('/api/v1/public/products/search/', {'q': 'soda kettle'})
        self.assertEqual(none.data['count'], 0)

    def test_swahili_query_expands_to_english_synonyms(self):
        # 'simu' appears nowhere literally; the synonym dictionary expands it
        # to 'phone', which hits the Phones category name.
        response = self.client.get('/api/v1/public/products/search/', {'q': 'simu'})

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'Samsung Galaxy A15')

    def test_search_gates_hide_unpublished_and_unapproved(self):
        draft = self.client.get('/api/v1/public/products/search/', {'q': 'draft'})
        self.assertEqual(draft.data['count'], 0)

        secret = self.client.get('/api/v1/public/products/search/', {'q': 'secret'})
        self.assertEqual(secret.data['count'], 0)

    def test_category_filter_narrows_search(self):
        drinks = self.client.get('/api/v1/public/products/search/', {'category': 'Drinks'})
        self.assertEqual(drinks.data['count'], 3)
        self.assertEqual({row['category'] for row in drinks.data['results']}, {'Drinks'})

        lower = self.client.get('/api/v1/public/products/search/', {'category': 'drinks'})
        self.assertEqual(lower.data['count'], 3)

        combined = self.client.get('/api/v1/public/products/search/',
                                   {'q': 'kettle', 'category': 'Drinks'})
        self.assertEqual(combined.data['count'], 2)

        empty = self.client.get('/api/v1/public/products/search/',
                                {'q': 'soda', 'category': 'Phones'})
        self.assertEqual(empty.data['count'], 0)

    def test_shop_filter_scopes_and_private_shop_is_empty(self):
        scoped = self.client.get('/api/v1/public/products/search/', {'shop': 'mama-electronics'})
        self.assertEqual(scoped.data['count'], 4)
        for row in scoped.data['results']:
            self.assertEqual(row['shopId'], 'shop-search-api-1')

        legacy = self.client.get('/api/v1/public/products/search/', {'shop': 'shop-search-api-1'})
        self.assertEqual(legacy.data['count'], 4)

        # A non-public shop resolves by direct link (storefront rule) but its
        # products never surface on search.
        hidden = self.client.get('/api/v1/public/products/search/', {'shop': 'secret-shop'})
        self.assertEqual(hidden.status_code, status.HTTP_200_OK)
        self.assertEqual(hidden.data['count'], 0)

        missing = self.client.get('/api/v1/public/products/search/', {'shop': 'no-such-shop'})
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)

    def test_empty_query_is_browse_order(self):
        first = self.client.get('/api/v1/public/products/search/')

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data['count'], 4)
        names = {row['name'] for row in first.data['results']}
        self.assertEqual(names, {
            'Samsung Galaxy A15', 'Electric Kettle', 'Kettle 1234567890123', 'Soda Crate',
        })

        # Noise-only queries carry no signal and fall through to browse order,
        # exactly like an empty search on the frontend ladder.
        noise = self.client.get('/api/v1/public/products/search/', {'q': 'the and of'})
        self.assertEqual(noise.data['count'], 4)

        storefront = self.client.get('/api/v1/public/shops/mama-electronics/products/')
        self.assertEqual({row['name'] for row in storefront.data['results']}, names)

    def test_results_paginate_with_disjoint_pages(self):
        first = self.client.get('/api/v1/public/products/search/', {'page_size': '2'})
        self.assertEqual(first.data['count'], 4)
        self.assertEqual(len(first.data['results']), 2)

        second = self.client.get('/api/v1/public/products/search/',
                                 {'page_size': '2', 'page': '2'})
        self.assertEqual(len(second.data['results']), 2)

        first_names = {row['name'] for row in first.data['results']}
        second_names = {row['name'] for row in second.data['results']}
        self.assertEqual(first_names & second_names, set())
        self.assertEqual(first_names | second_names, {
            'Samsung Galaxy A15', 'Electric Kettle', 'Kettle 1234567890123', 'Soda Crate',
        })

    def test_search_cards_hide_merchant_only_fields(self):
        response = self.client.get('/api/v1/public/products/search/', {'q': 'kettle'})
        self.assertEqual(response.data['count'], 2)

        for row in response.data['results']:
            for key in ('buyingPrice', 'buying_price', 'supplier', 'supplierShopId',
                        'supplier_shop_id', 'sourceProductId', 'attributes', 'variants'):
                self.assertNotIn(key, row)

    def test_store_products_action_accepts_search_and_q(self):
        by_search = self.client.get('/api/v1/public/shops/mama-electronics/products/',
                                    {'search': 'AB-100'})
        self.assertEqual(by_search.status_code, status.HTTP_200_OK)
        self.assertEqual(by_search.data['count'], 1)
        self.assertEqual(by_search.data['results'][0]['name'], 'Soda Crate')

        by_q = self.client.get('/api/v1/public/shops/mama-electronics/products/',
                               {'q': 'AB-100'})
        self.assertEqual(by_q.data['count'], 1)

        empty = self.client.get('/api/v1/public/shops/mama-electronics/products/',
                                {'search': 'zzzz'})
        self.assertEqual(empty.data['count'], 0)

    def test_shop_list_search_ranks_name_above_location(self):
        Shop.objects.create(name='Kariakoo Fresh Foods', slug='kariakoo-fresh', isPublic=True)

        response = self.client.get('/api/v1/public/shops/', {'search': 'kariakoo'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        names = [row['name'] for row in response.data['results']]
        self.assertEqual(names[0], 'Kariakoo Fresh Foods')
        self.assertEqual(set(names), {'Kariakoo Fresh Foods', 'Mama Electronics'})

    def test_invalid_token_does_not_block_search(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer not-a-real-token')
        response = self.client.get('/api/v1/public/products/search/', {'q': 'samsung'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)


class PublicTrendingSearchesTests(TestCase):
    """Popular-query suggestions aggregated from the search telemetry stream."""

    def setUp(self):
        self.client = APIClient()

    def _seed(self, event_type, query, count=1):
        for _ in range(count):
            AnalyticsEvent.objects.create(event_type=event_type, query=query)

    def test_empty_stream_returns_empty_trending(self):
        response = self.client.get('/api/v1/public/trending-searches/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'trending': []})

    def test_aggregates_search_events_and_merges_case_variants(self):
        self._seed('search_query', 'Samsung', 3)
        self._seed('search_submitted', 'samsung', 1)
        self._seed('search_query', 'Soda', 2)
        self._seed('search_query', 'Pikipiki', 1)

        response = self.client.get('/api/v1/public/trending-searches/')

        # Case variants merge into one row; the most frequent spelling wins.
        self.assertEqual(response.data['trending'], [
            {'query': 'Samsung', 'count': 4},
            {'query': 'Soda', 'count': 2},
            {'query': 'Pikipiki', 'count': 1},
        ])

    def test_ignores_other_event_types_and_blank_queries(self):
        self._seed('product_view', 'Samsung')
        self._seed('page_view', 'Dodoma')
        self._seed('search_query', '   ')

        response = self.client.get('/api/v1/public/trending-searches/')

        self.assertEqual(response.data['trending'], [])

    def test_window_excludes_old_rows_and_days_param_widens_it(self):
        self._seed('search_query', 'Old Search')
        self._seed('search_query', 'Fresh Search')
        AnalyticsEvent.objects.filter(query='Old Search').update(
            created_at=timezone.now() - timedelta(days=40))

        default = self.client.get('/api/v1/public/trending-searches/')
        self.assertEqual([row['query'] for row in default.data['trending']], ['Fresh Search'])

        widened = self.client.get('/api/v1/public/trending-searches/', {'days': '60'})
        self.assertEqual(
            {row['query'] for row in widened.data['trending']},
            {'Fresh Search', 'Old Search'},
        )

    def test_caps_trending_at_eight_queries(self):
        for index in range(10):
            self._seed('search_query', f'Query {index}')

        response = self.client.get('/api/v1/public/trending-searches/')

        self.assertEqual(len(response.data['trending']), 8)
