v# catalog.archives.gov-id-12044361

**Records Relating to Membership in the Nationalsozialistische Deutsche Arbeiterpartei (NSDAP), 1945–1994**

Bulk downloader for NARA catalog record [NAID 12044361](https://catalog.archives.gov/id/12044361) — 5,443 child file units, ~300,000+ digitized TIFF images hosted on S3.

---

## TL;DR — Working Solution

```bash
pip install requests tqdm
export NARA_API_KEY=your_key_here
python download_nara.py --out /path/to/output --workers 8
```

Files download immediately as metadata is fetched. Fully resumable — re-run the same command after an interruption and it picks up where it left off.

---

## Scripts

| Script | Purpose |
|---|---|
| `download_nara.py` | **Primary downloader** — uses NARA proxy API, streams metadata + downloads in parallel |
| `nara_playwright_scraper.py` | Browser-based fallback scraper (Playwright) — not needed if you have the API key |
| `nara_debug.py` | Dumps all network requests the catalog page makes — used to discover the real API endpoints |
| `nara_debug2.py` | Probes proxy endpoint response shapes — used to discover real field names (`objectUrl`, `objectFilename`) |

---

## download_nara.py

### How it works

The NARA catalog is a React SPA. Its UI fires XHR requests to internal proxy endpoints. By watching live browser traffic with `nara_debug.py`, we identified the real endpoints:

- **Child listing:** `GET /proxy/records/parentNaId/<naId>?abbreviated=false&limit=100&sort=naId:asc`  
  Returns up to 100 child file-unit records per page, including their `digitalObjects` arrays.
- **Object fields:** `objectUrl` (download URL), `objectFilename` (filename), `objectType` (e.g. `Image (TIFF)`)

The script pages through all 5,443 children 100 at a time and **starts downloading immediately** — it doesn't wait for all metadata to be collected before beginning. Downloads run in a thread pool while the main thread fetches the next metadata page.

### Features

- **Streaming pipeline** — metadata fetch and file downloads run concurrently
- **Resumable** — on restart, reads `log.csv` and skips already-completed URLs; failed files are retried
- **Parallel downloads** — configurable worker threads (`--workers`)
- **Organised output** — files saved in `naid_<NAID>/` subfolders matching their archival folder
- **CSV log** — append-mode, records status, size, and URL for every file

### Installation

```bash
pip install requests tqdm
```

### API Key

Request a free read-only key by emailing `Catalog_API@nara.gov` with your name and email.  
Default limit: 10,000 API calls/month (this job uses ~55 calls for metadata).

```bash
export NARA_API_KEY=your_key_here
```

### Usage

```bash
# Basic
python download_nara.py --out /path/to/output

# All options
python download_nara.py \
  --url https://catalog.archives.gov/id/12044361 \  # any NARA catalog URL
  --out /mnt/z/Download/archzi \                    # output directory
  --workers 8 \                                      # parallel download threads
  --limit 20 \                                       # stop after N files (for testing)
  --key your_key_here \                              # or use NARA_API_KEY env var
  --log /path/to/custom.csv                          # default: <out>/log.csv
```

### Output structure

```
/path/to/output/
├── log.csv
├── naid_581244230/
│   ├── A3340-MFKL-A0001-00001.tif
│   ├── A3340-MFKL-A0001-00002.tif
│   └── ...
├── naid_581247254/
│   └── ...
└── ...
```

### Resuming an interrupted download

Just re-run the same command. The script reads `log.csv`, skips URLs with status `ok` or `skip_exists`, and retries anything that errored.

```bash
python download_nara.py --out /mnt/z/Download/archzi --workers 8
# Resuming — 12453 files already completed (from log).
```

---

## What we tried and why it didn't work

### Attempt 1 — NARA v1 API (`/api/v1`)

The original `download_nara.py` used `catalog.archives.gov/api/v1?naIds=12044361`.  
**Failed:** the v1 API was retired in September 2023. Returns an empty body → `JSONDecodeError`.

### Attempt 2 — NARA v2 API (`/api/v2`) with `ancestors.naId`

Switched to v2 with `ancestors.naId=12044361` and `naIds=` parameters.  
**Failed:** `400 Bad Request` — these are v1 field names; v2 uses different (undocumented) parameters.

### Attempt 3 — Playwright browser scraping (static link harvesting)

`nara_playwright_scraper.py` (original version) harvested `<a href>` attributes from the rendered DOM.  
**Failed:** the catalog is a React SPA — content loads via XHR after the page shell renders, so static link scraping finds almost nothing.

### Attempt 4 — Playwright with `is_downloadable_url` filter

Added network response interception, but `is_downloadable_url()` matched `catalog.archives.gov` as a trusted host unconditionally.  
**Downloaded:** CSS bundles, JS chunks, fonts — not archival files.

### Attempt 5 — Playwright with `API_URL_RE` + `MEDIA_PATH_RE` filters

Tightened filters to only inspect `/api/` responses and only accept `fileUrl` fields matching archival media paths.  
**Failed:** the catalog uses `/proxy/` not `/api/`, so the API filter matched nothing → 0 files found.

### Attempt 6 — Proxy API with wrong field names

Switched to the correct `/proxy/` endpoints (discovered from live browser traffic via `nara_debug.py`), but `extract_files()` looked for `fileUrl` and `fileName`.  
**Failed:** proxy response actually uses `objectUrl` and `objectFilename` → 0 files extracted.

### Working solution — Proxy API with correct field names + streaming pipeline

Confirmed real field names using `nara_debug2.py`:
```json
{
  "objectFilename": "A3340-MFKL-A0001-00001.tif",
  "objectUrl": "https://s3.amazonaws.com/NARAprodstorage/lz/...",
  "objectFileSize": 17723460,
  "objectId": "581244231",
  "objectType": "Image (TIFF)"
}
```
Fixed `extract_files()` to use `objectUrl` / `objectFilename`, switched from collect-then-download to a streaming pipeline that starts downloading after the first metadata page.

---

## Playwright scraper (nara_playwright_scraper.py)

A browser-based fallback for cases where no API key is available. Intercepts XHR responses from the catalog's proxy API rather than scraping static HTML.

### How it works

1. Opens the record page in a real Chromium browser via Playwright.
2. Listens to all network responses with `page.on("response", ...)`.
3. Intercepts only `/proxy/` JSON responses (ignores CSS/JS/fonts).
4. Extracts `objectUrl` from `digitalObjects` arrays in the JSON.
5. Follows child-record links one level deep.
6. Downloads all files with `requests`.

### Installation

```bash
pip install playwright requests

# If Python browser install fails, use Node:
npm init -y
npm i -D playwright
npx playwright install
```

### Verify Playwright installation

```bash
python -c "import playwright; print(playwright.__file__)"
# Should print: /usr/local/lib/python3.13/dist-packages/playwright/__init__.py
```

### Usage

```bash
python nara_playwright_scraper.py
python nara_playwright_scraper.py --url https://catalog.archives.gov/id/12044361 --out my_folder
```

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'playwright'`

Python is not using the environment where Playwright was installed. Fix:
```bash
python -m pip install playwright
```

### `Executable doesn't exist ... chrome-headless-shell`

Browser binaries are missing. Fix:
```bash
npx playwright install
```

### `Cannot find module '/usr/share/nodejs/playwright/cli.js'`

Broken system Playwright/Node path mismatch. Use the Node install flow:
```bash
npm init -y && npm i -D playwright && npx playwright install
```

### `400 Bad Request` on `/api/v2/records/search`

You are using v1 field names (`ancestors.naId`, `naIds`). The working solution uses the proxy endpoints, not the v2 API, for child listing.

### `Files found: 0` after scanning records

Field name mismatch. The proxy returns `objectUrl` / `objectFilename`, not `fileUrl` / `fileName`. Make sure you're using the latest `download_nara.py`.

### Pagination looping indefinitely

Happens with the Playwright scraper when clicking the UI "Next" button — the button never disables, it just reloads the same data. The fix is to page the proxy API directly (`/proxy/records/parentNaId/<naId>?offset=N`) rather than clicking UI controls.

---

## Notes

- All files are TIFF images (~14–17 MB each) hosted on `s3.amazonaws.com/NARAprodstorage/`.
- Files are public domain (U.S. government records).
- The NARA API key is read-only and free; request at `Catalog_API@nara.gov`.
- 10,000 API calls/month limit resets on the 1st of each month. This job uses ~55 calls for metadata.
- The Python script imports Playwright from the Python package — `npm`/`npx` are only needed for browser binary installation.