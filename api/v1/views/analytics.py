from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.views import APIView
from django.db.models import Sum, Count, F, Q
from django.utils import timezone
from datetime import timedelta

# pyrefly: ignore [missing-import]
from apps.core.insights import generate_insights
# pyrefly: ignore [missing-import]
from apps.core.legacy import resolve_legacy_pk
# pyrefly: ignore [missing-import]
from apps.shops.models import Shop
# pyrefly: ignore [missing-import]
from apps.shops.permissions import assert_shop_access
# pyrefly: ignore [missing-import]
from apps.sales.models import Order, DailySalesSummary
# pyrefly: ignore [missing-import]
from apps.crm.models import Customer, Supplier
# pyrefly: ignore [missing-import]
from apps.products.models import Product, Inventory

class CommandCenterViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request):
        shop_id = request.query_params.get('shop_id')
        if not shop_id:
            return Response({"error": "shop_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Make sure user has access to shop
        if not Shop.objects.filter(id=shop_id).exists():
            return Response({"error": "Shop not found"}, status=status.HTTP_404_NOT_FOUND)
        assert_shop_access(request.user, shop_id)

        today = timezone.now().date()
        thirty_days_ago = today - timedelta(days=30)
        start_of_week = today - timedelta(days=today.weekday())
        start_of_month = today.replace(day=1)

        # 1. Sales
        summaries = DailySalesSummary.objects.filter(shop_id=shop_id, date__gte=thirty_days_ago)
        sales = {
            "today": 0,
            "week": 0,
            "month": 0,
            "revenue": 0,
            "expenses": 0
        }
        for s in summaries:
            sales["revenue"] += float(s.total_sales or 0)
            sales["expenses"] += float(s.total_expenses or 0)
            if s.date == today:
                sales["today"] += float(s.total_sales or 0)
            if s.date >= start_of_week:
                sales["week"] += float(s.total_sales or 0)
            if s.date >= start_of_month:
                sales["month"] += float(s.total_sales or 0)

        # 2. Orders
        order_stats = {"pending": 0, "processing": 0, "completed": 0}
        order_counts = Order.objects.filter(shop_id=shop_id).values('status').annotate(count=Count('id'))
        for o in order_counts:
            st = o['status']
            if st == 'pending':
                order_stats['pending'] += o['count']
            elif st in ['confirmed', 'picking', 'packed', 'ready_for_delivery', 'out_for_delivery']:
                order_stats['processing'] += o['count']
            elif st in ['completed', 'delivered']:
                order_stats['completed'] += o['count']

        # 3. Finance
        cust_debt = Customer.objects.filter(shop_id=shop_id).aggregate(Sum('outstanding_balance'))['outstanding_balance__sum'] or 0
        supp_debt = Supplier.objects.filter(shop_id=shop_id).aggregate(Sum('outstanding_balance'))['outstanding_balance__sum'] or 0
        finance = {
            "customerDebt": float(cust_debt),
            "supplierDebt": float(supp_debt)
        }

        # 4. Product Intelligence
        inventory_items = Inventory.objects.filter(product__shop_id=shop_id).select_related('product')
        inv_value = sum((item.quantity or 0) * (item.product.buying_price or 0) for item in inventory_items)
        low_stock = sum(1 for item in inventory_items if (item.quantity or 0) <= (item.low_stock_threshold or 5))
        
        product_int = {
            "inventoryValue": float(inv_value),
            "lowStockAlerts": low_stock,
            "totalProducts": Product.objects.filter(shop_id=shop_id).count(),
            "bestSellers": [],
            "slowMovers": []
        }

        # 5. Customer Intelligence
        customers = Customer.objects.filter(shop_id=shop_id).order_by('-total_purchases')
        customer_int = {
            "totalCustomers": customers.count(),
            "topCustomers": [
                {"id": str(c.id), "name": c.name, "totalPurchases": float(c.total_purchases or 0)} 
                for c in customers[:5]
            ],
            "outstandingBalances": float(cust_debt)
        }

        # 6. Supplier Intelligence
        suppliers = Supplier.objects.filter(shop_id=shop_id)
        supplier_int = {
            "totalSuppliers": suppliers.count(),
            "purchaseVolume": float(suppliers.aggregate(Sum('total_purchases'))['total_purchases__sum'] or 0),
            "outstandingPayments": float(supp_debt)
        }

        return Response({
            "sales": sales,
            "orderStats": order_stats,
            "finance": finance,
            "productInt": product_int,
            "customerInt": customer_int,
            "supplierInt": supplier_int
        })


class ShopInsightsView(APIView):
    """AI dashboard insights for one shop (legacy browser-side coach, server-run).

    ``GET /api/v1/shops/<shop_id>/insights/?lang=sw`` answers
    ``{"insights": [{"type", "text"}], "data": {...}}``. An empty ``insights``
    list is a normal answer (no API key or a failed OpenRouter call): the client
    fills the card from ``data`` with its own fallback tips.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, shop_id):
        pk = resolve_legacy_pk(Shop, shop_id)
        if pk is None or not Shop.objects.filter(id=pk).exists():
            return Response({"error": "Shop not found"}, status=status.HTTP_404_NOT_FOUND)
        assert_shop_access(request.user, pk)

        lang = 'en' if request.query_params.get('lang') == 'en' else 'sw'
        return Response(generate_insights(pk, lang=lang))
