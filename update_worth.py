#!/usr/bin/env python3
"""
Update worth.yml with the lowest current DonutSMP auction price per single
item, read from donut.build's auction list (https://www.donut.build/auction).

Price data source: donut.build (please credit it if you share the results).

How it works
  * Walks the paginated list (?page=1, 2, 3, ... ~24 items per page, about
    60 requests total) until a page has no items.
  * For every item, price = the per-unit price shown on the card. When the
    card also shows a 64x price, that is divided by 64 for extra precision
    (cheap items are rounded to whole dollars on the card).
  * Only items already in your worth.yml TYPE section are changed, matched by
    ID (IRON_NUGGET <-> iron_nugget). Anything containing SPAWNER is skipped.
  * Items not found on the site keep their old value and are listed in
    worth_report.csv (status "not_listed").
  * Only the numbers are rewritten, line by line - comments and layout stay.

Usage
  pip install requests beautifulsoup4
  python update_worth.py worth.yml --limit 24 --dry-run   # quick test, changes nothing
  python update_worth.py worth.yml                        # full run (~1-2 min)

Flags kept for the GitHub workflow: --refresh (ignored), --limit N (test mode:
only fetch the first N/24 pages), --dry-run.
"""
import argparse
import csv
import re
import shutil
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.donut.build/auction"
UA = "worth-yml-updater/2.0 (personal use; prices from donut.build)"
PER_PAGE = 24
MULT = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}

KEY_LINE_RE = re.compile(r"^(\s{4})([A-Z0-9_]+):\s*([0-9.eE+-]+)\s*$")
# "Iron Nugget $1,228(64× $78,611) -73.0% 7 days"
CARD_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([KMBT]?)\s*"
    r"(?:\(\s*(\d+)\s*[×xX]\s*\$\s*([\d,]+(?:\.\d+)?)\s*([KMBT]?)\s*\))?",
)
ID_RE = re.compile(r"/items/([a-z0-9_]+)")
LAST_ERROR = {"msg": ""}


def money(num: str, suffix: str) -> float:
    return float(num.replace(",", "")) * MULT[suffix.upper()]


def parse_cards(html: str) -> dict:
    """Return {item_id: price_per_unit} found on one page."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        m = ID_RE.search(a["href"])
        if not m:
            continue
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        c = CARD_RE.search(text)
        if not c:
            continue
        unit = money(c.group(1), c.group(2))
        if c.group(3) and c.group(4):  # stack price / stack size = more precise
            qty = int(c.group(3))
            if qty > 0:
                unit = money(c.group(4), c.group(5)) / qty
        item = m.group(1)
        if item not in out or unit < out[item]:
            out[item] = unit
    return out


def get_page(session, base_url, page, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(base_url, params={"page": page}, timeout=25)
            if r.status_code == 429:
                LAST_ERROR["msg"] = f"HTTP 429 (too many requests) on page {page}"
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code >= 400:
                LAST_ERROR["msg"] = f"HTTP {r.status_code} on page {page}"
                time.sleep(2 * (attempt + 1))
                continue
            return r.content.decode("utf-8", errors="replace")
        except requests.RequestException as e:
            LAST_ERROR["msg"] = f"{type(e).__name__}: {e}"
            time.sleep(2 * (attempt + 1))
    return None


def fmt(v: float) -> str:
    if v >= 1:
        s = f"{v:.2f}".rstrip("0")
        return s + "0" if s.endswith(".") else s
    return f"{v:.4f}".rstrip("0").rstrip(".") or "0.0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("worth_yml")
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between page requests")
    ap.add_argument("--limit", type=int, default=0, help="test mode: only fetch first N/24 pages")
    ap.add_argument("--max-pages", type=int, default=200, help="safety stop")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="(ignored, kept for compatibility)")
    args = ap.parse_args()

    path = Path(args.worth_yml)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    in_type, targets = False, []
    for i, line in enumerate(lines):
        if re.match(r"^TYPE:\s*$", line):
            in_type = True
            continue
        if in_type and re.match(r"^[A-Za-z]", line):
            break
        if in_type:
            m = KEY_LINE_RE.match(line.rstrip("\n"))
            if m and "SPAWNER" not in m.group(2):
                targets.append((i, m.group(2), float(m.group(3))))
    print(f"{len(targets)} items in worth.yml (spawners skipped).")

    max_pages = args.max_pages
    if args.limit:
        max_pages = min(max_pages, args.limit // PER_PAGE + 1)

    session = requests.Session()
    session.headers["User-Agent"] = UA
    prices, empty_streak = {}, 0
    for page in range(1, max_pages + 1):
        html = get_page(session, args.base_url, page)
        if html is None:
            if page == 1:
                print(f"STOPPED: could not load page 1 - {LAST_ERROR['msg']}")
                print("The site may be blocking this machine/network. Nothing was changed.")
                sys.exit(1)
            print(f"  page {page}: FAILED - {LAST_ERROR['msg']} (stopping here)")
            break
        found = parse_cards(html)
        if not found:
            empty_streak += 1
            if page == 1:
                print("STOPPED: page 1 loaded but no items were recognised, so the page "
                      "layout isn't what this script expects. Nothing was changed.")
                sys.exit(1)
            if empty_streak >= 1:
                print(f"  page {page}: no items - reached the end.")
                break
        for k, v in found.items():
            if k not in prices or v < prices[k]:
                prices[k] = v
        print(f"[page {page}] {len(found)} items ({len(prices)} total)")
        time.sleep(args.delay)

    changed, report = 0, []
    for i, key, old in targets:
        new = prices.get(key.lower())
        if new is not None:
            lines[i] = KEY_LINE_RE.sub(
                lambda m: f"{m.group(1)}{m.group(2)}: {fmt(new)}",
                lines[i].rstrip("\n"),
            ) + "\n"
            report.append((key, old, new, "updated"))
            changed += 1
        else:
            report.append((key, old, old, "not_listed"))

    with open("worth_report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item", "old", "new", "status"])
        w.writerows(report)

    if args.dry_run:
        print(f"Dry run: {changed} would change, {len(targets)-changed} not listed. "
              "See worth_report.csv")
        return
    shutil.copy(path, path.with_suffix(".yml.bak"))
    path.write_text("".join(lines), encoding="utf-8")
    print(f"Updated {changed} items, kept old value for {len(targets)-changed}. "
          f"Backup: {path.with_suffix('.yml.bak')}  Report: worth_report.csv")


if __name__ == "__main__":
    sys.exit(main())
