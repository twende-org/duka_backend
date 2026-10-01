from django.test import TestCase
from decimal import Decimal
from rest_framework.test import APIClient
from rest_framework import status
# pyrefly: ignore [missing-import]
from apps.crm.models import Customer, CustomerInvoice, CustomerPayment, Supplier, SupplierInvoice, SupplierPayment
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch, UserRole
# pyrefly: ignore [missing-import]
from apps.users.models import User
# pyrefly: ignore [missing-import]
from apps.crm.services import (
    create_customer, update_customer, process_customer_payment, process_supplier_payment,
)

class CRMServiceTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Test Shop")
        self.branch = Branch.objects.create(shop=self.shop, name="Test Branch")
        self.user = User.objects.create(username="testuser", phone="+255700000000")

    def test_auto_linking_on_create(self):
        # Create a customer with the same phone number as the platform user
        customer = create_customer(shop=self.shop, name="John Doe", phone="+255700000000")
        
        # User ID should be automatically linked
        self.assertEqual(customer.user_id, str(self.user.id))
        self.assertIsNotNone(customer.linked_at)

    def test_auto_linking_on_update(self):
        # Create a customer with a different phone number
        customer = create_customer(shop=self.shop, name="John Doe", phone="+255711111111")
        self.assertIsNone(customer.user_id)
        
        # Update phone to match platform user
        customer = update_customer(customer, phone="+255700000000")
        
        # User ID should now be linked
        self.assertEqual(customer.user_id, str(self.user.id))
        self.assertIsNotNone(customer.linked_at)

    def test_process_customer_payment(self):
        # Setup customer with balance and invoice
        customer = create_customer(shop=self.shop, name="John Doe", outstanding_balance=Decimal("100.00"))
        invoice1 = CustomerInvoice.objects.create(
            shop=self.shop, customer=customer,
            amount_due=Decimal("60.00"), amount_paid=Decimal("0.00"), status='pending'
        )
        invoice2 = CustomerInvoice.objects.create(
            shop=self.shop, customer=customer,
            amount_due=Decimal("40.00"), amount_paid=Decimal("0.00"), status='pending'
        )
        
        # Process payment of 80
        payment = process_customer_payment(
            shop=self.shop,
            customer=customer,
            amount=Decimal("80.00"),
            method="Cash",
            invoice_ids=[invoice1.id, invoice2.id]
        )
        
        # Check payment record
        self.assertEqual(payment.amount, Decimal("80.00"))
        self.assertEqual(payment.method, "Cash")
        self.assertEqual(payment.invoices.count(), 2)
        
        # Check customer balance
        customer.refresh_from_db()
        self.assertEqual(customer.outstanding_balance, Decimal("20.00"))
        
        # Check invoices
        invoice1.refresh_from_db()
        invoice2.refresh_from_db()
        
        self.assertEqual(invoice1.amount_paid, Decimal("60.00"))
        self.assertEqual(invoice1.status, 'paid')
        
        self.assertEqual(invoice2.amount_paid, Decimal("20.00"))
        self.assertEqual(invoice2.status, 'pending')

    def test_create_supplier(self):
        # pyrefly: ignore [missing-import]
        from apps.crm.services import create_supplier
        supplier = create_supplier(
            shop=self.shop,
            name="Test Supplier",
            phone="+255712345678",
            products="Milk, Bread",
            notes="Delivers on Monday",
            owner_id="owner123"
        )
        self.assertEqual(supplier.name, "Test Supplier")
        self.assertEqual(supplier.products, "Milk, Bread")
        self.assertEqual(supplier.notes, "Delivers on Monday")
        self.assertEqual(supplier.owner_id, "owner123")

    def test_update_supplier(self):
        # pyrefly: ignore [missing-import]
        from apps.crm.services import create_supplier, update_supplier
        supplier = create_supplier(
            shop=self.shop,
            name="Test Supplier",
            phone="+255712345678"
        )
        self.assertEqual(supplier.name, "Test Supplier")
        
        updated_supplier = update_supplier(
            supplier,
            name="Updated Supplier",
            products="Eggs"
        )
        self.assertEqual(updated_supplier.name, "Updated Supplier")
        self.assertEqual(updated_supplier.products, "Eggs")


class SupplierPaymentServiceTests(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Supplier Payment Shop")
        self.supplier = Supplier.objects.create(
            shop=self.shop, name="Jane Supplier", outstanding_balance=Decimal("100.00")
        )

    def test_process_supplier_payment_allocates_oldest_due_first(self):
        invoice_old = SupplierInvoice.objects.create(
            shop=self.shop, supplier=self.supplier,
            amount_due=Decimal("60.00"), amount_paid=Decimal("0.00"), status='pending',
            due_date="2026-09-01",
        )
        invoice_new = SupplierInvoice.objects.create(
            shop=self.shop, supplier=self.supplier,
            amount_due=Decimal("40.00"), amount_paid=Decimal("0.00"), status='pending',
            due_date="2026-10-01",
        )

        payment = process_supplier_payment(
            shop=self.shop,
            supplier=self.supplier,
            amount=Decimal("80.00"),
            method="Bank",
            reference="PMT-001",
            invoice_ids=[invoice_new.id, invoice_old.id],
        )

        self.assertEqual(payment.amount, Decimal("80.00"))
        self.assertEqual(payment.invoices.count(), 2)

        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.outstanding_balance, Decimal("20.00"))

        invoice_old.refresh_from_db()
        invoice_new.refresh_from_db()
        self.assertEqual(invoice_old.amount_paid, Decimal("60.00"))
        self.assertEqual(invoice_old.status, 'paid')
        self.assertEqual(invoice_new.amount_paid, Decimal("20.00"))
        self.assertEqual(invoice_new.status, 'pending')

    def test_process_supplier_payment_rejects_overpayment(self):
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            process_supplier_payment(
                shop=self.shop,
                supplier=self.supplier,
                amount=Decimal("150.00"),
                method="Cash",
            )
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.outstanding_balance, Decimal("100.00"))
        self.assertEqual(SupplierPayment.objects.count(), 0)


class SupplierAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='apuser', password='password')
        self.shop = Shop.objects.create(name='AP Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.supplier = Supplier.objects.create(
            shop=self.shop, name='Supplier One', outstanding_balance=Decimal('100.00')
        )
        self.client.force_authenticate(user=self.user)

    def _post_payment(self, supplier, **overrides):
        payload = {'amount': '80.00', 'method': 'Cash', 'reference': 'RCPT-1'}
        payload.update(overrides)
        return self.client.post(f'/api/v1/suppliers/{supplier.id}/record_payment/', payload, format='json')

    def test_record_payment_via_camelcase_payload(self):
        invoice = SupplierInvoice.objects.create(
            shop=self.shop, supplier=self.supplier,
            amount_due=Decimal('100.00'), amount_paid=Decimal('0.00'), status='pending',
        )
        response = self._post_payment(self.supplier, invoiceIds=[str(invoice.id)])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(str(response.data['amount'])), Decimal('80.00'))
        self.assertEqual(str(response.data['shopId']), str(self.shop.id))
        self.assertEqual(str(response.data['supplierId']), str(self.supplier.id))
        self.assertEqual([str(i) for i in response.data['invoiceIds']], [str(invoice.id)])

        self.supplier.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(self.supplier.outstanding_balance, Decimal('20.00'))
        self.assertEqual(invoice.amount_paid, Decimal('80.00'))
        self.assertEqual(invoice.status, 'pending')

        listing = self.client.get(f'/api/v1/supplier-payments/?shop_id={self.shop.id}')
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data['count'], 1)

    def test_record_payment_accepts_snake_case_invoice_ids(self):
        invoice = SupplierInvoice.objects.create(
            shop=self.shop, supplier=self.supplier, amount_due=Decimal('50.00'),
        )
        response = self._post_payment(self.supplier, amount='50.00', invoice_ids=[str(invoice.id)])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')

    def test_record_payment_exceeding_balance_is_400(self):
        response = self._post_payment(self.supplier, amount='150.00')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('exceeds', str(response.data['detail']))
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.outstanding_balance, Decimal('100.00'))
        self.assertEqual(SupplierPayment.objects.count(), 0)

    def test_foreign_supplier_payment_is_404(self):
        foreign_shop = Shop.objects.create(name='Foreign Supplier Shop')
        foreign_supplier = Supplier.objects.create(
            shop=foreign_shop, name='Foreign Supplier', outstanding_balance=Decimal('50.00')
        )
        response = self._post_payment(foreign_supplier)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(SupplierPayment.objects.count(), 0)

    def test_create_invoice_with_camelcase_aliases(self):
        response = self.client.post('/api/v1/supplier-invoices/', {
            'shopId': str(self.shop.id),
            'supplierId': str(self.supplier.id),
            'amountDue': '75.50',
            'dueDate': '2026-10-15',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'pending')
        # Responses carry both key sets (lossless dual-key convention).
        self.assertEqual(Decimal(str(response.data['amountDue'])), Decimal('75.50'))
        self.assertEqual(Decimal(str(response.data['amount_due'])), Decimal('75.50'))
        self.assertEqual(Decimal(str(response.data['amountPaid'])), Decimal('0.00'))

    def test_create_invoice_with_snake_case_keys(self):
        response = self.client.post('/api/v1/supplier-invoices/', {
            'shop': str(self.shop.id),
            'supplier': str(self.supplier.id),
            'amount_due': '12.00',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(str(response.data['amountDue'])), Decimal('12.00'))

    def test_create_invoice_without_required_fields_is_400(self):
        response = self.client.post('/api/v1/supplier-invoices/', {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        for key in ('shopId', 'supplierId', 'amountDue'):
            self.assertIn(key, response.data)

    def test_invoice_with_supplier_from_other_shop_is_400(self):
        foreign_supplier = Supplier.objects.create(
            shop=Shop.objects.create(name='Other Supplier Shop'), name='Other Supplier'
        )
        response = self.client.post('/api/v1/supplier-invoices/', {
            'shopId': str(self.shop.id),
            'supplierId': str(foreign_supplier.id),
            'amountDue': '10.00',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('supplierId', response.data)

    def test_invoice_list_and_detail_are_shop_scoped(self):
        own = SupplierInvoice.objects.create(
            shop=self.shop, supplier=self.supplier, amount_due=Decimal('10.00')
        )
        foreign_shop = Shop.objects.create(name='Foreign Invoice Shop')
        foreign_supplier = Supplier.objects.create(shop=foreign_shop, name='FS')
        foreign = SupplierInvoice.objects.create(
            shop=foreign_shop, supplier=foreign_supplier, amount_due=Decimal('99.00')
        )

        listing = self.client.get('/api/v1/supplier-invoices/')
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data['count'], 1)
        self.assertEqual(str(listing.data['results'][0]['id']), str(own.id))

        detail = self.client.get(f'/api/v1/supplier-invoices/{foreign.id}/')
        self.assertEqual(detail.status_code, status.HTTP_404_NOT_FOUND)

        create_foreign = self.client.post('/api/v1/supplier-invoices/', {
            'shopId': str(foreign_shop.id),
            'supplierId': str(foreign_supplier.id),
            'amountDue': '10.00',
        }, format='json')
        self.assertEqual(create_foreign.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(SupplierInvoice.objects.count(), 2)

    def test_invoice_filters_by_supplier_and_status(self):
        paid = SupplierInvoice.objects.create(
            shop=self.shop, supplier=self.supplier, amount_due=Decimal('10.00'), status='paid'
        )
        SupplierInvoice.objects.create(
            shop=self.shop, supplier=self.supplier, amount_due=Decimal('20.00'), status='pending'
        )

        by_status = self.client.get(f'/api/v1/supplier-invoices/?shop_id={self.shop.id}&status=paid')
        self.assertEqual(by_status.status_code, status.HTTP_200_OK)
        self.assertEqual(by_status.data['count'], 1)
        self.assertEqual(str(by_status.data['results'][0]['id']), str(paid.id))

        by_supplier = self.client.get(f'/api/v1/supplier-invoices/?supplierId={self.supplier.id}')
        self.assertEqual(by_supplier.data['count'], 2)

    def test_update_invoice_status_via_partial_update(self):
        invoice = SupplierInvoice.objects.create(
            shop=self.shop, supplier=self.supplier, amount_due=Decimal('10.00')
        )
        response = self.client.patch(
            f'/api/v1/supplier-invoices/{invoice.id}/', {'status': 'cancelled'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'cancelled')


class CustomerAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='custapuser', password='password')
        self.shop = Shop.objects.create(name='Customer Shop', legacy_id='shop-legacy-1')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.customer = Customer.objects.create(
            shop=self.shop, name='Customer One', outstanding_balance=Decimal('100.00'),
        )
        self.client.force_authenticate(user=self.user)

    def test_create_customer_with_camelcase_wizard_payload(self):
        response = self.client.post('/api/v1/customers/', {
            'shopId': 'shop-legacy-1',
            'name': 'Wizard Customer',
            'customerType': 'wholesale',
            'businessName': 'Wizard Traders',
            'contactPerson': 'Jane',
            'registrationNumber': 'TIN-9',
            'phone': '+255712345678',
            'email': 'wizard@example.com',
            'address': 'Kariakoo',
            'notes': 'from the wizard',
            'commercialSettings': {'priceTier': 'wholesale', 'creditEnabled': True},
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], 'Wizard Customer')
        self.assertEqual(response.data['customerType'], 'wholesale')
        self.assertEqual(response.data['commercialSettings']['priceTier'], 'wholesale')
        self.assertEqual(response.data['businessName'], 'Wizard Traders')
        self.assertEqual(str(response.data['shopId']), 'shop-legacy-1')
        self.assertIn('legacyId', response.data)
        self.assertIn('totalSpent', response.data)

    def test_create_customer_accepts_blank_contact_details(self):
        response = self.client.post('/api/v1/customers/', {
            'shopId': 'shop-legacy-1',
            'name': 'Walk-in',
            'phone': '',
            'email': '',
            'address': '',
            'notes': 'Created automatically via POS checkout',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['phone'], '')

    def test_create_customer_without_shop_is_400(self):
        response = self.client.post('/api/v1/customers/', {'name': 'No Shop'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('shopId', response.data)

    def test_create_customer_unknown_shop_is_400(self):
        response = self.client.post('/api/v1/customers/', {
            'shopId': 'does-not-exist', 'name': 'Ghost',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('shopId', response.data)

    def test_update_customer_ignores_echoed_shop_id(self):
        response = self.client.patch(
            f'/api/v1/customers/{self.customer.id}/',
            {'name': 'Renamed', 'shopId': 'shop-legacy-1'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, 'Renamed')
        self.assertEqual(self.customer.shop_id, self.shop.id)

    def test_customer_detail_and_update_accept_legacy_id(self):
        self.customer.legacy_id = 'cust-legacy-1'
        self.customer.save(update_fields=['legacy_id'])

        detail = self.client.get('/api/v1/customers/cust-legacy-1/')
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data['legacyId'], 'cust-legacy-1')

        patched = self.client.patch(
            '/api/v1/customers/cust-legacy-1/', {'notes': 'patched'}, format='json'
        )
        self.assertEqual(patched.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.notes, 'patched')

        deleted = self.client.delete('/api/v1/customers/cust-legacy-1/')
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)

    def test_customer_list_filters_by_camel_shop_id(self):
        foreign_shop = Shop.objects.create(name='Foreign Customer Shop')
        Customer.objects.create(shop=foreign_shop, name='Foreign Customer')

        listing = self.client.get('/api/v1/customers/?shopId=shop-legacy-1')
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data['count'], 1)
        self.assertEqual(listing.data['results'][0]['name'], 'Customer One')

    def test_record_customer_payment_via_camelcase_payload(self):
        invoice = CustomerInvoice.objects.create(
            shop=self.shop, customer=self.customer,
            amount_due=Decimal('100.00'), amount_paid=Decimal('0.00'), status='pending',
        )
        response = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record_payment/',
            {'amount': '80.00', 'method': 'Cash', 'reference': 'RCPT-9', 'invoiceIds': [str(invoice.id)]},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(str(response.data['amount'])), Decimal('80.00'))
        self.assertEqual(str(response.data['customerId']), str(self.customer.id))
        self.assertEqual([str(i) for i in response.data['invoiceIds']], [str(invoice.id)])

        self.customer.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(self.customer.outstanding_balance, Decimal('20.00'))
        self.assertEqual(invoice.amount_paid, Decimal('80.00'))

    def test_create_customer_invoice_with_camelcase_aliases(self):
        response = self.client.post('/api/v1/customer-invoices/', {
            'shopId': 'shop-legacy-1',
            'customerId': str(self.customer.id),
            'amountDue': '75.50',
            'dueDate': '2026-10-15',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(Decimal(str(response.data['amountDue'])), Decimal('75.50'))
        self.assertEqual(Decimal(str(response.data['amount_due'])), Decimal('75.50'))

    def test_create_customer_invoice_without_required_fields_is_400(self):
        response = self.client.post('/api/v1/customer-invoices/', {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        for key in ('shopId', 'customerId', 'amountDue'):
            self.assertIn(key, response.data)

    def test_customer_invoice_with_customer_from_other_shop_is_400(self):
        foreign_customer = Customer.objects.create(
            shop=Shop.objects.create(name='Other Customer Shop'), name='Other Customer'
        )
        response = self.client.post('/api/v1/customer-invoices/', {
            'shopId': 'shop-legacy-1',
            'customerId': str(foreign_customer.id),
            'amountDue': '10.00',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customerId', response.data)

    def test_customer_invoice_filters_by_camel_ids(self):
        own = CustomerInvoice.objects.create(
            shop=self.shop, customer=self.customer, amount_due=Decimal('10.00')
        )
        other = Customer.objects.create(shop=self.shop, name='Second Customer')
        CustomerInvoice.objects.create(
            shop=self.shop, customer=other, amount_due=Decimal('20.00')
        )

        listing = self.client.get(
            f'/api/v1/customer-invoices/?shopId=shop-legacy-1&customerId={self.customer.id}'
        )
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data['count'], 1)
        self.assertEqual(str(listing.data['results'][0]['id']), str(own.id))

    def test_create_supplier_with_camelcase_payload(self):
        response = self.client.post('/api/v1/suppliers/', {
            'shopId': 'shop-legacy-1',
            'name': 'Supplier Co',
            'phone': '',
            'email': '',
            'address': 'Dodoma',
            'products': 'Maize',
            'notes': '',
            'ownerId': str(self.user.id),
            'platformShopId': 'plat-1',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], 'Supplier Co')
        self.assertEqual(str(response.data['shopId']), 'shop-legacy-1')
        self.assertEqual(response.data['platformShopId'], 'plat-1')
        self.assertEqual(response.data['ownerId'], str(self.user.id))

    def test_update_supplier_ignores_echoed_shop_id(self):
        supplier = Supplier.objects.create(shop=self.shop, name='Supplier One')
        response = self.client.patch(
            f'/api/v1/suppliers/{supplier.id}/',
            {'name': 'Supplier Two', 'shopId': 'shop-legacy-1'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        supplier.refresh_from_db()
        self.assertEqual(supplier.name, 'Supplier Two')
        self.assertEqual(supplier.shop_id, self.shop.id)
