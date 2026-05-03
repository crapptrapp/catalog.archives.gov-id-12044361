#!/usr/bin/env python3
"""
NARA Catalog downloader — drives the catalog's own proxy API directly.

Discovered from live network traffic:
  - Record metadata:  /proxy/v3/records/search?naId_is=<naId>
  - Children list:    /proxy/records/parentNaId/<naId>?limit=N&sort=naId:asc&offset=N
  - Child objects:    /proxy/v3/records/search?naId_is=<childNaId>&includeObjects=true  (or similar)
  - Online check:     /proxy/online-availability/naId/<naId>

Strategy
--------
1. Fetch the parent record to understand what it is.
2. Page through ALL children via parentNaId API (5443 in this case).
3. For each child, fetch its full record to find digitalObjects / fileUrl fields.
4. Download every file found.

No browser needed for the bulk of the work — Playwright is only used once
to load the first page and capture the real session cookies/headers the
proxy API expects.

Install:   pip install playwright requests
           playwright install chromium
Usage:     python nara_playwright_scraper.py [--url URL] [--out DIR] [--workers N]
"""

import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse, urlencode

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ──────────────────────────────────────────────────────────────────
DEFAULT_URL    = "https://catalog.archives.gov/id/12044361"
DEFAULT_OUTDIR = "downloads"
PROXY_BASE     = "https://catalog.archives.gov/proxy"
PAGE_SIZE      = 100      # children per API page (catalog supports up to 100)
PAGE_TIMEOUT   = 60_000
DOWNLOAD_WORKERS = 4      # parallel download threads
# ────────────────────────────────────────────────────────────────────────────

MEDIA_PATH_RE = re.compile(
    r"(catalogmedia|arcmedia|/lz/\d|nara-media|content/arcmedia|s3\.amazonaws\.com)",
    re.IGNORECASE,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ── Step 1: get real browser cookies/headers via Playwright ─────────────────

def get_session_headers(start_url: str) -> dict:
    """
    Load the page in a real browser just long enough to capture the cookies
    and any auth headers the proxy expects, then return them for use in requests.
    """
    captured = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        pg = ctx.new_page()

        def on_request(req):
            if "catalog.archives.gov/proxy" in req.url:
                # Grab cookies and headers from first proxy request
                if not captured:
                    captured.update(req.headers)

        pg.on("request", on_request)
        print(f"Loading {start_url} to capture session …")
        try:
            pg.goto(start_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        except PWTimeout:
            pass
        pg.wait_for_timeout(2000)

        # Also grab browser cookies
        cookies = ctx.cookies()
        ctx.close()
        browser.close()

    # Build a headers dict suitable for requests
    headers = {
        "User-Agent": UA,
        "Referer":    start_url,
        "Accept":     "application/json, text/plain, */*",
    }
    # Forward any cookie header we saw
    if "cookie" in captured:
        headers["cookie"] = captured["cookie"]
    elif cookies:
        headers["cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    return headers


# ── Step 2: proxy API helpers ────────────────────────────────────────────────

def proxy_get(session: requests.Session, path: str, params: dict = None) -> dict | list | None:
    url = f"{PROXY_BASE}/{path.lstrip('/')}"
    try:
        r = session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"  [warn] GET {url} → {exc}")
        return None


def unwrap_hits(data) -> list:
    """Pull the hits list out of the nested body.hits.hits structure."""
    if isinstance(data, dict):
        return (data.get("body") or data).get("hits", {}).get("hits", [])
    return []


def extract_file_urls(obj, seen: set) -> list[dict]:
    """Recursively find every fileUrl in any JSON structure."""
    results = []

    def walk(o):
        if isinstance(o, dict):
            url = o.get("fileUrl", "")
            if url and url not in seen:
                seen.add(url)
                results.append({
                    "url":  url,
                    "name": o.get("fileName", ""),
                    "mime": o.get("mimeType", ""),
                })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(obj)
    return results


# ── Step 3: collect all child NAIDs ─────────────────────────────────────────

def get_all_children(session: requests.Session, parent_naid: str) -> list[str]:
    """
    Page through /proxy/records/parentNaId/<naId> and collect every child naId.
    Returns a list of naId strings.
    """
    children = []
    offset = 0

    print(f"\nFetching child record list for NAID {parent_naid} …")
    while True:
        data = proxy_get(session, f"records/parentNaId/{parent_naid}", {
            "abbreviated": "true",
            "limit":  PAGE_SIZE,
            "offset": offset,
            "sort":   "naId:asc",
        })
        if not data:
            break

        hits = unwrap_hits(data)
        if not hits:
            break

        for hit in hits:
            naid = hit.get("_id") or (hit.get("_source") or {}).get("record", {}).get("naId")
            if naid:
                children.append(str(naid))

        total = (data.get("body") or data).get("hits", {}).get("total", {})
        total_val = total.get("value", 0) if isinstance(total, dict) else int(total)

        offset += len(hits)
        print(f"  … {offset}/{total_val} children indexed", end="\r")

        if offset >= total_val or len(hits) == 0:
            break

    print(f"\n  Found {len(children)} child records.")
    return children


# ── Step 4: get files for one child ─────────────────────────────────────────

def get_files_for_naid(session: requests.Session, naid: str, seen: set) -> list[dict]:
    """Fetch a child record and extract all its fileUrls."""
    # Try the v3 search endpoint first (same one the page uses)
    data = proxy_get(session, "v3/records/search", {
        "naId_is":           naid,
        "allowLegacyOrgNames": "true",
        "includeObjects":    "true",
    })
    files = []
    if data:
        files = extract_file_urls(data, seen)
        if files:
            return files

    # Fallback: plain record lookup
    data2 = proxy_get(session, f"records/search", {"naId_is": naid})
    if data2:
        files = extract_file_urls(data2, seen)

    return files


# ── Step 5: download ─────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "unnamed"


def assign_filename(f: dict, seen_names: dict) -> str:
    raw  = f["name"] or urlparse(f["url"]).path.split("/")[-1] or "file"
    base = safe_filename(raw)
    stem, _, ext = base.rpartition(".")
    ext  = ("." + ext) if ext else ""
    stem = stem or base
    n    = seen_names.get(base, 0)
    seen_names[base] = n + 1
    return base if n == 0 else f"{stem}_{n}{ext}"


def download_file(url: str, dest: Path, session: requests.Session) -> tuple[bool, str, float]:
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
    ap.add_argument("--url",     default=DEFAULT_URL)
    ap.add_argument("--out",     default=DEFAULT_OUTDIR)
    ap.add_argument("--log",     default="")
    ap.add_argument("--workers", type=int, default=DOWNLOAD_WORKERS)
    args = ap.parse_args()

    # Extract NAID from URL
    m = re.search(r"/id/(\d+)", args.url)
    if not m:
        sys.exit(f"Cannot extract NAID from URL: {args.url}")
    parent_naid = m.group(1)

    out_dir  = Path(args.out)
    log_path = Path(args.log) if args.log else out_dir / "log.csv"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Get session cookies from browser ─────────────────────────────────
    headers = get_session_headers(args.url)
    session = requests.Session()
    session.headers.update(headers)

    # ── 2. Check if this record itself has files ─────────────────────────────
    seen_urls:  set = set()
    seen_names: dict = {}
    all_files: list[dict] = []

    print("\nChecking parent record for direct files …")
    parent_files = get_files_for_naid(session, parent_naid, seen_urls)
    if parent_files:
        print(f"  Found {len(parent_files)} files on parent record.")
        all_files.extend(parent_files)

    # ── 3. Collect all child NAIDs ───────────────────────────────────────────
    children = get_all_children(session, parent_naid)

    # ── 4. Fetch files for each child ────────────────────────────────────────
    print(f"\nFetching file metadata for {len(children)} child records …")
    for i, child_naid in enumerate(children, 1):
        files = get_files_for_naid(session, child_naid, seen_urls)
        if files:
            all_files.extend(files)
            print(f"  [{i:>5}/{len(children)}] NAID {child_naid}: {len(files)} file(s)  (total: {len(all_files)})")
        elif i % 100 == 0:
            print(f"  [{i:>5}/{len(children)}] …")
        time.sleep(0.05)  # be polite

    if not all_files:
        print("\nNo downloadable files found.")
        print("The records may not have digitized objects, or may be restricted.")
        sys.exit(0)

    # Assign filenames
    for f in all_files:
        f["filename"] = assign_filename(f, seen_names)

    print(f"\n{'─'*60}")
    print(f"Total files to download: {len(all_files)}")
    print(f"Output directory:        {out_dir.resolve()}")
    print(f"{'─'*60}\n")

    # ── 5. Download ──────────────────────────────────────────────────────────
    dl_session = requests.Session()
    dl_session.headers.update({"User-Agent": UA})

    with open(log_path, "w", newline="", encoding="utf-8") as lf:
        writer = csv.writer(lf)
        writer.writerow(["idx", "filename", "mime", "status", "size_kb", "error", "url"])

        def do_download(idx_f):
            idx, f = idx_f
            dest = out_dir / f["filename"]
            if dest.exists():
                return idx, f, "skip", "", 0.0, ""
            try:
                ok, ct, kb = download_file(f["url"], dest, dl_session)
                status = "ok" if ok else "fail"
                return idx, f, status, ct, kb, ""
            except Exception as exc:
                return idx, f, "error", "", 0.0, str(exc)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(do_download, (i, f)): i
                       for i, f in enumerate(all_files, 1)}
            for future in as_completed(futures):
                idx, f, status, ct, kb, err = future.result()
                icon = "✓" if status == "ok" else ("→" if status == "skip" else "✗")
                detail = f"{kb:.0f} KB" if kb else (err[:60] if err else status)
                print(f"[{idx:>5}/{len(all_files)}] {icon} {f['filename'][:60]:<60}  {detail}")
                writer.writerow([idx, f["filename"], f["mime"], status, f"{kb:.1f}", err, f["url"]])

    ok_n = sum(1 for f in all_files if (out_dir / f["filename"]).exists())
    print(f"\n{'─'*60}")
    print(f"Done — {ok_n}/{len(all_files)} files saved to '{out_dir}/'")
    print(f"Log:   {log_path}")


if __name__ == "__main__":
    main()
