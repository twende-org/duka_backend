"""Server-side OpenRouter calls for the two browser-side AI helpers.

``src/lib/ai.ts`` called OpenRouter straight from the bundle with
``VITE_OPENROUTER_API_KEY``: ``askBusinessAssistant`` backs the floating
dashboard copilot and ``extractProductDetailsFromImage`` backs the product
camera scan. Both prompts and request shapes are ported verbatim so answers
stay the same; only the transport moved server-side, which keeps the key out
of the frontend.
"""
import json
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
#: The models the legacy browser helpers requested.
CHAT_MODEL = 'google/gemini-2.5-flash-lite'
VISION_MODEL = 'google/gemini-1.5-flash'
CHAT_TIMEOUT = 20
VISION_TIMEOUT = 30
MAX_TOKENS = 1000

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
- quantity: The number of items in the pack or visible in the stack (number)
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
- quantity: The number of items in the pack or visible in the stack (number)
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

    Accepts real numbers and comma-formatted strings like ``"3,500"``; other
    strings, bools and negatives are rejected.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace(',', '')
        try:
            number = float(cleaned)
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


def extract_product_details(image):
    """Product fields read off a photo (legacy image extraction call).

    ``image`` is the data URL the camera capture produced. Known product fields
    survive as trimmed strings or coerced numbers and anything else readable
    lands in ``extra`` (see :func:`_clean_product`); an answer without usable
    JSON raises :class:`AssistantError`.
    """
    data = _post({
        'model': VISION_MODEL,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': EXTRACTION_PROMPT},
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


def extract_product_list(image):
    """Distinct products read off one photo, most prominent first.

    ``image`` is the data URL the camera capture produced. Same field cleaning
    as :func:`extract_product_details`; entries without a usable name are
    dropped and the answer is capped at :data:`MAX_PRODUCTS_PER_PHOTO`. An
    answer without usable JSON raises :class:`AssistantError`; an image with
    no products yields an empty list.
    """
    data = _post({
        'model': VISION_MODEL,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': MULTI_EXTRACTION_PROMPT},
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
