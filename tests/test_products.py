from django.db import IntegrityError
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient
# pyrefly: ignore [missing-import]
from apps.products.models import Product, Category, Inventory, InventoryMovement
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop, Branch, UserRole
# pyrefly: ignore [missing-import]
from apps.products.services import adjust_inventory

User = get_user_model()

class InventoryServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='test', password='password')
        self.shop = Shop.objects.create(name='Test Shop')
        self.branch = Branch.objects.create(shop=self.shop, name='Main Branch')
        self.product = Product.objects.create(shop=self.shop, name='Apple', buying_price=1.0, selling_price=2.0)
        
    def test_adjust_inventory_success(self):
        inv, mov = adjust_inventory(
            product=self.product, branch=self.branch, movement_type='in',
            quantity_change=50, user=self.user, reason='Restock'
        )
        self.assertEqual(inv.quantity, 50)
        
    def test_adjust_inventory_insufficient_stock(self):
        adjust_inventory(self.product, self.branch, 'in', 10, self.user)
        with self.assertRaises(ValidationError):
            adjust_inventory(self.product, self.branch, 'sale', -15, self.user)


class InventoryAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='apiuser', password='password')
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='API Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='API Branch')
        self.product = Product.objects.create(shop=self.shop, name='Banana', buying_price=1.0, selling_price=2.0)

    def test_api_adjust_inventory_success(self):
        response = self.client.post('/api/v1/inventory-movements/adjust/', {
            'product_id': self.product.id,
            'branch_id': self.branch.id,
            'movement_type': 'in',
            'quantity': 100,
            'reason': 'API Restock'
        }, format='json')
        
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['inventory']['quantity'], 100)
        self.assertEqual(response.data['movement']['quantity_changed'], 100)

    def test_api_adjust_inventory_negative_stock_error(self):
        # Try to sell 50 bananas when we have 0
        response = self.client.post('/api/v1/inventory-movements/adjust/', {
            'product_id': self.product.id,
            'branch_id': self.branch.id,
            'movement_type': 'sale',
            'quantity': -50,
            'reason': 'API Sale'
        }, format='json')
        
        self.assertEqual(response.status_code, 400)
        self.assertIn('Insufficient stock', response.data['detail'][0])

    def test_api_adjust_inventory_not_found(self):
        response = self.client.post('/api/v1/inventory-movements/adjust/', {
            'product_id': '00000000-0000-0000-0000-000000000000',
            'branch_id': self.branch.id,
            'movement_type': 'in',
            'quantity': 10
        }, format='json')
        
        self.assertEqual(response.status_code, 404)
        self.assertIn('not found', response.data['detail'])


class InventoryAppParityAPITests(TestCase):
    """The app reads stock rows with camelCase keys, addressed by Firestore ids."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='invapp', password='password')
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Stock Shop', legacy_id='stockShop01')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(
            shop=self.shop, name='Main', legacy_id='branchLegacy01', is_main=True)
        self.product = Product.objects.create(
            shop=self.shop, name='Milk', legacy_id='prodLegacy01',
            buying_price=1, selling_price=2)
        self.inventory = Inventory.objects.create(
            product=self.product, branch=self.branch, quantity=7,
            low_stock_threshold=3, location='Shelf A')

    def test_list_by_legacy_shop_and_product(self):
        response = self.client.get('/api/v1/inventory/', {
            'shop_id': 'stockShop01', 'product_id': 'prodLegacy01'})
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data['results']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['quantity'], 7)
        self.assertEqual(row['minStock'], 3)
        self.assertEqual(row['allocatedQty'], 0)
        self.assertEqual(row['location'], 'Shelf A')
        self.assertEqual(row['productId'], 'prodLegacy01')
        self.assertEqual(row['branchId'], 'branchLegacy01')
        self.assertEqual(row['shopId'], 'stockShop01')
        self.assertIsNotNone(row['lastUpdated'])

    def test_list_accepts_camel_case_filters(self):
        response = self.client.get('/api/v1/inventory/', {
            'shopId': 'stockShop01', 'productId': 'prodLegacy01'})
        self.assertEqual(len(response.data['results']), 1)
        unknown = self.client.get('/api/v1/inventory/', {'shopId': 'noSuchShop'})
        self.assertEqual(unknown.data['results'], [])

    def test_min_stock_patch_accepts_min_stock_alias(self):
        response = self.client.patch(
            f"/api/v1/inventory/{self.inventory.id}/", {'minStock': 11}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.low_stock_threshold, 11)
        self.assertEqual(response.data['minStock'], 11)

    def test_movements_use_app_field_names(self):
        adjust_inventory(self.product, self.branch, 'in', 5, self.user, reason='Restock')
        response = self.client.get('/api/v1/inventory-movements/', {
            'shop_id': 'stockShop01', 'product_id': 'prodLegacy01'})
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data['results']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['type'], 'in')
        self.assertEqual(row['quantityChanged'], 5)
        self.assertEqual(row['previousQty'], 7)
        self.assertEqual(row['newQty'], 12)
        self.assertEqual(row['reason'], 'Restock')
        self.assertEqual(row['productName'], 'Milk')
        self.assertEqual(row['productId'], 'prodLegacy01')
        self.assertEqual(row['shopId'], 'stockShop01')
        self.assertEqual(row['branchId'], 'branchLegacy01')
        self.assertEqual(row['userName'], 'invapp')
        self.assertIsNotNone(row['date'])

    def test_adjust_accepts_camel_case_without_branch(self):
        response = self.client.post('/api/v1/inventory-movements/adjust/', {
            'productId': 'prodLegacy01',
            'movementType': 'in',
            'quantity': 4,
            'reason': 'Restock',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['inventory']['quantity'], 11)
        self.assertEqual(response.data['movement']['quantityChanged'], 4)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 11)

    def test_adjust_defaults_to_shop_main_branch(self):
        product_without_branch = Product.objects.create(
            shop=self.shop, name='Bread', legacy_id='prodLegacy02')
        response = self.client.post('/api/v1/inventory-movements/adjust/', {
            'productId': 'prodLegacy02', 'movementType': 'in', 'quantity': 3,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        created = Inventory.objects.get(product=product_without_branch)
        self.assertEqual(created.branch_id, self.branch.id)
        self.assertEqual(created.quantity, 3)

    def test_adjust_with_legacy_branch_id(self):
        response = self.client.post('/api/v1/inventory-movements/adjust/', {
            'productId': 'prodLegacy01',
            'branchId': 'branchLegacy01',
            'movementType': 'adjustment',
            'quantity': -2,
            'reason': 'Damaged',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 5)

    def test_adjust_with_unknown_legacy_product_is_404(self):
        response = self.client.post('/api/v1/inventory-movements/adjust/', {
            'productId': 'noSuchProduct', 'movementType': 'in', 'quantity': 1,
        }, format='json')
        self.assertEqual(response.status_code, 404)
        self.assertIn('not found', response.data['detail'])

    def test_adjust_without_any_branch_is_404(self):
        lonely_shop = Shop.objects.create(name='No Branch Shop', legacy_id='noBranchShop01')
        UserRole.objects.create(user=self.user, shop=lonely_shop, role='owner')
        Product.objects.create(shop=lonely_shop, name='Ghost', legacy_id='ghostProd01')
        response = self.client.post('/api/v1/inventory-movements/adjust/', {
            'productId': 'ghostProd01', 'movementType': 'in', 'quantity': 1,
        }, format='json')
        self.assertEqual(response.status_code, 404)

    def test_foreign_legacy_inventory_is_hidden(self):
        other_shop = Shop.objects.create(name='Other Stock Shop', legacy_id='stockShop02')
        other_branch = Branch.objects.create(shop=other_shop, name='Other', legacy_id='branchLegacy02')
        foreign = Product.objects.create(
            shop=other_shop, name='Foreign Milk', legacy_id='prodLegacy99')
        Inventory.objects.create(product=foreign, branch=other_branch, quantity=1)
        response = self.client.get('/api/v1/inventory/', {'shop_id': 'stockShop02'})
        self.assertEqual(response.data['results'], [])
        detail = self.client.get(f"/api/v1/inventory/{Inventory.objects.get(product=foreign).id}/")
        self.assertEqual(detail.status_code, 404)

class StockTransferTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='transferuser', password='password')
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Transfer Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch1 = Branch.objects.create(shop=self.shop, name='Branch 1')
        self.branch2 = Branch.objects.create(shop=self.shop, name='Branch 2')
        self.product = Product.objects.create(shop=self.shop, name='Orange', buying_price=1.0, selling_price=2.0)
        
        # Add some initial stock to branch 1
        adjust_inventory(self.product, self.branch1, 'in', 50, self.user)

    def test_complete_stock_transfer(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import StockTransfer
        transfer = StockTransfer.objects.create(
            shop=self.shop,
            from_branch=self.branch1,
            to_branch=self.branch2,
            product=self.product,
            quantity=20,
            status='pending',
            created_by=self.user
        )
        
        response = self.client.post(f'/api/v1/stock-transfers/{transfer.id}/complete/', format='json')
        self.assertEqual(response.status_code, 200)
        
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'completed')
        
        # Verify inventory
        inv1 = Inventory.objects.get(product=self.product, branch=self.branch1)
        self.assertEqual(inv1.quantity, 30)  # 50 - 20
        
        inv2 = Inventory.objects.get(product=self.product, branch=self.branch2)
        self.assertEqual(inv2.quantity, 20)  # 0 + 20

    def test_create_transfer_via_api_with_camelcase_payload(self):
        response = self.client.post('/api/v1/stock-transfers/', {
            'shopId': str(self.shop.id),
            'fromBranchId': str(self.branch1.id),
            'toBranchId': str(self.branch2.id),
            'productId': str(self.product.id),
            'quantity': 5,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['productName'], 'Orange')

    def test_cancel_stock_transfer(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import StockTransfer
        transfer = StockTransfer.objects.create(
            shop=self.shop,
            from_branch=self.branch1,
            to_branch=self.branch2,
            product=self.product,
            quantity=20,
            status='pending',
            created_by=self.user
        )
        
        response = self.client.post(f'/api/v1/stock-transfers/{transfer.id}/cancel/', format='json')
        self.assertEqual(response.status_code, 200)
        
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, 'cancelled')
        
        # Verify inventory unchanged
        inv1 = Inventory.objects.get(product=self.product, branch=self.branch1)
        self.assertEqual(inv1.quantity, 50)
        
        # Branch 2 should not have inventory yet
        self.assertFalse(Inventory.objects.filter(product=self.product, branch=self.branch2).exists())


class ProductAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='produser', password='password')
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Product Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='Main')
        self.other_shop = Shop.objects.create(name='Other Shop')
        self.other_branch = Branch.objects.create(shop=self.other_shop, name='Other')
        self.product = Product.objects.create(
            shop=self.shop, name='Coca Cola', sku='CC-1',
            buying_price=1, selling_price=2, marketplace_categories=['beverages'],
        )
        self.foreign_product = Product.objects.create(shop=self.other_shop, name='Fanta', sku='FA-2')

    def test_list_only_returns_own_shop_products(self):
        response = self.client.get('/api/v1/products/')
        self.assertEqual(response.status_code, 200)
        names = [p['name'] for p in response.data['results']]
        self.assertIn('Coca Cola', names)
        self.assertNotIn('Fanta', names)

    def test_detail_of_foreign_product_is_404(self):
        response = self.client.get(f'/api/v1/products/{self.foreign_product.id}/')
        self.assertEqual(response.status_code, 404)

    def test_create_product_with_camelcase_payload(self):
        response = self.client.post('/api/v1/products/', {
            'shopId': str(self.shop.id),
            'name': 'Bread',
            'buyingPrice': '10.50',
            'sellingPrice': '15.00',
            'categories': ['food', 'bakery'],
            'branchId': str(self.branch.id),
            'storeLocation': 'Shelf A',
            'publishToFacebook': True,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(str(response.data['shopId']), str(self.shop.id))
        self.assertEqual(response.data['categories'], ['food', 'bakery'])
        self.assertTrue(response.data['publishToFacebook'])
        product = Product.objects.get(id=response.data['id'])
        self.assertEqual(product.marketplace_categories, ['food', 'bakery'])
        self.assertEqual(product.branch_id, self.branch.id)
        self.assertEqual(str(product.buying_price), '10.50')

    def test_create_product_for_foreign_shop_forbidden(self):
        response = self.client.post('/api/v1/products/', {
            'shopId': str(self.other_shop.id), 'name': 'Stolen',
        }, format='json')
        self.assertEqual(response.status_code, 403)

    def test_create_product_without_shop_rejected(self):
        response = self.client.post('/api/v1/products/', {'name': 'No Shop'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('shopId', response.data)

    def test_patch_product_with_camelcase_payload(self):
        response = self.client.patch(f'/api/v1/products/{self.product.id}/', {
            'sellingPrice': '3.25', 'status': 'inactive',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.product.refresh_from_db()
        self.assertEqual(str(self.product.selling_price), '3.25')
        self.assertEqual(self.product.status, 'inactive')

    def test_search_filter_matches_name_and_sku(self):
        Product.objects.create(shop=self.shop, name='Sprite', sku='SP-9')
        by_name = self.client.get('/api/v1/products/', {'search': 'cola'})
        self.assertEqual([p['name'] for p in by_name.data['results']], ['Coca Cola'])
        by_sku = self.client.get('/api/v1/products/', {'search': 'SP-9'})
        self.assertEqual([p['name'] for p in by_sku.data['results']], ['Sprite'])

    def test_low_stock_filter(self):
        healthy = Product.objects.create(shop=self.shop, name='Bulk Rice')
        Inventory.objects.create(product=self.product, branch=self.branch, quantity=3, low_stock_threshold=5)
        Inventory.objects.create(product=healthy, branch=self.branch, quantity=100, low_stock_threshold=5)
        response = self.client.get('/api/v1/products/', {'low_stock': 'true'})
        self.assertEqual([p['name'] for p in response.data['results']], ['Coca Cola'])

    def test_filter_by_branch(self):
        Product.objects.create(shop=self.shop, name='Branch Item', branch=self.branch)
        response = self.client.get('/api/v1/products/', {'branch_id': str(self.branch.id)})
        self.assertEqual([p['name'] for p in response.data['results']], ['Branch Item'])


class CategoryAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='catuser', password='password')
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Cat Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.other_shop = Shop.objects.create(name='Cat Other')
        self.category = Category.objects.create(shop=self.shop, name='Drinks')
        Category.objects.create(shop=self.other_shop, name='Hidden')

    def test_list_only_own_shop_categories(self):
        response = self.client.get('/api/v1/categories/')
        self.assertEqual([c['name'] for c in response.data['results']], ['Drinks'])

    def test_create_category_with_shop_id_alias(self):
        response = self.client.post('/api/v1/categories/', {
            'shopId': str(self.shop.id), 'name': 'Snacks',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(str(response.data['shopId']), str(self.shop.id))

    def test_create_category_for_foreign_shop_forbidden(self):
        response = self.client.post('/api/v1/categories/', {
            'shopId': str(self.other_shop.id), 'name': 'Steal',
        }, format='json')
        self.assertEqual(response.status_code, 403)

    def test_case_variant_duplicate_rejected(self):
        response = self.client.post('/api/v1/categories/', {
            'shopId': str(self.shop.id), 'name': 'drinks',
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('already exists', str(response.data))

    def test_rename_to_case_variant_of_sibling_rejected(self):
        Category.objects.create(shop=self.shop, name='Snacks')
        response = self.client.patch(f'/api/v1/categories/{self.category.id}/', {
            'name': 'snacks',
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.category.refresh_from_db()
        self.assertEqual(self.category.name, 'Drinks')

    def test_database_constraint_blocks_case_variants(self):
        with self.assertRaises(IntegrityError):
            Category.objects.create(shop=self.shop, name='DRINKS')


class LegacyIdAPITests(TestCase):
    """Rows imported from Firestore are addressed by their legacy document id."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='legacyuser', password='password')
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Legacy Shop', legacy_id='shopLegacyId01')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.category = Category.objects.create(
            shop=self.shop, name='Drinks', legacy_id='catLegacyId01')
        self.product = Product.objects.create(
            shop=self.shop, name='Legacy Coke', legacy_id='prodLegacyId01',
            category=self.category, buying_price=1, selling_price=2,
        )

    def test_filter_by_legacy_shop_and_category(self):
        by_shop = self.client.get('/api/v1/products/', {'shop_id': 'shopLegacyId01'})
        self.assertEqual([p['name'] for p in by_shop.data['results']], ['Legacy Coke'])
        by_category = self.client.get('/api/v1/products/', {'category_id': 'catLegacyId01'})
        self.assertEqual([p['name'] for p in by_category.data['results']], ['Legacy Coke'])
        unknown = self.client.get('/api/v1/products/', {'shop_id': 'doesNotExist'})
        self.assertEqual(unknown.data['results'], [])

    def test_detail_by_legacy_id(self):
        response = self.client.get('/api/v1/products/prodLegacyId01/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['legacyId'], 'prodLegacyId01')
        self.assertEqual(response.data['id'], str(self.product.id))
        self.assertEqual(response.data['category'], 'Drinks')
        # Relations mirror the Firestore ids the app stores.
        self.assertEqual(response.data['shopId'], 'shopLegacyId01')
        self.assertEqual(response.data['categoryId'], 'catLegacyId01')

    def test_unknown_legacy_id_is_404(self):
        response = self.client.get('/api/v1/products/noSuchProduct/')
        self.assertEqual(response.status_code, 404)
        response = self.client.get('/api/v1/categories/noSuchCategory/')
        self.assertEqual(response.status_code, 404)

    def test_create_with_legacy_shop_and_category_and_blank_expiry(self):
        response = self.client.post('/api/v1/products/', {
            'shopId': 'shopLegacyId01',
            'categoryId': 'catLegacyId01',
            'name': 'Legacy Bread',
            'expiryDate': '',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        product = Product.objects.get(id=response.data['id'])
        self.assertEqual(product.shop_id, self.shop.id)
        self.assertEqual(product.category_id, self.category.id)
        self.assertIsNone(product.expiry_date)
        self.assertIsNone(response.data['expiryDate'])
        self.assertEqual(response.data['category'], 'Drinks')
        self.assertEqual(response.data['shopId'], 'shopLegacyId01')
        self.assertEqual(response.data['categoryId'], 'catLegacyId01')

    def test_patch_and_delete_by_legacy_id(self):
        patched = self.client.patch('/api/v1/products/prodLegacyId01/', {
            'sellingPrice': '3.50',
        }, format='json')
        self.assertEqual(patched.status_code, 200, patched.data)
        self.product.refresh_from_db()
        self.assertEqual(str(self.product.selling_price), '3.50')

        deleted = self.client.delete('/api/v1/products/prodLegacyId01/')
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(Product.objects.filter(id=self.product.id).exists())

    def test_foreign_legacy_product_is_404(self):
        other_shop = Shop.objects.create(name='Other Legacy Shop', legacy_id='shopLegacyId02')
        foreign = Product.objects.create(
            shop=other_shop, name='Foreign', legacy_id='prodLegacyId02')
        response = self.client.get(f'/api/v1/products/{foreign.legacy_id}/')
        self.assertEqual(response.status_code, 404)


class MerchantCategoryAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='merchantcat', password='password')
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Wizard Shop', legacy_id='wizardShop01')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')

    def test_create_with_legacy_shop_id_autoslugs(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import MerchantCategory
        response = self.client.post('/api/v1/merchant-categories/', {
            'shopId': 'wizardShop01', 'name': 'Fresh Produce',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        category = MerchantCategory.objects.get(id=response.data['id'])
        self.assertEqual(category.shop_id, self.shop.id)
        self.assertEqual(category.slug, 'fresh-produce')
        self.assertEqual(response.data['status'], 'active')
        self.assertEqual(response.data['shopId'], 'wizardShop01')

    def test_list_is_shop_scoped(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import MerchantCategory
        other_shop = Shop.objects.create(name='Other Wizard')
        MerchantCategory.objects.create(shop=self.shop, name='Mine', slug='mine')
        MerchantCategory.objects.create(shop=other_shop, name='Hidden', slug='hidden')
        response = self.client.get('/api/v1/merchant-categories/', {'shop_id': 'wizardShop01'})
        self.assertEqual([c['name'] for c in response.data['results']], ['Mine'])

    def test_delete_by_legacy_id(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import MerchantCategory
        category = MerchantCategory.objects.create(
            shop=self.shop, name='Old', slug='old', legacy_id='mcLegacy01')
        response = self.client.delete('/api/v1/merchant-categories/mcLegacy01/')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(MerchantCategory.objects.filter(id=category.id).exists())

    def test_duplicate_name_at_same_level_rejected(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import MerchantCategory
        MerchantCategory.objects.create(shop=self.shop, name='Fresh Produce', slug='fresh-produce')
        response = self.client.post('/api/v1/merchant-categories/', {
            'shopId': 'wizardShop01', 'name': 'Fresh Produce',
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('already exists', str(response.data))

    def test_case_variant_name_rejected(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import MerchantCategory
        MerchantCategory.objects.create(shop=self.shop, name='Fresh Produce', slug='fresh-produce')
        response = self.client.post('/api/v1/merchant-categories/', {
            'shopId': 'wizardShop01', 'name': 'fresh produce',
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)

    def test_same_name_allowed_under_different_parents(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import MerchantCategory
        drinks = MerchantCategory.objects.create(shop=self.shop, name='Drinks', slug='drinks')
        food = MerchantCategory.objects.create(shop=self.shop, name='Food', slug='food')
        first = self.client.post('/api/v1/merchant-categories/', {
            'shopId': 'wizardShop01', 'name': 'Sodas', 'parentId': str(drinks.id),
        }, format='json')
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post('/api/v1/merchant-categories/', {
            'shopId': 'wizardShop01', 'name': 'sodas', 'parentId': str(food.id),
        }, format='json')
        self.assertEqual(second.status_code, 201, second.data)

    def test_rename_into_existing_sibling_rejected(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import MerchantCategory
        MerchantCategory.objects.create(shop=self.shop, name='Alpha', slug='alpha')
        beta = MerchantCategory.objects.create(
            shop=self.shop, name='Beta', slug='beta', legacy_id='mcBeta01')
        response = self.client.patch('/api/v1/merchant-categories/mcBeta01/', {
            'name': 'ALPHA',
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        beta.refresh_from_db()
        self.assertEqual(beta.name, 'Beta')

    def test_fourth_level_rejected(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import MerchantCategory
        root = MerchantCategory.objects.create(shop=self.shop, name='Root', slug='root')
        child = MerchantCategory.objects.create(shop=self.shop, name='Child', slug='child', parent=root)
        grandchild = MerchantCategory.objects.create(shop=self.shop, name='Grand', slug='grand', parent=child)
        response = self.client.post('/api/v1/merchant-categories/', {
            'shopId': 'wizardShop01', 'name': 'Too Deep', 'parentId': str(grandchild.id),
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('3 levels', str(response.data))

    def test_reparenting_into_own_subtree_rejected(self):
        # pyrefly: ignore [missing-import]
        from apps.products.models import MerchantCategory
        root = MerchantCategory.objects.create(
            shop=self.shop, name='Root', slug='root', legacy_id='mcRoot01')
        child = MerchantCategory.objects.create(shop=self.shop, name='Child', slug='child', parent=root)
        response = self.client.patch('/api/v1/merchant-categories/mcRoot01/', {
            'parentId': str(child.id),
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)
        root.refresh_from_db()
        self.assertIsNone(root.parent)



class ProductBulkImportAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='importer', password='password')
        self.client.force_authenticate(user=self.user)
        self.shop = Shop.objects.create(name='Import Shop')
        UserRole.objects.create(user=self.user, shop=self.shop, role='owner')
        self.branch = Branch.objects.create(shop=self.shop, name='Main')
        self.other_shop = Shop.objects.create(name='Foreign Shop')
        self.product = Product.objects.create(
            shop=self.shop, name='Coca Cola', buying_price=1, selling_price=2)

    def _post(self, rows, shop_id=None):
        return self.client.post('/api/v1/products/bulk_import/', {
            'shopId': str(shop_id or self.shop.id),
            'rows': rows,
        }, format='json')

    def test_updates_existing_by_name_and_sets_stock(self):
        response = self._post([
            {'name': 'coca cola', 'buyingPrice': '1,500', 'sellingPrice': '2,500', 'quantity': 10},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary'], {
            'total': 1, 'created': 0, 'updated': 1, 'errors': 0, 'stockChanges': 1,
        })
        self.product.refresh_from_db()
        self.assertEqual(str(self.product.buying_price), '1500.00')
        self.assertEqual(str(self.product.selling_price), '2500.00')
        self.assertEqual(self.product.name, 'Coca Cola')
        inventory = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inventory.quantity, 10)
        movement = InventoryMovement.objects.get(product=self.product)
        self.assertEqual(movement.movement_type, 'in')
        self.assertEqual(movement.reason, 'Excel import')

    def test_creates_new_product_with_new_category(self):
        response = self._post([
            {'name': 'Blueband', 'category': 'dairy', 'unit': 'pcs',
             'sellingPrice': '3500', 'quantity': 5},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary']['created'], 1)
        product = Product.objects.get(name='Blueband')
        self.assertEqual(product.shop_id, self.shop.id)
        self.assertEqual(product.branch_id, self.branch.id)
        self.assertEqual(product.category.name, 'dairy')
        self.assertEqual(product.unit, 'pcs')
        self.assertEqual(
            Inventory.objects.get(product=product, branch=self.branch).quantity, 5)

    def test_case_variant_categories_collapse_into_one(self):
        response = self._post([
            {'name': 'Wing A', 'category': 'Chakula', 'quantity': 1},
            {'name': 'Wing B', 'category': 'chakula', 'quantity': 2},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary']['created'], 2)
        self.assertEqual(response.data['summary']['errors'], 0)
        categories = Category.objects.filter(shop=self.shop)
        self.assertEqual(categories.count(), 1)
        self.assertEqual(categories.get().name, 'Chakula')
        keeper_id = categories.get().id
        self.assertEqual(Product.objects.get(name='Wing A').category_id, keeper_id)
        self.assertEqual(Product.objects.get(name='Wing B').category_id, keeper_id)

    def test_barcode_match_wins_over_differing_name(self):
        self.product.barcode = 'AZM-1'
        self.product.save(update_fields=['barcode'])
        response = self._post([
            {'name': 'Totally Different', 'barcode': 'AZM-1', 'quantity': 7},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary']['updated'], 1)
        self.assertTrue(Product.objects.filter(barcode='AZM-1').count(), 1)
        inventory = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inventory.quantity, 7)

    def test_empty_row_is_error_but_rest_apply(self):
        response = self._post([
            {'sellingPrice': '5'},
            {'name': 'Sprite', 'quantity': 3},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary']['errors'], 1)
        self.assertEqual(response.data['summary']['created'], 1)
        self.assertEqual(response.data['results'][0]['status'], 'error')
        self.assertIn('Name', response.data['results'][0]['message'])
        self.assertTrue(Product.objects.filter(name='Sprite').exists())

    def test_negative_quantity_is_error_and_creates_nothing(self):
        response = self._post([{'name': 'Fanta', 'quantity': -2}])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary']['errors'], 1)
        self.assertFalse(Product.objects.filter(name='Fanta').exists())

    def test_stock_reduction_uses_adjustment_movement(self):
        Inventory.objects.create(product=self.product, branch=self.branch, quantity=5)
        response = self._post([{'name': 'Coca Cola', 'quantity': 3}])
        self.assertEqual(response.status_code, 200, response.data)
        inventory = Inventory.objects.get(product=self.product, branch=self.branch)
        self.assertEqual(inventory.quantity, 3)
        movement = InventoryMovement.objects.get(product=self.product)
        self.assertEqual(movement.movement_type, 'adjustment')
        self.assertEqual(movement.quantity_changed, -2)

    def test_empty_cells_leave_existing_values(self):
        response = self._post([{'name': 'Coca Cola', 'quantity': 2}])
        self.assertEqual(response.status_code, 200, response.data)
        self.product.refresh_from_db()
        self.assertEqual(str(self.product.buying_price), '1.00')
        self.assertEqual(self.product.sku, '')

    def test_forbidden_for_foreign_shop(self):
        response = self._post([{'name': 'Stolen'}], shop_id=self.other_shop.id)
        self.assertEqual(response.status_code, 403)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self._post([{'name': 'Anything'}])
        self.assertEqual(response.status_code, 401)

    def test_over_500_rows_rejected(self):
        response = self._post([{'name': f'Row {i}'} for i in range(501)])
        self.assertEqual(response.status_code, 400)
