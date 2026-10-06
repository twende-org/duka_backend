from django.urls import path, include
from rest_framework.routers import DefaultRouter
from api.v1.views.shops import ShopViewSet, BranchViewSet, UserRoleViewSet, InvitationViewSet
from api.v1.views.public import (
    PublicProductSearchView, PublicShopViewSet, PublicTrendingSearchesView,
)
from api.v1.views.products import (
    ProductViewSet, CategoryViewSet, MerchantCategoryViewSet,
    InventoryViewSet, InventoryMovementViewSet, StockTransferViewSet,
)
from api.v1.views.transfers import B2BStockTransferViewSet
from api.v1.views.sales import SalesViewSet, OrderViewSet
from api.v1.views.expenses import ExpenseViewSet
from api.v1.views.shifts import ShiftViewSet
from api.v1.views.crm import (
    CustomerViewSet, CustomerInvoiceViewSet, CustomerPaymentViewSet, SupplierViewSet,
    SupplierInvoiceViewSet, SupplierPaymentViewSet,
)
from api.v1.views.purchases import PurchaseOrderViewSet, PurchaseShipmentViewSet, GRNViewSet
from api.v1.views.b2b import (
    B2BConnectionViewSet, B2BSupplierBalanceViewSet,
    B2BSupplierInvoiceViewSet, B2BSupplierPaymentViewSet,
)
from api.v1.views.corporate import (
    CorporateDepartmentViewSet, CorporateBuyerViewSet, CorporatePurchaseOrderViewSet,
)
from api.v1.views.marketing import DiscountCodeViewSet, CampaignViewSet
from api.v1.views.social import (
    SocialIntegrationViewSet, ConversationViewSet, MessageViewSet, SocialLogViewSet,
)
from api.v1.views.facebook import (
    FacebookCallbackView, FacebookPagesSessionView, FacebookConnectionView, FacebookPostView,
    FacebookWebhookView, MarketingDripTestView,
)
from api.v1.views.tiktok import (
    TikTokCallbackView, TikTokSessionView, TikTokConnectionView, TikTokCreatorInfoView,
    TikTokPostView,
)
from api.v1.views.analytics import CommandCenterViewSet, ShopInsightsView
from api.v1.views.telemetry import (
    ActivityLogViewSet, AnalyticsEventViewSet, ErrorEventViewSet,
    ShopAnalyticsSummaryView, GlobalAnalyticsSummaryView,
)
from api.v1.views.users import (
    AddressViewSet, StaffUserViewSet, SubscriptionViewSet, WishlistViewSet,
)
from api.v1.views.ai import (
    AIAssistantView, AIProductExtractionView, AIProductListExtractionView,
    AIPublicAssistantView, AIMarketplaceAssistantView, AISearchParseView,
    AIGenerateCopyView,
)
from api.v1.views.portal import PortalOrderListView, PortalReceiptListView
from api.v1.views.identity import ResolveIdentityView
from api.v1.views.uploads import MediaUploadView
from api.v1.views.support import AnnouncementViewSet, SupportTicketViewSet
from api.v1.views.intake import IntakeBatchViewSet, IntakeQuotaView, ProductDraftViewSet

router = DefaultRouter()
router.register(r'public/shops', PublicShopViewSet, basename='public-shop')
router.register(r'shops', ShopViewSet, basename='shop')
router.register(r'branches', BranchViewSet, basename='branch')
router.register(r'user-roles', UserRoleViewSet, basename='shop-role')
router.register(r'invitations', InvitationViewSet, basename='invitation')
router.register(r'products', ProductViewSet, basename='product')
router.register(r'categories', CategoryViewSet, basename='category')
router.register(r'merchant-categories', MerchantCategoryViewSet, basename='merchant-category')
# Twende Duka AI intake (staging). Registered BEFORE the plain 'inventory'
# route: its ^inventory/(?P<pk>...)/$ detail pattern would otherwise swallow
# 'inventory/intake/'.
router.register(r'inventory/intake', IntakeBatchViewSet, basename='intake-batch')
router.register(r'inventory/intake-drafts', ProductDraftViewSet, basename='intake-draft')
router.register(r'inventory', InventoryViewSet, basename='inventory')
router.register(r'inventory-movements', InventoryMovementViewSet, basename='inventory-movement')
router.register(r'stock-transfers', StockTransferViewSet, basename='stock-transfer')
# Inter-shop (wholesaler -> retailer) transfers; complete/cancel handshake.
router.register(r'transfers', B2BStockTransferViewSet, basename='b2b-stock-transfer')
router.register(r'sales', SalesViewSet, basename='sale')
router.register(r'orders', OrderViewSet, basename='order')
router.register(r'expenses', ExpenseViewSet, basename='expense')
router.register(r'shifts', ShiftViewSet, basename='shift')
router.register(r'customers', CustomerViewSet, basename='customer')
router.register(r'customer-invoices', CustomerInvoiceViewSet, basename='customer-invoice')
router.register(r'customer-payments', CustomerPaymentViewSet, basename='customer-payment')
router.register(r'suppliers', SupplierViewSet, basename='supplier')
router.register(r'supplier-invoices', SupplierInvoiceViewSet, basename='supplier-invoice')
router.register(r'supplier-payments', SupplierPaymentViewSet, basename='supplier-payment')

router.register(r'purchases/orders', PurchaseOrderViewSet, basename='purchase-order')
router.register(r'purchases/shipments', PurchaseShipmentViewSet, basename='purchase-shipment')
router.register(r'purchases/grns', GRNViewSet, basename='purchase-grn')
# B2B procurement finance (connections, AP balances, supplier invoices/payments).
router.register(r'b2b/connections', B2BConnectionViewSet, basename='b2b-connection')
router.register(r'b2b/supplier-balances', B2BSupplierBalanceViewSet, basename='b2b-supplier-balance')
router.register(r'b2b/supplier-invoices', B2BSupplierInvoiceViewSet, basename='b2b-supplier-invoice')
router.register(r'b2b/supplier-payments', B2BSupplierPaymentViewSet, basename='b2b-supplier-payment')
# Corporate procurement (departments, member buyers, purchase orders).
router.register(r'corporate/departments', CorporateDepartmentViewSet, basename='corporate-department')
router.register(r'corporate/buyers', CorporateBuyerViewSet, basename='corporate-buyer')
router.register(r'corporate/purchase-orders', CorporatePurchaseOrderViewSet, basename='corporate-purchase-order')

router.register(r'discounts', DiscountCodeViewSet, basename='discount')
router.register(r'campaigns', CampaignViewSet, basename='campaign')

router.register(r'social-integrations', SocialIntegrationViewSet, basename='social-integration')
router.register(r'conversations', ConversationViewSet, basename='conversation')
router.register(r'messages', MessageViewSet, basename='message')
router.register(r'analytics/command-center', CommandCenterViewSet, basename='command-center')
router.register(r'announcements', AnnouncementViewSet, basename='announcement')
router.register(r'support-tickets', SupportTicketViewSet, basename='support-ticket')
# Telemetry (activity trail, storefront events, client error reports).
router.register(r'activity-logs', ActivityLogViewSet, basename='activity-log')
router.register(r'analytics-events', AnalyticsEventViewSet, basename='analytics-event')
router.register(r'error-events', ErrorEventViewSet, basename='error-event')
# Platform users + their subscription / wishlist / addresses.
router.register(r'users', StaffUserViewSet, basename='platform-user')
router.register(r'subscriptions', SubscriptionViewSet, basename='subscription')
router.register(r'wishlist', WishlistViewSet, basename='wishlist')
router.register(r'addresses', AddressViewSet, basename='address')
# Social post/reply outcome log (read-only).
router.register(r'social-logs', SocialLogViewSet, basename='social-log')

urlpatterns = [
    # Cross-shop storefront search (legacy browser-side discoveryService +
    # IntelligentSearchBar suggestions) and popular-query suggestions built
    # from the search_query/search_submitted telemetry stream.
    path('public/products/search/', PublicProductSearchView.as_view(), name='public-product-search'),
    path('public/trending-searches/', PublicTrendingSearchesView.as_view(), name='public-trending-searches'),
    # Customer portal: cross-shop, caller-scoped reads (not router resources).
    path('portal/orders/', PortalOrderListView.as_view(), name='portal-orders'),
    path('portal/receipts/', PortalReceiptListView.as_view(), name='portal-receipts'),
    # Facebook OAuth (legacy Cloud Functions facebookCallback /
    # getFacebookPagesSession / saveFacebookConnection).
    path('social/facebook/callback', FacebookCallbackView.as_view(), name='facebook-callback'),
    path(
        'social/facebook/sessions/<str:session_id>',
        FacebookPagesSessionView.as_view(),
        name='facebook-session-pages',
    ),
    path(
        'social/facebook/connections',
        FacebookConnectionView.as_view(),
        name='facebook-connections',
    ),
    path(
        'social/facebook/posts',
        FacebookPostView.as_view(),
        name='facebook-posts',
    ),
    # Facebook -> Django webhook (legacy Cloud Function facebookWebhook).
    path(
        'social/facebook/webhook',
        FacebookWebhookView.as_view(),
        name='facebook-webhook',
    ),
    # TikTok OAuth + posting (same shape as the Facebook block above).
    path('social/tiktok/callback', TikTokCallbackView.as_view(), name='tiktok-callback'),
    path(
        'social/tiktok/sessions/<str:session_id>',
        TikTokSessionView.as_view(),
        name='tiktok-session',
    ),
    path(
        'social/tiktok/connections',
        TikTokConnectionView.as_view(),
        name='tiktok-connections',
    ),
    path(
        'social/tiktok/creator-info',
        TikTokCreatorInfoView.as_view(),
        name='tiktok-creator-info',
    ),
    path(
        'social/tiktok/posts',
        TikTokPostView.as_view(),
        name='tiktok-posts',
    ),
    # Peak-hours marketing drip (legacy Cloud Function testMarketingDrip).
    path(
        'social/marketing/test-drip',
        MarketingDripTestView.as_view(),
        name='marketing-test-drip',
    ),
    # AI dashboard insights (legacy browser-side AIBusinessCoach/InsightsCard).
    path('shops/<str:shop_id>/insights/', ShopInsightsView.as_view(), name='shop-insights'),
    # Storefront analytics rollups (legacy browser-side getShopAnalytics and
    # getGlobalAnalytics aggregates over analytics_events).
    path('analytics/shop-summary/', ShopAnalyticsSummaryView.as_view(), name='analytics-shop-summary'),
    path('analytics/global-summary/', GlobalAnalyticsSummaryView.as_view(), name='analytics-global-summary'),
    # AI copilot + product-photo extraction (legacy browser-side src/lib/ai.ts,
    # which called OpenRouter with the bundled VITE_OPENROUTER_API_KEY).
    path('ai/assistant/', AIAssistantView.as_view(), name='ai-assistant'),
    path('ai/extract-product/', AIProductExtractionView.as_view(), name='ai-extract-product'),
    # One photo -> many distinct products, queued into the Add Product form.
    path('ai/extract-products/', AIProductListExtractionView.as_view(), name='ai-extract-products'),
    # Anonymous storefront/marketplace/search endpoints (were browser-side
    # OpenRouter calls with the bundled VITE_OPENROUTER_API_KEY).
    path('ai/public-assistant/', AIPublicAssistantView.as_view(), name='ai-public-assistant'),
    path('ai/marketplace-assistant/', AIMarketplaceAssistantView.as_view(), name='ai-marketplace-assistant'),
    path('ai/parse-search/', AISearchParseView.as_view(), name='ai-parse-search'),
    # Authenticated marketing-copy generator for the share/ad dialogs.
    path('ai/generate-copy/', AIGenerateCopyView.as_view(), name='ai-generate-copy'),
    # Identity resolution across CRM rows (legacy Cloud Function resolveIdentity).
    path('identity/resolve/', ResolveIdentityView.as_view(), name='identity-resolve'),
    # Product/shop image uploads (legacy Firebase Storage in src/lib/imageUtils.ts).
    path('uploads/', MediaUploadView.as_view(), name='media-upload'),
    # Monthly AI photo allowance per shop. Standalone path BEFORE the router
    # include: the inventory/intake detail pattern would swallow 'quota' as a pk.
    path('inventory/intake/quota/', IntakeQuotaView.as_view(), name='intake-quota'),
    path('', include(router.urls)),
]
