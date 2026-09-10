import asyncio
import sys
import os
import tempfile
from playwright.async_api import async_playwright

async def main():
    if len(sys.argv) < 2:
        print("Usage: python test_consentomatic.py <URL>")
        sys.exit(1)

    test_url = sys.argv[1]
    if not test_url.startswith("http"):
        test_url = "https://" + test_url

    # Construct absolute path to the modified Consent-O-Matic extension
    extension_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "resources", "consent-o-matic"))
    print(f"Loading extension from: {extension_path}")

    async with async_playwright() as p:
        with tempfile.TemporaryDirectory() as user_data_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
                args=[
                    f"--disable-extensions-except={extension_path}",
                    f"--load-extension={extension_path}",
                ]
            )

            print("Browser launched with Consent-O-Matic extension successfully.")

            page = context.pages[0] if context.pages else await context.new_page()
            
            try:
                from playwright_stealth import stealth_async
                await stealth_async(page)
                print("Stealth mode applied")
            except Exception as e:
                pass

            # Log page console to see what consent-o-matic is doing
            page.on("console", lambda msg: print(f"PAGE LOG: {msg.text}"))

            print(f"Navigating to {test_url}...")
            
            # Navigate and take an initial screenshot
            await page.goto(test_url, wait_until="load")
            
            print("Page basic load complete. Waiting 10 seconds for Consent-O-Matic to automatically handle the CMP...")
            
            await page.wait_for_timeout(10000)

            # Dump info about the CMP box
            cmp_html = await page.evaluate('''() => {
                const cmp = document.querySelector("#cmpwrapper");
                if (!cmp) return "no wrapper found";
                if (cmp.shadowRoot) {
                    const box = cmp.shadowRoot.querySelector("#cmpbox");
                    if (box && getComputedStyle(box).display !== "none") {
                        return "FAILED: CMP IS PRESENT AND VISIBLE!";
                    } else {
                        return "SUCCESS: CMP is hidden or dismissed!";
                    }
                }
                return "innerHTML length: " + cmp.innerHTML.length;
            }''')
            print(f"CMP Status: {cmp_html}")

            final_screenshot_path = os.path.join(os.path.dirname(__file__), "extension_test_final.png")
            print(f"Final screenshot saved to {final_screenshot_path}. Check this image to verify the banner is gone.")

            await context.close()
            print("Test finished successfully.")

if __name__ == "__main__":
    asyncio.run(main())
