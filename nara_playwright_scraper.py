#!/usr/bin/env python3
"""
NARA Catalog Playwright scraper.

How it works
------------
The catalog is a React SPA.  When it loads a record page it fires XHR requests
to its own backend API.  We intercept ONLY those JSON API responses and pull
every `fileUrl` field out of them.  We never touch CSS / JS / fonts / images
that belong to the web-app itself.

After page load we also look for child-record links (the catalog may list many
child items under a parent) and repeat for each one.

Install
-------
    pip install playwright requests
    playwright install chromium

Usage
-----
    python nara_playwright_scraper.py
    python nara_playwright_scraper.py --url https://catalog.archives.gov/id/12044361
    python nara_playwright_scraper.py --url https://catalog.archives.gov/id/12044361 --out my_folder
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ──────────────────────────────────────────────────────────────────
DEFAULT_URL    = "https://catalog.archives.gov/id/12044361"
DEFAULT_OUTDIR = "downloads"

PAGE_TIMEOUT   = 90_000   # ms to wait for page load
IDLE_WAIT      = 3_000    # ms to wait after page settles
MAX_PAGES      = 500      # pagination click safety limit
# ────────────────────────────────────────────────────────────────────────────

# Only inspect responses from the catalog's own API routes
API_URL_RE = re.compile(r"catalog\.archives\.gov/api/", re.IGNORECASE)

# A real archival fileUrl always contains one of these path segments
MEDIA_PATH_RE = re.compile(
    r"(catalogmedia|arcmedia|/lz/\d+|nara-media|content/arcmedia)",
    re.IGNORECASE,
)

# Child-record page pattern
CHILD_PAGE_RE = re.compile(r"https://catalog\.archives\.gov/id/(\d+)", re.IGNORECASE)


def extract_file_urls(data: dict | list, seen: set) -> list[dict]:
    """
    Recursively walk any JSON value and collect objects that have a fileUrl
    pointing at real archival media (not page assets).
    """
    results = []

    def walk(obj):
        if isinstance(obj, dict):
            url = obj.get("fileUrl", "")
            if url and url not in seen and MEDIA_PATH_RE.search(url):
                seen.add(url)
                results.append({
                    "url":  url,
                    "name": obj.get("fileName", ""),
                    "mime": obj.get("mimeType", ""),
                })
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return results


def scrape_record(page, url: str, seen_urls: set, seen_pages: set) -> tuple[list, list]:
    """Load one record page, intercept API JSON, return (files, child_urls)."""
    if url in seen_pages:
        return [], []
    seen_pages.add(url)

    captured_files: list[dict] = []
    captured_children: list[str] = []

    def on_response(resp):
        if not API_URL_RE.search(resp.url):
            return
        ct = (resp.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return
        try:
            body = resp.json()
        except Exception:
            return
        found = extract_file_urls(body, seen_urls)
        captured_files.extend(found)
        # Scan raw JSON text for child NAID page links
        raw = json.dumps(body)
        for m in CHILD_PAGE_RE.finditer(raw):
            child = m.group(0)
            if child not in seen_pages:
                captured_children.append(child)

    page.on("response", on_response)
    print(f"  → {url}")

    try:
        page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
    except PWTimeout:
        print("    [warn] networkidle timeout, continuing anyway")

    page.wait_for_timeout(IDLE_WAIT)
    _click_pagination(page)

    # Also grab child links from the rendered DOM
    try:
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        for h in hrefs:
            if CHILD_PAGE_RE.match(h) and h not in seen_pages:
                captured_children.append(h)
    except Exception:
        pass

    page.remove_listener("response", on_response)
    return captured_files, list(dict.fromkeys(captured_children))


def _click_pagination(page):
    """Keep clicking Next until it disappears or we hit the safety limit."""
    selectors = [
        "button:has-text('Next')",
        "[aria-label='Next page']",
        "[aria-label='next page']",
        ".pagination-next",
        "a:has-text('Next')",
    ]
    clicks = 0
    while clicks < MAX_PAGES:
        advanced = False
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1200) and btn.is_enabled(timeout=1200):
                    btn.click()
                    try:
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except PWTimeout:
                        pass
                    page.wait_for_timeout(400)
                    clicks += 1
                    advanced = True
                    print(f"    [paginated → page {clicks + 1}]")
                    break
            except Exception:
                continue
        if not advanced:
            break


def collect_all(start_url: str) -> list[dict]:
    all_files: list[dict] = []
    seen_urls:  set = set()
    seen_pages: set = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ))
        pg = ctx.new_page()

        queue = [start_url]
        while queue:
            url = queue.pop(0)
            files, children = scrape_record(pg, url, seen_urls, seen_pages)
            all_files.extend(files)
            for c in children:
                if c not in seen_pages:
                    queue.append(c)

        ctx.close()
        browser.close()

    return all_files


# ── Utilities ────────────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "unnamed"


def assign_filenames(files: list[dict]) -> list[dict]:
    seen: dict[str, int] = {}
    for f in files:
        raw  = f["name"] or urlparse(f["url"]).path.split("/")[-1] or "file"
        base = safe_filename(raw)
        stem, _, ext = base.rpartition(".")
        ext  = ("." + ext) if ext else ""
        stem = stem or base
        n    = seen.get(base, 0)
        seen[base] = n + 1
        f["filename"] = base if n == 0 else f"{stem}_{n}{ext}"
    return files


def download_file(url: str, dest: Path, session: requests.Session):
    """Stream-download to a .part file, rename on success. Returns (ok, ct, kb)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with session.get(url, stream=True, timeout=180, allow_redirects=True) as r:
            r.raise_for_status()
            ct, size, first = r.headers.get("Content-Type", ""), 0, True
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(65536):
                    if not chunk:
                        continue
                    if first:
                        snip = chunk[:256].lstrip().lower()
                        if snip.startswith(b"<!doctype") or snip.startswith(b"<html"):
                            tmp.unlink(missing_ok=True)
                            return False, ct, 0.0
                        first = False
                    fh.write(chunk)
                    size += len(chunk)
        if size == 0:
            tmp.unlink(missing_ok=True)
            return False, "", 0.0
        tmp.replace(dest)
        return True, ct, size / 1024
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise exc


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Download all files from a NARA catalog record")
    ap.add_argument("--url",  default=DEFAULT_URL,    help="Catalog record URL")
    ap.add_argument("--out",  default=DEFAULT_OUTDIR, help="Output directory")
    ap.add_argument("--log",  default="",             help="CSV log path")
    args = ap.parse_args()

    out_dir  = Path(args.out)
    log_path = Path(args.log) if args.log else out_dir / "log.csv"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Collecting files from {args.url} …\n")
    files = collect_all(args.url)

    if not files:
        print("\nNo archival files found for this record.")
        print("Possible reasons:")
        print("  • No digitized objects are attached to this NAID")
        print("  • Files are restricted / not available online")
        print("  • Use the API-based downloader (download_nara.py) once you have a key")
        sys.exit(0)

    files = assign_filenames(files)
    print(f"\nFound {len(files)} archival file(s). Downloading to '{out_dir}/':\n")

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    with open(log_path, "w", newline="", encoding="utf-8") as lf:
        w = csv.writer(lf)
        w.writerow(["idx", "filename", "mime", "status", "size_kb", "error", "url"])

        for idx, f in enumerate(files, 1):
            dest = out_dir / f["filename"]

            if dest.exists():
                print(f"[{idx:>4}/{len(files)}] SKIP (exists)  {f['filename']}")
                w.writerow([idx, f["filename"], f["mime"], "skip_exists", "", "", f["url"]])
                continue

            print(f"[{idx:>4}/{len(files)}] {f['mime'] or '?':<30}  {f['filename']}")
            try:
                ok, ct, kb = download_file(f["url"], dest, session)
                if ok:
                    print(f"             ✓ {kb:.1f} KB")
                    w.writerow([idx, f["filename"], ct, "ok", f"{kb:.1f}", "", f["url"]])
                else:
                    print(f"             ✗ empty or HTML response")
                    w.writerow([idx, f["filename"], ct, "fail", "", "empty/HTML", f["url"]])
            except Exception as exc:
                print(f"             ✗ {exc}")
                w.writerow([idx, f["filename"], "", "error", "", str(exc), f["url"]])

            time.sleep(0.05)

    ok_n = sum(1 for f in files if (out_dir / f["filename"]).exists())
    print(f"\n{'─'*50}")
    print(f"Done — {ok_n}/{len(files)} saved  |  log: {log_path}")


if __name__ == "__main__":
    main()
