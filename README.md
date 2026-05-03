# catalog.archives.gov-id-12044361
Records Relating to Membership in the Nationalsozialistische Deutsche Arbeiterpartei (NSDAP), 1945–1994


# NARA Playwright Scraper

Python scraper for catalog.archives.gov using Playwright.

## Overview

This project uses the Playwright Python API to automate browser interactions and scrape data from the NARA catalog. The environment required some setup fixes because the Python package and the browser installation must be aligned correctly.

## What was done

- Installed Playwright with `pipx` first, which exposed a Python import mismatch.
- Fixed the Python-side import by installing `playwright` into the active Python environment.
- Verified the Python package location with:

```bash
python -c "import playwright; print(playwright.__file__)"
python -m pip show playwright
```

- Confirmed the package is installed at:

```text
/usr/local/lib/python3.13/dist-packages/playwright/__init__.py
```

- Resolved the browser install path by using the Node Playwright install flow successfully:

```bash
npm init -y
npm i -D playwright
npx playwright install
```

## Requirements

- Python 3.13+
- Node.js
- npm
- Playwright Python package
- Playwright browser binaries installed

## Installation

### 1. Install the Python package

```bash
python -m pip install playwright
```

### 2. Install browser binaries

If the Python browser install step fails in this environment, use the Node-based install that works:

```bash
npm init -y
npm i -D playwright
npx playwright install
```

### 3. Verify the Python package path

```bash
python -c "import playwright; print(playwright.__file__)"
python -m pip show playwright
```

Expected output should point to the active Python environment, such as:

```text
/usr/local/lib/python3.13/dist-packages/playwright/__init__.py
```

## Usage

Run the scraper with:

```bash
python nara_playwright_scraper.py
```

## Recommended script header

Use this import and startup pattern in `nara_playwright_scraper.py`:

```python
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto("https://example.com", wait_until="domcontentloaded")
            # scraping logic here
        finally:
            context.close()
            browser.close()

if __name__ == "__main__":
    main()
```

## Troubleshooting


###  pagination looping 

The proxy URL is catalog.archives.gov/proxy/ not /api/ — that's why the API filter matched nothing
Children are listed via /proxy/records/parentNaId/12044361 with pagination using limit=10&sort=naId:asc — this is a proper API, no need to click Next buttons at all
5443 child records — the Next button was clicking forever because the browser pagination is for browsing, but we should page the API directly
The child records are file units (level fileUnit) — files will be on those children, not the parent

### `ModuleNotFoundError: No module named 'playwright'`

This means Python is not using the environment where Playwright was installed. Reinstall with:

```bash
python -m pip install playwright
```

### `Executable doesn't exist ... chrome-headless-shell`

This means the browser binaries are missing. Install them with:

```bash
npx playwright install
```

### `Cannot find module '/usr/share/nodejs/playwright/cli.js'`

This indicates a broken system Playwright/Node path mismatch. Use the working Node install flow and keep the Python package and browser install in the same environment.

## Notes

- The Python script should import Playwright from the Python package, not from Node.
- `npm i -D playwright` and `npx playwright install` are only for browser setup in this workflow.
- The Python code should still use `from playwright.sync_api import sync_playwright`.

