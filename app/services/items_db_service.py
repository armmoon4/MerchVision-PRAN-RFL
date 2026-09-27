"""
app/services/items_db_service.py — PRAN-RFL Items Catalogue lookup service.

Loads itemsdb.csv once at startup (singleton) and provides fast taxonomy-aware
fuzzy/keyword search against Item Names and Categories.

CSV columns expected:
    Sub Category Name, Sub Category Code, Category Name, Category Code,
    Item Name, Item Code
"""
from __future__ import annotations

import csv
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── Types ─────────────────────────────────────────────────────────────────────

CatalogueRow = Dict[str, str]   # one CSV row, keys = column headers

# ── Singleton state ───────────────────────────────────────────────────────────

_catalogue: List[CatalogueRow] = []
_loaded: bool = False

# ── Path resolution ───────────────────────────────────────────────────────────

_DEFAULT_CSV_PATHS = [
    Path("itemsdb.csv"),                          # CWD (uvicorn launched from project root)
    Path(__file__).parent.parent.parent / "itemsdb.csv",  # repo root relative to this file
]


def _find_csv() -> Optional[Path]:
    env_path = os.getenv("ITEMS_DB_CSV")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        logger.warning("ITEMS_DB_CSV env var points to non-existent file: %s", env_path)

    for candidate in _DEFAULT_CSV_PATHS:
        if candidate.is_file():
            return candidate

    return None


# ── Canonical Vocabularies & Normalization ─────────────────────────────────────

ALIASES: Dict[str, str] = {
    "p.apple": "pineapple",
    "papple": "pineapple",
    "s.berry": "strawberry",
    "sberry": "strawberry",
    "p.granate": "pomegranate",
    "pgranate": "pomegranate",
    "donat": "donut",
    "choco": "chocolate",
    "choc": "chocolate",
    "vanila": "vanilla",
    "falvoured": "flavoured",
    "falvor": "flavor",
    "flavour": "flavored",
    "flavoured": "flavored",
    "flavor": "flavored",
    "bisc": "biscuit",
    "bis": "biscuit",
}

FLAVORS: Set[str] = {
    "strawberry", "pineapple", "orange", "chocolate", "vanilla",
    "banana", "coconut", "milk", "mango", "custard", "lemon",
    "butter", "peanut", "elachi", "cardamom", "jeera", "cumin",
    "spicy", "masala", "cheese", "onion", "garlic", "ginger",
    "lychee", "pomegranate", "apple", "badam", "almond", "pista",
    "cashew", "salt", "salted", "tomato", "chilli",
}

NOISE_WORDS: Set[str] = {
    "cream", "crunchy", "special", "regular", "original",
    "double", "filled", "mini", "plus", "club", "bisk",
}

# Third-party imported or distributed brands present in ERP itemsdb.csv
# These must NEVER be suggested unless explicitly searched by the user.
THIRD_PARTY_BRANDS: Set[str] = {
    # Competitor / Distributed Beverage & Food Brands
    "barbican", "red bull", "maduria", "sipco", "shams", "kuku bima", "kuku",
    "milo", "nescafe", "carabao", "m150", "shark", "hemavition", "fizze",
    "big bee", "kinza", "bauli", "canton", "tango", "spoti", "idopa",
    # Distributed Personal Care / Cosmetics / Toiletries
    "head & shoulders", "head and shoulders", "clear", "dove", "pantene",
    "sunsilk", "lux", "lifebuoy", "camay", "fair & lovely", "pepsodent",
    "colgate", "close up", "close-up", "fogg", "axe", "brylcreem",
    "vaseline", "vicks", "bajaj", "parachute", "dettol", "savlon",
    "harpic", "lizol", "wheel", "rin", "surf excel", "tide", "ariel",
}

TP_PATTERN: re.Pattern = re.compile(
    r"\b(" + "|".join(re.escape(b) for b in sorted(THIRD_PARTY_BRANDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Known PRAN in-house product lines and sub-brands
PRAN_BRANDS: Set[str] = {
    "pran", "drinko", "frooto", "bisk club", "mr. noodles", "all time",
    "latina", "lavila", "cheer up", "potata", "bravo", "shero", "wonder",
    "chashee", "mithai", "tasty treat", "winner", "active", "power",
}

IGNORE_TOKENS: Set[str] = {
    "pcs", "pc", "pack", "pck", "combo", "jar", "box", "ctn", "pac",
    "ltd", "atc", "with", "and", "the", "for",
}


def _standardize_row(row: CatalogueRow) -> None:
    """Defensive runtime cleanup for regional metadata or legacy misclassifications."""
    cat = row.get("Category Name", "")
    name = row.get("Item Name", "")
    name_upper = name.upper()

    # 1. Clean Singapore regional dumping to proper taxonomy
    if cat == "Singapore":
        if "CREAM BISCUIT" in name_upper or "BISCUIT" in name_upper:
            row["Category Name"] = "145-Biscuit"
            row["Category Code"] = "145"
            row["Sub Category Name"] = "272-Bisc-Cream"
            row["Sub Category Code"] = "272"
        elif "MASALA" in name_upper:
            row["Category Name"] = "150-Spc"
            row["Category Code"] = "150"
            row["Sub Category Name"] = "360-Spc-Parer Pack"
            row["Sub Category Code"] = "360"
        elif "GINGER PASTE" in name_upper:
            row["Category Name"] = "150-Spc"
            row["Category Code"] = "150"
            row["Sub Category Name"] = "Paste"
            row["Sub Category Code"] = "Paste"
        elif "KOREAN" in name_upper or "NOODLE" in name_upper:
            row["Category Name"] = "135-SN-GLB"
            row["Category Code"] = "135"
            row["Sub Category Name"] = "230-Noodles"
            row["Sub Category Code"] = "230"
        elif "DAL VAJA" in name_upper:
            row["Category Name"] = "130-SN-BD"
            row["Category Code"] = "130"
            row["Sub Category Name"] = "210-Fried Snacks"
            row["Sub Category Code"] = "210"

    # 2. Standardize all Cream Biscuits under 145-Biscuit to 272-Bisc-Cream
    if "cream biscuit" in name.lower() and "bisc" in row.get("Category Name", "").lower():
        row["Sub Category Name"] = "272-Bisc-Cream"
        row["Sub Category Code"] = "272"


# ── Load ──────────────────────────────────────────────────────────────────────


def load_items_db(force: bool = False) -> int:
    """
    Load (or re-load) itemsdb.csv into the in-memory catalogue.
    Returns number of rows loaded.
    """
    global _catalogue, _loaded

    if _loaded and not force:
        return len(_catalogue)

    csv_path = _find_csv()
    if csv_path is None:
        logger.warning(
            "itemsdb.csv not found. Product catalogue enrichment will be skipped. "
            "Set ITEMS_DB_CSV env var or place itemsdb.csv in the project root."
        )
        _catalogue = []
        _loaded = True
        return 0

    rows: List[CatalogueRow] = []
    try:
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                cleaned = {k.strip().strip('"'): v.strip().strip('"') for k, v in row.items() if k}
                if cleaned.get("Item Name"):
                    _standardize_row(cleaned)
                    rows.append(cleaned)
        _catalogue = rows
        _loaded = True
        logger.info("ItemsDB loaded: %d products from %s", len(rows), csv_path)
    except Exception as exc:
        logger.error("Failed to load itemsdb.csv from %s: %s", csv_path, exc)
        _catalogue = []
        _loaded = True

    return len(_catalogue)


# ── Tokenization & Taxonomy Matching ──────────────────────────────────────────


def _normalize_text(text: str) -> str:
    """Lowercase, expand abbreviations, standardize flavors and unit formats."""
    t = text.lower()
    # Normalize "pine apple" to "pineapple" to prevent false apple drink matches
    t = re.sub(r"\bpine\s+apple\b", "pineapple", t)
    # Standardize numeric units so "250 ml" and "250ml" match identically
    t = re.sub(r"(\d+)\s*(?:ml|milliliter)\b", r"\1ml", t)
    t = re.sub(r"(\d+)\s*(?:l|ltr|liter|litre)\b", r"\1ltr", t)
    t = re.sub(r"(\d+)\s*(?:g|gm|gram)\b", r"\1gm", t)
    t = re.sub(r"(\d+)\s*(?:kg|kilo)\b", r"\1kg", t)
    for k, v in ALIASES.items():
        t = re.sub(rf"\b{re.escape(k)}\b", v, t)
    return t


def _tokenise(text: str) -> List[str]:
    """Normalize, split on alphanumeric boundaries, apply aliases, and singularize."""
    norm = _normalize_text(text)
    raw = [t for t in re.split(r"[^a-z0-9]+", norm) if len(t) >= 2]
    res: List[str] = []
    for t in raw:
        if t in ALIASES:
            t = ALIASES[t]
        # Singularize English plurals (biscuits -> biscuit, wafers -> wafer)
        if t.endswith("s") and len(t) > 3 and not t.endswith("ss"):
            t = t[:-1]
        res.append(t)
    return res


def infer_category(query_tokens: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Infer the target PRAN category and subcategory from the product name tokens.
    Enables Category Hard-Locking to eliminate hallucinations (e.g. Rice for Biscuits).
    """
    q_set = set(query_tokens)

    # 1. Wafer -> 146-Bakery / 300-Wafer
    if "wafer" in q_set:
        return "146-Bakery", "300-Wafer"

    # 2. Cake / Donut -> 146-Bakery (Cakes only, exclude bread & wafer)
    if any(c in q_set for c in ["cake", "cupcake", "donut", "muffin"]):
        return "146-Bakery", "CAKE"

    # 3. Biscuit / Cookie / Cracker -> 145-Biscuit
    if any(b in q_set for b in ["biscuit", "cookie", "cracker"]):
        if "cream" in q_set:
            return "145-Biscuit", "272-Bisc-Cream"
        return "145-Biscuit", None

    # 4. Toast / Rusk / Bela -> 147-Toast
    if any(t in q_set for t in ["toast", "rusk", "bela"]):
        return "147-Toast", None

    # 5. Bread / Roll -> 146-Bakery / Bread
    if any(br in q_set for br in ["bread", "bun", "roti"]):
        return "146-Bakery", "Bread"

    # 6. Rice -> 170-Rice
    if any(r in q_set for r in ["rice", "basmati", "basmathi", "sella", "chinigura", "kalijeera"]):
        return "170-Rice", None

    # 7. Noodles / Snacks
    if "noodle" in q_set:
        return "SNACKS_NOODLES", None
    if any(s in q_set for s in ["chanachur", "dal", "jhalmuri", "chip"]):
        return "SNACKS_NOODLES", None

    # 8. Beverages (Unified: Juices, Liquid Drinks, Tetra Pak drinks, CSD, Vitality)
    if any(d in q_set for d in ["juice", "frooto", "drink", "beverage", "basil", "csd", "soda", "float"]):
        return "BEVERAGE", None

    # 9. Dairy
    if any(m in q_set for m in ["milk", "dairy", "lassi", "ghee", "butter", "cheese", "curd", "yogurt", "yoghourt"]):
        return "120-Dairy", None

    # 10. Spices
    if any(s in q_set for s in ["masala", "spice", "turmeric", "chilli", "coriander", "cumin", "jeera"]):
        return "150-Spc", None

    # 11. Pickles & Household foods
    if any(p in q_set for p in ["pickle", "achar", "sauce", "ketchup", "jam", "jelly"]):
        return "180-HH-Food", None

    # 12. Confectionery
    if any(c in q_set for c in ["candy", "lollipop", "chocolate", "chew", "gum"]):
        return "160-Confec", None

    return None, None


def matches_target_category(
    target_cat: Optional[str],
    target_sub: Optional[str],
    row_cat: str,
    row_sub: str,
) -> bool:
    """Check if a catalogue row strictly matches the inferred category constraints."""
    if not target_cat:
        return True

    if target_cat == "145-Biscuit":
        return row_cat == "145-Biscuit"

    if target_cat == "147-Toast":
        return row_cat == "147-Toast"

    if target_cat == "170-Rice":
        return row_cat == "170-Rice"

    if target_cat == "146-Bakery":
        if row_cat != "146-Bakery":
            return False
        if target_sub == "300-Wafer":
            return row_sub == "300-Wafer"
        if target_sub == "CAKE":
            return row_sub not in ["300-Wafer", "Bread"]
        if target_sub == "Bread":
            return row_sub == "Bread"
        return True

    if target_cat == "SNACKS_NOODLES":
        return row_cat in ["130-SN-BD", "135-SN-GLB"]

    if target_cat == "BEVERAGE":
        return row_cat in [
            "105-DR-LD", "106-DR-Basil", "107-DR-Oth",
            "110-JU-PET", "111-JU-Can", "112-JU-Pak",
            "115-VitaPower", "116-CSD",
        ]

    if target_cat == "120-Dairy":
        return row_cat == "120-Dairy"

    if target_cat == "150-Spc":
        return row_cat == "150-Spc"

    if target_cat == "180-HH-Food":
        return row_cat == "180-HH-Food"

    if target_cat == "160-Confec":
        return row_cat == "160-Confec"

    return True


def _score_candidate(
    q_tokens: List[str],
    target_cat: Optional[str],
    target_sub: Optional[str],
    row: CatalogueRow,
    hard_lock: bool = True,
    allow_third_party: bool = False,
) -> int:
    """
    Score a catalogue row against query tokens with category hard-locking,
    flavor-priority matching, brand boosting, size alignment, and third-party filtering.
    """
    row_cat = row.get("Category Name", "")
    row_sub = row.get("Sub Category Name", "")
    row_name = row.get("Item Name", "")

    # Exclude non-PRAN third-party brands unless explicitly queried
    if not allow_third_party:
        if TP_PATTERN.search(row_name) or TP_PATTERN.search(row_sub):
            return 0

    # Hard-Locking check
    if hard_lock and not matches_target_category(target_cat, target_sub, row_cat, row_sub):
        return 0

    item_tokens = _tokenise(row_name)
    item_tokens_set = set(item_tokens)
    name_lower = row_name.lower()

    score = 0

    # Subcategory bonus
    if target_sub and target_sub != "CAKE" and row_sub == target_sub:
        score += 50

    # PRAN Brand boost: Prioritize PRAN family products over generic entries
    q_has_pran = any(b in q_tokens for b in ["pran", "rfl"])
    row_has_pran = "pran" in item_tokens_set or any(pb in name_lower for pb in PRAN_BRANDS)
    if q_has_pran and row_has_pran:
        score += 40
    elif row_has_pran:
        score += 20

    # Flavor matching & conflict detection
    q_flavors = set(q_tokens) & FLAVORS
    row_flavors = item_tokens_set & FLAVORS

    if q_flavors:
        if q_flavors & row_flavors:
            # Matches the query flavor (e.g. Apple, Orange, Mango, Chocolate, Strawberry)
            score += 100
        elif row_flavors:
            # Conflicting flavor penalty (e.g., Orange biscuit for a Strawberry query)
            score -= 90
    elif row_flavors:
        # Slight penalty when query has no flavor requested but item is a specialized flavor
        score -= 2

    # Token matching
    for q in q_tokens:
        if q in IGNORE_TOKENS:
            continue
        if q in item_tokens_set:
            if re.match(r"^\d+(?:ml|gm|kg|ltr)$", q):
                score += 50  # High bonus for exact package size match (e.g. 250ml)
            elif q in FLAVORS:
                score += 25  # High weight for matching flavor
            elif q in PRAN_BRANDS:
                score += 15
            elif q == "drink":
                score += 20
            elif q in NOISE_WORDS:
                score += 5   # Low weight for generic descriptors (cream, crunchy, etc.)
            else:
                score += 20  # High weight for distinct product names
        elif any(q in it for it in item_tokens_set if len(it) >= 3):
            score += 5

    return score


# ── Public Search & Enrichment ────────────────────────────────────────────────


def search_item(product_name: str, top_k: int = 5) -> List[CatalogueRow]:
    """
    Search the catalogue for relevant items matching an AI-detected product name.

    Strategy:
      1. Normalize & tokenise input (expand abbreviations like s.berry, p.apple, donat, units).
      2. Filter out non-PRAN third-party distributed brands unless explicitly queried.
      3. Infer target category & subcategory to hard-lock the candidate pool.
      4. Score with flavor-affinity, PRAN brand boost, size alignment, and subcategory boost.
      5. Fall back to relaxed search if hard-locked search yields 0 matches.
      6. Return top_k highest-scoring results.
    """
    if not _catalogue:
        load_items_db()
        if not _catalogue:
            return []

    clean_query = (product_name or "").strip()
    if not clean_query:
        return []

    # Exact item code direct lookup
    if clean_query.isdigit():
        code_matches = [r for r in _catalogue if r.get("Item Code") == clean_query]
        if code_matches:
            return code_matches[:top_k]

    query_tokens = _tokenise(clean_query)
    if not query_tokens:
        return []

    target_cat, target_sub = infer_category(query_tokens)
    allow_third_party = bool(TP_PATTERN.search(clean_query))

    # Pass 1: Strict Category Hard-Locking
    scored: List[Tuple[int, CatalogueRow]] = []
    for row in _catalogue:
        s = _score_candidate(
            query_tokens,
            target_cat,
            target_sub,
            row,
            hard_lock=True,
            allow_third_party=allow_third_party,
        )
        if s >= 15:
            scored.append((s, row))

    # Pass 2: Fallback without hard-locking if no matches found
    if not scored and target_cat:
        for row in _catalogue:
            s = _score_candidate(
                query_tokens,
                target_cat,
                target_sub,
                row,
                hard_lock=False,
                allow_third_party=allow_third_party,
            )
            if s >= 20:
                scored.append((s, row))

    # Sort descending by score, then stable by Item Name
    scored.sort(key=lambda x: (-x[0], x[1].get("Item Name", "")))
    return [row for _, row in scored[:top_k]]


def enrich_product(product_name: str, top_k: int = 5) -> List[CatalogueRow]:
    """Return up to top_k best-matching catalogue rows for the given product name."""
    return search_item(product_name, top_k=top_k)


def search_catalogue(query: str, limit: int = 20) -> List[Dict[str, str]]:
    """
    Public lookup for searching items catalogue with keyword, item code, subcategory, or category.
    """
    clean = (query or "").strip()
    if not clean:
        return []

    # Direct match on Item Code
    if clean.isdigit():
        direct = [
            r for r in _catalogue
            if r.get("Item Code") == clean
            or r.get("Sub Category Code") == clean
            or r.get("Category Code") == clean
        ]
        if direct:
            return [
                {
                    "sub_category_name": m.get("Sub Category Name", ""),
                    "sub_category_code": m.get("Sub Category Code", ""),
                    "category_name": m.get("Category Name", ""),
                    "category_code": m.get("Category Code", ""),
                    "item_name": m.get("Item Name", ""),
                    "item_code": m.get("Item Code", ""),
                }
                for m in direct[:limit]
            ]

    matches = search_item(clean, top_k=limit)
    return [
        {
            "sub_category_name": m.get("Sub Category Name", ""),
            "sub_category_code": m.get("Sub Category Code", ""),
            "category_name": m.get("Category Name", ""),
            "category_code": m.get("Category Code", ""),
            "item_name": m.get("Item Name", ""),
            "item_code": m.get("Item Code", ""),
        }
        for m in matches
    ]


def get_catalogue_stats() -> Dict[str, Any]:
    """Return summary statistics of loaded catalogue."""
    if not _catalogue:
        load_items_db()
    sub_cats = {r.get("Sub Category Name") for r in _catalogue if r.get("Sub Category Name")}
    cats = {r.get("Category Name") for r in _catalogue if r.get("Category Name")}
    return {
        "total_items": len(_catalogue),
        "total_sub_categories": len(sub_cats),
        "total_categories": len(cats),
    }


def enrich_products(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Enrich a list of AI-detected product dicts with catalogue suggestions.

    Each input dict must have at least "product_name".
    Each output dict gains:
        catalogue_suggestions : list of matching catalogue rows, each with:
            sub_category_name, sub_category_code,
            category_name, category_code,
            item_name, item_code
        matched (bool) : True if at least one catalogue match was found
    """
    if not _catalogue:
        load_items_db()
        if not _catalogue:
            return [dict(p, catalogue_suggestions=[], matched=False) for p in products]

    enriched: List[Dict[str, Any]] = []
    for product in products:
        p = dict(product)
        ai_name: str = str(p.get("product_name", "")).strip()
        matches = enrich_product(ai_name, top_k=5)
        if matches:
            p["catalogue_suggestions"] = [
                {
                    "sub_category_name": m.get("Sub Category Name", ""),
                    "sub_category_code": m.get("Sub Category Code", ""),
                    "category_name": m.get("Category Name", ""),
                    "category_code": m.get("Category Code", ""),
                    "item_name": m.get("Item Name", ""),
                    "item_code": m.get("Item Code", ""),
                }
                for m in matches
            ]
            p["matched"] = True
        else:
            p["catalogue_suggestions"] = []
            p["matched"] = False
        enriched.append(p)

    return enriched
