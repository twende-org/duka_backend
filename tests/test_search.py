"""Tests for apps.core.search, the shared marketplace ranking primitive.

Scoring is pure Python on every backend, so everything here runs on SQLite.
The only vendor-conditional piece is candidate generation: the trigram leg is
asserted structurally on the ``Q`` object, since PostgreSQL is only available
in deployment.
"""
from types import SimpleNamespace

from django.db.models import Q, QuerySet
from django.test import SimpleTestCase, TestCase

from apps.core.search import (
    SearchField,
    candidates_q,
    expand_term,
    normalize_search_text,
    rank_objects,
    search_queryset,
    search_terms,
)
# pyrefly: ignore [missing-import]
from apps.products.models import Category, Product
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop

# Mirrors the public product search field map: an exact SKU/barcode match
# outranks an exact name match (12 > 10).
PRODUCT_FIELDS = (
    SearchField('name', 10),
    SearchField('sku', 12),
    SearchField('barcode', 12),
    SearchField('brand', 7),
    SearchField('description', 2),
)


def named(*names):
    return [SimpleNamespace(name=name) for name in names]


def flatten_lookups(node):
    """All (lookup, value) leaves of a Q tree, in order."""
    found = []
    for child in node.children:
        if isinstance(child, Q):
            found.extend(flatten_lookups(child))
        else:
            key, value = child
            found.append((str(key), value))
    return found


class NormalizeSearchTextTests(SimpleTestCase):
    def test_folds_accents_case_and_punctuation(self):
        self.assertEqual(normalize_search_text("Café —  Olive's, Oil!"), "cafe olive s oil")

    def test_handles_none_and_non_strings(self):
        self.assertEqual(normalize_search_text(None), "")
        self.assertEqual(normalize_search_text(12000), "12000")


class SearchTermsTests(SimpleTestCase):
    def test_strips_noise_words_in_english_and_swahili(self):
        self.assertEqual(search_terms("nataka simu"), ["simu"])
        self.assertEqual(search_terms("I want the best phone please"), ["phone"])

    def test_punctuation_splits_terms(self):
        self.assertEqual(search_terms("AB-100"), ["ab", "100"])

    def test_noise_only_or_empty_query_has_no_terms(self):
        self.assertEqual(search_terms("naomba"), [])
        self.assertEqual(search_terms("   "), [])
        self.assertEqual(search_terms(""), [])


class ExpandTermTests(SimpleTestCase):
    def test_expands_swahili_to_english_and_back(self):
        self.assertEqual(expand_term("simu")[0], "simu")
        self.assertIn("phone", expand_term("simu"))
        self.assertIn("simu", expand_term("phone"))

    def test_unknown_term_expands_to_itself_only(self):
        self.assertEqual(expand_term("zzzzz"), ["zzzzz"])


class RankObjectsTests(SimpleTestCase):
    """The score ladder: exact > prefix > word-start > substring > fuzzy."""

    FIELDS = (SearchField('name', 10),)

    def test_ladder_order(self):
        items = named(
            "MySamsungCover", "Original Samsung Case", "Samsung", "Samsung Galaxy A15",
        )

        ranked = rank_objects(items, "samsung", self.FIELDS)

        self.assertEqual(
            [item.name for item in ranked],
            ["Samsung", "Samsung Galaxy A15", "Original Samsung Case", "MySamsungCover"],
        )

    def test_every_term_must_match(self):
        items = named("Samsung TV", "Samsung Phone", "Phone Case")

        ranked = rank_objects(items, "samsung phone", self.FIELDS)

        self.assertEqual([item.name for item in ranked], ["Samsung Phone"])

    def test_typo_matches_via_fuzzy_leg(self):
        items = named("Television", "Samsung Galaxy")

        ranked = rank_objects(items, "samsng", self.FIELDS)

        self.assertEqual([item.name for item in ranked], ["Samsung Galaxy"])

    def test_synonym_query_matches_equivalent_words(self):
        items = named("Smart Phone 4G", "Soda Crate")

        ranked = rank_objects(items, "simu", self.FIELDS)

        self.assertEqual([item.name for item in ranked], ["Smart Phone 4G"])

    def test_exact_barcode_beats_name_prefix(self):
        fields = (SearchField('name', 10), SearchField('barcode', 12))
        kettle = SimpleNamespace(name='Kettle', barcode='123456')
        cable = SimpleNamespace(name='123456 Cable', barcode='')

        ranked = rank_objects([cable, kettle], "123456", fields)

        self.assertEqual([item.name for item in ranked], ["Kettle", "123456 Cable"])

    def test_empty_or_noise_query_passes_rows_through_in_order(self):
        items = named("B", "A")

        self.assertEqual(rank_objects(items, "", self.FIELDS), items)
        self.assertEqual(rank_objects(items, "naomba", self.FIELDS), items)

    def test_ties_keep_incoming_order(self):
        items = named("Soda", "Soda")

        ranked = rank_objects(items, "soda", self.FIELDS)

        self.assertEqual([id(item) for item in ranked], [id(item) for item in items])


class CandidatesQTests(SimpleTestCase):
    FIELDS = (SearchField('name', 10),)

    def test_plain_candidates_use_substring_lookups_only(self):
        q = candidates_q(["simu"], self.FIELDS, trigram=False)

        lookups = flatten_lookups(q)
        self.assertIn(("name__icontains", "simu"), lookups)
        # Synonyms participate in candidate generation too.
        self.assertIn(("name__icontains", "phone"), lookups)
        self.assertFalse(any("trigram" in key for key, _ in lookups))

    def test_trigram_candidates_add_word_similarity_lookups(self):
        q = candidates_q(["simu"], self.FIELDS, trigram=True)

        lookups = flatten_lookups(q)
        self.assertIn(("name__trigram_word_similar", "simu"), lookups)
        self.assertIn(("name__trigram_word_similar", "phone"), lookups)
        self.assertIn(("name__icontains", "simu"), lookups)

    def test_terms_are_combined_with_and(self):
        q = candidates_q(["simu", "nokia"], self.FIELDS, trigram=False)

        self.assertEqual(q.connector, "AND")
        self.assertEqual(len(q.children), 2)
        values = {value for _, value in flatten_lookups(q)}
        self.assertIn("simu", values)
        self.assertIn("nokia", values)


class SearchQuerysetTests(TestCase):
    """Database path: candidates come from SQL, ranking stays in Python."""

    def setUp(self):
        self.shop = Shop.objects.create(
            name='Mama Shop', legacy_id='shop-search-1', slug='mama-shop', isPublic=True)
        self.category = Category.objects.create(shop=self.shop, name='Electronics')
        for name in (
            'Samsung', 'Samsung Galaxy A15', 'Original Samsung Case', 'MySamsungCover',
            'Soda', 'Soda Crate', 'Smart Phone 4G', 'Viatu vya Ngozi',
            'Kettle', '123456 Cable', 'Widget',
        ):
            Product.objects.create(
                shop=self.shop, category=self.category, name=name,
                selling_price='1000.00', status='active',
            )
        Product.objects.filter(name='Kettle').update(barcode='123456')
        Product.objects.filter(name='Widget').update(sku='AB-100')

    def names(self, result):
        return [product.name for product in result]

    def test_returns_ranked_list_in_ladder_order(self):
        result = search_queryset(Product.objects.all(), "samsung", PRODUCT_FIELDS)

        self.assertEqual(
            self.names(result),
            ['Samsung', 'Samsung Galaxy A15', 'Original Samsung Case', 'MySamsungCover'],
        )

    def test_multi_term_query_requires_every_term(self):
        result = search_queryset(Product.objects.all(), "soda crate", PRODUCT_FIELDS)

        self.assertEqual(self.names(result), ['Soda Crate'])

    def test_synonym_query_finds_english_named_product(self):
        result = search_queryset(Product.objects.all(), "simu", PRODUCT_FIELDS)

        self.assertEqual(self.names(result), ['Smart Phone 4G'])

    def test_english_query_finds_swahili_named_product(self):
        result = search_queryset(Product.objects.all(), "shoes", PRODUCT_FIELDS)

        self.assertEqual(self.names(result), ['Viatu vya Ngozi'])

    def test_barcode_query_puts_exact_barcode_first(self):
        result = search_queryset(Product.objects.all(), "123456", PRODUCT_FIELDS)

        self.assertEqual(self.names(result), ['Kettle', '123456 Cable'])

    def test_punctuated_sku_is_findable_by_the_same_shape(self):
        result = search_queryset(Product.objects.all(), "AB-100", PRODUCT_FIELDS)

        self.assertEqual(self.names(result), ['Widget'])

    def test_relation_path_fields_are_scored(self):
        fields = (SearchField('name', 10), SearchField('category__name', 6))

        result = search_queryset(
            Product.objects.select_related('category'), "electronics", fields)

        self.assertEqual(set(self.names(result)), {
            'Samsung', 'Samsung Galaxy A15', 'Original Samsung Case', 'MySamsungCover',
            'Soda', 'Soda Crate', 'Smart Phone 4G', 'Viatu vya Ngozi',
            'Kettle', '123456 Cable', 'Widget',
        })

    def test_empty_or_noise_query_returns_rows_unchanged(self):
        for query in ("", "naomba"):
            result = search_queryset(Product.objects.all(), query, PRODUCT_FIELDS)
            self.assertIsInstance(result, QuerySet)
            self.assertEqual(result.count(), Product.objects.count())

    def test_no_match_returns_empty_list(self):
        result = search_queryset(Product.objects.all(), "zzzzz", PRODUCT_FIELDS)

        self.assertEqual(list(result), [])
