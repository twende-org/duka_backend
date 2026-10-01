"""Heuristic TRA (Tanzania Revenue Authority) VFD staging classifier.

Incoming invoice items get a provisional category code + VAT rate so the data
can flow into the multi-vendor VFD setup when a final retail sale occurs. The
keyword map is deliberately conservative — medicines, medical supplies and
printed/educational materials are long-standing exemptions; everything else
defaults to the standard 18% rate. Merchants see (and can override) both values
in the review grid before a draft is applied to production.
"""
from decimal import Decimal

TRA_STANDARD = ('VAT_STD', Decimal('18.00'))
TRA_EXEMPT = ('VAT_EXEMPT', Decimal('0.00'))
TRA_ZERO = ('VAT_ZERO', Decimal('0.00'))

_EXEMPT_KEYWORDS = (
    'dawa', 'madawa', 'medicine', 'medical', 'paracetamol', 'panadol', 'aspirin',
    'antibiotic', 'kitabu', 'vitabu', 'book', 'textbook', 'exercise book',
    'newspaper', 'gazeti', 'magazine',
)

_ZERO_KEYWORDS = (
    'export', 'agizo la nje',
)


def classify_tra(item_name):
    """Return ``(tra_item_code, tax_rate_percent)`` for one item name."""
    lowered = (item_name or '').lower()
    if any(keyword in lowered for keyword in _ZERO_KEYWORDS):
        return TRA_ZERO
    if any(keyword in lowered for keyword in _EXEMPT_KEYWORDS):
        return TRA_EXEMPT
    return TRA_STANDARD
