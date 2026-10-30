#!/usr/bin/env python3
"""
Update worth.yml with the LOWEST current DonutSMP auction price per unit,
using donutstats.net item pages (https://www.donutstats.net/prices/<item_id>).

Price data source: donutstats.net (please keep attribution if you share results).

How it works
  * Reads every item key under TYPE: in worth.yml (ACACIA_LOG -> acacia_log).
  * Skips any key containing SPAWNER (you said you'd handle those yourself).
  * Fetches each item's page, reads the "Cheapest live ... listings" section,
    and takes the lowest (listing price / stack size) = lowest price per unit.
  * Rewrites ONLY the numeric values, line by line, so comments and layout
    stay exactly as they were. Items with no live listing / no page keep the
    old value and are listed in the report.
  * Progress is cached in price_cache.json, so you can stop (Ctrl+C) and
    re-run to resume without re-fetching.

Usage
  pip install requests beautifulsoup4
  python update_worth.py worth.yml --limit 20 --dry-run     # quick test first
  python update_worth.py worth.yml                          # full run (~3-4 min)
  python update_worth.py worth.yml --refresh                # ignore cache
"""
import argparse
import csv
import json
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.donutstats.net/prices/"
UA = "worth-yml-updater/1.0 (personal use; prices from donutstats.net)"
MULT = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}

# "x1 - sold by modernmeistro $200K"  (after whitespace is collapsed)
LISTING_RE = re.compile(
    r"x\s*([\d,]+)\s*-\s*sold by\s+\S+\s+\$\s*([\d.,]+)\s*([KMBT]?)", re.I
)
KEY_LINE_RE = re.compile(r"^(\s{4})([A-Z0-9_]+):\s*([0-9.eE+-]+)\s*$")


def parse_money(num: str, suffix: str) -> float:
    return float(num.replace(",", "")) * MULT[suffix.upper()]


def lowest_unit_price(html: str):
    """Return the lowest per-unit live listing price, or None."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    start = re.search(r"Cheapest live .*? listings", text, re.I)
    if not start:
        return None
    section = text[start.end():]
    end = re.search(r"Recent .*? sales|More DonutSMP item prices", section, re.I)
    if end:
        section = section[: end.start()]
    best = None
    for qty, num, suf in LISTING_RE.findall(section):
        qty = int(qty.replace(",", ""))
        if qty <= 0:
            continue
        unit = parse_money(num, suf) / qty
        if best is None or unit < best:
            best = unit
    return best


def fetch(session, item_id, base_url, retries=3):
    url = f"{base_url}{item_id}"
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 404:
                return "404"
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return None


def fmt(v: float) -> str:
    # keep the file's "51.0" style; avoid scientific notation
    if v >= 1:
        s = f"{v:.2f}".rstrip("0")
        return s + "0" if s.endswith(".") else s
    return f"{v:.4f}".rstrip("0").rstrip(".") or "0.0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("worth_yml")
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--rps", type=float, default=8.0, help="max requests per second (all workers combined)")
    ap.add_argument("--workers", type=int, default=8, help="parallel downloads")
    ap.add_argument("--limit", type=int, default=0, help="only first N items (testing)")
    ap.add_argument("--dry-run", action="store_true", help="don't write worth.yml")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--cache", default="price_cache.json")
    args = ap.parse_args()

    path = Path(args.worth_yml)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    # locate the TYPE: section
    in_type, targets = False, []
    for i, line in enumerate(lines):
        if re.match(r"^TYPE:\s*$", line):
            in_type = True
            continue
        if in_type and re.match(r"^[A-Za-z]", line):  # next top-level key
            break
        if in_type:
            m = KEY_LINE_RE.match(line.rstrip("\n"))
            if m:
                targets.append((i, m.group(2), float(m.group(3))))

    targets = [t for t in targets if "SPAWNER" not in t[1]]
    if args.limit:
        targets = targets[: args.limit]
    print(f"{len(targets)} items to price (spawners skipped).")

    cache_path = Path(args.cache)
    cache = {}
    if cache_path.exists() and not args.refresh:
        cache = json.loads(cache_path.read_text())

    lock = threading.Lock()
    gate = threading.Lock()
    next_slot = [time.monotonic()]
    local = threading.local()

    def throttle():
        with gate:
            now = time.monotonic()
            wait = next_slot[0] - now
            next_slot[0] = max(now, next_slot[0]) + 1.0 / args.rps
        if wait > 0:
            time.sleep(wait)

    def work(key):
        if not hasattr(local, "s"):
            local.s = requests.Session()
            local.s.headers["User-Agent"] = UA
        throttle()
        html = fetch(local.s, key.lower(), args.base_url)
        if html == "404":
            return key, {"status": "no_page"}
        if html is None:
            return key, None
        price = lowest_unit_price(html)
        return key, ({"status": "ok", "price": price} if price is not None
                     else {"status": "no_listings"})

    todo = [k for _, k, _ in targets if k not in cache]
    done = 0
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(work, k) for k in todo]
            for fut in as_completed(futs):
                key, res = fut.result()
                done += 1
                if res is None:
                    print(f"  {key}: network error, will retry next run")
                else:
                    with lock:
                        cache[key] = res
                if done % 50 == 0 or done == len(todo):
                    cache_path.write_text(json.dumps(cache))
                    print(f"[{done}/{len(todo)}] {time.time()-t0:.0f}s elapsed")
    except KeyboardInterrupt:
        print("\nInterrupted - progress saved, re-run to resume.")
    finally:
        cache_path.write_text(json.dumps(cache))

    # apply
    changed, report = 0, []
    for i, key, old in targets:
        c = cache.get(key)
        if c and c.get("status") == "ok":
            new = c["price"]
            lines[i] = KEY_LINE_RE.sub(
                lambda m: f"{m.group(1)}{m.group(2)}: {fmt(new)}",
                lines[i].rstrip("\n"),
            ) + "\n"
            report.append((key, old, new, "updated"))
            changed += 1
        else:
            report.append((key, old, old, (c or {}).get("status", "not_fetched")))

    with open("worth_report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item", "old", "new", "status"])
        w.writerows(report)

    if args.dry_run:
        print(f"Dry run: {changed} would change. See worth_report.csv")
        return
    shutil.copy(path, path.with_suffix(".yml.bak"))
    path.write_text("".join(lines), encoding="utf-8")
    kept = len(targets) - changed
    print(f"Updated {changed} items, kept old value for {kept}. "
          f"Backup: {path.with_suffix('.yml.bak')}  Report: worth_report.csv")


if __name__ == "__main__":
    sys.exit(main())
