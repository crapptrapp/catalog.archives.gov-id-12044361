#!/usr/bin/env python3
"""
NARA Catalog bulk downloader — uses the official v2 API with your API key.

How it works
------------
1. Searches for all child records of the given NAID that are available online
   (i.e. have digital objects), fetching 100 at a time.
2. Extracts every fileUrl from each record's digitalObjects list.
3. Downloads all files into subfolders named by NAID (so files from the same
   folder stay together), with a CSV log of every result.

API budget: ~55 calls for 5,443 children — well within the 10,000/month limit.

Install
-------
    pip install requests tqdm

Usage
-----
    # Set your key as an environment variable (recommended):
    export NARA_API_KEY=your_key_here
    python download_nara.py

    # Or pass it directly:
    python download_nara.py --key your_key_here

    # Different record or output folder:
    python download_nara.py --url https://catalog.archives.gov/id/12044361 --out my_folder

    # Limit how many files to download (useful for testing):
    python download_nara.py --limit 20

    # More parallel download threads (default 4):
    python download_nara.py --workers 8
"""

import argparse
import csv
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ── Config ──────────────────────────────────────────────────────────────────
DEFAULT_URL     = "https://catalog.archives.gov/id/12044361"
DEFAULT_OUTDIR  = "downloads"
API_BASE        = "https://catalog.archives.gov/api/v2"
PROXY_BASE      = "https://catalog.archives.gov/proxy"
PAGE_SIZE       = 100     # records per API page (max 100)
REQUEST_DELAY   = 0.1     # seconds between metadata API calls (be polite)
DOWNLOAD_DELAY  = 0.05    # seconds between download requests
UA = "Mozilla/5.0 (compatible; NARA-downloader/1.0)"
# ────────────────────────────────────────────────────────────────────────────


def make_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "x-api-key":    api_key,
        "Content-Type": "application/json",
        "User-Agent":   UA,
    })
    return s


# ── API helpers ──────────────────────────────────────────────────────────────

def api_get(session: requests.Session, path: str, params: dict) -> dict:
    url = f"{API_BASE}/{path.lstrip('/')}"
    resp = session.get(url, params=params, timeout=30)

    if resp.status_code == 401:
        sys.exit("ERROR: Invalid API key. Check your key and try again.")
    if resp.status_code == 429:
        print("\n[warn] Rate limited — waiting 60 s …")
        time.sleep(60)
        return api_get(session, path, params)
    resp.raise_for_status()
    return resp.json()


def unwrap(data: dict) -> tuple[list, int]:
    """Return (hits_list, total_count) from the nested v2 response."""
    hits_block = data.get("body", data).get("hits", {})
    hits = hits_block.get("hits", [])
    total = hits_block.get("total", {})
    total_val = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
    return hits, total_val


def extract_files(hits: list, seen_urls: set) -> list[dict]:
    """Pull every digitalObject out of a page of search hits.
    
    The proxy returns:  objectUrl, objectFilename, objectType, objectId
    (NOT fileUrl / fileName / mimeType — those are v2 API field names)
    """
    files = []
    for hit in hits:
        record = hit.get("_source", {}).get("record", {})
        naid   = str(hit.get("_id") or record.get("naId", "unknown"))
        title  = record.get("title", "")
        for obj in record.get("digitalObjects", []):
            url = obj.get("objectUrl", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            files.append({
                "naid":  naid,
                "title": title,
                "url":   url,
                "name":  obj.get("objectFilename", ""),
                "mime":  obj.get("objectType", ""),
            })
    return files


# ── Metadata collection ──────────────────────────────────────────────────────

def proxy_get(session: requests.Session, path: str, params: dict = None) -> dict:
    """
    Call the catalog's internal proxy API (no API key needed, confirmed working
    from live browser traffic). Used for child record listing and object lookup.
    """
    url = f"{PROXY_BASE}/{path.lstrip('/')}"
    resp = session.get(url, params=params, timeout=30)
    if resp.status_code == 429:
        print("\n[warn] Rate limited — waiting 60 s …")
        time.sleep(60)
        return proxy_get(session, path, params)
    resp.raise_for_status()
    return resp.json()


def iter_file_pages(session: requests.Session, parent_naid: str, limit: int = 0):
    """
    Generator — yields one list[dict] of files per API page so the caller
    can start downloading immediately without waiting for all metadata.
    """
    seen_urls: set = set()
    offset = 0
    total = None
    found = 0

    # Check the parent itself for direct digital objects
    try:
        parent_data = proxy_get(session, "v3/records/search", {
            "naId_is": parent_naid,
            "allowLegacyOrgNames": "true",
        })
        p_hits, _ = unwrap(parent_data)
        p_files = extract_files(p_hits, seen_urls)
        if p_files:
            found += len(p_files)
            yield p_files
    except Exception as e:
        print(f"  [warn] Could not fetch parent record: {e}")

    # Page through children
    while True:
        data = proxy_get(session, f"records/parentNaId/{parent_naid}", {
            "abbreviated": "false",
            "limit":  PAGE_SIZE,
            "offset": offset,
            "sort":   "naId:asc",
        })
        hits, total_val = unwrap(data)

        if total is None:
            total = total_val
            print(f"  {total} child records found.\n")

        if not hits:
            break

        page_files = extract_files(hits, seen_urls)
        found += len(page_files)
        offset += len(hits)

        print(f"  Metadata: {offset:>6}/{total} records scanned  |  {found} files queued", end="\r")

        if page_files:
            if limit and found >= limit:
                # trim to exact limit
                excess = found - limit
                yield page_files[:len(page_files) - excess]
                print(f"\n  [--limit {limit} reached]")
                return
            yield page_files

        if offset >= total:
            break

        time.sleep(REQUEST_DELAY)

    print(f"\n  Metadata scan complete — {found} files total.")


# ── Filename assignment ──────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "unnamed"


def assign_paths(files: list[dict], seen_names: dict = None) -> list[dict]:
    """
    Each file gets a relative path: naid_<NAID>/<filename>
    Pass seen_names across calls to keep filenames unique across pages.
    """
    if seen_names is None:
        seen_names = {}
    for f in files:
        raw  = f["name"] or urlparse(f["url"]).path.split("/")[-1] or "file"
        base = safe_filename(raw)
        stem, _, ext = base.rpartition(".")
        ext  = ("." + ext) if ext else ""
        stem = stem or base

        folder = f"naid_{f['naid']}"
        key    = f"{folder}/{base}"
        n      = seen_names.get(key, 0)
        seen_names[key] = n + 1
        fname  = base if n == 0 else f"{stem}_{n}{ext}"
        f["rel_path"] = f"{folder}/{fname}"
    return files


# ── Downloading ──────────────────────────────────────────────────────────────

def download_file(url: str, dest: Path) -> tuple[bool, str, float]:
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=180,
                          headers={"User-Agent": UA},
                          allow_redirects=True) as r:
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


def load_completed(log_path: Path) -> set[str]:
    """Read an existing log and return the set of URLs already downloaded OK."""
    done = set()
    if not log_path.exists():
        return done
    try:
        with open(log_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") in ("ok", "skip_exists"):
                    done.add(row["url"])
        if done:
            print(f"  Resuming — {len(done)} files already completed (from log).")
    except Exception as e:
        print(f"  [warn] Could not read existing log: {e}")
    return done



# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Download all digitized files from a NARA catalog record."
    )
    ap.add_argument("--url",     default=DEFAULT_URL,
                    help="Catalog record URL (default: NAID 12044361)")
    ap.add_argument("--out",     default=DEFAULT_OUTDIR,
                    help="Output directory (default: downloads/)")
    ap.add_argument("--key",     default="",
                    help="API key (or set env var NARA_API_KEY)")
    ap.add_argument("--log",     default="",
                    help="CSV log file path (default: <out>/log.csv); append-mode, safe to resume")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel download threads (default: 4)")
    ap.add_argument("--limit",   type=int, default=0,
                    help="Stop after N files (0 = no limit; useful for testing)")
    args = ap.parse_args()

    # ── API key ──────────────────────────────────────────────────────────────
    api_key = args.key or os.environ.get("NARA_API_KEY", "")
    if not api_key:
        sys.exit(
            "ERROR: No API key provided.\n"
            "  Set it with:  export NARA_API_KEY=your_key_here\n"
            "  Or pass it:   --key your_key_here"
        )

    # ── Extract NAID ─────────────────────────────────────────────────────────
    m = re.search(r"/id/(\d+)", args.url)
    if not m:
        sys.exit(f"Cannot find NAID in URL: {args.url}")
    parent_naid = m.group(1)

    out_dir  = Path(args.out)
    log_path = Path(args.log) if args.log else out_dir / "log.csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    workers  = args.workers

    print(f"{'─'*65}")
    print(f"  NAID     : {parent_naid}")
    print(f"  Out dir  : {out_dir.resolve()}")
    print(f"  Log      : {log_path}")
    print(f"  Workers  : {workers}")
    print(f"{'─'*65}\n")

    session = make_session(api_key)

    # ── Stream metadata and download page-by-page ────────────────────────────
    print(f"Fetching child records for NAID {parent_naid} …")
    completed_urls = load_completed(log_path)

    log_exists = log_path.exists()
    seen_names: dict = {}
    total_queued = 0
    total_ok = len(completed_urls)

    log_file = open(log_path, "a", newline="", encoding="utf-8")
    writer = csv.writer(log_file)
    if not log_exists:
        writer.writerow(["idx", "naid", "rel_path", "mime",
                         "status", "size_kb", "error", "url"])

    dl_session = requests.Session()
    dl_session.headers.update({"User-Agent": UA})

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for page_files in iter_file_pages(session, parent_naid, limit=args.limit):
                # Assign filenames and filter already-done
                page_files = assign_paths(page_files, seen_names)
                pending = [f for f in page_files if f["url"] not in completed_urls]
                total_queued += len(page_files)

                def make_task(f):
                    def task():
                        dest = out_dir / f["rel_path"]
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if dest.exists():
                            return f, "skip_exists", "", 0.0, ""
                        try:
                            ok, ct, kb = download_file(f["url"], dest)
                            time.sleep(DOWNLOAD_DELAY)
                            return f, "ok" if ok else "fail_empty", ct, kb, ""
                        except Exception as exc:
                            return f, "error", "", 0.0, str(exc)
                    return task

                futures = [pool.submit(make_task(f)) for f in pending]
                for future in as_completed(futures):
                    f, status, ct, kb, err = future.result()
                    completed_urls.add(f["url"])
                    if status == "ok":
                        total_ok += 1
                    icon = {"ok": "✓", "skip_exists": "→", "fail_empty": "✗", "error": "✗"}.get(status, "?")
                    detail = f"{kb:.0f} KB" if kb else (err[:50] if err else status)
                    print(f"\n  {icon} {f['rel_path'][:70]:<70}  {detail}", end="")
                    writer.writerow([total_queued, f["naid"], f["rel_path"], f["mime"],
                                     status, f"{kb:.1f}", err, f["url"]])
                    log_file.flush()

    finally:
        log_file.close()

    print(f"\n\n{'─'*65}")
    print(f"  Done — {total_ok} files saved.")
    print(f"  Log   : {log_path}")
    print(f"{'─'*65}")


if __name__ == "__main__":
    main()