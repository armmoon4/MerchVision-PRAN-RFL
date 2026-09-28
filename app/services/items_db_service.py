"""
app/services/items_db_service.py — PRAN-RFL Items Catalogue Semantic Search Service.

Architecture:
  itemsdb.csv  (1,954 products)
      |
      |  on first startup (or force rebuild)
      v
  SentenceTransformer model: all-MiniLM-L6-v2  (384-dim embeddings)
      |
      v
  embeddings/product_embeddings.npy   -- float32 matrix (N x 384), L2-normalised
  embeddings/products_meta.json       -- catalogue metadata list

  At search time:
    query  -->  encode (1 vector)  -->  cosine similarity  -->  top-K above threshold

Public API (drop-in replacement, same signatures):
  load_items_db(force=False)         -> int
  search_item(product_name, top_k=5) -> List[CatalogueRow]
  search_catalogue(query, limit=20)  -> List[Dict]
  enrich_product(product_name, top_k=5) -> List[CatalogueRow]
  enrich_products(products)          -> List[Dict]
  get_catalogue_stats()              -> Dict
  rebuild_embeddings()               -> int   (force rebuild after CSV update)

CSV columns expected:
    Sub Category Name, Sub Category Code, Category Name, Category Code,
    Item Name, Item Code

Environment variables:
  ITEMS_DB_CSV                      -- override CSV path
  EMBEDDING_MODEL                   -- HuggingFace model name (default: all-MiniLM-L6-v2)
  CATALOGUE_SIMILARITY_THRESHOLD    -- float 0-1, default 0.30
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

CatalogueRow = Dict[str, str]   # one CSV row; keys are CSV column headers

# ---------------------------------------------------------------------------
# Singleton state
# ---------------------------------------------------------------------------

_catalogue: List[CatalogueRow] = []         # all rows from CSV (full metadata)
_embeddings: Optional[np.ndarray] = None    # shape (N, 384) float32, L2-normalised
_model = None                               # SentenceTransformer singleton
_loaded: bool = False                       # True once load_items_db() has completed

# ---------------------------------------------------------------------------
# Configuration (overridable via env vars)
# ---------------------------------------------------------------------------

# Cosine similarity threshold: results below this score are discarded.
# Range: 0.0 (unrelated) to 1.0 (identical).  0.30 is a good starting point.
SIMILARITY_THRESHOLD: float = float(
    os.getenv("CATALOGUE_SIMILARITY_THRESHOLD", "0.30")
)

DEFAULT_TOP_K: int = 5

# Sentence Transformer model. all-MiniLM-L6-v2 is ~22 MB and runs on CPU.
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent   # …/MerchVision-PRAN-RFL/

_DEFAULT_CSV_PATHS: List[Path] = [
    Path("itemsdb.csv"),           # CWD (uvicorn launched from project root)
    _REPO_ROOT / "itemsdb.csv",    # absolute fallback
]

# Pre-computed embeddings are persisted here so they survive restarts
_EMBEDDINGS_DIR: Path = Path(
    os.getenv("EMBEDDINGS_DIR", str(_REPO_ROOT / "embeddings"))
)
_EMBEDDINGS_FILE: Path = _EMBEDDINGS_DIR / "product_embeddings.npy"
_META_FILE: Path       = _EMBEDDINGS_DIR / "products_meta.json"


def _find_csv() -> Optional[Path]:
    env_path = os.getenv("ITEMS_DB_CSV")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        logger.warning(
            "ITEMS_DB_CSV env var points to non-existent file: %s", env_path
        )
    for candidate in _DEFAULT_CSV_PATHS:
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Text pre-processing (abbreviation expansion)
# ---------------------------------------------------------------------------

_ABBR_MAP: Dict[str, str] = {
    "p.apple":   "pineapple",
    "papple":    "pineapple",
    "s.berry":   "strawberry",
    "sberry":    "strawberry",
    "p.granate": "pomegranate",
    "pgranate":  "pomegranate",
    "donat":     "donut",
    "choco":     "chocolate",
    "choc":      "chocolate",
    "vanila":    "vanilla",
    "falvoured": "flavoured",
    "falvor":    "flavor",
}


def _expand_abbreviations(text: str) -> str:
    """Lowercase and expand product-name abbreviations; normalise unit tokens."""
    t = text.lower()
    t = re.sub(r"(\d+)\s*ml\b",   r"\1ml",  t)
    t = re.sub(r"(\d+)\s*gm?\b",  r"\1gm",  t)
    t = re.sub(r"(\d+)\s*kg\b",   r"\1kg",  t)
    t = re.sub(r"(\d+)\s*ltr?\b", r"\1ltr", t)
    for abbr, full in _ABBR_MAP.items():
        t = re.sub(rf"\b{re.escape(abbr)}\b", full, t)
    return t


def _build_search_text(row: CatalogueRow) -> str:
    """
    Build the embedding text for a catalogue row.
    Concatenating Item Name + Sub Category + Category gives the model richer
    context (e.g. the word 'Lassi' also associates with '130-Lassi 120-Dairy').
    """
    item_name = _expand_abbreviations(row.get("Item Name", ""))
    sub_cat   = row.get("Sub Category Name", "")
    category  = row.get("Category Name", "")
    parts = [item_name]
    if sub_cat:
        parts.append(sub_cat)
    if category:
        parts.append(category)
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


def _load_csv(csv_path: Path) -> List[CatalogueRow]:
    """Read itemsdb.csv and return a list of cleaned row dicts."""
    rows: List[CatalogueRow] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            cleaned = {
                k.strip().strip('"'): v.strip().strip('"')
                for k, v in raw.items()
                if k
            }
            if cleaned.get("Item Name"):
                rows.append(cleaned)
    return rows


# ---------------------------------------------------------------------------
# SentenceTransformer model (lazy singleton)
# ---------------------------------------------------------------------------


def _get_model():
    """Return the SentenceTransformer singleton, loading it on first call."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            logger.info("Loading SentenceTransformer model: %s ...", EMBEDDING_MODEL)
            _model = SentenceTransformer(EMBEDDING_MODEL)
            logger.info("SentenceTransformer model loaded.")
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is not installed. "
                "Add it to requirements.txt and rebuild the Docker image."
            ) from exc
    return _model


# ---------------------------------------------------------------------------
# Embedding generation & persistence
# ---------------------------------------------------------------------------


def _build_and_save_embeddings(catalogue: List[CatalogueRow]) -> np.ndarray:
    """
    Encode all catalogue rows with SentenceTransformer and save to disk.
    This is called exactly once (at startup or after a CSV update).
    """
    model = _get_model()
    _EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Generating embeddings for %d products ...", len(catalogue))
    texts = [_build_search_text(row) for row in catalogue]

    embeddings: np.ndarray = model.encode(
        texts,
        normalize_embeddings=True,   # L2-normalise so cosine sim == dot product
        show_progress_bar=False,
        batch_size=64,
    ).astype(np.float32)

    np.save(str(_EMBEDDINGS_FILE), embeddings)
    logger.info("Saved product embeddings -> %s", _EMBEDDINGS_FILE)

    meta = [
        {
            "item_name":         row.get("Item Name", ""),
            "item_code":         row.get("Item Code", ""),
            "sub_category_name": row.get("Sub Category Name", ""),
            "sub_category_code": row.get("Sub Category Code", ""),
            "category_name":     row.get("Category Name", ""),
            "category_code":     row.get("Category Code", ""),
        }
        for row in catalogue
    ]
    _META_FILE.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("Saved product metadata -> %s", _META_FILE)

    return embeddings


def _load_saved_embeddings() -> Optional[Tuple[np.ndarray, List[Dict]]]:
    """
    Load pre-computed embeddings + metadata from disk.
    Returns (embeddings, meta_list) on success, or None if missing/corrupt.
    """
    if not _EMBEDDINGS_FILE.is_file() or not _META_FILE.is_file():
        return None
    try:
        embeddings = np.load(str(_EMBEDDINGS_FILE))
        meta       = json.loads(_META_FILE.read_text(encoding="utf-8"))
        if len(embeddings) != len(meta):
            logger.warning(
                "Embeddings/meta size mismatch (%d vs %d) -- will rebuild.",
                len(embeddings), len(meta),
            )
            return None
        logger.info(
            "Loaded embeddings from disk: %d products, dim=%d.",
            len(embeddings), embeddings.shape[1],
        )
        return embeddings, meta
    except Exception as exc:
        logger.warning("Could not load saved embeddings (%s) -- will rebuild.", exc)
        return None


# ---------------------------------------------------------------------------
# Public: load_items_db
# ---------------------------------------------------------------------------


def load_items_db(force: bool = False) -> int:
    """
    Initialise the semantic catalogue search engine.

    Steps:
      1. Read itemsdb.csv into memory.
      2. Load pre-computed embeddings from disk, OR generate+save them if missing.

    Args:
        force: If True, always regenerate embeddings even if saved ones exist.

    Returns:
        Number of products loaded.
    """
    global _catalogue, _embeddings, _loaded

    if _loaded and not force:
        return len(_catalogue)

    csv_path = _find_csv()
    if csv_path is None:
        logger.warning(
            "itemsdb.csv not found -- catalogue search disabled. "
            "Set ITEMS_DB_CSV env var or place itemsdb.csv in the project root."
        )
        _catalogue, _embeddings, _loaded = [], None, True
        return 0

    try:
        _catalogue = _load_csv(csv_path)
        logger.info("Loaded %d products from %s", len(_catalogue), csv_path)
    except Exception as exc:
        logger.error("Failed to read itemsdb.csv: %s", exc)
        _catalogue, _embeddings, _loaded = [], None, True
        return 0

    if force:
        _embeddings = _build_and_save_embeddings(_catalogue)
    else:
        saved = _load_saved_embeddings()
        if saved is None:
            _embeddings = _build_and_save_embeddings(_catalogue)
        else:
            _embeddings, saved_meta = saved
            # Rebuild if the CSV has grown or shrunk since embeddings were saved
            if len(saved_meta) != len(_catalogue):
                logger.info(
                    "CSV row count changed (%d -> %d) -- rebuilding embeddings.",
                    len(saved_meta), len(_catalogue),
                )
                _embeddings = _build_and_save_embeddings(_catalogue)

    _loaded = True
    return len(_catalogue)


# ---------------------------------------------------------------------------
# Core semantic search
# ---------------------------------------------------------------------------


def _semantic_search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    threshold: float = SIMILARITY_THRESHOLD,
) -> List[Tuple[float, CatalogueRow]]:
    """
    Encode *query* with SentenceTransformer, compute cosine similarity against
    all pre-built product embeddings, and return the top-K results that meet
    the similarity threshold.

    Returns:
        List of (score, CatalogueRow) tuples, sorted descending by score.
    """
    if _embeddings is None or not _catalogue:
        return []

    model = _get_model()
    expanded_query = _expand_abbreviations(query.strip())

    # Encode the single query vector (1 x D)
    q_vec: np.ndarray = model.encode(
        expanded_query,
        normalize_embeddings=True,
    ).astype(np.float32)

    # Fast cosine similarity via dot product (embeddings are L2-normalised)
    scores: np.ndarray = _embeddings @ q_vec   # shape: (N,)

    # Over-fetch to leave room for threshold filtering
    fetch_n     = min(top_k * 4, len(scores))
    top_indices = np.argpartition(scores, -fetch_n)[-fetch_n:]
    top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

    results: List[Tuple[float, CatalogueRow]] = []
    for idx in top_indices:
        score = float(scores[idx])
        if score < threshold:
            break          # array is sorted descending; no need to keep going
        if idx < len(_catalogue):
            results.append((score, _catalogue[idx]))
        if len(results) >= top_k:
            break

    return results


# ---------------------------------------------------------------------------
# Direct item-code lookup
# ---------------------------------------------------------------------------


def _lookup_by_code(code: str) -> List[CatalogueRow]:
    """Exact match on Item Code, Sub Category Code, or Category Code."""
    return [
        r for r in _catalogue
        if r.get("Item Code") == code
        or r.get("Sub Category Code") == code
        or r.get("Category Code") == code
    ]


# ---------------------------------------------------------------------------
# Public: search_item
# ---------------------------------------------------------------------------


def search_item(product_name: str, top_k: int = DEFAULT_TOP_K) -> List[CatalogueRow]:
    """
    Find the best-matching catalogue rows for a given product name using
    semantic (embedding-based) search.

    Args:
        product_name: Free-text string, e.g. "Pran Lassi Drink 250ml".
        top_k:        Maximum results to return.

    Returns:
        List of CatalogueRow dicts, best match first.
    """
    if not _loaded:
        load_items_db()

    clean = (product_name or "").strip()
    if not clean:
        return []

    if clean.isdigit():
        return _lookup_by_code(clean)[:top_k]

    return [row for _, row in _semantic_search(clean, top_k=top_k)]


# ---------------------------------------------------------------------------
# Public: search_catalogue
# ---------------------------------------------------------------------------


def search_catalogue(query: str, limit: int = 20) -> List[Dict[str, str]]:
    """
    Keyword/semantic search used by GET /catalogue/search.

    Returns a list of dicts with keys:
      sub_category_name, sub_category_code, category_name, category_code,
      item_name, item_code
    """
    clean = (query or "").strip()
    if not clean:
        return []

    if clean.isdigit():
        direct = _lookup_by_code(clean)
        if direct:
            return [_row_to_suggestion(r) for r in direct[:limit]]

    matches = search_item(clean, top_k=limit)
    return [_row_to_suggestion(r) for r in matches]


def _row_to_suggestion(row: CatalogueRow) -> Dict[str, str]:
    return {
        "sub_category_name":  row.get("Sub Category Name", ""),
        "sub_category_code":  row.get("Sub Category Code", ""),
        "category_name":      row.get("Category Name", ""),
        "category_code":      row.get("Category Code", ""),
        "item_name":          row.get("Item Name", ""),
        "item_code":          row.get("Item Code", ""),
    }


# ---------------------------------------------------------------------------
# Public: enrich_product / enrich_products
# ---------------------------------------------------------------------------


def enrich_product(
    product_name: str, top_k: int = DEFAULT_TOP_K
) -> List[CatalogueRow]:
    """Return up to top_k best-matching catalogue rows for a product name."""
    return search_item(product_name, top_k=top_k)


def enrich_products(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Enrich a list of AI-detected products with catalogue suggestions.

    Each input dict must contain at least 'product_name'.
    Each output dict gains:
      - catalogue_suggestions: list of suggestion dicts
      - matched: bool
    """
    if not _loaded:
        load_items_db()
        if not _catalogue:
            return [
                dict(p, catalogue_suggestions=[], matched=False) for p in products
            ]

    enriched: List[Dict[str, Any]] = []
    for product in products:
        p       = dict(product)
        ai_name = str(p.get("product_name", "")).strip()
        matches = enrich_product(ai_name, top_k=DEFAULT_TOP_K)
        if matches:
            p["catalogue_suggestions"] = [_row_to_suggestion(m) for m in matches]
            p["matched"] = True
        else:
            p["catalogue_suggestions"] = []
            p["matched"] = False
        enriched.append(p)
    return enriched


# ---------------------------------------------------------------------------
# Public: get_catalogue_stats
# ---------------------------------------------------------------------------


def get_catalogue_stats() -> Dict[str, Any]:
    """Return summary statistics of the loaded catalogue and search engine."""
    if not _loaded:
        load_items_db()
    sub_cats = {r.get("Sub Category Name") for r in _catalogue if r.get("Sub Category Name")}
    cats     = {r.get("Category Name") for r in _catalogue if r.get("Category Name")}
    return {
        "total_items":          len(_catalogue),
        "total_sub_categories": len(sub_cats),
        "total_categories":     len(cats),
        "embedding_model":      EMBEDDING_MODEL,
        "embeddings_loaded":    _embeddings is not None,
        "similarity_threshold": SIMILARITY_THRESHOLD,
    }


# ---------------------------------------------------------------------------
# Utility: rebuild on demand
# ---------------------------------------------------------------------------


def rebuild_embeddings() -> int:
    """
    Force a full re-generation of embeddings from the current itemsdb.csv.
    Call this after updating the CSV without restarting the server.

    Returns:
        Number of products embedded.
    """
    global _loaded
    _loaded = False
    load_items_db(force=True)
    return len(_catalogue)
