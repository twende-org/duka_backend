"""Intake pipeline + endpoint tests (spec Part 1).

The background worker runs on a thread in production; tests call
``process_intake_batch`` directly so extraction is deterministic, and patch
``dispatch_intake_processing`` where the endpoint is exercised.
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.assistant import AssistantError
from apps.intake.models import IntakeBatch, ProductDraft
from apps.intake.qr import MARKER, parse_wholesale_qr
from apps.intake.services import (
    _group_duplicates,
    _with_conservative_confidence,
    apply_intake_batch,
    create_intake_batch,
    process_intake_batch,
)
from apps.intake.tra import classify_tra
from apps.products.models import Category, Inventory, InventoryMovement, Product
from apps.shops.models import Branch, Shop, UserRole

User = get_user_model()

PNG_BYTES = bytes.fromhex(
    '89504e470d0a1a0a0000000d494844520000000100000001080600000'
    '01f15c4890000000a49444154789c6300010000050001'
    '0d0a2db40000000049454e44ae426082'
)


def wholesale_payload(**item_overrides):
    item = {
        'name': 'Azam Soda 500ml',
        'name_sw': 'Soda Azam 500ml',
        'quantity': 24,
        'unit': 'pcs',
        'unit_cost': 500,
        'unit_price': 800,
        'category': 'Mvinyo',
    }
    item.update(item_overrides)
    return {'type': MARKER, 'invoice_no': 'INV-1', 'supplier': 'Kilimanjaro Wholesalers', 'items': [item]}


class BaseIntakeTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='owner@example.com', email='owner@example.com', password='secret123')
        self.shop = Shop.objects.create(name='Duka la Mama')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='Main', is_main=True)


class QRParsingTest(TestCase):
    def test_valid_payload_normalized(self):
        parsed = parse_wholesale_qr(json.dumps(wholesale_payload()))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['supplier'], 'Kilimanjaro Wholesalers')
        item = parsed['items'][0]
        self.assertEqual(item['name_en'], 'Azam Soda 500ml')
        self.assertEqual(item['name_sw'], 'Soda Azam 500ml')
        self.assertEqual(item['quantity'], 24)
        self.assertEqual(item['unit'], 'pcs')
        self.assertEqual(item['confidence'], 1.0)

    def test_garbage_and_wrong_marker_return_none(self):
        self.assertIsNone(parse_wholesale_qr('not json at all'))
        self.assertIsNone(parse_wholesale_qr(json.dumps({'type': 'other.v1', 'items': []})))
        self.assertIsNone(parse_wholesale_qr(json.dumps({'type': MARKER, 'items': []})))
        self.assertIsNone(parse_wholesale_qr(json.dumps({'type': MARKER, 'items': 'nope'})))

    def test_missing_fields_default_sensibly(self):
        payload = {'type': MARKER, 'items': [{'name': 'Unga 2kg', 'quantity': '10'}]}
        parsed = parse_wholesale_qr(json.dumps(payload))
        item = parsed['items'][0]
        self.assertEqual(item['unit'], 'pcs')
        self.assertEqual(item['quantity'], 10)
        self.assertEqual(item['unit_cost'], 0)


class TRAClassificationTest(TestCase):
    def test_exempt_items_map_to_zero_rate(self):
        for name in ('Panadol 500mg', 'Madawa ya kichwa', 'Exercise book A4', 'Kitabu cha Kiswahili'):
            code, rate = classify_tra(name)
            self.assertEqual(code, 'VAT_EXEMPT', name)
            self.assertEqual(rate, Decimal('0.00'), name)

    def test_standard_items_map_to_18(self):
        for name in ('Azam Soda 500ml', ('Phone charger'), 'Unga wa sembe'):
            code, rate = classify_tra(name)
            self.assertEqual(code, 'VAT_STD', name)
            self.assertEqual(rate, Decimal('18.00'), name)


class DuplicateGroupingTest(TestCase):
    def test_same_name_and_unit_merge_quantities_min_confidence(self):
        grouped = _group_duplicates([
            {'name_en': 'Sabuni', 'unit': 'pcs', 'quantity': 10, 'confidence': 0.95, 'unit_cost': 100},
            {'name_en': 'sabuni ', 'unit': 'PCS', 'quantity': 5, 'confidence': 0.6},
            {'name_en': 'Sabuni', 'unit': 'carton', 'quantity': 2, 'confidence': 0.9},
        ])
        self.assertEqual(len(grouped), 2)
        merged = next(item for item in grouped if item['unit'] == 'pcs')
        self.assertEqual(merged['quantity'], 15)
        self.assertEqual(merged['confidence'], 0.6)
        self.assertEqual(merged['unit_cost'], 100)

    def test_blank_and_unparsable_rows_dropped(self):
        grouped = _group_duplicates([
            {'name_en': '  ', 'quantity': 3},
            {'name_en': 'Chakula', 'quantity': 'abc', 'confidence': 'x'},
        ])
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]['quantity'], 0.0)
        self.assertEqual(grouped[0]['confidence'], 0.0)


class ConservativeConfidenceTest(TestCase):
    def test_no_legible_price_caps_at_035(self):
        items = _with_conservative_confidence([
            {'name_en': 'Sukari', 'quantity': 5, 'unit_cost': 0, 'unit_price': 0,
             'confidence': 0.95},
        ])
        self.assertEqual(items[0]['confidence'], 0.35)

    def test_inverted_margin_caps_at_06(self):
        items = _with_conservative_confidence([
            {'name_en': 'Unga', 'quantity': 3, 'unit_cost': 2000, 'unit_price': 1500,
             'confidence': 0.9},
        ])
        self.assertEqual(items[0]['confidence'], 0.6)

    def test_one_sided_price_keeps_model_confidence(self):
        items = _with_conservative_confidence([
            {'name_en': 'Unga', 'quantity': 3, 'unit_cost': 2000, 'unit_price': 0,
             'confidence': 0.9},
            {'name_en': 'Chumvi', 'quantity': 3, 'unit_cost': 0, 'unit_price': 500,
             'confidence': 0.9},
        ])
        self.assertEqual([item['confidence'] for item in items], [0.9, 0.9])

    def test_zero_quantity_caps_at_07(self):
        items = _with_conservative_confidence([
            {'name_en': 'Chumvi', 'quantity': 0, 'unit_cost': 100, 'unit_price': 150,
             'confidence': 0.95},
        ])
        self.assertEqual(items[0]['confidence'], 0.7)

    def test_clean_row_is_untouched_and_confidence_never_raised(self):
        items = _with_conservative_confidence([
            {'name_en': 'Sabuni', 'quantity': 2, 'unit_cost': 100, 'unit_price': 150,
             'confidence': 0.9},
            {'name_en': 'Soda', 'quantity': 2, 'unit_cost': 100, 'unit_price': 150,
             'confidence': 0.2},
        ])
        self.assertEqual([item['confidence'] for item in items], [0.9, 0.2])


class ProcessBatchTest(BaseIntakeTest):
    def test_qr_payload_parsed_without_any_ai_call(self):
        batch = create_intake_batch(
            self.shop, self.user,
            qr_payloads=[json.dumps(wholesale_payload()), 'garbage payload'],
        )
        with mock.patch('apps.intake.services.extract_invoice_items') as ai:
            process_intake_batch(batch.id)
            ai.assert_not_called()
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'completed')
        self.assertEqual(batch.engine_used, 'qr')
        self.assertEqual(batch.item_count, 1)
        draft = batch.drafts.get()
        self.assertEqual(draft.name_en, 'Azam Soda 500ml')
        self.assertEqual(draft.name_sw, 'Soda Azam 500ml')
        self.assertEqual(draft.quantity, 24)
        self.assertEqual(draft.buying_price, Decimal('500.00'))
        self.assertEqual(draft.selling_price, Decimal('800.00'))
        self.assertEqual(draft.category_name, 'Mvinyo')
        self.assertEqual(draft.ai_confidence_score, 1.0)
        self.assertEqual(draft.tra_item_code, 'VAT_STD')
        self.assertEqual(draft.tax_rate_percent, Decimal('18.00'))
        self.assertIsNone(batch.ai_usage)

    def test_qr_hidden_inside_image_skips_ai(self):
        batch = create_intake_batch(
            self.shop, self.user,
            images=[SimpleUploadedFile('note.jpg', PNG_BYTES, content_type='image/jpeg')],
        )
        with mock.patch('apps.intake.services.decode_qr_from_image',
                        return_value=json.dumps(wholesale_payload(quantity=2))), \
             mock.patch('apps.intake.services.extract_invoice_items') as ai:
            process_intake_batch(batch.id)
            ai.assert_not_called()
        batch.refresh_from_db()
        self.assertEqual(batch.engine_used, 'qr')
        self.assertEqual(batch.drafts.get().quantity, 2)
        # Uploads are wiped once processed; only drafts + sources metadata remain.
        self.assertFalse(default_storage.exists(batch.sources[0]['ref']))

    def test_ai_fallback_creates_drafts_and_wipes_upload(self):
        batch = create_intake_batch(
            self.shop, self.user,
            images=[SimpleUploadedFile('invoice.jpg', PNG_BYTES, content_type='image/jpeg')],
        )
        ai_items = [
            {'name_en': 'Unga wa sembe 2kg', 'quantity': 30, 'unit': 'pcs',
             'unit_cost': 3000, 'unit_price': 4500, 'confidence': 0.93},
            {'name_en': 'Panadol', 'quantity': 12, 'unit': 'pcs',
             'unit_cost': 800, 'unit_price': 1200, 'confidence': 0.42},
        ]
        ai_usage = {
            'model': 'test/cheap', 'models_tried': ['test/cheap'],
            'prompt_tokens': 120, 'completion_tokens': 40,
        }
        with mock.patch('apps.intake.services.decode_qr_from_image', return_value=None), \
             mock.patch('apps.intake.services.extract_invoice_items',
                        return_value=(ai_items, ai_usage)):
            process_intake_batch(batch.id)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'completed')
        self.assertEqual(batch.engine_used, 'ai')
        self.assertEqual(batch.item_count, 2)
        self.assertEqual(batch.ai_usage, {
            'models_tried': ['test/cheap'],
            'images': 1, 'prompt_tokens': 120, 'completion_tokens': 40,
        })
        low = batch.drafts.get(name_en='Panadol')
        self.assertEqual(low.ai_confidence_score, 0.42)
        self.assertEqual(low.tra_item_code, 'VAT_EXEMPT')
        self.assertEqual(low.tax_rate_percent, Decimal('0.00'))
        self.assertFalse(default_storage.exists(batch.sources[0]['ref']))

    def test_unparseable_qr_without_images_fails_with_message(self):
        batch = create_intake_batch(self.shop, self.user, qr_payloads=['nonsense'])
        process_intake_batch(batch.id)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'failed')
        self.assertIn('wholesale format', batch.error_message)
        self.assertEqual(batch.drafts.count(), 0)

    def test_missing_openrouter_key_fails_batch(self):
        batch = create_intake_batch(
            self.shop, self.user,
            images=[SimpleUploadedFile('invoice.jpg', PNG_BYTES, content_type='image/jpeg')],
        )
        with mock.patch('apps.intake.services.decode_qr_from_image', return_value=None), \
             mock.patch('apps.intake.services.extract_invoice_items',
                        side_effect=AssistantError('OPENROUTER_API_KEY is not configured on the server.', 503)):
            process_intake_batch(batch.id)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'failed')
        self.assertIn('OPENROUTER_API_KEY', batch.error_message)


class ApplyBatchTest(BaseIntakeTest):
    def _completed_batch(self):
        batch = create_intake_batch(
            self.shop, self.user,
            qr_payloads=[json.dumps(wholesale_payload())],
        )
        process_intake_batch(batch.id)
        return batch

    def test_apply_creates_products_stock_and_ledger(self):
        batch = self._completed_batch()
        summary = apply_intake_batch(batch, self.user)
        self.assertEqual(summary, {'created': 1, 'updated': 0, 'skipped': 0})
        product = Product.objects.get(shop=self.shop, name='Azam Soda 500ml')
        self.assertEqual(product.unit, 'pcs')
        self.assertEqual(product.tax_rate, Decimal('18.00'))
        self.assertFalse(product.publish_to_directory)
        inventory = Inventory.objects.get(product=product, branch=self.branch)
        self.assertEqual(inventory.quantity, 24)
        movement = InventoryMovement.objects.get(product=product)
        self.assertEqual(movement.movement_type, 'in')
        self.assertEqual(movement.new_qty, 24)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'applied')
        self.assertEqual(batch.drafts.get().applied_product, product)

    def test_apply_updates_existing_product_instead_of_duplicating(self):
        existing = Product.objects.create(
            shop=self.shop, name='azam soda 500ml', buying_price=400, selling_price=600)
        batch = self._completed_batch()
        summary = apply_intake_batch(batch, self.user)
        self.assertEqual(summary, {'created': 0, 'updated': 1, 'skipped': 0})
        existing.refresh_from_db()
        self.assertEqual(existing.buying_price, Decimal('500.00'))
        self.assertEqual(existing.selling_price, Decimal('800.00'))
        self.assertEqual(Product.objects.filter(shop=self.shop, name__iexact='Azam Soda 500ml').count(), 1)

    def test_apply_twice_rejected(self):
        batch = self._completed_batch()
        apply_intake_batch(batch, self.user)
        with self.assertRaises(Exception):
            apply_intake_batch(batch, self.user)
        self.assertEqual(Product.objects.filter(shop=self.shop).count(), 1)

    def test_apply_reuses_existing_category_ignoring_case(self):
        existing = Category.objects.create(shop=self.shop, name='mvinyo')
        batch = self._completed_batch()
        summary = apply_intake_batch(batch, self.user)
        self.assertEqual(summary, {'created': 1, 'updated': 0, 'skipped': 0})
        self.assertEqual(Category.objects.filter(shop=self.shop).count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.name, 'mvinyo')
        product = Product.objects.get(shop=self.shop, name='Azam Soda 500ml')
        self.assertEqual(product.category_id, existing.id)

    def test_apply_requires_completed_batch(self):
        batch = create_intake_batch(self.shop, self.user, qr_payloads=['nonsense'])
        process_intake_batch(batch.id)  # -> failed
        with self.assertRaises(Exception):
            apply_intake_batch(batch, self.user)


class IntakeAPITest(BaseIntakeTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.user)
        self.dispatch = mock.patch('api.v1.views.intake.dispatch_intake_processing')
        self.dispatch_mock = self.dispatch.start()
        self.addCleanup(self.dispatch.stop)

    def test_create_from_qr_payload_answers_202_and_dispatches(self):
        response = self.client.post('/api/v1/inventory/intake/', {
            'shopId': str(self.shop.id),
            'qrPayloads': [json.dumps(wholesale_payload())],
        }, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data['sourceType'], 'qr')
        self.assertIn('id', response.data)
        self.dispatch_mock.assert_called_once()
        batch = IntakeBatch.objects.get(id=response.data['id'])
        self.assertEqual(batch.status, 'pending')
        self.assertEqual(batch.sources[0]['kind'], 'qr')

    def test_create_with_image_upload(self):
        response = self.client.post('/api/v1/inventory/intake/', {
            'shopId': str(self.shop.id),
            'images': SimpleUploadedFile('receipt.jpg', PNG_BYTES, content_type='image/jpeg'),
        }, format='multipart')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data['sourceType'], 'image')
        self.assertEqual(IntakeBatch.objects.get(id=response.data['id']).sources[0]['kind'], 'image')

    def test_create_from_media_url(self):
        response = self.client.post('/api/v1/inventory/intake/', {
            'shopId': str(self.shop.id),
            'imageUrls': ['https://example.com/invoice.jpg'],
        }, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data['sourceType'], 'url')

    def test_rejects_non_http_url(self):
        response = self.client.post('/api/v1/inventory/intake/', {
            'shopId': str(self.shop.id),
            'imageUrls': ['file:///etc/passwd'],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_rejects_bad_image_type_and_missing_sources(self):
        response = self.client.post('/api/v1/inventory/intake/', {
            'shopId': str(self.shop.id),
            'images': SimpleUploadedFile('doc.svg', b'<svg/>', content_type='image/svg+xml'),
        }, format='multipart')
        self.assertEqual(response.status_code, 400)

        response = self.client.post('/api/v1/inventory/intake/', {
            'shopId': str(self.shop.id),
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_other_shop_batch_is_invisible(self):
        other_user = User.objects.create_user(username='other@example.com', email='other@example.com', password='secret123')
        other_shop = Shop.objects.create(name='Competing Duka')
        UserRole.objects.create(user=other_user, shop=other_shop, role='owner')
        batch = create_intake_batch(other_shop, other_user, qr_payloads=[json.dumps(wholesale_payload())])

        response = self.client.get(f'/api/v1/inventory/intake/{batch.id}/')
        self.assertEqual(response.status_code, 404)
        response = self.client.get('/api/v1/inventory/intake/')
        self.assertEqual(response.data['count'], 0)

    def test_draft_edit_and_apply_endpoint(self):
        batch = create_intake_batch(self.shop, self.user, qr_payloads=[json.dumps(wholesale_payload())])
        process_intake_batch(batch.id)
        draft = batch.drafts.get()

        response = self.client.patch(
            f'/api/v1/inventory/intake-drafts/{draft.id}/',
            {'quantity': 30, 'sellingPrice': '950.00', 'nameSw': 'Soda Azam'},
            format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['quantity'], 30)
        self.assertEqual(response.data['sellingPrice'], '950.00')

        response = self.client.post(f'/api/v1/inventory/intake/{batch.id}/apply/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary']['created'], 1)
        self.assertTrue(Product.objects.filter(shop=self.shop, name='Azam Soda 500ml').exists())

        # Frozen once applied.
        response = self.client.patch(
            f'/api/v1/inventory/intake-drafts/{draft.id}/', {'quantity': 5}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_draft_delete_before_apply(self):
        batch = create_intake_batch(self.shop, self.user, qr_payloads=[json.dumps(wholesale_payload())])
        process_intake_batch(batch.id)
        draft = batch.drafts.get()
        response = self.client.delete(f'/api/v1/inventory/intake-drafts/{draft.id}/')
        self.assertEqual(response.status_code, 204)
        self.assertEqual(batch.drafts.count(), 0)

    def test_apply_denied_for_outsider(self):
        batch = create_intake_batch(self.shop, self.user, qr_payloads=[json.dumps(wholesale_payload())])
        process_intake_batch(batch.id)
        outsider = User.objects.create_user(username='outsider@example.com', email='outsider@example.com', password='secret123')
        self.client.force_authenticate(user=outsider)
        response = self.client.post(f'/api/v1/inventory/intake/{batch.id}/apply/')
        # Scoped get_object hides the batch from non-members entirely.
        self.assertEqual(response.status_code, 404)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'completed')
