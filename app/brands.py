"""
app/brands.py — Dynamic PRAN Brand Registry & Classification Guardrails.

Provides:
  1. Config-driven PRAN brand portfolio registry including Bisk Club and FMCG lines.
  2. Dynamic auto-extension of registry from itemsdb.csv with strict generic-word filtering.
  3. Comprehensive competitor brand registry preventing false-positive PRAN detections.
  4. BrandClass classification and evidence-based guardrails.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)

# Base canonical PRAN portfolio brands, sub-brands, and FMCG product lines
BASE_PRAN_BRANDS: Set[str] = {
    "pran",
    "bisk club",
    "biskclub",
    "mama cube",
    "europa",
    "shero",
    "potata",
    "frooto",
    "drinko",
    "mr. noodles",
    "mr noodles",
    "mr. noodle",
    "mr noodle",
    "pran up",
    "pranup",
    "all time",
    "alltime",
    "jhatpat",
    "jhatpot",
    "wonder bakery",
    "wonder",
    "power",
    "kofi house",
    "aktive",
    "lavilla",
    "lavila",
    "latina",
    "goodlife",
    "cheer up",
    "cheerup",
    "maxx",
    "veve",
    "canton",
    "current",
    "badam vaja",
    "dal vaja",
    "chanachur",
    "pran lassi",
}

# Generic FMCG product category and descriptor words that MUST NOT identify brand on their own
GENERIC_CATEGORY_WORDS: Set[str] = {
    "wafer", "wafers", "biscuit", "biscuits", "cookie", "cookies", "cracker", "crackers",
    "juice", "juices", "drink", "drinks", "water", "tea", "coffee", "beverage", "beverages",
    "chips", "snack", "snacks", "noodle", "noodles", "cake", "cakes", "rusk", "toast",
    "roll", "rolls", "oil", "sauce", "spices", "spice", "salt", "sugar", "flour", "rice",
    "cube", "cubes", "cream", "creams", "milk", "chocolate", "choco", "mango", "orange",
    "apple", "lemon", "strawberry", "pineapple", "pomegranate", "cheese", "vanilla", "butter",
    "peanut", "plain", "spicy", "hot", "sweet", "chilli", "chili", "mustard", "dry", "super",
    "daily", "red", "green", "yellow", "white", "black", "gold", "premium", "classic",
    "family", "party", "cup", "pack", "packet", "jar", "tin", "can", "mini", "jumbo",
    "maxi", "regular", "original", "flavour", "flavor", "flavoured", "flavored", "powder",
    "paste", "pure", "crispy", "special", "instant", "mix", "plus", "extra", "natural",
    "taste", "crunchy", "crisp", "combo", "box", "piece", "pieces", "pcs", "gm", "g", "ml", "kg", "ltr",
    "ghee", "semolina", "suji", "semai", "dal", "lentil", "lentils", "atta", "maida", "besan",
    "pickle", "pickles", "chutney", "jam", "jelly", "vinegar", "vermicelli", "laccha",
    "soybean", "sunflower", "turmeric", "cumin", "coriander", "pepper", "candy", "lollipop",
}

# Major known competitor brands to prevent false-positive PRAN classification
COMPETITOR_BRANDS: Set[str] = {
    # Biscuits, wafers & confectionery
    "julie", "julie's", "julies", "nabati", "jacker", "muzic", "cocopie", "choco pie",
    "chocopie", "oreo", "munchy", "munchy's", "munchys", "hup seng", "hupseng", "roma",
    "olympic", "lexus", "chipsmore", "chips more", "tiger", "kraft", "good day", "goodday",
    "britannia", "parle", "sunfeast", "cadbury", "kitkat", "kit kat", "toblerone",
    # Instant noodles & foods
    "maggi", "knorr", "indomie", "samyang", "nissin", "mama noodles", "mama instant",
    "danish", "fresh", "acme", "aci", "igloo", "bashundhara", "teer", "rupchanda",
    # Dairy & beverages
    "nestle", "danone", "milo", "horlicks", "coca-cola", "coca cola", "coke", "pepsi",
    "sprite", "fanta", "7up", "mirinda", "mountain dew", "sting", "twister", "tropicana",
    "v-soy", "vsoy", "yeo's", "yeos", "pokka", "marigold", "f&n", "ribena", "tang",
    "red bull", "shark", "monster", "heineken", "carlsberg", "tiger beer",
}

_EXTENDED_REGISTRY: Set[str] | None = None


def build_brand_registry(csv_path: str | None = None) -> Set[str]:
    """
    Builds and caches a comprehensive PRAN brand token registry.
    Combines the static portfolio with valid dynamic tokens from itemsdb.csv.
    Strictly filters out generic food category descriptors and competitor tokens.
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
                    # 1. Inspect Sub Category Name for explicit sub-brands (e.g. 015-DRINKO, 060-Frooto, 272-Bisc-Cream)
                    subcat = (row.get("Sub Category Name") or row.get("sub_category_name") or "").strip().lower()
                    if subcat:
                        parts = re.split(r"[-_/,\s]+", subcat)
                        for part in parts:
                            part_clean = part.strip()
                            if (
                                len(part_clean) >= 4
                                and part_clean not in GENERIC_CATEGORY_WORDS
                                and not any(comp in part_clean for comp in COMPETITOR_BRANDS)
                                and not part_clean.isdigit()
                            ):
                                registry.add(part_clean)

                    # 2. Inspect Item Name prefixes
                    name = row.get("Item Name") or row.get("item_name") or ""
                    if name:
                        clean_name = name.strip().lower()
                        words = [w.strip("-,.()") for w in re.split(r"\s+", clean_name) if w.strip("-,.()")]
                        if words:
                            first_one = words[0]
                            # Only add single word if known brand or >=4 chars and not a generic word/competitor
                            if (
                                len(first_one) >= 4
                                and first_one not in GENERIC_CATEGORY_WORDS
                                and not any(comp in first_one for comp in COMPETITOR_BRANDS)
                                and not first_one.isdigit()
                            ):
                                # Must start with known brand or be in a PRAN-owned line
                                if any(b in first_one for b in ["pran", "bisk", "froot", "drink", "potat", "mama", "shero", "europ", "noodl"]):
                                    registry.add(first_one)
                            if len(words) >= 2:
                                first_two = f"{words[0]} {words[1]}"
                                if (
                                    not any(w in GENERIC_CATEGORY_WORDS for w in [words[0], words[1]])
                                    and not any(comp in first_two for comp in COMPETITOR_BRANDS)
                                ):
                                    if any(b in first_two for b in ["bisk club", "mama cube", "mr noodle", "pran up", "all time"]):
                                        registry.add(first_two)

            logger.info("Brand registry initialized with %d dynamic PRAN brand tokens from %s", len(registry), csv_file)
        except Exception as e:
            logger.warning("Could not auto-extend brand registry from CSV (%s): %s", csv_file, e)

    _EXTENDED_REGISTRY = registry
    return _EXTENDED_REGISTRY


def is_competitor_brand(text: str) -> bool:
    """Return True if text explicitly matches or contains any known competitor brand."""
    if not text:
        return False
    norm = text.strip().lower()
    for comp in COMPETITOR_BRANDS:
        if re.search(rf"\b{re.escape(comp)}\b", norm):
            return True
    return False


def is_known_pran_token(text: str) -> bool:
    """Check if the provided text matches or contains any known PRAN brand token."""
    if not text:
        return False
    norm = text.strip().lower()

    # If explicit competitor brand is in the name, it is NOT PRAN
    if is_competitor_brand(norm):
        return False

    reg = build_brand_registry()
    if norm in reg:
        return True

    # Word boundary match against PRAN brands
    for token in reg:
        if len(token) >= 3 and re.search(rf"\b{re.escape(token)}\b", norm):
            return True

    return False
