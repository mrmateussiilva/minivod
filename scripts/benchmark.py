#!/usr/bin/env python3
"""Small, non-destructive HTTP benchmark for a running MiniVOD instance."""

from __future__ import annotations

import argparse
import getpass
import json
import time
from urllib.parse import urlencode
from urllib.request import urlopen


def request(url: str) -> tuple[int, float, bytes]:
    started = time.perf_counter()
    with urlopen(url, timeout=15) as response:
        return response.status, time.perf_counter() - started, response.read()


def endpoint_url(base_url: str, path: str, **params: str) -> str:
    query = urlencode(params)
    return f"{base_url.rstrip('/')}{path}" + (f"?{query}" if query else "")


def measure(label: str, url: str, repetitions: int) -> bytes | None:
    timings: list[float] = []
    body: bytes | None = None
    for _ in range(repetitions):
        status, elapsed, body = request(url)
        if status != 200:
            raise RuntimeError(f"{label}: HTTP {status}")
        timings.append(elapsed)
    print(
        f"{label}: {repetitions} requests, "
        f"total={sum(timings):.3f}s, average={sum(timings) / repetitions * 1000:.1f}ms"
    )
    return body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="ex.: http://127.0.0.1:8079")
    parser.add_argument("--username", help="usuário Xtream para endpoints autenticados")
    parser.add_argument("--password", help="senha Xtream; omitida = pergunta no terminal")
    parser.add_argument("--category-id", help="categoria VOD para medir; padrão = primeira")
    parser.add_argument("--requests", type=int, default=3, help="repetições por endpoint")
    args = parser.parse_args()

    repetitions = max(1, min(args.requests, 20))
    measure("health", endpoint_url(args.base_url, "/health"), repetitions)

    if not args.username:
        return

    password = args.password or getpass.getpass("Senha Xtream: ")
    common = {"username": args.username, "password": password}
    measure("player_api auth", endpoint_url(args.base_url, "/player_api.php", **common), repetitions)
    categories_body = measure(
        "vod categories",
        endpoint_url(args.base_url, "/player_api.php", action="get_vod_categories", **common),
        repetitions,
    )
    categories = json.loads(categories_body or b"[]")
    category_id = args.category_id or (str(categories[0]["category_id"]) if categories else None)
    if category_id:
        measure(
            f"vod streams category {category_id}",
            endpoint_url(
                args.base_url,
                "/player_api.php",
                action="get_vod_streams",
                category_id=category_id,
                **common,
            ),
            repetitions,
        )
        cover = next((row.get("cover") for row in categories if str(row.get("category_id")) == category_id), "")
        if cover:
            measure("collection cover", cover, repetitions)


if __name__ == "__main__":
    main()
