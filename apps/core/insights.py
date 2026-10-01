"""Server-side AI business insights for the dashboard coach.

The legacy insight surfaces were browser-side: ``AIBusinessCoach.tsx`` read
today's/yesterday's sales, today's expenses and low-stock items straight from
Firestore and called OpenRouter with a live API key baked into the bundle. The
Firebase Cloud Function that was supposed to own this
(``functions/src/insights/generator.js``) listens on ``insight_requests`` docs
that nothing ever created, so this module replaces the *live* path: it gathers
the same numbers from the Django models and asks OpenRouter server-side, which
also keeps ``OPENROUTER_API_KEY`` out of the frontend.
"""
import logging
import re
from datetime import timedelta

import requests
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

# pyrefly: ignore [missing-import]
from apps.expenses.models import Expense
# pyrefly: ignore [missing-import]
from apps.products.models import Product
# pyrefly: ignore [missing-import]
from apps.sales.models import DailySalesSummary

logger = logging.getLogger(__name__)

OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
OPENROUTER_MODEL = 'google/gemini-2.5-flash-lite'
REQUEST_TIMEOUT = 20

#: The components scanned the first 10 shop products for low stock.
PRODUCT_SCAN_LIMIT = 10
#: Legacy ``minStock || 5``: a falsy threshold read as 5.
FALLBACK_THRESHOLD = 5

SUCCESS_MARKERS = ('pongezi', 'vizuri', 'great', 'increase', 'hongera')
WARNING_MARKERS = ('tahadhari', 'warning')


def _sales_total(shop_id, day):
    total = DailySalesSummary.objects.filter(shop_id=shop_id, date=day).aggregate(
        Sum('total_sales'))['total_sales__sum']
    return float(total or 0)


def _expenses_total(shop_id, day):
    total = Expense.objects.filter(shop_id=shop_id, date=day).aggregate(
        Sum('amount'))['amount__sum']
    return float(total or 0)


def _low_stock_items(shop_id):
    """``"Name (qty)"`` labels for products below their low-stock threshold.

    Quantities are summed shop-wide (across branch inventory rows) and compared
    against the summed thresholds, which reduces to the legacy single-branch
    comparison ``quantity < (minStock || 5)``. Products without any inventory
    row are skipped, mirroring the legacy ``if (invDoc.exists)`` guard.
    """
    labels = []
    products = Product.objects.filter(shop_id=shop_id).prefetch_related('inventory')[:PRODUCT_SCAN_LIMIT]
    for product in products:
        rows = list(product.inventory.all())
        if not rows:
            continue
        quantity = sum(row.quantity or 0 for row in rows)
        threshold = sum(row.low_stock_threshold or FALLBACK_THRESHOLD for row in rows)
        if quantity < threshold:
            labels.append(f"{product.name} ({quantity})")
    return labels


def gather_snapshot(shop_id):
    """Today's/yesterday's numbers, matching the fields the components read."""
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    return {
        'todaySales': _sales_total(shop_id, today),
        'yesterdaySales': _sales_total(shop_id, yesterday),
        'todayExpenses': _expenses_total(shop_id, today),
        'lowStockItems': _low_stock_items(shop_id),
    }


def build_prompt(snapshot, lang='sw'):
    """The AIBusinessCoach prompt, moved verbatim to the server."""
    lang_name = 'Kiswahili Sanifu' if lang == 'sw' else 'English'
    low_stock = ', '.join(snapshot['lowStockItems']) or 'None'
    return (
        f"You are Twende Duka AI Business Assistant & Coach (Mshauri wa Biashara).\n"
        f"Provide 3 direct, actionable business insights based on today's data.\n"
        f'Speak directly to the user like a proactive advisor (e.g., "Hongera...", '
        f'"Tahadhari...", "Ushauri...").\n'
        f"\n"
        f"Data for {lang_name}:\n"
        f"- Sales Today: TZS {snapshot['todaySales']:g}\n"
        f"- Sales Yesterday: TZS {snapshot['yesterdaySales']:g}\n"
        f"- Expenses Today: TZS {snapshot['todayExpenses']:g}\n"
        f"- Items with low stock: {low_stock}\n"
        f"\n"
        f"RULES:\n"
        f"1. Use CLEAR, CONVERSATIONAL {lang_name}.\n"
        f"2. Start directly with the observation and recommendation.\n"
        f"3. No intro preambles.\n"
        f"4. One insight per line."
    )


def parse_insights(text):
    """Split the model answer into up to 3 ``{type, text}`` insights.

    Mirrors the browser parser: drop short/markup lines, unwrap ``1.``/``-``
    prefixes, then classify by keyword so the client can colour the card.
    """
    insights = []
    for raw_line in (text or '').split('\n'):
        line = raw_line.strip()
        if len(line) <= 5:
            continue
        clean = line.replace('**', '')
        clean = re.sub(r'^\d+\.\s*', '', clean)
        clean = re.sub(r'^-\s*', '', clean).strip()
        lowered = clean.lower()
        if any(marker in lowered for marker in SUCCESS_MARKERS):
            kind = 'success'
        elif any(marker in lowered for marker in WARNING_MARKERS):
            kind = 'warning'
        else:
            kind = 'info'
        insights.append({'type': kind, 'text': clean})
        if len(insights) == 3:
            break
    return insights


def generate_insights(shop_id, lang='sw'):
    """Gather the shop snapshot and (when possible) AI tips for it.

    Returns ``{"insights": [...], "data": {...}}``. AI being unavailable is not
    an error: ``insights`` comes back empty and the client renders its own
    fallback tips from ``data``, the way the components always did.
    """
    snapshot = gather_snapshot(shop_id)
    insights = []
    api_key = getattr(settings, 'OPENROUTER_API_KEY', '')
    if not api_key:
        logger.warning('OPENROUTER_API_KEY is not configured; returning data without AI insights.')
        return {'insights': insights, 'data': snapshot}
    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': OPENROUTER_MODEL,
                'messages': [{'role': 'user', 'content': build_prompt(snapshot, lang)}],
                'temperature': 0.4,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        choices = payload.get('choices') or []
        content = ''
        if choices:
            content = (choices[0].get('message') or {}).get('content') or ''
        insights = parse_insights(content)
    except (requests.RequestException, ValueError, AttributeError, TypeError) as exc:
        logger.warning('OpenRouter insights request failed: %s', exc)
    return {'insights': insights, 'data': snapshot}
