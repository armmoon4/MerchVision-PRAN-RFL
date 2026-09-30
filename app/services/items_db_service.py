"""
app/services/items_db_service.py — PRAN-RFL Items Catalogue Hybrid Search Service.

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

  At search time (per detected product):
    ┌─────────────────────────────────────────────────────────────────────┐
    │ Step 1 │ Sub-Category filter + Brand filter                         │
    │        │ → search only inside same sub-cat (Lassi) + PRAN products  │
    │        │ → Hybrid score: BM25 + FAISS cosine                        │
    │        │ → If ≥3 results  ──►  proceed to Reranker                  │
    ├────────┼─────────────────────────────────────────────────────────────┤
    │ Step 2 │ Brand-only filter (all sub-categories)                      │
    │        │ → merge with Step 1, deduplicate                            │
    │        │ → If ≥3 results  ──►  proceed to Reranker                  │
    ├────────┼─────────────────────────────────────────────────────────────┤
    │ Step 3 │ Global semantic fallback (original behaviour)               │
    ├────────┴─────────────────────────────────────────────────────────────┤
    │  Reranker: Cross-Encoder  ms-marco-MiniLM-L-6-v2                    │
    │  Final Top-K returned                                                │
    └──────────────────────────────────────────────────────────────────────┘

Public API (drop-in replacement, same signatures):
  load_items_db(force=False)         -> int
  search_item_with_scores(name, top_k=5) -> List[Tuple[float, CatalogueRow]]  (confidence 0.0-1.0)
  search_item(product_name, top_k=5) -> List[CatalogueRow]
  search_catalogue(query, limit=20)  -> List[Dict] (includes 'confidence')
  enrich_product(product_name, top_k=5) -> List[Tuple[float, CatalogueRow]]
  enrich_products(products)          -> List[Dict] (each suggestion includes 'confidence')
  get_catalogue_stats()              -> Dict
  rebuild_embeddings()               -> int   (force rebuild after CSV update)

CSV columns expected:
    Sub Category Name, Sub Category Code, Category Name, Category Code,
    Item Name, Item Code

Environment variables:
  ITEMS_DB_CSV                      -- override CSV path
  EMBEDDING_MODEL                   -- HuggingFace model name (default: all-MiniLM-L6-v2)
  RERANKER_MODEL                    -- Cross-Encoder model (default: cross-encoder/ms-marco-MiniLM-L-6-v2)
  CATALOGUE_SIMILARITY_THRESHOLD    -- float 0-1, default 0.25
  BM25_WEIGHT                       -- float 0-1, weight for BM25 in hybrid (default: 0.35)
  FAISS_WEIGHT                      -- float 0-1, weight for FAISS in hybrid (default: 0.65)
  ENABLE_RERANKER                   -- "true"/"false", default "true"
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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
_reranker = None                            # CrossEncoder singleton (lazy)
_bm25 = None                               # BM25Okapi index over all product names
_bm25_corpus: List[List[str]] = []         # tokenised corpus (parallel to _catalogue)
_loaded: bool = False                       # True once load_items_db() has completed

# Index: sub_category_code -> list of row indices into _catalogue / _embeddings
_subcat_index: Dict[str, List[int]] = {}
# Index: category_code -> list of row indices
_cat_index: Dict[str, List[int]] = {}

# ---------------------------------------------------------------------------
# Configuration (overridable via env vars)
# ---------------------------------------------------------------------------

# Cosine similarity threshold for candidate retrieval in hybrid search
SIMILARITY_THRESHOLD: float = float(
    os.getenv("CATALOGUE_SIMILARITY_THRESHOLD", "0.25")
)

DEFAULT_TOP_K: int = 5

# Sentence Transformer model. all-MiniLM-L6-v2 is ~22 MB and runs on CPU.
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# Cross-Encoder reranker model (tiny, ~66 MB, CPU-friendly)
RERANKER_MODEL: str = os.getenv(
    "RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"
)

# Hybrid score weights (must sum to 1.0)
BM25_WEIGHT: float  = float(os.getenv("BM25_WEIGHT",  "0.35"))
FAISS_WEIGHT: float = float(os.getenv("FAISS_WEIGHT", "0.65"))

# Toggle reranker (set ENABLE_RERANKER=false to skip in resource-constrained envs)
ENABLE_RERANKER: bool = os.getenv("ENABLE_RERANKER", "true").lower() == "true"

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
    "litchi":    "lychee",
}


def _expand_abbreviations(text: str) -> str:
    """Lowercase and expand product-name abbreviations; normalise unit tokens."""
    t = text.lower()
    t = re.sub(r"\b1\s*(?:l|ltr|liter|litre)\b", "1000ml", t)
    t = re.sub(r"\b2\s*(?:l|ltr|liter|litre)\b", "2000ml", t)
    t = re.sub(r"(\d+)\s*ml\b",   r"\1ml",  t)
    t = re.sub(r"(\d+)\s*gm?\b",  r"\1gm",  t)
    t = re.sub(r"(\d+)\s*kg\b",   r"\1kg",  t)
    t = re.sub(r"(\d+)\s*ltr?\b", r"\1ltr", t)
    for abbr, full in _ABBR_MAP.items():
        t = re.sub(rf"\b{re.escape(abbr)}\b", full, t)
    return t


def _tokenize(text: str) -> List[str]:
    """Simple whitespace+punctuation tokenizer for BM25."""
    expanded = _expand_abbreviations(text)
    tokens = re.findall(r"[a-z0-9]+", expanded)
    return tokens


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
# Cross-Encoder Reranker (lazy singleton)
# ---------------------------------------------------------------------------


def _get_reranker():
    """Return the CrossEncoder singleton, loading it on first call."""
    global _reranker
    if _reranker is None and ENABLE_RERANKER:
        try:
            from sentence_transformers.cross_encoder import CrossEncoder  # type: ignore
            logger.info("Loading Cross-Encoder reranker: %s ...", RERANKER_MODEL)
            _reranker = CrossEncoder(RERANKER_MODEL, max_length=128)
            logger.info("Cross-Encoder reranker loaded.")
        except Exception as exc:
            logger.warning(
                "Could not load Cross-Encoder reranker (%s). "
                "Reranking disabled. Error: %s",
                RERANKER_MODEL, exc,
            )
            _reranker = None
    return _reranker


# ---------------------------------------------------------------------------
# BM25 index
# ---------------------------------------------------------------------------


def _build_bm25_index(catalogue: List[CatalogueRow]) -> None:
    """Build a BM25 index over product Item Names."""
    global _bm25, _bm25_corpus
    try:
        from rank_bm25 import BM25Okapi  # type: ignore
        logger.info("Building BM25 index for %d products ...", len(catalogue))
        _bm25_corpus = [_tokenize(r.get("Item Name", "")) for r in catalogue]
        _bm25 = BM25Okapi(_bm25_corpus)
        logger.info("BM25 index built.")
    except ImportError:
        logger.warning(
            "rank_bm25 is not installed — BM25 disabled. "
            "Add 'rank-bm25' to requirements.txt for hybrid search."
        )
        _bm25 = None
        _bm25_corpus = []


def _bm25_scores(query: str, n: int) -> Optional[np.ndarray]:
    """
    Return BM25 scores for *query* across all catalogue products.
    Returns shape (N,) array normalised to [0,1], or None if BM25 unavailable.
    """
    if _bm25 is None:
        return None
    tokens = _tokenize(query)
    if not tokens:
        return None
    scores: np.ndarray = np.array(_bm25.get_scores(tokens), dtype=np.float32)
    max_score = scores.max()
    if max_score > 0:
        scores = scores / max_score   # normalise to [0, 1]
    return scores


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
# Category & sub-category index
# ---------------------------------------------------------------------------


def _build_category_index() -> None:
    """Build fast lookup indexes: sub_category_code -> [indices], category_code -> [indices]."""
    global _subcat_index, _cat_index
    _subcat_index = {}
    _cat_index = {}
    for i, row in enumerate(_catalogue):
        sc = row.get("Sub Category Code", "").strip()
        cc = row.get("Category Code", "").strip()
        if sc:
            _subcat_index.setdefault(sc, []).append(i)
        if cc:
            _cat_index.setdefault(cc, []).append(i)


# ---------------------------------------------------------------------------
# Public: load_items_db
# ---------------------------------------------------------------------------


def load_items_db(force: bool = False) -> int:
    """
    Initialise the hybrid catalogue search engine.

    Steps:
      1. Read itemsdb.csv into memory.
      2. Load pre-computed FAISS embeddings from disk, or generate+save them.
      3. Build BM25 index from product names.
      4. Build category/sub-category lookup indexes.
      5. Warm up Cross-Encoder reranker (lazy, first-use load).

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

    # FAISS embeddings
    if force:
        _embeddings = _build_and_save_embeddings(_catalogue)
    else:
        saved = _load_saved_embeddings()
        if saved is None:
            _embeddings = _build_and_save_embeddings(_catalogue)
        else:
            _embeddings, saved_meta = saved
            if len(saved_meta) != len(_catalogue):
                logger.info(
                    "CSV row count changed (%d -> %d) -- rebuilding embeddings.",
                    len(saved_meta), len(_catalogue),
                )
                _embeddings = _build_and_save_embeddings(_catalogue)

    # BM25 index
    _build_bm25_index(_catalogue)

    # Category indexes
    _build_category_index()
    logger.info(
        "Category index built: %d sub-categories, %d categories.",
        len(_subcat_index), len(_cat_index),
    )

    # Warm up models into memory eagerly so incoming requests have ZERO cold start
    try:
        _get_model()
        if ENABLE_RERANKER:
            _get_reranker()
    except Exception as exc:
        logger.warning("Model warm-up note: %s", exc)

    _loaded = True
    return len(_catalogue)


# ---------------------------------------------------------------------------
# Core: FAISS cosine similarity (global)
# ---------------------------------------------------------------------------


def _faiss_scores_global(query: str) -> Optional[np.ndarray]:
    """
    Encode *query* and return cosine similarity scores for all catalogue rows.
    Returns shape (N,) float32 array (already normalised, range roughly 0-1).
    """
    if _embeddings is None or not _catalogue:
        return None
    model = _get_model()
    expanded = _expand_abbreviations(query.strip())
    q_vec: np.ndarray = model.encode(
        expanded, normalize_embeddings=True
    ).astype(np.float32)
    return _embeddings @ q_vec   # shape (N,)


# ---------------------------------------------------------------------------
# Core: Hybrid score for a subset of indices
# ---------------------------------------------------------------------------


def _hybrid_search_indices(
    query: str,
    candidate_indices: List[int],
    top_k: int,
    threshold: float = SIMILARITY_THRESHOLD,
    q_vec: Optional[np.ndarray] = None,
) -> List[Tuple[float, CatalogueRow]]:
    """
    Compute Hybrid (BM25 + FAISS) scores restricted to *candidate_indices*.

    Hybrid score = FAISS_WEIGHT * faiss_score + BM25_WEIGHT * bm25_score

    Returns:
        List of (hybrid_score, CatalogueRow) sorted descending, above threshold.
    """
    if _embeddings is None or not _catalogue or not candidate_indices:
        return []

    idx_array = np.array(candidate_indices, dtype=np.int32)

    # --- FAISS scores for the subset ---
    if q_vec is None:
        model = _get_model()
        expanded = _expand_abbreviations(query.strip())
        q_vec = model.encode(
            expanded, normalize_embeddings=True, show_progress_bar=False
        ).astype(np.float32)

    sub_emb    = _embeddings[idx_array]
    faiss_sub  = (sub_emb @ q_vec).astype(np.float32)  # shape (M,)

    # --- BM25 scores for the subset ---
    bm25_global = _bm25_scores(query, len(_catalogue))
    if bm25_global is not None:
        bm25_sub = bm25_global[idx_array]              # shape (M,)
        hybrid   = FAISS_WEIGHT * faiss_sub + BM25_WEIGHT * bm25_sub
    else:
        hybrid = faiss_sub   # BM25 unavailable, fall back to FAISS only

    # Sort descending by hybrid score
    sorted_order = np.argsort(hybrid)[::-1]

    results: List[Tuple[float, CatalogueRow]] = []
    for pos in sorted_order:
        score = float(hybrid[pos])
        if score < threshold:
            break
        orig_idx = int(idx_array[pos])
        if orig_idx < len(_catalogue):
            results.append((score, _catalogue[orig_idx]))
        if len(results) >= top_k:
            break

    return results


def _hybrid_search_global(
    query: str,
    top_k: int,
    threshold: float = SIMILARITY_THRESHOLD,
    q_vec: Optional[np.ndarray] = None,
) -> List[Tuple[float, CatalogueRow]]:
    """Global hybrid search across all catalogue rows (Step 3 fallback)."""
    if _embeddings is None or not _catalogue:
        return []

    all_indices = list(range(len(_catalogue)))
    return _hybrid_search_indices(
        query, all_indices, top_k=top_k * 2, threshold=threshold, q_vec=q_vec
    )


# ---------------------------------------------------------------------------
# Brand & sub-category keyword extraction helpers
# ---------------------------------------------------------------------------

# Common brand keywords found in product names (case-insensitive)
_KNOWN_BRANDS: List[str] = [
    "pran", "fresh", "rfl", "bisk club", "moo", "frooto", "mr noodles",
    "acme", "aci", "frutika", "igloo", "danish", "bashundhara",
]


def _extract_brand_tokens(product_name: str) -> Set[str]:
    """
    Return lowercase brand tokens found in *product_name*.
    E.g. "PRAN Lassi Strawberry 200ml" -> {"pran"}
    """
    name_lower = product_name.lower()
    found: Set[str] = set()
    for brand in _KNOWN_BRANDS:
        if brand in name_lower:
            found.add(brand)
    return found


def _row_contains_brand(row: CatalogueRow, brand_tokens: Set[str]) -> bool:
    """
    True if Item Name or Sub Category Name contains any of the brand tokens.
    In PRAN's catalogue, items without an explicit corporate brand prefix
    (e.g. 'Pomegranate TP 1000ml', 'Drinko', 'Frooto', 'All Time')
    belong to PRAN unless they have an explicit competing brand prefix.
    """
    if not brand_tokens:
        return True   # no brand filter → accept all
    combined_name = (
        row.get("Item Name", "") + " " + row.get("Sub Category Name", "")
    ).lower()
    if any(b in combined_name for b in brand_tokens):
        return True
    if "pran" in brand_tokens:
        competing_brands = {"fresh", "danish", "acme", "aci", "igloo", "bashundhara"}
        if not any(cb in combined_name for cb in competing_brands):
            return True
    return False


# Sub-category keyword hints: words in detected product name -> sub-cat keywords.
# Maps a trigger word (must appear in detected name) to a list of sub-category
# or category name fragments to look for (case-insensitive substring match).
_SUBCAT_KEYWORD_HINTS: List[Tuple[str, List[str]]] = [
    ("lassi",       ["lassi"]),
    ("yogurt",      ["lassi", "yogurt"]),
    ("juice",       ["juice", "ju-", "ju ", "jus", "frooto"]),
    ("drink",       ["drink", "dr-", "dr ", "ju-", "ju ", "jus", "frooto", "bever"]),
    ("cocktail",    ["ju-", "jus", "pak"]),
    ("fruit",       ["ju-", "jus", "pak", "frooto", "fruit"]),
    ("noodle",      ["noodle"]),
    ("biscuit",     ["bisc", "cookie", "cracke"]),
    ("cookie",      ["bisc", "cookie"]),
    ("oil",         ["oil"]),
    ("soap",        ["soap"]),
    ("shampoo",     ["shampoo"]),
    ("rice",        ["rice"]),
    ("flour",       ["flour", "atta"]),
    ("milk",        ["milk", "dairy"]),
    ("water",       ["water"]),
    ("chips",       ["chips", "snack"]),
    ("chocolate",   ["choc"]),
    ("candy",       ["candy", "confect"]),
    ("vinegar",     ["vinegar"]),
    ("sauce",       ["sauce"]),
    ("mustard",     ["mustard"]),
]


def _infer_subcat_codes(product_name: str) -> List[str]:
    """
    Given a detected product name, return a priority list of Sub Category Codes
    to search within first.

    Strategy:
      1. Find keyword hints matching the product name.
      2. For each hint, find all sub-category codes whose Sub Category Name
         or Category Name contains any of the hint fragments.
    Returns a deduplicated list of sub-category codes (may be empty).
    """
    name_lower = product_name.lower()
    matched_fragments: List[str] = []

    # If explicitly lassi or yogurt, do not include broad beverage/juice hints
    is_lassi = "lassi" in name_lower or "yogurt" in name_lower

    for trigger, fragments in _SUBCAT_KEYWORD_HINTS:
        if trigger in name_lower:
            if is_lassi and trigger in ("drink", "juice", "fruit"):
                continue
            matched_fragments.extend(fragments)

    if not matched_fragments:
        return []

    matched_codes: List[str] = []
    seen: Set[str] = set()
    for code, indices in _subcat_index.items():
        if not indices:
            continue
        first_row = _catalogue[indices[0]]
        cat_search_text = (
            first_row.get("Sub Category Name", "")
            + " "
            + first_row.get("Category Name", "")
        ).lower()
        for frag in matched_fragments:
            if frag in cat_search_text and code not in seen:
                matched_codes.append(code)
                seen.add(code)
                break

    return matched_codes


# ---------------------------------------------------------------------------
# Cross-Encoder Reranker
# ---------------------------------------------------------------------------


def _rerank(
    query: str,
    candidates: List[Tuple[float, CatalogueRow]],
    top_k: int,
) -> List[Tuple[float, CatalogueRow]]:
    """
    Pass *candidates* through the Cross-Encoder reranker for better final ranking.

    If the reranker is unavailable (not installed / ENABLE_RERANKER=false)
    or if the top candidate already has high confidence (>= 0.82),
    the original hybrid-scored list is returned with clamped confidence scores in [0, 1].

    Args:
        query:      The detected product name string.
        candidates: List of (hybrid_score, row) sorted by hybrid score.
        top_k:      How many results to return.

    Returns:
        Re-sorted (confidence, row) list, truncated to top_k.
    """
    if not candidates:
        return []

    # Fast-path bypass: If top match is already very confident (>= 0.82), skip heavy Cross-Encoder
    if candidates[0][0] >= 0.82 or len(candidates) == 1:
        return [
            (round(max(min(float(s), 1.0), 0.0), 4), row)
            for s, row in candidates[:top_k]
        ]

    reranker = _get_reranker()
    if reranker is None:
        return [
            (round(max(min(float(s), 1.0), 0.0), 4), row)
            for s, row in candidates[:top_k]
        ]

    # Build (query, product_name) pairs for the cross-encoder
    pairs = [
        (query, row.get("Item Name", ""))
        for _, row in candidates
    ]
    try:
        raw_scores: List[float] = reranker.predict(pairs).tolist()
    except Exception as exc:
        logger.warning("Reranker prediction failed (%s); using hybrid scores.", exc)
        return [
            (round(max(min(float(s), 1.0), 0.0), 4), row)
            for s, row in candidates[:top_k]
        ]

    # Sigmoid mapping: 1 / (1 + exp(-logit)) -> calibrated confidence in [0.0, 1.0]
    def _sigmoid(logit: float) -> float:
        clipped = max(min(logit, 30.0), -30.0)
        return 1.0 / (1.0 + math.exp(-clipped))

    confidences = [round(_sigmoid(float(s)), 4) for s in raw_scores]

    ranked = sorted(
        zip(confidences, [row for _, row in candidates]),
        key=lambda t: t[0],
        reverse=True,
    )
    logger.debug(
        "Reranked %d candidates -> top: %s (confidence: %.4f)",
        len(candidates),
        ranked[0][1].get("Item Name", "?") if ranked else "?",
        ranked[0][0] if ranked else 0.0,
    )
    return ranked[:top_k]


# ---------------------------------------------------------------------------
# 3-Step Priority + Hybrid Search Pipeline
# ---------------------------------------------------------------------------


def _priority_search(
    product_name: str,
    top_k: int = DEFAULT_TOP_K,
    q_vec: Optional[np.ndarray] = None,
) -> List[Tuple[float, CatalogueRow]]:
    """
    Full pipeline:

    Step 1: Sub-Category filter + Brand filter → Hybrid (BM25 + FAISS)
    Step 2: Brand-only filter (all categories) → Hybrid, merged with Step 1
    Step 3: Global hybrid fallback
    ────────────────────────────────────────────────────────────
    Cross-Encoder Reranker applied to final candidate pool (with high-confidence fast-path).

    Returns deduplicated (score, CatalogueRow) list, best first.
    """
    FALLBACK_THRESHOLD = 3   # min good matches before skipping to reranker

    # Pre-encode query vector once so Step 1, 2, and 3 do not re-encode redundantly
    if q_vec is None and _embeddings is not None:
        try:
            model = _get_model()
            expanded = _expand_abbreviations(product_name.strip())
            q_vec = model.encode(
                expanded, normalize_embeddings=True, show_progress_bar=False
            ).astype(np.float32)
        except Exception as exc:
            logger.warning("Encoding failed in _priority_search (%s)", exc)
            q_vec = None

    brand_tokens = _extract_brand_tokens(product_name)
    subcat_codes = _infer_subcat_codes(product_name)

    # ---- Step 1: sub-category + brand -----------------------------------
    step1_results: List[Tuple[float, CatalogueRow]] = []
    if subcat_codes:
        candidate_idx: List[int] = []
        for code in subcat_codes:
            for idx in _subcat_index.get(code, []):
                if _row_contains_brand(_catalogue[idx], brand_tokens):
                    candidate_idx.append(idx)

        if candidate_idx:
            step1_results = _hybrid_search_indices(
                product_name,
                candidate_idx,
                top_k=top_k * 2,   # fetch extra so reranker has more to work with
                q_vec=q_vec,
            )
            logger.debug(
                "Step 1 (sub-cat+brand) for '%s': %d candidates -> %d results (codes=%s)",
                product_name, len(candidate_idx), len(step1_results), subcat_codes,
            )

    seen_codes: Set[str] = {row.get("Item Code", "") for _, row in step1_results}
    merged: List[Tuple[float, CatalogueRow]] = list(step1_results)

    if len(merged) >= FALLBACK_THRESHOLD:
        return _rerank(product_name, merged, top_k)

    # ---- Step 2: brand-only (all categories) ----------------------------
    if brand_tokens:
        brand_candidate_idx: List[int] = [
            i for i, row in enumerate(_catalogue)
            if _row_contains_brand(row, brand_tokens)
            and row.get("Item Code", "") not in seen_codes
        ]
        if brand_candidate_idx:
            brand_results = _hybrid_search_indices(
                product_name,
                brand_candidate_idx,
                top_k=top_k * 2,
                q_vec=q_vec,
            )
            logger.debug(
                "Step 2 (brand-only) for '%s': %d candidates -> %d results",
                product_name, len(brand_candidate_idx), len(brand_results),
            )
            for score, row in brand_results:
                code = row.get("Item Code", "")
                if code not in seen_codes:
                    merged.append((score, row))
                    seen_codes.add(code)
                if len(merged) >= top_k * 2:
                    break

    if len(merged) >= FALLBACK_THRESHOLD:
        merged.sort(key=lambda t: t[0], reverse=True)
        return _rerank(product_name, merged, top_k)

    # ---- Step 3: global fallback ----------------------------------------
    logger.debug(
        "Step 3 (global fallback) for '%s': only %d results so far",
        product_name, len(merged),
    )
    global_results = _hybrid_search_global(product_name, top_k=top_k * 2, q_vec=q_vec)
    for score, row in global_results:
        code = row.get("Item Code", "")
        if code not in seen_codes:
            merged.append((score, row))
            seen_codes.add(code)
        if len(merged) >= top_k * 2:
            break

    merged.sort(key=lambda t: t[0], reverse=True)
    return _rerank(product_name, merged, top_k)


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
# Public: search_item & search_item_with_scores
# ---------------------------------------------------------------------------


def search_item_with_scores(
    product_name: str,
    top_k: int = DEFAULT_TOP_K,
    q_vec: Optional[np.ndarray] = None,
) -> List[Tuple[float, CatalogueRow]]:
    """
    Find best-matching catalogue rows with calibrated confidence [0.0 - 1.0]
    for a given product name using the hybrid pipeline:
      1. Sub-category + brand filter   → BM25 + FAISS hybrid
      2. Brand-only filter             → BM25 + FAISS hybrid (fallback)
      3. Global hybrid                 → global fallback
      4. Cross-Encoder reranker        → final ordering & sigmoid confidence

    Returns:
        List of (confidence, CatalogueRow) tuples, best match first.
    """
    if not _loaded:
        load_items_db()

    clean = (product_name or "").strip()
    if not clean:
        return []

    if clean.isdigit():
        return [(1.0, r) for r in _lookup_by_code(clean)[:top_k]]

    return _priority_search(clean, top_k=top_k, q_vec=q_vec)


def search_item(product_name: str, top_k: int = DEFAULT_TOP_K) -> List[CatalogueRow]:
    """Return catalogue rows for product name (backward compatible)."""
    return [row for _, row in search_item_with_scores(product_name, top_k=top_k)]


# ---------------------------------------------------------------------------
# Public: search_catalogue
# ---------------------------------------------------------------------------


def search_catalogue(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Keyword/semantic search used by GET /catalogue/search.

    Returns a list of dicts with keys:
      sub_category_name, sub_category_code, category_name, category_code,
      item_name, item_code, confidence
    """
    clean = (query or "").strip()
    if not clean:
        return []

    if clean.isdigit():
        direct = _lookup_by_code(clean)
        if direct:
            return [_row_to_suggestion(r, confidence=1.0) for r in direct[:limit]]

    matches = search_item_with_scores(clean, top_k=limit)
    return [_row_to_suggestion(r, confidence=score) for score, r in matches]


def _row_to_suggestion(
    row: CatalogueRow, confidence: Optional[float] = None
) -> Dict[str, Any]:
    sug: Dict[str, Any] = {
        "sub_category_name":  row.get("Sub Category Name", ""),
        "sub_category_code":  row.get("Sub Category Code", ""),
        "category_name":      row.get("Category Name", ""),
        "category_code":      row.get("Category Code", ""),
        "item_name":          row.get("Item Name", ""),
        "item_code":          row.get("Item Code", ""),
    }
    if confidence is not None:
        sug["confidence"] = round(float(confidence), 4)
    return sug


# ---------------------------------------------------------------------------
# Public: enrich_product / enrich_products
# ---------------------------------------------------------------------------


def enrich_product(
    product_name: str,
    top_k: int = DEFAULT_TOP_K,
    q_vec: Optional[np.ndarray] = None,
) -> List[Tuple[float, CatalogueRow]]:
    """Return up to top_k best-matching (confidence, CatalogueRow) tuples for a product name."""
    return search_item_with_scores(product_name, top_k=top_k, q_vec=q_vec)


def enrich_products(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Enrich a list of AI-detected products with catalogue suggestions and confidence scores.

    High performance batching:
      - Encodes ALL detected product names in a single batch forward pass (~10-15ms)
      - Reuses pre-computed query vectors across hybrid steps (zero redundant encodes)
      - Employs fast-path bypass for confident matches (>=0.82)
    """
    if not _loaded:
        load_items_db()
        if not _catalogue:
            return [
                dict(p, catalogue_suggestions=[], matched=False) for p in products
            ]

    # Pre-batch vector encoding for all detected products in ONE single forward pass
    clean_names = [str(p.get("product_name", "")).strip() for p in products]
    non_empty_indices = [
        i for i, name in enumerate(clean_names) if name and not name.isdigit()
    ]

    q_vecs: Dict[int, np.ndarray] = {}
    if non_empty_indices and _embeddings is not None:
        try:
            model = _get_model()
            texts_to_encode = [
                _expand_abbreviations(clean_names[i]) for i in non_empty_indices
            ]
            batch_embs = model.encode(
                texts_to_encode,
                normalize_embeddings=True,
                batch_size=max(len(texts_to_encode), 1),
                show_progress_bar=False,
            ).astype(np.float32)
            for i_idx, emb in zip(non_empty_indices, batch_embs):
                q_vecs[i_idx] = emb
        except Exception as exc:
            logger.warning("Batch encoding failed (%s); fallback to single encoding.", exc)

    enriched: List[Dict[str, Any]] = []
    for idx, product in enumerate(products):
        p       = dict(product)
        ai_name = clean_names[idx]
        q_vec   = q_vecs.get(idx)
        matches = enrich_product(ai_name, top_k=DEFAULT_TOP_K, q_vec=q_vec)
        if matches:
            p["catalogue_suggestions"] = [
                _row_to_suggestion(row, confidence=score)
                for score, row in matches
            ]
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
        "reranker_model":       RERANKER_MODEL if ENABLE_RERANKER else "disabled",
        "bm25_enabled":         _bm25 is not None,
        "embeddings_loaded":    _embeddings is not None,
        "similarity_threshold": SIMILARITY_THRESHOLD,
        "bm25_weight":          BM25_WEIGHT,
        "faiss_weight":         FAISS_WEIGHT,
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
