#!/usr/bin/env python3
"""
scripts/benchmark_runner.py — Comprehensive Benchmark Suite
Measures:
1. Fresh inference runs (testing newly processed photos with 768px downscaling and pooled connections)
2. Cached duplicate runs (measuring LRU exact image hash cache speedup & 100% token savings)
3. TTFT (Time to First Token) on OpenRouter streaming
4. End-to-end latency and stage breakdowns
"""
import json
import time
import urllib.request
import urllib.error
import os

FRESH_CASES = [
    {
        "id": "Fresh-1",
        "name": "Drinko Float Litchi 330ml",
        "url": "https://prgspro.sgp1.cdn.digitaloceanspaces.com/mal/Master/item/6a3cc4bea54201.jfif",
    },
    {
        "id": "Fresh-2",
        "name": "Drinko Float Pineapple 330ml",
        "url": "https://prgspro.sgp1.cdn.digitaloceanspaces.com/mal/Master/item/6a3cc54b39a721.jpg",
    },
    {
        "id": "Fresh-3",
        "name": "Drinko Float Strawberry 330ml",
        "url": "https://prgspro.sgp1.cdn.digitaloceanspaces.com/mal/Master/item/6a3cc50cc894d1.jfif",
    },
]

def run_benchmarks(base_url="http://localhost:8000"):
    print("=" * 75)
    print(" 1. FRESH INFERENCE BENCHMARK (Uncached Photos, 768px Tiling, Pooled TLS)")
    print("=" * 75)
    fresh_metrics = []
    
    for case in FRESH_CASES:
        payload = json.dumps({"image_url": case["url"]}).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/analyze",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=60) as resp:
            dur = time.perf_counter() - t0
            body = json.loads(resp.read().decode("utf-8"))
        
        u = body.get("token_usage", {})
        in_t = u.get("input_tokens", 0)
        out_t = u.get("output_tokens", 0)
        tot_t = u.get("total_tokens", 0)
        cost = u.get("estimated_cost_usd", 0.0)
        cached = u.get("cached", False)
        products = body.get("detected_products", [])
        top_sku = products[0].get("product_name", "N/A") if products else "N/A"
        match_conf = products[0].get("catalogue_suggestions", [{}])[0].get("confidence", 0.0) if products else 0.0

        print(f"[{case['id']}] {case['name']}")
        print(f"  Fresh Call: Latency={dur:.3f}s | In={in_t} | Out={out_t} | Total={tot_t} | Cost=${cost:.6f} | Match={top_sku} ({match_conf:.2f})")
        fresh_metrics.append({
            "id": case["id"],
            "name": case["name"],
            "latency": dur,
            "input_tokens": in_t,
            "output_tokens": out_t,
            "total_tokens": tot_t,
            "cost": cost,
            "cached": cached,
        })
        
        # Immediate second call to measure cache hit speedup
        t0 = time.perf_counter()
        req2 = urllib.request.Request(
            f"{base_url}/analyze",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req2, timeout=60) as resp:
            dur_cache = time.perf_counter() - t0
            body_cache = json.loads(resp.read().decode("utf-8"))
        u_c = body_cache.get("token_usage", {})
        print(f"  Cached Call: Latency={dur_cache:.3f}s | In={u_c.get('input_tokens')} | Out={u_c.get('output_tokens')} | Total={u_c.get('total_tokens')} | Cost=${u_c.get('estimated_cost_usd'):.6f} | Cached={u_c.get('cached')}\n")

    print("=" * 75)
    print(" 2. TTFT (TIME TO FIRST TOKEN) STREAMING MEASUREMENT")
    print("=" * 75)
    from openai import OpenAI
    api_key = os.getenv("OPENROUTER_API_KEY", "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")
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
    
    test_prompts = [
        "What is PRAN Frooto?",
        "Name 3 PRAN drinks:"
    ]
    for p in test_prompts:
        t0 = time.perf_counter()
        stream = client.chat.completions.create(
            model="google/gemini-3.7-flash",
            messages=[{"role": "user", "content": p}],
            extra_body={"reasoning": {"effort": "low"}},
            max_tokens=80,
            stream=True
        )
        ttft = None
        full = ""
        for chunk in stream:
            if ttft is None and chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                ttft = time.perf_counter() - t0
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                full += chunk.choices[0].delta.content
        tot_time = time.perf_counter() - t0
        ttft_str = f"{ttft:.3f}s" if ttft is not None else "N/A"
        print(f"Prompt: '{p}'")
        print(f"  TTFT: {ttft_str} | Total Latency: {tot_time:.3f}s | Response: {full[:50].strip()}...\n")

if __name__ == "__main__":
    run_benchmarks()
