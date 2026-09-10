import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright
from Helpers.easylist_selectors import load_selectors
from Collectors.AdCollector import AdCollector

async def main():
    selectors = load_selectors()
    print(f"Loaded {len(selectors)} selectors from easylist.")

    # 1. Test basic statistics
    unique_count = len(set(selectors))
    assert unique_count == len(selectors), "Duplicates found in selector list"
    assert len(selectors) > 10000, "Too few selectors loaded"

    # 2. Test in-browser evaluation with Playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        html_content = """
        <!DOCTYPE html>
        <html>
        <head><title>EasyList Test Page</title></head>
        <body>
            <header><h1>News Website</h1></header>
            <div id="AD_300" class="ad-banner" style="width:300px; height:250px; display:block;">
                <iframe src="https://googleads.g.doubleclick.net/pagead/ads" style="width:300px; height:250px;"></iframe>
            </div>
            <article>
                <p>This is standard non-ad content on the page.</p>
            </article>
            <div id="Ad-Container" style="width:160px; height:600px; display:block;">
                <a href="https://adclick.g.doubleclick.net/aclk">
                    <img src="https://example.com/ad.jpg" style="width:160px; height:600px;">
                </a>
            </div>
            <div class="sidebar">
                <div data-ad-slot="12345" style="width:300px; height:250px;"></div>
            </div>
        </body>
        </html>
        """
        await page.set_content(html_content)

        t0 = time.perf_counter()
        candidates = await page.evaluate(AdCollector._FIND_ADS_JS, selectors)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        print(f"\n[OK] Playwright evaluated {len(selectors)} selectors across DOM in {elapsed_ms:.2f} ms")
        print(f"[OK] Detected {len(candidates)} candidate ad elements:")
        for idx, c in enumerate(candidates, start=1):
            print(f"  {idx}. Tag: <{c.get('nodeType')}> | ID: '{c.get('id')}' | Rule: {c.get('matchedRule')} | Size: {c.get('width')}x{c.get('height')}")

        assert len(candidates) >= 2, "Expected at least 2 ad elements detected"
        await browser.close()

    print("\n[SUCCESS] All EasyList selector tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
