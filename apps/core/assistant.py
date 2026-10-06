"""Server-side OpenRouter calls for the browser-side AI helpers.

``src/lib/ai.ts`` called OpenRouter straight from the bundle with
``VITE_OPENROUTER_API_KEY``: ``askBusinessAssistant`` backs the floating
dashboard copilot and ``extractProductDetailsFromImage`` backs the product
camera scan. The storefront widgets (``src/lib/public-ai.ts``,
``src/lib/marketplace-ai.ts``), the directory search parser
(``src/lib/services/aiRecommendationService.ts``) and the three marketing
copy dialogs did the same. All prompts and request shapes are ported
verbatim so answers stay the same; only the transport moved server-side,
which keeps the key out of the frontend.
"""
import json
import logging
import re

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
#: The models the legacy browser helpers requested. gemini-1.5-flash was
#: retired on OpenRouter (400 "not a valid model ID"), so vision calls move
#: to the same live model the invoice intake uses.
CHAT_MODEL = 'google/gemini-2.5-flash-lite'
VISION_MODEL = 'google/gemini-2.5-flash'
#: The storefront widgets used gemini-2.5-pro; keep them on it so moving the
#: transport server-side changes neither answer quality nor cost.
PUBLIC_CHAT_MODEL = 'google/gemini-2.5-pro'
CHAT_TIMEOUT = 20
VISION_TIMEOUT = 30
#: The pro model is slower, especially the 4000-token marketplace concierge.
PUBLIC_CHAT_TIMEOUT = 30
MARKETPLACE_TIMEOUT = 45
MAX_TOKENS = 1000
MARKETPLACE_MAX_TOKENS = 4000
#: The search-intent parser answered in <=150 tokens by design.
SEARCH_MAX_TOKENS = 150
#: Safety caps on the anonymous marketplace conversation history.
MAX_HISTORY_MESSAGES = 20
MAX_MESSAGE_CHARS = 2000

#: The legacy helper fell back to this when the model answered nothing.
FALLBACK_REPLY = 'Samahani, sijaelewa. Tafadhali rudia.'
#: Fields the camera scan keeps from the model answer.
EXTRACTED_FIELDS = ('name', 'brand', 'description', 'barcode', 'unit')
#: Product-form fields the scan should also try to read off the packaging.
EXTENDED_FIELDS = ('category', 'size', 'weight', 'color', 'expiryDate')
#: Product-form fields that arrive as numbers rather than strings.
NUMERIC_FIELDS = ('buyingPrice', 'sellingPrice', 'quantity')
#: Safety cap on distinct products pulled out of one photo.
MAX_PRODUCTS_PER_PHOTO = 20
#: Safety cap on leftover details kept per product.
MAX_EXTRA_FIELDS = 10


class AssistantError(RuntimeError):
    """The assistant could not answer; ``status_code`` is the HTTP answer.

    503 means the deployment has no OpenRouter key, 502 that the upstream call
    or its payload failed.
    """

    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


ASSISTANT_PROMPT = """You are Antigravity AI (functioning as Twende AI), a premium embedded AI assistant inside a multi-tenant SaaS business platform for POS, inventory, products, suppliers, customers, invoices, expenses, purchases, reports, users, branches, and business operations.

Your role is to act as a context-aware business copilot for a shop named "{shop_name}", not a generic chatbot. You speak fluent English and Swahili. Your purpose is to help the logged-in user understand their business data, navigate the system, generate safe outputs, and complete workflows faster while respecting permissions, business rules, privacy, and accuracy.

==================================================
CORE IDENTITY & PRIORITIES
==================================================
You are a specialized product assistant for SME business software. You must behave like a premium SaaS assistant.
Priorities: 1. Accuracy 2. Context-awareness 3. Action-oriented advice 4. Simple, business-focused language.

==================================================
STRICT OPERATING RULES
==================================================
1) RESPONSES MUST BE ACTION-ORIENTED & SIMPLE: Write for SME owners. Do not just state facts; provide an actionable recommendation based on the data. 
   - Example Bad: "Revenue increased 15%."
   - Example Good: "Your sales increased 15% compared with yesterday. Consider increasing stock for your fast-selling products."
2) MULTI-LANGUAGE READINESS: You must respond in the same language the user uses (English or Swahili). Ensure business terms are translated appropriately.
3) ALWAYS BE CONTEXT-AWARE: Use the loaded data below. Do not ask the user to repeat info you already have.
4) NEVER HALLUCINATE: Never guess or invent data. If data is unavailable, clearly state: "I don't currently have access to that data."
5) ENFORCE ROLE PERMISSIONS: The user's role is provided in context. Never expose owner/admin data (like net profit or overall debt) to a Cashier/Staff. 
6) BUSINESS COPILOT ONLY: If the user asks something unrelated to the business platform, politely redirect them.
7) EXPLAIN THE BASIS OF ANSWERS: Mention if data is based on the current context payload.
6) NO FINANCIAL/LEGAL GUARANTEES: Include disclaimers for tax or legal advice.
7) NEVER EXECUTE DESTRUCTIVE ACTIONS: You cannot delete, refund, or modify data directly yet, but always act as if safety is paramount.

==================================================
AUTO-NAVIGATION (CRITICAL RULES)
==================================================
ONLY trigger navigation if the user EXPLICITLY asks you to "take me to", "open", or "go to" a specific page.
DO NOT navigate if the user is just asking a question (e.g., "How are my sales?").
To trigger navigation, include this exact tag anywhere in your response: [NAVIGATE:/exact-path]

Valid routes MUST be strictly chosen from this list:
- /dashboard
- /dashboard/shops
- /dashboard/products
- /dashboard/sales
- /dashboard/orders
- /dashboard/expenses
- /dashboard/reports
- /dashboard/inventory
- /dashboard/suppliers
- /dashboard/users
- /dashboard/customers
- /dashboard/branches
- /dashboard/marketing
- /dashboard/social
- /dashboard/profile

Example:
User: "Nipeleke kwenye wateja wangu"
Twende AI: Sawa, ninakufungulia ukurasa wa wateja sasa hivi. [NAVIGATE:/dashboard/customers]

==================================================
CURRENT CONTEXT DATA
==================================================
{context_json}"""

EXTRACTION_PROMPT = """You are a product data extraction assistant. The user will provide an image of a product.
Your task is to extract the product details and return ONLY a valid JSON object. Do not include markdown code blocks, just raw JSON.
Extract these fields if visible:
- name: The full product name (string)
- brand: The brand name (string)
- description: A brief description of the product based on what you see (string)
- barcode: If a barcode number is visible, extract it (string)
- unit: The unit of measurement (e.g., 'kg', 'g', 'litre', 'ml', 'pcs', 'box', 'pack') if visible on the packaging (string)
- category: The product category or type (e.g., 'Beverages', 'Cooking oil', 'Toiletries') (string)
- buyingPrice: The wholesale/buying price if visible (number, no currency symbol)
- sellingPrice: The retail/selling price if visible (number, no currency symbol)
- quantity: The number of items in the pack or visible in the stack — an integer, digits only, no unit words (number)
- size: The pack or serving size written on the packaging (e.g., '500ml', '1kg') (string)
- weight: The net weight if printed (e.g., '250g') (string)
- color: The product colour when it matters for identification (string)
- expiryDate: The expiry or best-before date if printed (format YYYY-MM-DD) (string)

If a field is not determinable, omit it from the JSON.
Any other readable detail that does not fit those fields (e.g. flavour, material, storage instructions) should be added as an extra key with a string value."""

MULTI_EXTRACTION_PROMPT = """You are a product data extraction assistant. The user will provide an image that may show ONE product or MANY distinct products (a shelf, a crate, or a spread of items).
Your task is to identify EVERY distinct product and return ONLY a valid JSON object. Do not include markdown code blocks, just raw JSON.
Format: {"products": [{"name": "...", "brand": "...", "description": "...", "barcode": "...", "unit": "..."}]}
Extract these fields for each product if visible:
- name: The full product name (string)
- brand: The brand name (string)
- description: A brief description of the product based on what you see (string)
- barcode: If a barcode number is visible, extract it (string)
- unit: The unit of measurement (e.g., 'kg', 'g', 'litre', 'ml', 'pcs', 'box', 'pack') if visible on the packaging (string)
- category: The product category or type (e.g., 'Beverages', 'Cooking oil', 'Toiletries') (string)
- buyingPrice: The wholesale/buying price if visible (number, no currency symbol)
- sellingPrice: The retail/selling price if visible (number, no currency symbol)
- quantity: The number of items in the pack or visible in the stack — an integer, digits only, no unit words (number)
- size: The pack or serving size written on the packaging (e.g., '500ml', '1kg') (string)
- weight: The net weight if printed (e.g., '250g') (string)
- color: The product colour when it matters for identification (string)
- expiryDate: The expiry or best-before date if printed (format YYYY-MM-DD) (string)
Rules:
- One entry per DISTINCT product. If the same product appears several times (stacked or repeated boxes), list it once.
- Order the entries from the most prominent product to the least prominent.
- If a field is not determinable for a product, omit it from that product's JSON.
- Any other readable detail that does not fit those fields (e.g. flavour, material, storage instructions) should be added as an extra key with a string value.
- If the image shows no products at all, return {"products": []}."""

#: Category names the caller injects into the prompt. The product form only
#: accepts the merchant's own category list, so steering the model to reuse
#: those names verbatim keeps the apply-time match rate high.
MAX_CATEGORY_HINTS = 40


def _category_hint(category_hints):
    """Prompt sentence that offers the shop's own category names, or ''."""
    if not category_hints:
        return ''
    names = [str(name).strip() for name in category_hints if str(name).strip()]
    if not names:
        return ''
    return (
        ' Known categories for this shop: '
        + ', '.join(names[:MAX_CATEGORY_HINTS])
        + '. Reuse one of them verbatim when a product fits; otherwise invent a short category.'
    )


def _api_key():
    key = getattr(settings, 'OPENROUTER_API_KEY', '')
    if not key:
        raise AssistantError(
            'OPENROUTER_API_KEY is not configured on the server.', status_code=503,
        )
    return key


def _post(payload, timeout):
    """POST one payload to OpenRouter and return the parsed JSON object."""
    key = _api_key()
    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AssistantError(f'OpenRouter request failed: {exc}') from exc
    if not isinstance(data, dict):
        raise AssistantError('OpenRouter answered an unexpected payload.')
    return data


def _first_content(data, default=''):
    """``choices[0].message.content``, or ``default`` when it is missing."""
    choices = data.get('choices')
    if not choices or not isinstance(choices[0], dict):
        return default
    message = choices[0].get('message')
    content = message.get('content') if isinstance(message, dict) else None
    return content if content else default


def build_assistant_prompt(shop_name, context_data):
    """The legacy system prompt, with the shop name and context inlined."""
    return ASSISTANT_PROMPT.format(
        shop_name=shop_name,
        context_json=json.dumps(context_data, indent=2, ensure_ascii=False),
    )


def ask_assistant(shop_name, user_message, context_data):
    """Reply text for a dashboard question (legacy ``askBusinessAssistant``)."""
    data = _post({
        'model': CHAT_MODEL,
        'max_tokens': MAX_TOKENS,
        'messages': [
            {'role': 'system', 'content': build_assistant_prompt(shop_name, context_data)},
            {'role': 'user', 'content': user_message},
        ],
        'temperature': 0.7,
    }, timeout=CHAT_TIMEOUT)
    return _first_content(data, FALLBACK_REPLY)


def _strip_json_fences(content):
    """Drop ```` ```json ```` fences, the way the browser helper did."""
    if content.startswith('```json'):
        return content.replace('```json', '').replace('```', '').strip()
    if content.startswith('```'):
        return content.replace('```', '').strip()
    return content


def _clean_number(value):
    """Coerce a model answer into a non-negative float, else ``None``.

    Accepts real numbers, comma-formatted strings like ``"3,500"``, and
    strings that merely START with a number, like ``"6 pcs"`` or
    ``"TSh 3,500"`` — the first numeric token wins. Bools and negatives are
    rejected. Models routinely answer quantity with the unit attached; without
    this, the value would be dropped (numeric keys never fall through to
    ``extra``) and the field would silently stay empty in the review table.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r'\d[\d,]*(?:\.\d+)?', value)
        if not match:
            return None
        try:
            number = float(match.group(0).replace(',', ''))
        except ValueError:
            return None
    else:
        return None
    return number if number >= 0 else None


def _clean_product(entry):
    """Keep the known product fields from one model answer, numbers coerced.

    The string fields from :data:`EXTRACTED_FIELDS` and :data:`EXTENDED_FIELDS`
    are trimmed and kept when non-empty, the :data:`NUMERIC_FIELDS` are coerced
    via :func:`_clean_number`, and every other scalar key lands in the
    ``extra`` mapping so nothing the model read off the packaging is lost.
    """
    details = {}
    for field in EXTRACTED_FIELDS + EXTENDED_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            details[field] = value.strip()
    for field in NUMERIC_FIELDS:
        number = _clean_number(entry.get(field))
        if number is not None:
            details[field] = number
    extras = {}
    for key, value in entry.items():
        if key in EXTRACTED_FIELDS or key in EXTENDED_FIELDS or key in NUMERIC_FIELDS:
            continue
        if not isinstance(key, str):
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        if not text:
            continue
        extras[key.strip()[:40]] = text[:200]
        if len(extras) >= MAX_EXTRA_FIELDS:
            break
    if extras:
        details['extra'] = extras
    return details


def extract_product_details(image, category_hints=None):
    """Product fields read off a photo (legacy image extraction call).

    ``image`` is the data URL the camera capture produced. Known product fields
    survive as trimmed strings or coerced numbers and anything else readable
    lands in ``extra`` (see :func:`_clean_product`); an answer without usable
    JSON raises :class:`AssistantError`. ``category_hints`` (the shop's own
    category names) is appended to the prompt so the returned ``category``
    matches the form's dropdown.
    """
    data = _post({
        'model': VISION_MODEL,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': EXTRACTION_PROMPT + _category_hint(category_hints)},
                {'type': 'image_url', 'image_url': {'url': image}},
            ],
        }],
        'temperature': 0,
    }, timeout=VISION_TIMEOUT)
    content = _strip_json_fences(_first_content(data, '{}'))
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise AssistantError(f'Image extraction returned unparsable JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise AssistantError('Image extraction returned a non-object payload.')
    return _clean_product(parsed)


def extract_product_list(image, category_hints=None):
    """Distinct products read off one photo, most prominent first.

    ``image`` is the data URL the camera capture produced. Same field cleaning
    as :func:`extract_product_details`; entries without a usable name are
    dropped and the answer is capped at :data:`MAX_PRODUCTS_PER_PHOTO`. An
    answer without usable JSON raises :class:`AssistantError`; an image with
    no products yields an empty list. ``category_hints`` (the shop's own
    category names) is appended to the prompt so returned ``category`` values
    match the form's dropdown.
    """
    data = _post({
        'model': VISION_MODEL,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': MULTI_EXTRACTION_PROMPT + _category_hint(category_hints)},
                {'type': 'image_url', 'image_url': {'url': image}},
            ],
        }],
        'temperature': 0,
    }, timeout=VISION_TIMEOUT)
    content = _strip_json_fences(_first_content(data, '{}'))
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise AssistantError(f'Image extraction returned unparsable JSON: {exc}') from exc
    if isinstance(parsed, dict):
        parsed = parsed.get('products')
    if not isinstance(parsed, list):
        raise AssistantError('Image extraction returned a non-object payload.')
    products = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        details = _clean_product(entry)
        if details.get('name'):
            products.append(details)
        if len(products) >= MAX_PRODUCTS_PER_PHOTO:
            break
    return products


PUBLIC_ASSISTANT_PROMPT = """You are a friendly, helpful Virtual Shop Assistant for a store named "{shop_name}".
You speak fluent English and Swahili (respond in the language the user speaks).

==================================================
CORE IDENTITY & PRIORITIES
==================================================
You are a public-facing customer service and sales agent. You do not manage the business; you help shoppers buy things.
Your priorities: 1. Be polite and helpful 2. Help customers find products 3. Encourage them to add items to their cart or contact the shop via WhatsApp 4. Never reveal business secrets.

==================================================
STRICT OPERATING RULES
==================================================
1) ONLY PUBLIC DATA: Use the public context data provided below to answer questions about products, prices, and availability.
2) NEVER REVEAL SECRETS: Never talk about profit, wholesale costs, supplier names, or exact inventory numbers (just say "It is in stock").
3) NO HALLUCINATION: If the shop doesn't sell a product the user asks for, say: "Samahani, hatuna bidhaa hiyo kwa sasa" (Sorry, we don't have that currently). Do not invent products.
4) DRIVE SALES: When a user finds a product they like, encourage them to "Add to Cart" or click the WhatsApp button to finalize the order.
5) NO PAYMENT PROCESSING: Never ask the user for credit card numbers, passwords, or M-Pesa PINs in the chat.

==================================================
AUTO-NAVIGATION (PRODUCT DISCOVERY)
==================================================
If you recommend a specific product to the customer, you MUST provide a direct link to it so they can view it.
To navigate them to a product, include this exact tag anywhere in your response: [NAVIGATE:?productId=ID]
Replace ID with the actual product ID from the context data.

Example:
User: "I am looking for a cheap laptop"
AI: "We have the Lenovo Thinkpad for TZS 400,000! [NAVIGATE:?productId=123]"

==================================================
CURRENT PUBLIC CONTEXT DATA
==================================================
{context_json}"""

MARKETPLACE_PROMPT = """You are a helpful and persuasive Marketplace Concierge for a SaaS business platform called "{platform_name}".
You speak fluent English and Swahili (respond in the language the user speaks).

==================================================
CORE IDENTITY & PRIORITIES
==================================================
You are a global search assistant for the Twende Duka marketplace directory. Your goal is to help shoppers find the right store to buy from, based on location or categories.
Your priorities: 1. Be polite and helpful 2. Recommend relevant shops from the context data 3. Guide the user directly to those shops.

==================================================
STRICT OPERATING RULES
==================================================
1) ONLY CONTEXT DATA: You can only recommend shops that exist in the context data provided below. Do not invent shops.
2) NO SPECIFIC PRODUCTS: You only know what categories a shop sells (e.g. "Electronics", "Clothing"), you do not know their specific inventory items or prices. Tell the user to visit the shop to see specific products.
3) DRIVE TRAFFIC: Always encourage the user to visit the recommended shop's storefront.

==================================================
SHOP RECOMMENDATION & NAVIGATION
==================================================
When a user asks for a shop, you must first recommend it and ASK the user if they would like you to navigate them to the shop's page.
ONLY IF the user explicitly agrees or says "yes" to visiting the shop, you should then include this exact tag anywhere in your response: [NAVIGATE:/shop/SLUG]
Replace SLUG with the EXACT slug value from the shop's "slug" field in the context data below.
CRITICAL: NEVER use a placeholder like "SLUG" or "shop-name". NEVER output [NAVIGATE:/shop/] with an empty or invented slug. ONLY use real slugs from the context data.
DO NOT use the [NAVIGATE:/shop/SLUG] tag in your first recommendation. Wait for the user's confirmation.

Example (using real slug from context):
User: "Where can I find phones in Arusha?"
AI: "I recommend checking out Tech Store! They are located in Arusha and sell Electronics. Would you like me to take you to their storefront?"
User: "Yes please!"
AI: "Great! Navigating you to Tech Store now. [NAVIGATE:/shop/tech-store]"

==================================================
CURRENT PUBLIC MARKETPLACE DATA (TOP 50 SHOPS)
==================================================
{context_json}"""

SEARCH_PARSE_PROMPT = """You are a commerce search intent analyzer for a Tanzanian marketplace. You extract structured filter criteria from a user's natural language search query in either English or Swahili.
Respond ONLY with a JSON object with the following structure, with NO markdown formatting, NO backticks, and NO additional text:
{
  "cleanQuery": "the core search terms translated into BOTH English and Swahili, separated by a space (so it matches products named in either language)",
  "category": "product category if specified (e.g. phones, electronics, shoes)",
  "brand": "brand name if specified",
  "maxPrice": numeric maximum price in TZS if specified,
  "minPrice": numeric minimum price in TZS if specified
}

Example 1: "I want to buy a samsung phone under 500,000 tzs"
{"cleanQuery": "phone simu", "category": "phones", "brand": "samsung", "maxPrice": 500000}

Example 2: "natafuta viatu vya kukimbilia chini ya elfu 50"
{"cleanQuery": "viatu vya kukimbilia running shoes", "category": "shoes", "maxPrice": 50000}
"""


def ask_public_assistant(shop_name, user_message, context_data):
    """Reply text for the anonymous storefront widget (legacy ``askPublicAssistant``)."""
    data = _post({
        'model': PUBLIC_CHAT_MODEL,
        'max_tokens': MAX_TOKENS,
        'messages': [
            {'role': 'system', 'content': PUBLIC_ASSISTANT_PROMPT.format(
                shop_name=shop_name,
                context_json=json.dumps(context_data, indent=2, ensure_ascii=False),
            )},
            {'role': 'user', 'content': user_message},
        ],
    }, timeout=PUBLIC_CHAT_TIMEOUT)
    return _first_content(data, FALLBACK_REPLY)


def _normalize_history(messages):
    """Widget history as OpenRouter messages: ``ai`` -> ``assistant``, capped."""
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get('role')
        content = message.get('content')
        if role not in ('user', 'assistant', 'ai', 'system') or not isinstance(content, str):
            continue
        normalized.append({
            'role': 'assistant' if role == 'ai' else role,
            'content': content[:MAX_MESSAGE_CHARS],
        })
        if len(normalized) >= MAX_HISTORY_MESSAGES:
            break
    return normalized


def ask_marketplace_assistant(messages, context_data):
    """Reply text for the anonymous marketplace concierge (multi-turn, legacy
    ``askMarketplaceAssistant``). ``context_data['platformName']`` names the
    platform in the system prompt, the rest is inlined as the shop directory.
    """
    history = _normalize_history(messages)
    if not history:
        raise AssistantError('messages must contain at least one valid message.')
    platform_name = context_data.get('platformName') or 'Twende Duka'
    data = _post({
        'model': PUBLIC_CHAT_MODEL,
        'max_tokens': MARKETPLACE_MAX_TOKENS,
        'messages': [
            {'role': 'system', 'content': MARKETPLACE_PROMPT.format(
                platform_name=platform_name,
                context_json=json.dumps(context_data, indent=2, ensure_ascii=False),
            )},
            *history,
        ],
    }, timeout=MARKETPLACE_TIMEOUT)
    return _first_content(data, FALLBACK_REPLY)


def parse_search_query(query):
    """Structured filters parsed out of a natural-language directory search.

    Raises :class:`AssistantError` (also when the key is missing, 503) so the
    caller can fall back to its local basic parser exactly like the browser
    helper, which treated any failure the same way.
    """
    data = _post({
        'model': CHAT_MODEL,
        'max_tokens': SEARCH_MAX_TOKENS,
        'messages': [
            {'role': 'system', 'content': SEARCH_PARSE_PROMPT},
            {'role': 'user', 'content': query},
        ],
        'temperature': 0.1,
    }, timeout=CHAT_TIMEOUT)
    content = _strip_json_fences(_first_content(data, '')).strip()
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise AssistantError(f'Search parsing returned unparsable JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise AssistantError('Search parsing returned a non-object payload.')
    return {
        'cleanQuery': parsed.get('cleanQuery') if isinstance(parsed.get('cleanQuery'), str) else query,
        'category': parsed.get('category') if isinstance(parsed.get('category'), str) else None,
        'brand': parsed.get('brand') if isinstance(parsed.get('brand'), str) else None,
        'maxPrice': _clean_number(parsed.get('maxPrice')),
        'minPrice': _clean_number(parsed.get('minPrice')),
    }


def generate_copy(prompt):
    """Raw completion text for a single marketing-copy prompt.

    The three dialogs (ad generator, feed caption, sold-out caption) build
    their own prompts and strip/parse the answer locally, so this stays a
    plain single-message call without a system prompt.
    """
    data = _post({
        'model': CHAT_MODEL,
        'max_tokens': MAX_TOKENS,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.7,
    }, timeout=CHAT_TIMEOUT)
    return _first_content(data, '')
