"""IMEI selection at the counter: the POS checkout, the receipt and the return.

Every rule the cashier can trip over lives in ``apps.sales.services``; this file
drives it through the real endpoint so the serializer aliases, the transaction
rollback and the response the receipt prints are all covered at once.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.products.models import Inventory, InventoryMovement, InventorySerialItem, Product
from apps.products.tests.helpers import add_staff, make_shop
from apps.sales.models import DailySalesSummary, Sale, SaleItem, Shift
from apps.shops.models import Branch

IMEIS = ('352019010000001', '352019010000002', '352019010000003')


def phone(shop, branch, name='Tecno Spark 20', requires_imei=True, quantity=3, price='300000'):
    """A product with ``quantity`` units of stock, no IMEIs and a 100000 margin."""
    product = Product.objects.create(
        shop=shop, name=name, requires_imei=requires_imei,
        selling_price=Decimal(price), buying_price=Decimal('200000'),
        publish_to_directory=True)
    Inventory.objects.create(product=product, branch=branch, quantity=quantity)
    return product


def shelve(shop, product, branch, imeis):
    return InventorySerialItem.objects.bulk_create([
        InventorySerialItem(shop=shop, product=product, branch=branch, imei_number=imei)
        for imei in imeis
    ])


def line(product, quantity=1, **extra):
    row = {'product_id': str(product.id), 'quantity': quantity, 'unit_price': '300000'}
    row.update(extra)
    return row


class PosCheckoutTestCase(TestCase):
    """One phone shop: a tracked product, three devices on the shelf, an owner at the till."""

    def setUp(self):
        self.shop, self.branch, self.owner = make_shop('Simu Duka')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.product = phone(self.shop, self.branch, quantity=3)
        shelve(self.shop, self.product, self.branch, IMEIS)

    def sell(self, items, **overrides):
        payload = {
            'shopId': str(self.shop.id),
            'branchId': str(self.branch.id),
            'paymentMethod': 'cash',
            'items': items,
        }
        payload.update(overrides)
        return self.client.post('/api/v1/sales/', payload, format='json')


class PosImeiCheckoutTests(PosCheckoutTestCase):
    def test_scanning_the_devices_sells_them_and_links_them_to_the_line(self):
        response = self.sell([line(self.product, 2, imeiNumbers=list(IMEIS[:2]))])
        self.assertEqual(response.status_code, 201, response.data)

        units = list(InventorySerialItem.objects.filter(imei_number__in=IMEIS[:2]))
        self.assertEqual({u.status for u in units}, {'sold'})
        self.assertEqual({str(u.sale_item_id) for u in units},
                         {response.data['items'][0]['id']})
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.branch).quantity, 1)
        self.assertEqual(
            InventoryMovement.objects.filter(product=self.product,
                                             movement_type='sale').count(), 1)

    def test_the_third_device_stays_on_the_shelf_when_only_two_are_sold(self):
        self.sell([line(self.product, 2, imeiNumbers=list(IMEIS[:2]))])
        spare = InventorySerialItem.objects.get(imei_number=IMEIS[2])
        self.assertEqual(spare.status, 'in_stock')
        self.assertIsNone(spare.sale_item_id)

    def test_units_can_also_be_bought_by_their_serial_row_ids(self):
        row_ids = list(InventorySerialItem.objects.filter(
            imei_number=IMEIS[0]).values_list('id', flat=True))
        response = self.sell([line(self.product, 1, serialItemIds=[str(i) for i in row_ids])])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['items'][0]['imeiNumbers'], [IMEIS[0]])

    def test_all_three_devices_of_the_line_are_tracked_separately(self):
        response = self.sell([line(self.product, 3, imeiNumbers=list(IMEIS))])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(sorted(response.data['items'][0]['imeiNumbers']), sorted(IMEIS))

    def test_an_imei_already_sold_is_refused_and_names_the_device(self):
        self.sell([line(self.product, 1, imeiNumbers=[IMEIS[0]])])
        response = self.sell([line(self.product, 1, imeiNumbers=[IMEIS[0]])])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(IMEIS[0], str(response.data))
        self.assertIn('sold', str(response.data).lower())

    def test_an_imei_of_another_product_is_refused(self):
        other = phone(self.shop, self.branch, name='Itel A70', quantity=1)
        shelve(self.shop, other, self.branch, ['352019019999999'])
        response = self.sell([line(self.product, 1, imeiNumbers=['352019019999999'])])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('352019019999999', str(response.data))

    def test_an_imei_of_another_shop_is_refused(self):
        """Two shops may own the same model number, never each other's units."""
        rival_imei = '352019018888888'
        other_shop, other_branch, _ = make_shop('Duka Jirani')
        rival = phone(other_shop, other_branch, quantity=1)
        shelve(other_shop, rival, other_branch, [rival_imei])
        response = self.sell([line(self.product, 1, imeiNumbers=[rival_imei])])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(rival_imei, str(response.data))
        self.assertEqual(
            InventorySerialItem.objects.get(imei_number=rival_imei).status, 'in_stock')

    def test_a_device_sitting_in_another_branch_is_refused(self):
        night = Branch.objects.create(shop=self.shop, name='Night Market')
        InventorySerialItem.objects.filter(imei_number=IMEIS[0]).update(branch=night)
        response = self.sell([line(self.product, 1, imeiNumbers=[IMEIS[0]])])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('branch', str(response.data).lower())

    def test_an_unregistered_imei_is_refused(self):
        response = self.sell([line(self.product, 1, imeiNumbers=['000000000000000'])])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('000000000000000', str(response.data))

    def test_fewer_devices_than_units_sold_is_refused(self):
        response = self.sell([line(self.product, 2, imeiNumbers=[IMEIS[0]])])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('identified', str(response.data))

    def test_more_devices_than_units_sold_is_refused(self):
        response = self.sell([line(self.product, 1, imeiNumbers=list(IMEIS[:2]))])
        self.assertEqual(response.status_code, 400, response.data)

    def test_the_same_device_scanned_twice_counts_as_one_unit(self):
        """Dedup is what turns a double-scan into the mismatch error, not a double sale."""
        response = self.sell([line(self.product, 2, imeiNumbers=[IMEIS[0], IMEIS[0]])])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('identified', str(response.data))

    def test_a_tracked_product_cannot_be_sold_without_scanning(self):
        response = self.sell([line(self.product, 1)])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('IMEI', str(response.data))

    def test_a_rejected_sale_writes_nothing_at_all(self):
        response = self.sell([line(self.product, 2, imeiNumbers=[IMEIS[0]])])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(SaleItem.objects.count(), 0)
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.branch).quantity, 3)
        self.assertEqual(InventorySerialItem.objects.filter(status='sold').count(), 0)
        self.assertEqual(InventoryMovement.objects.count(), 0)

    def test_the_shop_can_opt_out_of_mandatory_scanning(self):
        self.shop.store_policies = {'allowSaleWithoutImei': True}
        self.shop.save(update_fields=['store_policies'])
        response = self.sell([line(self.product, 1)])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['items'][0]['imeiNumbers'], [])

    def test_opting_out_still_validates_the_codes_that_are_scanned(self):
        self.shop.store_policies = {'allowSaleWithoutImei': True}
        self.shop.save(update_fields=['store_policies'])
        response = self.sell([line(self.product, 1, imeiNumbers=['000000000000000'])])
        self.assertEqual(response.status_code, 400, response.data)

    def test_an_untracked_product_needs_no_codes_and_reports_no_imeis(self):
        sukuma = phone(self.shop, self.branch, name='Sukuma', requires_imei=False,
                       quantity=5, price='500')
        response = self.client.post('/api/v1/sales/', {
            'shopId': str(self.shop.id), 'branchId': str(self.branch.id),
            'paymentMethod': 'cash',
            'items': [{'product_id': str(sukuma.id), 'quantity': 2, 'unit_price': '500'}],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['items'][0]['imeiNumbers'], [])
        self.assertEqual(
            Inventory.objects.get(product=sukuma, branch=self.branch).quantity, 3)


class ReceiptImeiTests(PosCheckoutTestCase):
    """The receipt has to name the device, and keep naming it after a return."""

    def _sell(self):
        response = self.sell([line(self.product, 2, imeiNumbers=[
            f' {IMEIS[0]} ', '   ', IMEIS[1]])])
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_the_create_response_carries_the_imeis_on_the_line(self):
        self.assertEqual(sorted(self._sell()['items'][0]['imeiNumbers']), sorted(IMEIS[:2]))

    def test_a_later_read_of_the_sale_still_carries_them(self):
        body = self._sell()
        fetched = self.client.get(f"/api/v1/sales/{body['id']}/")
        self.assertEqual(sorted(fetched.data['items'][0]['imeiNumbers']), sorted(IMEIS[:2]))

    def test_scanned_codes_are_trimmed_and_blanks_dropped(self):
        self.assertEqual(len(self._sell()['items'][0]['imeiNumbers']), 2)

    def test_a_voided_sale_keeps_printing_the_device_it_sold(self):
        body = self._sell()
        self.client.post(f"/api/v1/sales/{body['id']}/void/", {}, format='json')
        fetched = self.client.get(f"/api/v1/sales/{body['id']}/")
        self.assertEqual(sorted(fetched.data['items'][0]['imeiNumbers']), sorted(IMEIS[:2]))
        # ...while the register itself shows both devices back on the shelf.
        self.assertEqual(
            InventorySerialItem.objects.filter(sale_item__isnull=True, status='in_stock').count(), 3)

    def test_a_line_that_never_had_devices_reads_as_an_empty_list_not_null(self):
        """``imei_numbers`` is nullable, so an old row must not hand a receipt a null."""
        from api.v1.serializers.sales import SaleItemSerializer

        legacy = SaleItem.objects.create(
            sale=Sale.objects.create(shop=self.shop, branch=self.branch,
                                     total_amount=Decimal('300000'),
                                     payment_method='cash', status='completed'),
            product=self.product, product_name=self.product.name,
            quantity=1, unit_price=Decimal('300000'), total_price=Decimal('300000'))
        legacy.imei_numbers = None
        legacy.save(update_fields=['imei_numbers'])
        self.assertIsNone(SaleItem.objects.get(pk=legacy.pk).imei_numbers)
        self.assertEqual(SaleItemSerializer(legacy).data['imeiNumbers'], [])


class PosDraftImeiTests(PosCheckoutTestCase):
    """A saved cart names its devices but claims nothing until it is confirmed."""

    def _draft(self, imei=IMEIS[0]):
        return self.sell([line(self.product, 1, imeiNumbers=[imei])], status='draft')

    def test_a_draft_does_not_take_the_device_off_the_shelf(self):
        response = self._draft()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['status'], 'draft')
        self.assertEqual(response.data['items'][0]['imeiNumbers'], [IMEIS[0]])
        unit = InventorySerialItem.objects.get(imei_number=IMEIS[0])
        self.assertEqual(unit.status, 'in_stock')
        self.assertIsNone(unit.sale_item_id)
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.branch).quantity, 3)

    def test_confirming_the_draft_is_what_sells_the_device(self):
        sale_id = self._draft().data['id']
        response = self.client.post(f'/api/v1/sales/{sale_id}/confirm/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        unit = InventorySerialItem.objects.get(imei_number=IMEIS[0])
        self.assertEqual(unit.status, 'sold')
        self.assertEqual(str(unit.sale_item_id), response.data['items'][0]['id'])
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.branch).quantity, 2)

    def test_a_device_sold_over_the_counter_first_blocks_the_draft_at_checkout(self):
        sale_id = self._draft().data['id']
        self.sell([line(self.product, 1, imeiNumbers=[IMEIS[0]])])
        response = self.client.post(f'/api/v1/sales/{sale_id}/confirm/', {}, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(IMEIS[0], str(response.data))
        self.assertEqual(Sale.objects.get(pk=sale_id).status, 'draft')

    def test_an_invalid_scan_is_rejected_when_the_draft_is_saved_not_at_checkout(self):
        response = self.sell([line(self.product, 1, imeiNumbers=['000000000000000'])],
                             status='draft')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(Sale.objects.count(), 0)


class SaleVoidTests(PosCheckoutTestCase):
    def setUp(self):
        super().setUp()
        response = self.sell([line(self.product, 2, imeiNumbers=list(IMEIS[:2]))])
        self.assertEqual(response.status_code, 201, response.data)
        self.sale_id = response.data['id']

    def _void(self, reason='Buyer changed his mind', sale_id=None):
        return self.client.post(
            f"/api/v1/sales/{sale_id or self.sale_id}/void/",
            {'reason': reason}, format='json')

    def test_voiding_puts_the_stock_and_the_devices_back(self):
        response = self._void()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'voided')
        self.assertEqual(
            Inventory.objects.get(product=self.product, branch=self.branch).quantity, 3)
        units = InventorySerialItem.objects.filter(imei_number__in=IMEIS[:2])
        self.assertEqual({u.status for u in units}, {'in_stock'})
        self.assertTrue(all(u.sale_item_id is None for u in units))

    def test_voiding_records_the_restock_in_the_ledger(self):
        self._void()
        movement = InventoryMovement.objects.filter(movement_type='adjustment').latest('created_at')
        self.assertEqual(movement.quantity_changed, 2)
        self.assertIn(f'#{self.sale_id}', movement.reason)
        self.assertIn('Buyer changed his mind', movement.reason)

    def test_voiding_reverses_the_day_totals(self):
        self.assertEqual(
            DailySalesSummary.objects.get(shop=self.shop, branch=self.branch).total_sales,
            Decimal('600000'))
        self._void()
        summary = DailySalesSummary.objects.get(shop=self.shop, branch=self.branch)
        self.assertEqual(summary.total_sales, Decimal('0'))
        self.assertEqual(summary.transactions, 0)
        self.assertEqual(summary.profit, Decimal('0'))

    def test_voiding_credits_the_cash_drawer_back(self):
        shift = Shift.objects.create(shop=self.shop, status='OPEN',
                                     opening_cash=Decimal('0'))
        sale_id = self.sell([line(self.product, 1, imeiNumbers=[IMEIS[2]])],
                            shiftId=str(shift.id)).data['id']
        self.assertEqual(
            Shift.objects.get(pk=shift.id).cash_sales_total, Decimal('300000'))
        self._void(sale_id=sale_id)
        self.assertEqual(Shift.objects.get(pk=shift.id).cash_sales_total, Decimal('0'))

    def test_a_sale_cannot_be_voided_twice(self):
        self.assertEqual(self._void().status_code, 200)
        response = self._void()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('already voided', str(response.data).lower())

    def test_a_draft_is_deleted_not_voided(self):
        draft = self.sell([line(self.product, 1, imeiNumbers=[IMEIS[2]])], status='draft')
        self.assertEqual(draft.status_code, 201, draft.data)
        response = self._void(sale_id=draft.data['id'])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('draft', str(response.data).lower())

    def test_an_attendant_cannot_void_a_sale(self):
        attendant = add_staff(self.shop, 'Simu Duka', 'attendant')
        client = APIClient()
        client.force_authenticate(attendant)
        response = client.post(f'/api/v1/sales/{self.sale_id}/void/', {}, format='json')
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(Sale.objects.get(pk=self.sale_id).status, 'completed')
        self.assertEqual(InventorySerialItem.objects.filter(status='sold').count(), 2)

    def test_a_create_call_cannot_post_a_sale_that_is_already_voided(self):
        # ``voided`` is a state only ``void_sale`` reaches.
        response = self.sell([line(self.product, 1, imeiNumbers=[IMEIS[2]])], status='voided')
        self.assertEqual(response.status_code, 400, response.data)

    def test_a_device_re_registered_as_new_stock_blocks_the_return(self):
        """Two in-stock rows for one IMEI is what the shop's unique constraint forbids."""
        InventorySerialItem.objects.create(
            shop=self.shop, product=self.product, branch=self.branch,
            imei_number=IMEIS[0], status='in_stock')
        response = self._void()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(IMEIS[0], str(response.data))
        self.assertEqual(InventorySerialItem.objects.filter(status='sold').count(), 2)
