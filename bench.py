"""Read-path load numbers for this repo only. Stdlib, GETs only.

Stack up first (deploy/docker-compose.yml), then:  python bench.py [n] [workers]
"""

import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://localhost:8001"


def hammer(name: str, url: str, n: int, workers: int) -> None:
    def one(_: int) -> float:
        t0 = time.perf_counter()
        with urllib.request.urlopen(url, timeout=20) as response:
            response.read()
        return (time.perf_counter() - t0) * 1000

    with ThreadPoolExecutor(max_workers=workers) as pool:
        lat = sorted(pool.map(one, range(n)))
    print(
        f"{name}: n={n} min={lat[0]:.1f}ms p50={statistics.median(lat):.1f}ms "
        f"p95={lat[int(0.95 * n) - 1]:.1f}ms max={lat[-1]:.1f}ms"
    )


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    hammer("GET /v1/budget", f"{BASE}/v1/budget", n, workers)
