#!/usr/bin/env python3
"""
scripts/build_embeddings.py — Pre-compute product embeddings from itemsdb.csv.

Run this script ONCE (or after updating itemsdb.csv) to generate:
  - embeddings/product_embeddings.npy
  - embeddings/products_meta.json

Usage (from project root):
    python scripts/build_embeddings.py
    python scripts/build_embeddings.py --csv path/to/itemsdb.csv
    python scripts/build_embeddings.py --model all-mpnet-base-v2
    python scripts/build_embeddings.py --threshold 0.40

This script is also run automatically inside the Docker image at build time
via the Dockerfile CMD so embeddings are ready before the first request.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("build_embeddings")

# ---------------------------------------------------------------------------
# Defaults (mirrors items_db_service.py)
# ---------------------------------------------------------------------------

DEFAULT_CSV      = Path("itemsdb.csv")
DEFAULT_OUT_DIR  = Path("embeddings")
DEFAULT_MODEL    = "all-MiniLM-L6-v2"
DEFAULT_THRESHOLD = 0.30
BATCH_SIZE       = 64

# ---------------------------------------------------------------------------
# Abbreviation expansion (must stay in sync with items_db_service.py)
# ---------------------------------------------------------------------------

_ABBR_MAP = {
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


def _expand(text: str) -> str:
    t = text.lower()
    t = re.sub(r"(\d+)\s*ml\b",   r"\1ml",  t)
    t = re.sub(r"(\d+)\s*gm?\b",  r"\1gm",  t)
    t = re.sub(r"(\d+)\s*kg\b",   r"\1kg",  t)
    t = re.sub(r"(\d+)\s*ltr?\b", r"\1ltr", t)
    for abbr, full in _ABBR_MAP.items():
        t = re.sub(rf"\b{re.escape(abbr)}\b", full, t)
    return t


def _build_search_text(row: dict) -> str:
    item = _expand(row.get("Item Name", ""))
    sub  = row.get("Sub Category Name", "")
    cat  = row.get("Category Name", "")
    parts = [item]
    if sub:
        parts.append(sub)
    if cat:
        parts.append(cat)
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build(csv_path: Path, out_dir: Path, model_name: str) -> None:
    """Read CSV, encode all rows, save embeddings + metadata."""
    if not csv_path.is_file():
        logger.error("CSV not found: %s", csv_path)
        sys.exit(1)

    # 1. Load CSV
    logger.info("Reading catalogue: %s", csv_path)
    rows = []
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

    logger.info("Loaded %d products.", len(rows))

    # 2. Build text corpus
    texts = [_build_search_text(r) for r in rows]

    # 3. Load model and encode
    logger.info("Loading model: %s", model_name)
    model = SentenceTransformer(model_name)

    logger.info("Encoding %d products (batch_size=%d) ...", len(texts), BATCH_SIZE)
    t0 = time.time()
    embeddings: np.ndarray = model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=True,
        batch_size=BATCH_SIZE,
    ).astype(np.float32)
    elapsed = time.time() - t0
    logger.info(
        "Done! Embedding shape: %s. Time: %.1f s (%.0f products/s)",
        embeddings.shape, elapsed, len(rows) / max(elapsed, 0.001),
    )

    # 4. Save artefacts
    out_dir.mkdir(parents=True, exist_ok=True)
    emb_file  = out_dir / "product_embeddings.npy"
    meta_file = out_dir / "products_meta.json"

    np.save(str(emb_file), embeddings)
    logger.info("Saved embeddings  -> %s", emb_file)

    meta = [
        {
            "item_name":         r.get("Item Name", ""),
            "item_code":         r.get("Item Code", ""),
            "sub_category_name": r.get("Sub Category Name", ""),
            "sub_category_code": r.get("Sub Category Code", ""),
            "category_name":     r.get("Category Name", ""),
            "category_code":     r.get("Category Code", ""),
        }
        for r in rows
    ]
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Saved metadata    -> %s", meta_file)

    # 5. Quick sanity check
    logger.info("\n--- Quick sanity check ---")
    test_queries = [
        "Pran Lassi",
        "Bisk Club cream biscuit orange",
        "Frooto mango juice 250ml",
        "Mr Noodles chicken cup",
    ]
    for q in test_queries:
        q_vec = model.encode(_expand(q), normalize_embeddings=True).astype(np.float32)
        scores = embeddings @ q_vec
        top_idx = int(np.argmax(scores))
        top_score = float(scores[top_idx])
        top_name  = rows[top_idx].get("Item Name", "?")
        logger.info("  Query: %-40s -> [%.3f] %s", f'"{q}"', top_score, top_name)

    logger.info("\nBuild complete. Ready for use.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-compute catalogue embeddings for PRAN-RFL semantic search."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help=f"Path to itemsdb.csv (default: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Output directory for embeddings (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help=f"SentenceTransformer model (default: {DEFAULT_MODEL})",
    )
    args = parser.parse_args()
    build(args.csv, args.out_dir, args.model)


if __name__ == "__main__":
    main()
