#!/usr/bin/env python3
"""
NARA Catalog Playwright scraper — intercepts the catalog's own API calls
to reliably collect every downloadable file for a given record.

Strategy
--------
The NARA catalog is a React SPA. Static link-harvesting is unreliable because
content renders dynamically and pagination is JS-driven.  Instead we:

  1. Open the record page in a real browser (Playwright).
  2. Intercept all XHR/fetch responses that look like catalog API calls.
  3. Extract every `fileUrl` from the JSON payloads — all file types.
  4. Also scan rendered <a> tags as a fallback for any URLs missed by step 2.
  5. Download everything with requests, streaming, with a .part temp file.

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
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ─────────────────────────────────────────────────────────────────
DEFAULT_URL    = "https://catalog.archives.gov/id/12044361"
DEFAULT_OUTDIR = "downloads"
NARA_BASE      = "https://catalog.archives.gov"

# Extensions / MIME types we treat as downloadable
DOWNLOAD_EXTS  = {
    ".pdf", ".jpg", ".jpeg", ".tif", ".tiff", ".png", ".gif",
    ".mp3", ".mp4", ".wav", ".mov", ".avi",
    ".doc", ".docx", ".xls", ".xlsx", ".txt", ".xml", ".csv",
}
NARA_MEDIA_HOSTS = {
    "catalog.archives.gov",
    "s3.amazonaws.com",
    "nara-media-001.s3.amazonaws.com",
}

# How long to wait for the page and pagination to settle (ms)
PAGE_TIMEOUT   = 90_000
IDLE_AFTER_NAV = 3_000   # ms of network quiet after navigation
# ───────────────────────────────────────────────────────────────────────────


def is_downloadable_url(url: str) -> bool:
    parsed = urlparse(url)
    ext    = Path(parsed.path).suffix.lower()
    host   = parsed.netloc.lower()
    return (
        ext in DOWNLOAD_EXTS
        or host in NARA_MEDIA_HOSTS
        or "/catalogmedia/" in url
        or "/arcmedia/"     in url
        or "/content/"      in url
    )


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip()
    return name or "unnamed_file"


def url_to_filename(url: str, idx: int, fallback_ext: str = "") -> str:
    path = urlparse(url).path
    raw  = path.rstrip("/").split("/")[-1]   # may be "" for bare-host URLs
    if not raw:
        return f"file_{idx:05d}{fallback_ext}"
    name = safe_filename(raw)
    if not name or name in ("_", "unnamed_file"):
        name = f"file_{idx:05d}{fallback_ext}"
    return name


def extract_files_from_json(data: dict) -> list[dict]:
    """Recursively pull every object that has a fileUrl out of a JSON blob."""
    files = []

    def walk(obj):
        if isinstance(obj, dict):
            url = obj.get("fileUrl") or obj.get("url") or obj.get("file", {}).get("@url", "")
            if url and is_downloadable_url(url):
                files.append({
                    "url":  url,
                    "name": obj.get("fileName") or obj.get("file", {}).get("@name", ""),
                    "mime": obj.get("mimeType") or obj.get("file", {}).get("@mime", ""),
                })
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return files


def collect_urls_via_browser(start_url: str) -> list[dict]:
    """
    Launch Chromium, intercept API responses and harvest anchor hrefs.
    Returns a deduplicated list of {url, name, mime} dicts.
    """
    collected: dict[str, dict] = {}   # url → metadata
    api_pattern = re.compile(
        r"catalog\.archives\.gov/(api/|catalogmedia/|arcmedia/)",
        re.IGNORECASE,
    )

    def on_response(response):
        """Intercept every network response and mine it for file URLs."""
        url = response.url
        ct  = (response.headers.get("content-type") or "").lower()

        # Grab JSON from catalog API calls
        if "catalog.archives.gov" in url and "json" in ct:
            try:
                body  = response.json()
                found = extract_files_from_json(body)
                for f in found:
                    collected[f["url"]] = f
            except Exception:
                pass

        # Also capture direct media/content URLs (binary responses)
        if is_downloadable_url(url) and response.status == 200:
            if url not in collected:
                path = urlparse(url).path
                name = path.split("/")[-1] or ""
                collected[url] = {"url": url, "name": safe_filename(name), "mime": ct}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )
        page = context.new_page()
        page.on("response", on_response)

        print(f"Opening {start_url} …")
        try:
            page.goto(start_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        except PWTimeout:
            print("  [warn] page timed out waiting for networkidle — continuing anyway")

        # Give the React app a moment to fire its data fetches
        page.wait_for_timeout(IDLE_AFTER_NAV)

        # ── Pagination: click "Next" / "Load more" until it disappears ─────
        _click_through_pagination(page)

        # ── Fallback: harvest <a href> links from the rendered DOM ──────────
        _harvest_anchor_links(page, start_url, collected)

        context.close()
        browser.close()

    return list(collected.values())


def _click_through_pagination(page):
    """
    Click pagination controls so that all pages of objects load.
    NARA uses various button labels — we try them all.
    """
    pagination_selectors = [
        "button:has-text('Next')",
        "button:has-text('next')",
        "a:has-text('Next page')",
        "[aria-label='Next page']",
        "[aria-label='next page']",
        ".pagination-next",
        "button[data-testid='next-page']",
    ]
    pages_clicked = 0
    max_pages = 200

    while pages_clicked < max_pages:
        clicked = False
        for sel in pagination_selectors:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=2000) and btn.is_enabled(timeout=2000):
                    btn.click()
                    page.wait_for_load_state("networkidle", timeout=15_000)
                    page.wait_for_timeout(500)
                    pages_clicked += 1
                    clicked = True
                    print(f"  [pagination] clicked page {pages_clicked + 1}")
                    break
            except Exception:
                continue
        if not clicked:
            break


def _harvest_anchor_links(page, start_url: str, collected: dict):
    """Scan rendered <a> tags and add any downloadable hrefs to `collected`."""
    try:
        hrefs = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.href)",
        )
    except Exception:
        return

    for href in hrefs:
        if not href:
            continue
        if not href.startswith("http"):
            href = urljoin(start_url, href)
        if is_downloadable_url(href) and href not in collected:
            name = urlparse(href).path.split("/")[-1] or ""
            collected[href] = {"url": href, "name": safe_filename(name), "mime": ""}


def download_file(url: str, dest: Path, session: requests.Session) -> tuple[bool, str, float]:
    """
    Download url → dest using a .part temp file.
    Returns (success, content_type, size_kb).
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with session.get(url, stream=True, timeout=180, allow_redirects=True) as r:
            r.raise_for_status()
            ct        = r.headers.get("Content-Type", "")
            size      = 0
            first     = True
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1024 * 512):
                    if not chunk:
                        continue
                    if first:
                        # Reject HTML error pages masquerading as files
                        snippet = chunk[:256].lstrip().lower()
                        if snippet.startswith(b"<!doctype html") or snippet.startswith(b"<html"):
                            tmp.unlink(missing_ok=True)
                            return False, ct, 0.0
                        first = False
                    fh.write(chunk)
                    size += len(chunk)

        if size == 0:
            tmp.unlink(missing_ok=True)
            return False, ct, 0.0

        tmp.replace(dest)
        return True, ct, size / 1024

    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise exc


def main():
    parser = argparse.ArgumentParser(description="Download all files from a NARA catalog record")
    parser.add_argument("--url",  default=DEFAULT_URL,    help="Catalog record URL")
    parser.add_argument("--out",  default=DEFAULT_OUTDIR, help="Output directory")
    parser.add_argument("--log",  default="",             help="CSV log path (default: <out>/log.csv)")
    args = parser.parse_args()

    out_dir  = Path(args.out)
    log_path = Path(args.log) if args.log else out_dir / "log.csv"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: collect all file URLs ─────────────────────────────────────
    files = collect_urls_via_browser(args.url)

    if not files:
        print("No downloadable files found. Check the URL and try again.")
        sys.exit(0)

    # Deduplicate and assign filenames (resolve collisions with a counter suffix)
    seen_names: dict[str, int] = {}
    for f in files:
        base = f["name"] or url_to_filename(f["url"], 0)
        if not base:
            base = "file"
        stem, _, ext = base.rpartition(".")
        ext = ("." + ext) if ext else ""
        stem = stem or base
        count = seen_names.get(base, 0)
        seen_names[base] = count + 1
        f["filename"] = base if count == 0 else f"{stem}_{count}{ext}"

    print(f"\nFound {len(files)} file(s) to download.\n")

    # ── Phase 2: download ───────────────────────────────────────────────────
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    with open(log_path, "w", newline="", encoding="utf-8") as lf:
        writer = csv.writer(lf)
        writer.writerow(["idx", "filename", "mime", "status", "size_kb", "error", "url"])

        for idx, f in enumerate(files, 1):
            dest = out_dir / f["filename"]

            if dest.exists():
                print(f"[{idx:>4}/{len(files)}] SKIP (exists)  {f['filename']}")
                writer.writerow([idx, f["filename"], f["mime"], "skip_exists", "", "", f["url"]])
                continue

            print(f"[{idx:>4}/{len(files)}] {f['mime'] or '?':<28}  {f['filename']}")
            try:
                ok, ct, kb = download_file(f["url"], dest, session)
                if ok:
                    print(f"             ✓ {kb:.1f} KB")
                    writer.writerow([idx, f["filename"], ct, "ok", f"{kb:.1f}", "", f["url"]])
                else:
                    print(f"             ✗ empty or HTML response")
                    writer.writerow([idx, f["filename"], ct, "fail_empty", "", "empty or HTML", f["url"]])
            except Exception as exc:
                print(f"             ✗ ERROR: {exc}")
                writer.writerow([idx, f["filename"], "", "fail_error", "", str(exc), f["url"]])

            time.sleep(0.05)

    print(f"\nDone. Files saved to '{out_dir}/', log at '{log_path}'.")


if __name__ == "__main__":
    main()
