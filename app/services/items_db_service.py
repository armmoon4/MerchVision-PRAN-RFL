"""
app/services/items_db_service.py — PRAN-RFL Items Catalogue lookup service.

Loads itemsdb.csv once at startup (singleton) and provides fast fuzzy/keyword
search against Item Names.  Zero extra AI tokens are consumed — the AI returns
only product names, then we enrich each name locally using this service.

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
from typing import Any, Dict, List, Optional

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
    # 1. Env override
    env_path = os.getenv("ITEMS_DB_CSV")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        logger.warning("ITEMS_DB_CSV env var points to non-existent file: %s", env_path)

    # 2. Default search paths
    for candidate in _DEFAULT_CSV_PATHS:
        if candidate.is_file():
            return candidate

    return None


# ── Load ──────────────────────────────────────────────────────────────────────


def load_items_db(force: bool = False) -> int:
    """
    Load (or re-load) itemsdb.csv into the in-memory catalogue.

    Returns number of rows loaded.  Safe to call multiple times (no-op if
    already loaded unless force=True).
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
                # Normalise keys & strip surrounding whitespace / quotes
                cleaned = {k.strip().strip('"'): v.strip().strip('"') for k, v in row.items() if k}
                if cleaned.get("Item Name"):
                    rows.append(cleaned)
        _catalogue = rows
        _loaded = True
        logger.info("ItemsDB loaded: %d products from %s", len(rows), csv_path)
    except Exception as exc:
        logger.error("Failed to load itemsdb.csv from %s: %s", csv_path, exc)
        _catalogue = []
        _loaded = True

    return len(_catalogue)


# ── Search ────────────────────────────────────────────────────────────────────


def _tokenise(text: str) -> List[str]:
    """Lower-case, split on non-alphanumeric boundaries, drop short tokens."""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 2]


def _score_by_name(query_tokens: List[str], item_name: str) -> int:
    """
    Score a catalogue row's Item Name against the AI-detected product name.
    Scoring is done ONLY against Item Name — not codes, subcategories, or categories.
    """
    item_name_lower = item_name.strip().lower()
    item_tokens = set(_tokenise(item_name_lower))

    score = 0
    for q in query_tokens:
        if q in item_tokens:
            # Exact token match in item name
            score += 10
        elif any(q in it for it in item_tokens if len(it) >= 3):
            # Partial match (e.g., "pran" inside "pranfrooto")
            score += 4
    return score


def search_item(product_name: str, top_k: int = 5) -> List[CatalogueRow]:
    """
    Search the catalogue for relevant items matching an AI-detected product name.

    Strategy:
      1. Tokenise the AI-detected product name.
      2. Score each catalogue row by matching tokens ONLY against Item Name.
      3. Return top_k results with score > 0, sorted best-match first.

    Returns an empty list if no match found or catalogue not loaded.
    """
    if not _catalogue:
        return []

    clean_query = (product_name or "").strip()
    if not clean_query:
        return []

    query_tokens = _tokenise(clean_query)
    if not query_tokens:
        return []

    scored: List[tuple[int, CatalogueRow]] = []
    for row in _catalogue:
        item_name = row.get("Item Name", "")
        s = _score_by_name(query_tokens, item_name)
        # Require at least 2 matching tokens to avoid noisy low-quality results
        if s >= 20:
            scored.append((s, row))

    # Sort descending by score, then stable by Item Name for determinism
    scored.sort(key=lambda x: (-x[0], x[1].get("Item Name", "")))
    return [row for _, row in scored[:top_k]]


def enrich_product(product_name: str, top_k: int = 5) -> List[CatalogueRow]:
    """
    Return up to top_k best-matching catalogue rows for the given product name.
    Returns an empty list if no match found.
    """
    return search_item(product_name, top_k=top_k)


def search_catalogue(query: str, limit: int = 20) -> List[Dict[str, str]]:
    """
    Public lookup for searching items catalogue with keyword, item code, subcategory, or category.
    """
    matches = search_item(query, top_k=limit)
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
        return [dict(p, catalogue_suggestions=[], matched=False) for p in products]

    enriched: List[Dict[str, Any]] = []
    for product in products:
        p = dict(product)
        ai_name: str = str(p.get("product_name", "")).strip()
        matches = enrich_product(ai_name, top_k=5)  # returns up to 5 best matches
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
