"""B2B procurement endpoints the React pages call.

Covers the purchases orders/shipments/GRN routes plus the b2b connections,
AP balances, supplier invoices and supplier payments routes. Payloads mirror
what the dialogs send (camelCase ids, ``supplierId`` as the platform shop) and
assert the legacy timeline texts/messages the UI shows.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

# pyrefly: ignore [missing-import]
from apps.products.models import Inventory, Product
# pyrefly: ignore [missing-import]
from apps.purchases.models import (
    B2BConnection, B2BSupplierBalance, B2BSupplierInvoice, PurchaseOrder,
)
# pyrefly: ignore [missing-import]
from apps.purchases.services import create_purchase_order
# pyrefly: ignore [missing-import]
from apps.shops.models import Branch, Shop, UserRole
# pyrefly: ignore [missing-import]
from apps.users.models import User

ORDERS_URL = '/api/v1/purchases/orders/'
SHIPMENTS_URL = '/api/v1/purchases/shipments/'
GRNS_URL = '/api/v1/purchases/grns/'
CONNECTIONS_URL = '/api/v1/b2b/connections/'
BALANCES_URL = '/api/v1/b2b/supplier-balances/'
INVOICES_URL = '/api/v1/b2b/supplier-invoices/'
PAYMENTS_URL = '/api/v1/b2b/supplier-payments/'


class B2BApiBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='buyer', email='buyer@test.com', password='password')
        self.client.force_authenticate(user=self.user)

        self.buyer = Shop.objects.create(name='Buyer Duka')
        self.supplier_shop = Shop.objects.create(name='Supplier Depot')
        UserRole.objects.create(user=self.user, shop=self.buyer, role='owner')
        self.supplier_user = User.objects.create_user(
            username='supplier', email='supplier@test.com', password='password')
        UserRole.objects.create(user=self.supplier_user, shop=self.supplier_shop, role='owner')
        self.branch = Branch.objects.create(shop=self.buyer, name='Main', is_main=True)

        self.product = Product.objects.create(
            shop=self.buyer, name='Crate', buying_price=Decimal('50.00'),
            selling_price=Decimal('80.00'))

    def create_po(self, expected_qty=10, unit_cost='50.00'):
        return create_purchase_order(
            buyer_shop=self.buyer,
            supplier_shop=self.supplier_shop,
            items_data=[{
                'product_id': str(self.product.pk),
                'product_name': self.product.name,
                'expected_qty': expected_qty,
                'unit_cost': unit_cost,
            }],
        )

    def receive_grn(self, po, accepted_qty=10, unit_cost='50.00'):
        return self.client.post(ORDERS_URL + 'process_grn/', {
            'poId': str(po.pk),
            'shopId': str(self.buyer.pk),
            'supplierId': str(self.supplier_shop.pk),
            'items': [{
                'productId': str(self.product.pk),
                'expectedQty': po.items.first().expected_qty,
                'receivedQty': accepted_qty,
                'acceptedQty': accepted_qty,
                'rejectedQty': 0,
                'unitCost': unit_cost,
            }],
            'status': 'completed',
        }, format='json')


class B2BConnectionApiTests(B2BApiBase):
    def test_request_returns_existing_then_approve(self):
        payload = {
            'buyerShopId': str(self.buyer.pk),
            'supplierShopId': str(self.supplier_shop.pk),
        }
        resp = self.client.post(CONNECTIONS_URL, payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['status'], 'pending')
        connection_id = resp.data['id']

        # Legacy requestB2BConnection returned the existing link instead of duplicating it.
        resp = self.client.post(CONNECTIONS_URL, payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['id'], connection_id)
        self.assertEqual(B2BConnection.objects.count(), 1)

        resp = self.client.post(
            f'{CONNECTIONS_URL}{connection_id}/approve/',
            {'pricingTier': 'reseller'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'approved')
        self.assertEqual(resp.data['pricingTier'], 'reseller')

    def test_missing_supplier_shop_is_a_field_error(self):
        resp = self.client.post(
            CONNECTIONS_URL, {'buyerShopId': str(self.buyer.pk)}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['supplierShopId'][0], 'This field is required.')


class PurchaseOrderApiTests(B2BApiBase):
    def test_create_list_update_status_and_process_grn(self):
        payload = {
            'buyerShopId': str(self.buyer.pk),
            'supplierShopId': str(self.supplier_shop.pk),
            'supplierName': self.supplier_shop.name,
            'items': [{
                'productId': str(self.product.pk),
                'productName': self.product.name,
                'expectedQty': 10,
                'unitCost': '50.00',
            }],
            'status': 'draft',
            'currency': 'TZS',
        }
        resp = self.client.post(ORDERS_URL, payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['subtotal'], '500.00')
        self.assertEqual(resp.data['totalAmount'], '500.00')
        self.assertEqual(resp.data['timeline'][0]['status'], 'draft')
        self.assertEqual(resp.data['items'][0]['productId'], str(self.product.pk))
        po_id = resp.data['id']

        resp = self.client.get(f'{ORDERS_URL}?shop_id={self.buyer.pk}&role=buyer')
        self.assertEqual(resp.data['count'], 1)

        resp = self.client.post(
            f'{ORDERS_URL}{po_id}/update_status/',
            {'status': 'submitted', 'notes': 'Please review'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'submitted')
        self.assertIn('Please review', resp.data['timeline'][-1]['description'])

        resp = self.receive_grn(PurchaseOrder.objects.get(pk=po_id), accepted_qty=6)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['status'], 'completed')
        self.assertEqual(str(resp.data['supplierId']), str(self.supplier_shop.pk))

        po = PurchaseOrder.objects.get(pk=po_id)
        self.assertEqual(po.status, 'partially_received')
        self.assertEqual(po.items.first().received_qty, 6)
        self.assertEqual(
            Inventory.objects.get(branch=self.branch, product=self.product).quantity, 6)
        balance = B2BSupplierBalance.objects.get(
            buyer_shop=self.buyer, supplier_shop=self.supplier_shop)
        self.assertEqual(balance.outstanding_balance, Decimal('300.00'))

        resp = self.client.get(f'{GRNS_URL}?po_id={po_id}')
        self.assertEqual(resp.data['count'], 1)

    def test_process_grn_unknown_po_is_a_field_error(self):
        resp = self.client.post(ORDERS_URL + 'process_grn/', {
            'poId': str(self.product.pk),
            'shopId': str(self.buyer.pk),
            'items': [{'productId': str(self.product.pk), 'acceptedQty': 1, 'unitCost': '50.00'}],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['poId'], 'Purchase order not found.')


class PurchaseShipmentApiTests(B2BApiBase):
    def test_create_shipment_list_and_update_status(self):
        po = self.create_po()
        # The supplier shop's user ships against the buyer's PO.
        self.client.force_authenticate(user=self.supplier_user)
        payload = {
            'poId': str(po.pk),
            'supplierShopId': str(self.supplier_shop.pk),
            'buyerShopId': str(self.buyer.pk),
            'status': 'dispatched',
            'carrier': 'Boda',
            'driverName': 'John Doe',
            'vehicleNumber': 'T123ABC',
            'items': [{
                'productId': str(self.product.pk),
                'sourceProductId': 'SUP-PROD-001',
                'productName': self.product.name,
                'shippedQty': 10,
            }],
        }
        resp = self.client.post(SHIPMENTS_URL, payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['status'], 'dispatched')
        self.assertEqual(resp.data['driverName'], 'John Doe')
        self.assertEqual(resp.data['buyerShopId'], str(self.buyer.pk))
        self.assertEqual(resp.data['supplierShopId'], str(self.supplier_shop.pk))
        self.assertEqual(resp.data['items'][0]['shippedQty'], 10)
        self.assertEqual(resp.data['items'][0]['sourceProductId'], 'SUP-PROD-001')
        shipment_id = resp.data['id']

        po.refresh_from_db()
        self.assertEqual(po.status, 'awaiting_shipment')
        self.assertEqual(po.timeline[-1]['status'], 'shipment_created')
        self.assertEqual(po.timeline[-1]['description'], 'Shipment created')

        resp = self.client.get(f'{SHIPMENTS_URL}?po_id={po.pk}')
        self.assertEqual(resp.data['count'], 1)

        resp = self.client.post(
            f'{SHIPMENTS_URL}{shipment_id}/update_status/',
            {'status': 'delivered', 'notes': 'Handed over'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'delivered')
        self.assertEqual(resp.data['supplierNotes'], 'Handed over')


class B2BSupplierInvoiceApiTests(B2BApiBase):
    def test_create_invoice_then_approve_adjusts_balance(self):
        po = self.create_po()
        self.assertEqual(self.receive_grn(po).status_code, status.HTTP_201_CREATED)
        grn = po.grns.first()
        balance = B2BSupplierBalance.objects.get(
            buyer_shop=self.buyer, supplier_shop=self.supplier_shop)
        self.assertEqual(balance.outstanding_balance, Decimal('500.00'))

        # CreateInvoiceDialog payload: totals a bit above the GRN value.
        resp = self.client.post(INVOICES_URL, {
            'supplierShopId': str(self.supplier_shop.pk),
            'buyerShopId': str(self.buyer.pk),
            'purchaseOrderId': str(po.pk),
            'grnIds': [str(grn.pk)],
            'invoiceNumber': 'INV-001',
            'invoiceDate': '2026-09-30',
            'dueDate': '2026-10-30',
            'currency': 'TZS',
            'subtotal': '520.00',
            'taxAmount': '0.00',
            'discountAmount': '0.00',
            'totalAmount': '520.00',
            'status': 'under_review',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['invoiceNumber'], 'INV-001')
        self.assertEqual(resp.data['invoiceDate'], '2026-09-30')
        self.assertEqual(resp.data['grnIds'], [str(grn.pk)])
        self.assertEqual(str(resp.data['purchaseOrderId']), str(po.pk))
        invoice_id = resp.data['id']

        resp = self.client.get(f'{INVOICES_URL}?buyer_shop_id={self.buyer.pk}')
        self.assertEqual(resp.data['count'], 1)

        resp = self.client.post(
            f'{INVOICES_URL}{invoice_id}/update_status/',
            {'status': 'approved'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data['status'], 'approved')

        # under_review -> approved books the invoice-vs-GRN difference on the AP balance.
        balance.refresh_from_db()
        self.assertEqual(balance.outstanding_balance, Decimal('520.00'))
        self.assertEqual(balance.received_goods_value, Decimal('520.00'))

    def test_create_invoice_unknown_po_is_a_field_error(self):
        resp = self.client.post(INVOICES_URL, {
            'supplierShopId': str(self.supplier_shop.pk),
            'buyerShopId': str(self.buyer.pk),
            'purchaseOrderId': str(self.product.pk),
            'invoiceNumber': 'INV-002',
            'totalAmount': '10.00',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['purchaseOrderId'], 'Purchase order not found.')


class B2BSupplierPaymentApiTests(B2BApiBase):
    def _seed_balance(self, outstanding='500.00'):
        return B2BSupplierBalance.objects.create(
            buyer_shop=self.buyer, supplier_shop=self.supplier_shop,
            supplier_name=self.supplier_shop.name, total_purchases=1,
            received_goods_value=Decimal(outstanding),
            outstanding_balance=Decimal(outstanding))

    def test_payment_draws_down_balance_and_settles_invoices(self):
        po = self.create_po()
        self.receive_grn(po)
        invoice = B2BSupplierInvoice.objects.create(
            buyer_shop=self.buyer, supplier_shop=self.supplier_shop,
            invoice_number='INV-003', status='approved',
            total_amount=Decimal('500.00'))

        resp = self.client.post(PAYMENTS_URL, {
            'supplierShopId': str(self.supplier_shop.pk),
            'buyerShopId': str(self.buyer.pk),
            'amount': '200.00',
            'method': 'Mobile Money',
            'reference': 'MPESA-1',
            'invoiceIds': [str(invoice.pk)],
            'date': '2026-09-30T10:00:00.000Z',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['amount'], '200.00')
        self.assertEqual(resp.data['method'], 'Mobile Money')
        self.assertEqual(resp.data['invoiceIds'], [str(invoice.pk)])

        balance = B2BSupplierBalance.objects.get(
            buyer_shop=self.buyer, supplier_shop=self.supplier_shop)
        self.assertEqual(balance.outstanding_balance, Decimal('300.00'))
        self.assertEqual(balance.paid_amount, Decimal('200.00'))

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')

        resp = self.client.get(f'{PAYMENTS_URL}?buyer_shop_id={self.buyer.pk}')
        self.assertEqual(resp.data['count'], 1)

    def test_overpayment_requires_confirmation(self):
        self._seed_balance()
        payload = {
            'supplierShopId': str(self.supplier_shop.pk),
            'buyerShopId': str(self.buyer.pk),
            'amount': '600.00',
            'method': 'Cash',
        }
        resp = self.client.post(PAYMENTS_URL, payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        # The plain string becomes the toast text, matching the legacy message.
        self.assertEqual(
            resp.data['detail'][0],
            'Payment amount (600) exceeds outstanding balance (500). '
            'Please confirm overpayment.',
        )

        resp = self.client.post(
            PAYMENTS_URL, {**payload, 'allowOverpayment': True}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        balance = B2BSupplierBalance.objects.get(
            buyer_shop=self.buyer, supplier_shop=self.supplier_shop)
        self.assertEqual(balance.outstanding_balance, Decimal('-100.00'))

    def test_payment_without_balance_is_rejected(self):
        resp = self.client.post(PAYMENTS_URL, {
            'supplierShopId': str(self.supplier_shop.pk),
            'buyerShopId': str(self.buyer.pk),
            'amount': '10.00',
            'method': 'Cash',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            resp.data['detail'][0],
            'Supplier balance record not found. Cannot process payment.')
