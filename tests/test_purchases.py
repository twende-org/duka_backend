from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch
# pyrefly: ignore [missing-import]
from apps.products.models import Product, Inventory
# pyrefly: ignore [missing-import]
from apps.crm.models import Supplier
# pyrefly: ignore [missing-import]
from apps.purchases.services import create_purchase_order, process_grn
# pyrefly: ignore [missing-import]
from apps.purchases.models import PurchaseOrder, GRN

User = get_user_model()

class PurchasesServicesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser', 
            password='password123',
            phone='+1234567890'
        )
        
        self.buyer_shop = Shop.objects.create(name="Buyer Shop")
        self.supplier_shop = Shop.objects.create(name="Supplier Shop")
        self.branch = Branch.objects.create(shop=self.buyer_shop, name="Main Branch", is_main=True)
        
        self.supplier = Supplier.objects.create(
            shop=self.buyer_shop,
            name="Platform Supplier",
            platform_shop_id=self.supplier_shop.id
        )
        
        # pyrefly: ignore [missing-import]
        from apps.products.models import Category
        self.category = Category.objects.create(shop=self.buyer_shop, name="Test Category")
        
        self.product = Product.objects.create(
            shop=self.buyer_shop,
            name="Test Product",
            category=self.category,
            selling_price=Decimal('100.00'),
            buying_price=Decimal('50.00')
        )

    def test_create_and_process_grn(self):
        # 1. Create PO
        items_data = [{
            'product_id': self.product.id,
            'product_name': self.product.name,
            'expected_qty': 10,
            'unit_cost': '50.00'
        }]
        
        po = create_purchase_order(
            buyer_shop=self.buyer_shop,
            supplier_shop=self.supplier_shop,
            items_data=items_data
        )
        
        self.assertEqual(po.subtotal, Decimal('500.00'))
        self.assertEqual(po.status, 'draft')
        self.assertEqual(po.items.count(), 1)
        
        # 2. Process GRN (Partial Receipt)
        grn_items = [{
            'product_id': self.product.id,
            'accepted_qty': 6,
            'unit_cost': '50.00'
        }]
        
        grn = process_grn(
            purchase_order_id=po.id,
            buyer_shop=self.buyer_shop,
            supplier=self.supplier,
            items_data=grn_items,
            user=self.user
        )
        
        self.assertEqual(grn.status, 'completed')
        
        # Verify PO Status updated
        po.refresh_from_db()
        self.assertEqual(po.status, 'partially_received')
        
        po_item = po.items.first()
        self.assertEqual(po_item.received_qty, 6)
        
        # Verify Inventory Increased
        inventory = Inventory.objects.get(branch=self.branch, product=self.product)
        self.assertEqual(inventory.quantity, 6)
        
        # Verify Supplier Balance Increased (6 * 50 = 300)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.outstanding_balance, Decimal('300.00'))
        self.assertEqual(self.supplier.total_purchases, 1)

        # 3. Process second GRN (Remaining Receipt)
        grn_items2 = [{
            'product_id': self.product.id,
            'accepted_qty': 4,
            'unit_cost': '50.00'
        }]
        
        grn2 = process_grn(
            purchase_order_id=po.id,
            buyer_shop=self.buyer_shop,
            supplier=self.supplier,
            items_data=grn_items2,
            user=self.user
        )
        
        po.refresh_from_db()
        self.assertEqual(po.status, 'completed')
        
        po_item.refresh_from_db()
        self.assertEqual(po_item.received_qty, 10)
        
        inventory.refresh_from_db()
        self.assertEqual(inventory.quantity, 10)
        
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.outstanding_balance, Decimal('500.00'))

    def test_create_purchase_shipment(self):
        # pyrefly: ignore [missing-import]
        from apps.purchases.services import create_purchase_shipment
        
        # 1. Create PO
        items_data = [{
            'product_id': self.product.id,
            'product_name': self.product.name,
            'expected_qty': 10,
            'unit_cost': '50.00'
        }]

        po = create_purchase_order(
            buyer_shop=self.buyer_shop,
            supplier_shop=self.supplier_shop,
            items_data=items_data
        )
        
        self.assertEqual(po.status, 'draft')
        
        # 2. Create Shipment as Supplier
        shipment_items = [{
            'product_id': self.product.id,
            'source_product_id': 'SUP-PROD-001',
            'product_name': self.product.name,
            'shipped_qty': 10
        }]
        
        shipment = create_purchase_shipment(
            po_id=po.id,
            supplier_shop=self.supplier_shop,
            items_data=shipment_items,
            status='preparing',
            driver_name='John Doe',
            vehicle_number='T123ABC'
        )
        
        self.assertEqual(shipment.status, 'preparing')
        self.assertEqual(shipment.items.count(), 1)
        
        # Validate timeline added
        self.assertTrue(len(shipment.timeline) > 0)
        self.assertEqual(shipment.timeline[0]['status'], 'preparing')
        
        # Verify PO status updated
        po.refresh_from_db()
        self.assertEqual(po.status, 'awaiting_shipment')
        self.assertTrue(len(po.timeline) > 0)
