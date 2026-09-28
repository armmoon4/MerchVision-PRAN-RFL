# ════════════════════════════════════════════════════════════════════════════
# Dockerfile — PRAN-RFL Rack Recognition System
#
# Multi-stage aware build:
#   Stage 1: Install deps (cached layer)
#   Stage 2: Copy app code
#   Stage 3: Pre-build product embeddings (runs build_embeddings.py once)
#            -> saves embeddings/product_embeddings.npy + products_meta.json
#            -> these files are baked INTO the image, so no model download
#               or CPU burn is needed on the first request.
#
# To rebuild (e.g. after updating itemsdb.csv):
#   docker compose build --no-cache backend
# ════════════════════════════════════════════════════════════════════════════

FROM python:3.13-slim

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
# Install in a separate layer so code changes don't invalidate the dep cache.
COPY requirements.txt .

# Install PyTorch CPU-only wheel first (much smaller than the default CUDA build).
# This avoids pulling multi-GB CUDA libraries that are useless on a CPU-only server.
RUN pip install --no-cache-dir \
        torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# ── Application source ────────────────────────────────────────────────────────
COPY . .

# Ensure required directories exist
RUN mkdir -p media/uploads embeddings scripts

# ── Pre-compute product embeddings at image build time ────────────────────────
# The model (~22 MB) is downloaded from HuggingFace Hub during 'docker build'.
# The resulting .npy and .json files are baked into the image layer, so
# the container starts instantly without any embedding work on first request.
#
# If ITEMS_DB_CSV is overridden at runtime, mount a new embeddings/ volume
# and run:   docker exec <container> python scripts/build_embeddings.py
RUN python scripts/build_embeddings.py \
        --csv itemsdb.csv \
        --out-dir embeddings \
        --model all-MiniLM-L6-v2

# ── Runtime ───────────────────────────────────────────────────────────────────
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
