"""Deterministic marketplace search ranking shared by storefront and portal.

This is the API-side twin of ``src/lib/services/discoveryService.ts``: the same
noise-word stripping, EN<->SW synonym expansion and exact/prefix/word/substring
score ladder, so a query answered from the browser's instant local layer and the
same query served by the API return the same order.

Candidate generation is database-portable. Substring candidates (``icontains``)
run on every backend; on PostgreSQL a ``trigram_word_similar`` leg adds typo
tolerance, and the pg_trgm GIN indexes created by the search migrations make
both legs index-backed. Scoring itself is pure Python on both backends, which
keeps tests deterministic on SQLite.

Deliberately no ``SearchVector``/``ts_rank``: the ``simple`` FTS config would
only re-implement tokenization the scorer already does (and would fight the
ported ladder with a second, differently-weighted ranking).
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Callable, Iterable, Sequence

from django.db import connections
from django.db.models import Q, QuerySet

# Words that carry no search signal (conversational filler in EN + SW).
NOISE_WORDS = frozenset((
    "the", "a", "an", "of", "for", "and", "with", "in", "on", "to", "me", "i",
    "want", "need", "show", "give", "find", "buy", "please", "good", "best",
    "cheap", "some", "any",
    "na", "ya", "wa", "za", "la", "kwa", "nataka", "natafuta", "nipe", "naomba",
    "tafadhali",
))

# Bidirectional Swahili <-> English synonym dictionary, ported verbatim from
# discoveryService.ts so client and server expand queries identically.
SYNONYMS: dict[str, tuple[str, ...]] = {
    # Electronics / Simu & Teknolojia
    "simu": ("phone", "smartphone", "mobile", "cellphone", "handset", "rununu"),
    "rununu": ("simu", "phone", "smartphone", "mobile"),
    "phone": ("simu", "rununu", "smartphone", "mobile", "cellphone", "handset"),
    "smartphone": ("simu", "rununu", "phone", "mobile"),
    "mobile": ("simu", "rununu", "phone", "smartphone"),
    "kompyuta": ("computer", "pc", "laptop", "desktop", "notebook"),
    "laptop": ("kompyuta", "computer", "notebook", "pc"),
    "computer": ("kompyuta", "laptop", "pc", "desktop", "notebook"),
    "pc": ("kompyuta", "computer", "laptop", "desktop"),
    "notebook": ("laptop", "kompyuta", "computer"),
    "tv": ("televisheni", "television", "screen", "oled", "smart tv", "runinga"),
    "televisheni": ("tv", "television", "runinga", "screen", "smart tv"),
    "runinga": ("tv", "televisheni", "television"),
    "television": ("tv", "televisheni", "runinga", "screen"),
    "charger": ("chaja", "adapter", "cable", "kebo"),
    "chaja": ("charger", "adapter", "cable"),
    "earphone": ("headphone", "earbuds", "headset", "vipokea sauti"),
    "headphone": ("earphone", "earbuds", "headset", "vipokea sauti"),
    "vipokea sauti": ("headphone", "earphone", "earbuds"),
    "betri": ("battery", "power bank"),
    "battery": ("betri", "power bank"),
    "power bank": ("betri", "battery", "chaja ya akiba"),
    "kamera": ("camera", "picha"),
    "camera": ("kamera", "picha"),
    "printer": ("printa", "chapisha"),
    "printa": ("printer",),
    # Clothing / Mavazi & Nguo
    "nguo": ("clothes", "clothing", "apparel", "garments", "mavazi", "outfit", "dress"),
    "mavazi": ("nguo", "clothes", "clothing", "apparel", "garments"),
    "clothes": ("nguo", "mavazi", "clothing", "apparel", "garments"),
    "clothing": ("nguo", "mavazi", "clothes", "apparel"),
    "dress": ("nguo ya msichana", "gauni", "skirt"),
    "gauni": ("dress", "gown", "skirt"),
    "shati": ("shirt", "blouse", "top"),
    "shirt": ("shati", "blouse", "top", "t-shirt"),
    "suruali": ("trousers", "pants", "jeans", "shorts"),
    "trousers": ("suruali", "pants", "jeans"),
    "pants": ("suruali", "trousers", "jeans"),
    "jeans": ("suruali", "denim", "pants"),
    "kanga": ("leso", "wrap", "fabric", "kitenge"),
    "kitenge": ("kanga", "leso", "fabric", "ankara"),
    "leso": ("kanga", "kitenge", "scarf", "fabric"),
    "kofia": ("hat", "cap", "headwear"),
    "hat": ("kofia", "cap"),
    "cap": ("kofia", "hat"),
    "mfuko": ("bag", "handbag", "purse", "backpack"),
    "bag": ("mfuko", "handbag", "purse", "backpack", "mkoba"),
    "mkoba": ("bag", "mfuko", "handbag", "wallet", "purse"),
    "soksi": ("socks", "stockings"),
    "socks": ("soksi", "stockings"),
    "chupi": ("underwear", "inner wear", "boxers"),
    "underwear": ("chupi", "inner wear"),
    # Footwear / Viatu
    "viatu": ("shoes", "footwear", "sneakers", "sandals", "boots", "slippers"),
    "shoes": ("viatu", "footwear", "sneakers", "boots"),
    "sneakers": ("viatu", "shoes", "sports shoes", "trainers"),
    "sandals": ("viatu", "slippers", "ndala"),
    "ndala": ("sandals", "slippers", "viatu"),
    "boots": ("viatu", "shoes", "buti"),
    "buti": ("boots", "shoes", "viatu"),
    # Food & Groceries / Chakula & Vitu vya Nyumba
    "chakula": ("food", "groceries", "vyakula", "kula", "meals"),
    "food": ("chakula", "vyakula", "groceries", "meals"),
    "groceries": ("chakula", "vyakula", "food", "provisions", "mahitaji"),
    "vyakula": ("chakula", "food", "groceries"),
    "unga": ("flour", "maize flour", "wheat flour"),
    "flour": ("unga",),
    "mchele": ("rice",),
    "rice": ("mchele",),
    "sukari": ("sugar",),
    "sugar": ("sukari",),
    "mafuta": ("oil", "cooking oil", "fat"),
    "oil": ("mafuta", "cooking oil"),
    "chumvi": ("salt",),
    "salt": ("chumvi",),
    "maziwa": ("milk", "dairy"),
    "milk": ("maziwa", "dairy"),
    "nyama": ("meat", "beef", "chicken", "pork"),
    "meat": ("nyama", "beef", "chicken"),
    "samaki": ("fish", "seafood"),
    "fish": ("samaki", "seafood"),
    "mboga": ("vegetables", "greens", "veggies"),
    "vegetables": ("mboga", "greens", "veggies"),
    "matunda": ("fruits", "fruit"),
    "fruits": ("matunda", "fruit"),
    # Beauty & Personal Care / Urembo & Usafi
    "sabuni": ("soap", "detergent", "cleanser"),
    "soap": ("sabuni", "cleanser"),
    "urembo": ("beauty", "cosmetics", "makeup", "skincare"),
    "beauty": ("urembo", "cosmetics", "makeup", "skincare"),
    "cosmetics": ("urembo", "beauty", "makeup"),
    "makeup": ("urembo", "beauty", "cosmetics"),
    "mzigo wa nywele": ("hair products", "shampoo", "conditioner"),
    "nywele": ("hair", "weave", "braids", "wigs"),
    "hair": ("nywele", "weave", "braids", "wigs", "hair products"),
    "weave": ("nywele", "hair", "wigs", "extensions"),
    "manukato": ("perfume", "cologne", "fragrance", "deodorant"),
    "perfume": ("manukato", "cologne", "fragrance", "deodorant"),
    "cologne": ("manukato", "perfume", "fragrance"),
    "cream": ("krimu", "lotion", "moisturizer"),
    "krimu": ("cream", "lotion", "moisturizer"),
    "lotion": ("krimu", "cream", "moisturizer", "body lotion"),
    # Home & Living / Nyumbani
    "samani": ("furniture", "sofa", "kitanda", "meza", "kiti"),
    "furniture": ("samani", "sofa", "kitanda", "meza", "kiti"),
    "kitanda": ("bed", "furniture", "samani"),
    "bed": ("kitanda", "furniture", "samani"),
    "shuka": ("bedsheet", "bedsheets", "linen", "blanket"),
    "bedsheet": ("shuka", "linen"),
    "godoro": ("mattress", "matress", "bed"),
    "mattress": ("godoro", "bed"),
    "matress": ("godoro", "bed"),
    "table": ("meza", "desk"),
    "meza": ("table", "desk"),
    "kiti": ("chair", "stool", "seat"),
    "chair": ("kiti", "stool", "seat"),
    "jiko": ("cooker", "stove", "oven", "grill"),
    "cooker": ("jiko", "stove", "oven", "grill"),
    "stove": ("jiko", "cooker", "grill"),
    "sufuria": ("pot", "pan", "cookware", "saucepan"),
    "pot": ("sufuria", "pan", "cookware"),
    "pan": ("sufuria", "pot", "frying pan"),
    "vyombo": ("utensils", "kitchenware", "dishes", "plates"),
    "utensils": ("vyombo", "kitchenware"),
    "friji": ("fridge", "refrigerator", "freezer"),
    "fridge": ("friji", "refrigerator", "freezer"),
    "refrigerator": ("friji", "fridge", "freezer"),
    "mashine ya kuosha": ("washing machine", "washer", "laundry machine"),
    "washing machine": ("mashine ya kuosha", "washer"),
    "blanket": ("blanketi", "comforter", "duvet", "shuka"),
    "blanketi": ("blanket", "comforter", "duvet"),
    # Health & Medicine / Afya & Dawa
    "dawa": ("medicine", "drugs", "medication", "pharmacy"),
    "medicine": ("dawa", "medication", "drugs"),
    "afya": ("health", "wellness", "medical"),
    "health": ("afya", "wellness", "medical"),
    # Vehicles & Transport / Magari & Usafiri
    "gari": ("car", "vehicle", "auto", "automobile"),
    "car": ("gari", "vehicle", "auto"),
    "vehicle": ("gari", "car", "auto"),
    "baiskeli": ("bicycle", "bike", "cycle"),
    "bicycle": ("baiskeli", "bike", "cycle"),
    "pikipiki": ("motorcycle", "motorbike", "boda boda"),
    "motorcycle": ("pikipiki", "motorbike", "boda boda"),
    "boda boda": ("pikipiki", "motorcycle", "motorbike"),
    "spare parts": ("vipande", "spea", "auto parts"),
    "vipande": ("spare parts", "spea", "parts"),
    "spea": ("spare parts", "vipande"),
    # Sports & Fitness / Michezo
    "michezo": ("sports", "fitness", "gym", "exercise"),
    "sports": ("michezo", "fitness", "gym"),
    "fitness": ("michezo", "sports", "gym", "exercise"),
    "gym": ("fitness", "michezo", "exercise"),
    # Agriculture / Kilimo
    "kilimo": ("agriculture", "farming", "farm"),
    "agriculture": ("kilimo", "farming", "farm"),
    "mbegu": ("seeds", "seedlings"),
    "seeds": ("mbegu",),
    "mbolea": ("fertilizer", "compost"),
    "fertilizer": ("mbolea", "compost"),
    # General / Jumla
    "duka": ("shop", "store", "vendor"),
    "shop": ("duka", "store", "vendor"),
    "store": ("duka", "shop", "vendor"),
    "bei": ("price", "cost", "value"),
    "price": ("bei", "cost"),
    "mpya": ("new", "fresh", "latest"),
    "new": ("mpya", "fresh", "latest", "brand new"),
    "ya watoto": ("kids", "children", "baby", "infants"),
    "kids": ("ya watoto", "children", "baby"),
    "children": ("ya watoto", "kids", "baby"),
    "baby": ("ya watoto", "kids", "infant", "mtoto"),
    "mtoto": ("baby", "child", "infant", "kids"),
}

# Score multipliers, mirroring the frontend ladder exactly.
_EXACT = 12.0
_PREFIX = 9.0
_WORD = 7.0
_SUBSTRING = 4.0
_FUZZY = 1.5
_FUZZY_MIN_LENGTH = 4
# Fuse.js scores by distance with ``threshold: 0.26``; the Python twin uses
# difflib similarity, where the equivalent gate is >= 0.74.
_FUZZY_MIN_RATIO = 0.74
_ALL_TERMS_BONUS = 20.0

_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize_search_text(value: Any) -> str:
    """Fold a value the way the frontend does: strip accents, lowercase,
    non-alphanumerics to spaces, collapse whitespace."""
    text = unicodedata.normalize("NFD", str(value if value is not None else ""))
    text = "".join(ch for ch in text if not "\u0300" <= ch <= "\u036f")
    text = _NON_ALNUM.sub(" ", text.lower())
    return _WHITESPACE.sub(" ", text).strip()


def expand_term(term: str) -> list[str]:
    """A single term plus its EN/SW synonyms."""
    return [term, *SYNONYMS.get(term, ())]


def search_terms(query: str) -> list[str]:
    """Normalized, noise-stripped terms; empty when the query has no signal."""
    normalized = normalize_search_text(query)
    if not normalized:
        return []
    return [term for term in normalized.split(" ") if term and term not in NOISE_WORDS]


def _normalized_variants(term: str) -> list[str]:
    seen: dict[str, None] = {}
    for variant in expand_term(term):
        normalized = normalize_search_text(variant)
        if normalized:
            seen[normalized] = None
    return list(seen)


@dataclass(frozen=True)
class SearchField:
    """One searchable column: its ORM path (for SQL candidates), its ladder
    weight, and an optional extractor for values Django cannot read straight
    off the path (e.g. JSON arrays)."""

    field: str
    weight: float
    extract: Callable[[Any], Any] | None = None

    def value(self, obj: Any) -> Any:
        if self.extract is not None:
            return self.extract(obj)
        current = obj
        for part in self.field.split("__"):
            if current is None:
                return ""
            current = getattr(current, part, "")
        return current


@lru_cache(maxsize=512)
def _word_boundary_pattern(variant: str) -> re.Pattern[str]:
    return re.compile(r"(^|\s)" + re.escape(variant) + r"(\s|$)")


def _fuzzy_ratio(text: str, variant: str) -> float:
    """Approximate Fuse.js ``ignoreLocation``: best alignment of the variant
    against the whole text or against any single word of it."""
    best = SequenceMatcher(None, text, variant).ratio()
    if best < _FUZZY_MIN_RATIO and " " in text:
        for token in text.split(" "):
            ratio = SequenceMatcher(None, token, variant).ratio()
            if ratio > best:
                best = ratio
                if best >= _FUZZY_MIN_RATIO:
                    break
    return best


def score_object(obj: Any, terms: Sequence[str], fields: Sequence[SearchField]) -> float:
    """Weighted ladder score; zero unless *every* term matched somewhere."""
    field_values = [(normalize_search_text(spec.value(obj)), spec.weight) for spec in fields]
    score = 0.0
    matched = 0

    for term in terms:
        variants = _normalized_variants(term)
        best = 0.0
        for text, weight in field_values:
            if not text or not weight:
                continue
            for variant in variants:
                if text == variant:
                    candidate = weight * _EXACT
                elif text.startswith(variant):
                    candidate = weight * _PREFIX
                elif _word_boundary_pattern(variant).search(text):
                    candidate = weight * _WORD
                elif variant in text:
                    candidate = weight * _SUBSTRING
                elif len(variant) >= _FUZZY_MIN_LENGTH and _fuzzy_ratio(text, variant) >= _FUZZY_MIN_RATIO:
                    candidate = weight * _FUZZY
                else:
                    continue
                if candidate > best:
                    best = candidate
        if best > 0:
            matched += 1
            score += best

    if matched != len(terms):
        return 0.0
    return score + matched * _ALL_TERMS_BONUS


def rank_objects(objects: Iterable[Any], query: str, fields: Sequence[SearchField]) -> list[Any]:
    """Rank objects against the query; ties keep the incoming order.

    A query with no meaningful terms passes the rows through unchanged, which
    matches ``rankBySearch`` on the frontend (an empty search is not a filter).
    """
    terms = search_terms(query)
    rows = list(objects)
    if not terms:
        return rows

    scored: list[tuple[float, int, Any]] = []
    for index, obj in enumerate(rows):
        value = score_object(obj, terms, fields)
        if value > 0:
            scored.append((-value, index, obj))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [obj for _, _, obj in scored]


def candidates_q(terms: Sequence[str], fields: Sequence[SearchField], *, trigram: bool) -> Q:
    """Broad Q that admits any row that could score > 0.

    Every term (via itself and its synonyms) must appear as a substring in some
    field; on PostgreSQL a word-similarity leg adds typo tolerance on top. The
    final ordering still comes from :func:`rank_objects`.
    """
    combined = Q()
    for term in terms:
        variants = _normalized_variants(term)
        per_term = Q()
        for variant in variants:
            for spec in fields:
                per_term |= Q(**{f"{spec.field}__icontains": variant})
        if trigram:
            for variant in variants:
                for spec in fields:
                    per_term |= Q(**{f"{spec.field}__trigram_word_similar": variant})
        combined &= per_term
    return combined


def _uses_trigram(queryset: QuerySet) -> bool:
    return connections[queryset.db].vendor == "postgresql"


def search_queryset(
    queryset: QuerySet, query: str, fields: Sequence[SearchField]
) -> QuerySet | list[Any]:
    """Rank a queryset against a query.

    Returns the queryset untouched when the query carries no signal; otherwise
    a ranked list of the matching rows (callers may paginate the list directly —
    DRF's paginator accepts lists). Callers should ``select_related`` any
    relation used in a field path so scoring does not lazy-load per row.
    """
    terms = search_terms(query)
    if not terms:
        return queryset
    candidates = queryset.filter(candidates_q(terms, fields, trigram=_uses_trigram(queryset)))
    return rank_objects(candidates, query, fields)
