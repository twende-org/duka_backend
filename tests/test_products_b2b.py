"""B2B inter-shop transfer tests (spec Part 2).

Covers the quarantine semantics (stock leaves the sender at dispatch, enters
the buyer only on confirm), the buyer-side line mapping (nothing is
auto-created on the buyer's account), and the row-locked complete/cancel
lifecycle. The select_for_update lock is exercised through repeated
complete/cancel calls — the first flips the status, the second is rejected —
which is the same invariant threads would race on.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.products.b2b_services import (
    cancel_b2b_transfer, complete_b2b_transfer, create_b2b_transfer,
    map_b2b_transfer_items, stage_sale_delivery, suggest_product_for_item,
)
from apps.products.models import B2BStockTransfer, Inventory, InventoryMovement, Product
from apps.sales.models import Sale
from apps.shops.models import Branch, Shop, UserRole

User = get_user_model()


class B2BBase(TestCase):
    def setUp(self):
        self.sender = User.objects.create_user(username='wholesaler@example.com', email='wholesaler@example.com', password='secret123')
        self.buyer = User.objects.create_user(username='retailer@example.com', email='retailer@example.com', password='secret123')
        self.stranger = User.objects.create_user(username='stranger@example.com', email='stranger@example.com', password='secret123')

        self.wholesale_shop = Shop.objects.create(name='Kariakoo Wholesalers')
        UserRole.objects.create(user=self.sender, shop=self.wholesale_shop, role='owner')
        self.wholesale_branch = Branch.objects.create(
            shop=self.wholesale_shop, name='Wholesale Main', is_main=True)

        self.retail_shop = Shop.objects.create(name='Duka la Mama')
        UserRole.objects.create(user=self.buyer, shop=self.retail_shop, role='owner')
        self.retail_branch = Branch.objects.create(shop=self.retail_shop, name='Retail Main', is_main=True)

        # What the buyer already stocks (same SKU so suggestions prefill it).
        self.buyer_product = Product.objects.create(
            shop=self.retail_shop, name='Azam Soda 500ml', sku='AZM-500',
            buying_price=Decimal('0.00'), selling_price=Decimal('850.00'), unit='pcs')

        self.client = APIClient()

        self.product = Product.objects.create(
            shop=self.wholesale_shop, name='Azam Soda 500ml', sku='AZM-500',
            buying_price=Decimal('500.00'), selling_price=Decimal('800.00'), unit='pcs')
        Inventory.objects.create(product=self.product, branch=self.wholesale_branch, quantity=100)

    def _transfer_payload(self, **overrides):
        payload = {
            'fromShopId': str(self.wholesale_shop.id),
            'toShopId': str(self.retail_shop.id),
            'fromBranchId': str(self.wholesale_branch.id),
            'items': [
                {'productId': str(self.product.id), 'quantity': 24, 'unitCost': '520.00'},
            ],
            'note': 'Weekly restock',
            'reference': 'WB-001',
        }
        payload.update(overrides)
        return payload

    def _pending_transfer(self, quantity=24):
        return create_b2b_transfer(
            self.wholesale_shop, self.retail_shop, self.wholesale_branch,
            items=[{'product': self.product, 'quantity': quantity, 'unit_cost': Decimal('520.00')}],
            created_by=self.sender,
        )

    def _map_transfer(self, transfer, product):
        return map_b2b_transfer_items(
            transfer,
            mappings=[{'item': item, 'product': product} for item in transfer.items.all()],
            user=self.buyer,
        )


class CreateTransferTest(B2BBase):
    def test_dispatch_deducts_sender_stock_and_quarantines(self):
        self.client.force_authenticate(user=self.sender)
        response = self.client.post('/api/v1/transfers/', self._transfer_payload(), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['reference'], 'WB-001')
        self.assertEqual(response.data['lineCount'], 1)
        self.assertEqual(response.data['totalQuantity'], 24)

        sender_inv = Inventory.objects.get(product=self.product, branch=self.wholesale_branch)
        self.assertEqual(sender_inv.quantity, 76)  # 100 - 24
        movement = InventoryMovement.objects.filter(product=self.product, movement_type='transfer').latest('created_at')
        self.assertEqual(movement.quantity_changed, -24)

        # Quarantined: the buyer's shop has nothing live yet.
        self.assertFalse(Inventory.objects.filter(product__shop=self.retail_shop).exists())

    def test_same_shop_transfer_rejected(self):
        self.client.force_authenticate(user=self.sender)
        response = self.client.post('/api/v1/transfers/', self._transfer_payload(
            toShopId=str(self.wholesale_shop.id)), format='json')
        self.assertEqual(response.status_code, 400)

    def test_insufficient_stock_rolls_back_everything(self):
        self.client.force_authenticate(user=self.sender)
        response = self.client.post('/api/v1/transfers/', self._transfer_payload(
            items=[{'productId': str(self.product.id), 'quantity': 1000}]), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(B2BStockTransfer.objects.count(), 0)
        self.assertEqual(Inventory.objects.get(product=self.product, branch=self.wholesale_branch).quantity, 100)
        self.assertEqual(InventoryMovement.objects.count(), 0)

    def test_zero_or_negative_quantity_rejected(self):
        self.client.force_authenticate(user=self.sender)
        response = self.client.post('/api/v1/transfers/', self._transfer_payload(
            items=[{'productId': str(self.product.id), 'quantity': 0}]), format='json')
        self.assertEqual(response.status_code, 400)

    def test_sender_without_role_cannot_dispatch(self):
        self.client.force_authenticate(user=self.stranger)
        response = self.client.post('/api/v1/transfers/', self._transfer_payload(), format='json')
        self.assertEqual(response.status_code, 403)


class CompleteTransferTest(B2BBase):
    def test_buyer_completes_mapped_stock_lands_and_history_kept(self):
        transfer = self._pending_transfer()
        self._map_transfer(transfer, self.buyer_product)
        self.client.force_authenticate(user=self.buyer)
        response = self.client.post(f'/api/v1/transfers/{transfer.id}/complete/', format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'completed')
        self.assertEqual(response.data['completedById'], self.buyer.id)

        buyer_inv = Inventory.objects.get(product=self.buyer_product, branch=self.retail_branch)
        self.assertEqual(buyer_inv.quantity, 24)
        movement = InventoryMovement.objects.filter(movement_type='in').latest('created_at')
        self.assertEqual(movement.branch, self.retail_branch)
        self.assertEqual(movement.new_qty, 24)

        item = transfer.items.get()
        self.assertEqual(item.received_product, self.buyer_product)
        self.assertEqual(response.data['items'][0]['receivedProductId'], self.buyer_product.id)

    def test_unmapped_complete_refused_and_creates_nothing(self):
        transfer = self._pending_transfer()
        with self.assertRaises(Exception):
            complete_b2b_transfer(transfer, self.buyer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'pending')
        # Nothing was auto-created and no stock entered the buyer's shop.
        self.assertEqual(Product.objects.filter(shop=self.retail_shop).count(), 1)
        self.assertFalse(Inventory.objects.filter(product__shop=self.retail_shop).exists())
        self.assertFalse(InventoryMovement.objects.filter(movement_type='in').exists())

    def test_partial_map_refuses_completion_and_names_the_line(self):
        second = Product.objects.create(
            shop=self.wholesale_shop, name='Cadbury Drinking Chocolate 500g', sku='CBD-500',
            buying_price=Decimal('3000.00'), selling_price=Decimal('4500.00'))
        Inventory.objects.create(product=second, branch=self.wholesale_branch, quantity=40)
        transfer = create_b2b_transfer(
            self.wholesale_shop, self.retail_shop, self.wholesale_branch,
            items=[
                {'product': self.product, 'quantity': 24, 'unit_cost': Decimal('520.00')},
                {'product': second, 'quantity': 6, 'unit_cost': Decimal('3100.00')},
            ],
            created_by=self.sender,
        )
        soda_item = transfer.items.get(product_name='Azam Soda 500ml')
        map_b2b_transfer_items(
            transfer,
            mappings=[{'item': soda_item, 'product': self.buyer_product}],
            user=self.buyer,
        )
        with self.assertRaises(Exception) as ctx:
            complete_b2b_transfer(transfer, self.buyer)
        self.assertIn('Cadbury', str(ctx.exception))
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'pending')
        self.assertFalse(Inventory.objects.filter(product__shop=self.retail_shop).exists())

    def test_mapped_product_inherits_cost_only_if_missing(self):
        buyer_product = Product.objects.create(
            shop=self.retail_shop, name='Soda Shelf', buying_price=Decimal('0.00'))
        transfer = self._pending_transfer()
        self._map_transfer(transfer, buyer_product)
        complete_b2b_transfer(transfer, self.buyer)
        buyer_product.refresh_from_db()
        item = transfer.items.get()
        self.assertEqual(item.received_product, buyer_product)
        self.assertEqual(buyer_product.buying_price, Decimal('520.00'))  # was 0 -> inherits

    def test_existing_cost_basis_not_clobbered(self):
        buyer_product = Product.objects.create(
            shop=self.retail_shop, name='Soda Shelf', buying_price=Decimal('490.00'))
        transfer = self._pending_transfer()
        self._map_transfer(transfer, buyer_product)
        complete_b2b_transfer(transfer, self.buyer)
        buyer_product.refresh_from_db()
        self.assertEqual(buyer_product.buying_price, Decimal('490.00'))

    def test_double_complete_rejected_without_double_stock(self):
        transfer = self._pending_transfer()
        self._map_transfer(transfer, self.buyer_product)
        complete_b2b_transfer(transfer, self.buyer)
        with self.assertRaises(Exception):
            complete_b2b_transfer(transfer, self.buyer)
        buyer_inv = Inventory.objects.get(product__shop=self.retail_shop)
        self.assertEqual(buyer_inv.quantity, 24)

    def test_complete_via_api_denied_for_stranger_and_sender(self):
        transfer = self._pending_transfer()
        self._map_transfer(transfer, self.buyer_product)
        self.client.force_authenticate(user=self.stranger)
        response = self.client.post(f'/api/v1/transfers/{transfer.id}/complete/', format='json')
        self.assertEqual(response.status_code, 404)  # scoped out: existence not leaked

        self.client.force_authenticate(user=self.sender)
        response = self.client.post(f'/api/v1/transfers/{transfer.id}/complete/', format='json')
        self.assertEqual(response.status_code, 403)  # visible but not the receiver

    def test_completion_without_explicit_branch_uses_buyer_main(self):
        transfer = create_b2b_transfer(
            self.wholesale_shop, self.retail_shop, self.wholesale_branch,
            items=[{'product': self.product, 'quantity': 5, 'unit_cost': Decimal('520.00')}],
            created_by=self.sender, to_branch=None,
        )
        self._map_transfer(transfer, self.buyer_product)
        complete_b2b_transfer(transfer, self.buyer)
        inv = Inventory.objects.get(product__shop=self.retail_shop)
        self.assertEqual(inv.branch, self.retail_branch)


class MapTransferTest(B2BBase):
    def _map_via_api(self, transfer, item, product_id, user):
        self.client.force_authenticate(user=user)
        return self.client.post(
            f'/api/v1/transfers/{transfer.id}/map/',
            {'items': [{'itemId': str(item.id), 'productId': product_id}]},
            format='json',
        )

    def test_buyer_maps_line_via_api(self):
        transfer = self._pending_transfer()
        item = transfer.items.get()
        response = self._map_via_api(transfer, item, str(self.buyer_product.id), self.buyer)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['items'][0]['mappedProductId'], self.buyer_product.id)
        self.assertEqual(response.data['items'][0]['mappedProductName'], 'Azam Soda 500ml')
        item.refresh_from_db()
        self.assertEqual(item.mapped_product, self.buyer_product)

    def test_suggestion_prefills_sku_then_name(self):
        self.buyer_product.name = 'Different Label'
        self.buyer_product.save(update_fields=['name'])
        transfer = self._pending_transfer()
        item = transfer.items.get()
        # The SKU match wins even though the name differs.
        self.assertEqual(suggest_product_for_item(self.retail_shop, item), self.buyer_product)

        renamed = Product.objects.create(shop=self.retail_shop, name='Azam Soda 500ml')
        self.buyer_product.sku = ''
        self.buyer_product.save(update_fields=['sku'])
        self.assertEqual(suggest_product_for_item(self.retail_shop, item), renamed)

    def test_buyer_sees_suggestion_sender_does_not(self):
        transfer = self._pending_transfer()

        self.client.force_authenticate(user=self.buyer)
        response = self.client.get(f'/api/v1/transfers/{transfer.id}/')
        self.assertEqual(response.data['items'][0]['suggestedProductId'], self.buyer_product.id)
        self.assertEqual(
            response.data['items'][0]['suggestedProductName'], self.buyer_product.name)

        self.client.force_authenticate(user=self.sender)
        response = self.client.get(f'/api/v1/transfers/{transfer.id}/')
        self.assertIsNone(response.data['items'][0]['suggestedProductId'])

    def test_only_buyer_can_map(self):
        transfer = self._pending_transfer()
        item = transfer.items.get()

        response = self._map_via_api(transfer, item, str(self.buyer_product.id), self.sender)
        self.assertEqual(response.status_code, 403)  # visible, but not the receiver

        response = self._map_via_api(transfer, item, str(self.buyer_product.id), self.stranger)
        self.assertEqual(response.status_code, 404)  # scoped out entirely

        item.refresh_from_db()
        self.assertIsNone(item.mapped_product)

    def test_product_from_another_shop_rejected(self):
        transfer = self._pending_transfer()
        item = transfer.items.get()
        response = self._map_via_api(transfer, item, str(self.product.id), self.buyer)
        self.assertEqual(response.status_code, 400)
        item.refresh_from_db()
        self.assertIsNone(item.mapped_product)

    def test_unknown_item_rejected(self):
        transfer = self._pending_transfer()
        other = self._pending_transfer(quantity=1)
        response = self._map_via_api(
            transfer, other.items.get(), str(self.buyer_product.id), self.buyer)
        self.assertEqual(response.status_code, 400)

    def test_completed_transfer_cannot_be_remapped(self):
        transfer = self._pending_transfer()
        self._map_transfer(transfer, self.buyer_product)
        complete_b2b_transfer(transfer, self.buyer)
        other = Product.objects.create(shop=self.retail_shop, name='Other Shelf')
        with self.assertRaises(Exception):
            self._map_transfer(transfer, other)
        item = transfer.items.get()
        item.refresh_from_db()
        self.assertEqual(item.mapped_product, self.buyer_product)

    def test_null_clears_mapping(self):
        transfer = self._pending_transfer()
        item = transfer.items.get()
        self._map_transfer(transfer, self.buyer_product)
        response = self._map_via_api(transfer, item, None, self.buyer)
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertIsNone(item.mapped_product)


class CancelTransferTest(B2BBase):
    def test_cancel_restores_sender_stock(self):
        transfer = self._pending_transfer(quantity=10)
        self.client.force_authenticate(user=self.sender)
        response = self.client.post(f'/api/v1/transfers/{transfer.id}/cancel/', format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'cancelled')
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.wholesale_branch).quantity, 100)
        self.assertFalse(Inventory.objects.filter(product__shop=self.retail_shop).exists())

    def test_buyer_may_reject_a_pending_dispatch(self):
        transfer = self._pending_transfer(quantity=10)
        self.client.force_authenticate(user=self.buyer)
        response = self.client.post(f'/api/v1/transfers/{transfer.id}/cancel/', format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(transfer.items.get().received_product, None)

    def test_cancel_after_complete_rejected_and_stock_untouched(self):
        transfer = self._pending_transfer(quantity=10)
        self._map_transfer(transfer, self.buyer_product)
        complete_b2b_transfer(transfer, self.buyer)
        buyer_qty = Inventory.objects.get(product__shop=self.retail_shop).quantity
        with self.assertRaises(Exception):
            cancel_b2b_transfer(transfer, self.sender)
        self.assertEqual(
            Inventory.objects.get(product__shop=self.retail_shop).quantity, buyer_qty)
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.wholesale_branch).quantity, 90)


class TransferScopingTest(B2BBase):
    def test_direction_filters(self):
        transfer = self._pending_transfer()
        self.client.force_authenticate(user=self.buyer)
        response = self.client.get(
            f'/api/v1/transfers/?direction=incoming&shopId={self.retail_shop.id}')
        self.assertEqual([row['id'] for row in response.data['results']], [str(transfer.id)])

        self.client.force_authenticate(user=self.sender)
        response = self.client.get(
            f'/api/v1/transfers/?direction=incoming&shopId={self.wholesale_shop.id}')
        self.assertEqual(response.data['count'], 0)
        response = self.client.get(
            f'/api/v1/transfers/?direction=outgoing&shopId={self.wholesale_shop.id}')
        self.assertEqual(response.data['count'], 1)

    def test_stranger_sees_nothing(self):
        self._pending_transfer()
        self.client.force_authenticate(user=self.stranger)
        response = self.client.get('/api/v1/transfers/')
        self.assertEqual(response.data['count'], 0)


class SaleDeliveryManifestTest(B2BBase):
    """POS sale -> auto-staged manifest -> one-tap accept / decline.

    The sale already deducted the seller, so staging must move nothing,
    declining must not restock, and accepting auto-maps via the snapshots
    (creating missing buyer products) instead of asking the buyer to type.
    """

    def setUp(self):
        super().setUp()
        self.product.barcode = 'AZM-BRC-1'
        self.product.save(update_fields=['barcode'])
        self.buyer_product.barcode = 'AZM-BRC-1'
        self.buyer_product.save(update_fields=['barcode'])
        self.unmatched = Product.objects.create(
            shop=self.wholesale_shop, name='Cadbury Drinking Chocolate 500g', sku='CBD-500',
            barcode='CBD-BRC-9', buying_price=Decimal('3000.00'), selling_price=Decimal('4500.00'))
        Inventory.objects.create(product=self.unmatched, branch=self.wholesale_branch, quantity=40)

    def _sale_payload(self, **overrides):
        payload = {
            'shopId': str(self.wholesale_shop.id),
            'paymentMethod': 'cash',
            'buyerShopId': str(self.retail_shop.id),
            'items': [
                {'productId': str(self.product.id), 'quantity': 24, 'price': '520.00'},
                {'productId': str(self.unmatched.id), 'quantity': 6, 'price': '3100.00'},
            ],
        }
        payload.update(overrides)
        return payload

    def _stage_sale(self, **overrides):
        self.client.force_authenticate(user=self.sender)
        response = self.client.post('/api/v1/sales/', self._sale_payload(**overrides), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def _manifest(self):
        return B2BStockTransfer.objects.get(source='sale', to_shop=self.retail_shop)

    def test_sale_to_buyer_business_stages_pending_manifest(self):
        response = self._stage_sale()
        transfer = self._manifest()
        self.assertEqual(transfer.from_shop, self.wholesale_shop)
        self.assertEqual(str(transfer.sale_id), response.data['id'])
        self.assertEqual(transfer.status, 'pending')
        soda = transfer.items.get(product_name='Azam Soda 500ml')
        self.assertEqual(soda.barcode, 'AZM-BRC-1')
        self.assertEqual(soda.quantity, 24)
        self.assertEqual(soda.unit_cost, Decimal('520.00'))

        # Deducted once, by the sale — staging moves nothing.
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.wholesale_branch).quantity, 76)
        self.assertEqual(
            Inventory.objects.get(product=self.unmatched, branch=self.wholesale_branch).quantity, 34)
        self.assertFalse(Inventory.objects.filter(product__shop=self.retail_shop).exists())

    def test_plain_sale_without_buyer_stages_nothing(self):
        self._stage_sale(buyerShopId='')
        self.assertEqual(B2BStockTransfer.objects.count(), 0)

    def test_unknown_or_same_shop_buyer_rejected_before_the_sale(self):
        self.client.force_authenticate(user=self.sender)
        response = self.client.post(
            '/api/v1/sales/', self._sale_payload(buyerShopId='nope'), format='json')
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/v1/sales/', self._sale_payload(
            buyerShopId=str(self.wholesale_shop.id)), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(B2BStockTransfer.objects.count(), 0)

    def test_restaging_same_sale_returns_same_manifest(self):
        self._stage_sale()
        transfer = self._manifest()
        again = stage_sale_delivery(Sale.objects.get(), self.retail_shop)
        self.assertEqual(again.pk, transfer.pk)
        self.assertEqual(B2BStockTransfer.objects.count(), 1)

    def test_accept_auto_maps_barcode_and_creates_missing_products(self):
        self._stage_sale()
        transfer = self._manifest()
        self.client.force_authenticate(user=self.buyer)
        response = self.client.post(f'/api/v1/transfers/{transfer.id}/accept/', format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'completed')

        soda_inv = Inventory.objects.get(product=self.buyer_product, branch=self.retail_branch)
        self.assertEqual(soda_inv.quantity, 24)
        chocolate = Product.objects.get(shop=self.retail_shop, name='Cadbury Drinking Chocolate 500g')
        self.assertEqual(chocolate.barcode, 'CBD-BRC-9')
        self.assertEqual(chocolate.buying_price, Decimal('3100.00'))
        self.assertEqual(
            Inventory.objects.get(product=chocolate, branch=self.retail_branch).quantity, 6)
        # The sender's stock already left with the sale; accept moves only the copy.
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.wholesale_branch).quantity, 76)

    def test_accept_requires_the_buyer(self):
        self._stage_sale()
        transfer = self._manifest()

        self.client.force_authenticate(user=self.sender)
        response = self.client.post(f'/api/v1/transfers/{transfer.id}/accept/', format='json')
        self.assertEqual(response.status_code, 403)  # visible, but not the receiver

        self.client.force_authenticate(user=self.stranger)
        response = self.client.post(f'/api/v1/transfers/{transfer.id}/accept/', format='json')
        self.assertEqual(response.status_code, 404)  # scoped out entirely
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'pending')

    def test_accept_refused_for_manual_transfers(self):
        transfer = self._pending_transfer()
        self.client.force_authenticate(user=self.buyer)
        response = self.client.post(f'/api/v1/transfers/{transfer.id}/accept/', format='json')
        self.assertEqual(response.status_code, 400)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'pending')
        self.assertFalse(Inventory.objects.filter(product__shop=self.retail_shop).exists())

    def test_declining_delivery_does_not_restock_sender(self):
        self._stage_sale()
        transfer = self._manifest()
        self.client.force_authenticate(user=self.buyer)
        response = self.client.post(f'/api/v1/transfers/{transfer.id}/cancel/', format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'cancelled')
        # The goods were sold; the sender stays at post-sale levels.
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.wholesale_branch).quantity, 76)
        self.assertEqual(
            Inventory.objects.get(product=self.unmatched, branch=self.wholesale_branch).quantity, 34)
        self.assertFalse(Inventory.objects.filter(product__shop=self.retail_shop).exists())

    def test_manifest_serialization_exposes_source_and_barcode(self):
        self._stage_sale()
        transfer = self._manifest()
        self.client.force_authenticate(user=self.buyer)
        response = self.client.get(f'/api/v1/transfers/{transfer.id}/')
        self.assertEqual(response.data['source'], 'sale')
        self.assertEqual(response.data['items'][0]['barcode'], 'AZM-BRC-1')
