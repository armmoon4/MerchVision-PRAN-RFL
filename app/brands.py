"""
app/brands.py — Dynamic PRAN Brand Registry & Classification Guardrails.

Provides:
  1. Config-driven PRAN brand portfolio registry.
  2. Auto-extension of registry from itemsdb.csv catalogue.
  3. BrandClass classification and evidence-based guardrails.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)

# Base canonical PRAN portfolio brands and sub-brands
BASE_PRAN_BRANDS: Set[str] = {
    "pran",
    "frooto",
    "drinko",
    "mr. noodles",
    "mr noodles",
    "potata",
    "bisk club",
    "biskclub",
    "pran up",
    "all time",
    "alltime",
    "mama",
    "jhatpat",
    "jhatpot",
    "wonder bakery",
    "wonder",
    "power",
    "kofi house",
    "aktive",
    "lavilla",
    "latina",
    "shero",
    "goodlife"
}

# Generic FMCG product category descriptors that should NOT identify brand on their own
GENERIC_CATEGORY_WORDS: Set[str] = {
    "wafer", "wafers", "biscuit", "biscuits", "cookie", "cookies",
    "juice", "juices", "drink", "drinks", "water", "tea", "coffee",
    "chips", "snack", "snacks", "noodle", "noodles", "cake", "cakes",
    "roll", "rolls", "oil", "sauce", "spices", "spice", "salt",
    "sugar", "flour", "rice", "cube", "cubes", "cream", "milk",
    "chocolate", "mango", "orange", "apple", "lemon", "strawberry",
    "cheese", "vanilla", "butter", "plain", "spicy", "hot", "sweet",
}

# Major known competitor brands to prevent false classification
COMPETITOR_BRANDS: Set[str] = {
    "julie", "julie's", "julies", "nabati", "jacker", "muzic", "cocopie",
    "choco pie", "chocopie", "oreo", "munchy", "munchy's", "hup seng",
    "coca-cola", "coke", "pepsi", "sprite", "fanta", "7up", "mirinda",
    "mountain dew", "sting", "maggi", "knorr", "nestle", "danone", "milo", "tiger",
}

_EXTENDED_REGISTRY: Set[str] | None = None


def build_brand_registry(csv_path: str | None = None) -> Set[str]:
    """
    Builds and caches a comprehensive PRAN brand token registry.
    Combines the static portfolio with dynamic tokens from itemsdb.csv.
    Filters out generic food category descriptors.
    """
    global _EXTENDED_REGISTRY
    if _EXTENDED_REGISTRY is not None:
        return _EXTENDED_REGISTRY

    registry = {b for b in BASE_PRAN_BRANDS if b not in GENERIC_CATEGORY_WORDS}

    target_path = csv_path or os.getenv("ITEMS_DB_CSV", "itemsdb.csv")
    csv_file = Path(target_path)
    if not csv_file.exists():
        alt_path = Path(__file__).resolve().parent.parent / "itemsdb.csv"
        if alt_path.exists():
            csv_file = alt_path

    if csv_file.exists():
        try:
            import csv
            with open(csv_file, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = row.get("Item Name") or row.get("item_name") or ""
                    if name:
                        clean_name = name.strip().lower()
                        words = [w.strip("-,.()") for w in re.split(r"\s+", clean_name) if w.strip("-,.()")]
                        if words:
                            first_one = words[0]
                            # Only add single word if >=3 chars and not a generic category word
                            if len(first_one) >= 3 and first_one not in GENERIC_CATEGORY_WORDS:
                                registry.add(first_one)
                            if len(words) >= 2:
                                first_two = f"{words[0]} {words[1]}"
                                # Add 2-word phrase if it doesn't consist solely of generic words
                                if not all(w in GENERIC_CATEGORY_WORDS for w in [words[0], words[1]]):
                                    registry.add(first_two)
            logger.info("Brand registry initialized with %d dynamic PRAN brand tokens from %s", len(registry), csv_file)
        except Exception as e:
            logger.warning("Could not auto-extend brand registry from CSV (%s): %s", csv_file, e)

    _EXTENDED_REGISTRY = registry
    return _EXTENDED_REGISTRY


def is_known_pran_token(text: str) -> bool:
    """Check if the provided text matches or contains any known PRAN brand token."""
    if not text:
        return False
    norm = text.strip().lower()

    # If explicit competitor brand is in the name, it is NOT PRAN
    for comp in COMPETITOR_BRANDS:
        if re.search(rf"\b{re.escape(comp)}\b", norm):
            return False

    reg = build_brand_registry()
    if norm in reg:
        return True
    # Word boundary match
    for token in reg:
        if len(token) >= 3 and re.search(rf"\b{re.escape(token)}\b", norm):
            return True
    return False
