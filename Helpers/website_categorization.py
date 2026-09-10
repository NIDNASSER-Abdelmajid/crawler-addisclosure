import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

_DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "resources" / "domain_categories.json"


def _normalize_domain(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.netloc or parsed.path).strip().lower()
    host = host.split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


class WebsiteCategorizer:
    def __init__(
        self,
        enabled: bool = True,
        headless: bool = True,
        timeout_ms: int = 8000,
        cache_path: Optional[Path] = None,
        use_cache: bool = True,
    ) -> None:
        self.enabled = enabled
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.use_cache = use_cache
        self.cache_path = Path(cache_path) if cache_path else (_DEFAULT_CACHE_PATH if use_cache else None)
        self._cache: dict[str, str] = {}
        self._dirty = False

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

        if self.use_cache and self.cache_path:
            self._load_cache()

    def __enter__(self) -> WebsiteCategorizer:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _load_cache(self) -> None:
        try:
            if self.cache_path and self.cache_path.is_file():
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._cache = {
                        str(k).strip().lower(): str(v).strip()
                        for k, v in data.items()
                        if str(k).strip()
                    }
        except Exception:
            self._cache = {}

    def flush_cache(self) -> None:
        if not self.cache_path or not self._dirty:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._cache, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._dirty = False
        except Exception:
            pass

    def close(self) -> None:
        self.flush_cache()
        try:
            if self._page:
                self._page.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None

    def _ensure_browser(self) -> bool:
        if self._page is not None:
            return True

        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return False

        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 800},
            )
            self._page = self._context.new_page()
            return True
        except Exception:
            self.close()
            return False

    def _parse_categories(self, raw_text: str) -> str:
        lines = [line.strip(" -\t") for line in (raw_text or "").splitlines() if line.strip()]
        if not lines:
            return ""

        joined = "|".join(lines)
        joined = re.sub(r"\|{2,}", "|", joined).strip("|")
        return joined

    def _lookup_with_page(self, domain: str) -> str:
        if not self._ensure_browser():
            return ""

        page = self._page
        lookup_url = (
            "https://sitelookup.mcafee.com/en/feedback/url"
            f"?action=checksingle&url={domain}"
        )

        try:
            response = page.goto(lookup_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if response is not None and int(response.status or 0) >= 400:
                return ""
        except Exception:
            return ""

        try:
            url_input = page.locator('input[name="url"]').first
            if url_input.count() > 0:
                url_input.fill(domain)
        except Exception:
            pass

        # The workflow clicks the check button once before reading category cells.
        click_xpaths = [
            'input[value="Check URL"]',
            "/html/body/div[1]/div[3]/div[2]/div[2]/div/div/div/div[2]/div[1]/div/form[1]/table/tbody/tr[4]/td/div/input",
            "//input[contains(@value,'Check URL')]",
            "//button[contains(.,'Check URL')]",
        ]
        for xp in click_xpaths:
            try:
                locator = page.locator(f"xpath={xp}" if xp.startswith(("/", "//")) else xp).first
                if locator.count() > 0:
                    locator.click(timeout=min(2500, self.timeout_ms))
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        candidates = [
            "/html/body/div[1]/div[3]/div[2]/div[2]/div/div/div/div[2]/div[1]/div/form[2]/table/tbody/tr[2]/td[4]",
            "//td[contains(@class,'category')]",
            "//th[contains(translate(.,'CATEGORY','category'),'category')]/following-sibling::td",
        ]

        for xp in candidates:
            try:
                locator = page.locator(f"xpath={xp}").first
                if locator.count() == 0:
                    continue
                text = locator.inner_text(timeout=min(3000, self.timeout_ms))
                parsed = self._parse_categories(text)
                if parsed:
                    return parsed
            except Exception:
                continue

        # Fallback: scan visible text for "Category" label near result sections.
        try:
            text_blob = page.inner_text("body", timeout=min(3000, self.timeout_ms))
            match = re.search(r"category\s*[:\-]?\s*([^\n]{2,120})", text_blob, re.IGNORECASE)
            if match:
                parsed = self._parse_categories(match.group(1))
                if parsed:
                    return parsed
        except Exception:
            pass

        return ""

    def _keyword_category(self, haystack: str) -> str:
        text = (haystack or "").lower()
        if not text:
            return ""

        keyword_map = {
            "News/Media": ["news", "journal", "headline", "breaking", "magazine", "press"],
            "Business/Finance": ["finance", "invest", "market", "bank", "stock", "business", "economy"],
            "Shopping/Ecommerce": ["shop", "buy", "cart", "checkout", "deal", "store", "price"],
            "Technology": ["software", "developer", "code", "tech", "cloud", "ai", "programming"],
            "Education": ["university", "school", "course", "learn", "education", "student"],
            "Health": ["health", "medical", "clinic", "doctor", "wellness", "hospital"],
            "Sports": ["sports", "football", "soccer", "nba", "nfl", "cricket", "match"],
            "Entertainment": ["movie", "music", "tv", "stream", "entertainment", "celebrity", "show"],
            "Travel": ["travel", "hotel", "flight", "trip", "tour", "booking"],
            "Gaming": ["game", "gaming", "xbox", "playstation", "steam", "esports"],
            "Government": ["gov", "government", "ministry", "public service", "official"],
        }

        scores: Counter[str] = Counter()
        for category, keywords in keyword_map.items():
            for kw in keywords:
                if re.search(r"\b" + re.escape(kw) + r"\b", text):
                    scores[category] += 1

        if not scores:
            return ""
        best = scores.most_common(1)[0]
        if best[1] <= 0:
            return ""
        return best[0]

    def _lookup_from_homepage(self, domain: str) -> str:
        urls = [f"https://{domain}", f"http://{domain}"]
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        for url in urls:
            try:
                resp = requests.get(url, headers=headers, timeout=max(3, self.timeout_ms // 1000), allow_redirects=True)
                if resp.status_code >= 400:
                    continue

                html = resp.text[:300000]
                pieces: list[str] = []

                title = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                if title:
                    pieces.append(title.group(1))

                for meta_name in ["description", "keywords", "og:description", "og:title"]:
                    m = re.search(
                        rf"<meta[^>]+(?:name|property)=['\"]{re.escape(meta_name)}['\"][^>]*content=['\"](.*?)['\"]",
                        html,
                        re.IGNORECASE | re.DOTALL,
                    )
                    if m:
                        pieces.append(m.group(1))

                body_text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
                body_text = re.sub(r"<style[\s\S]*?</style>", " ", body_text, flags=re.IGNORECASE)
                body_text = re.sub(r"<[^>]+>", " ", body_text)
                body_text = re.sub(r"\s+", " ", body_text)
                pieces.append(body_text[:20000])

                guessed = self._keyword_category(" ".join(pieces))
                if guessed:
                    return guessed
            except Exception:
                continue

        return ""

    def get_category(self, value: str) -> str:
        if not self.enabled:
            return ""

        domain = _normalize_domain(value)
        if not domain:
            return ""

        if domain in self._cache:
            return self._cache[domain]

        category = self._lookup_with_page(domain)
        if not category:
            category = self._lookup_from_homepage(domain)
        self._cache[domain] = category
        self._dirty = True
        return category

    def get_categories(self, values: list[str]) -> dict[str, str]:
        """Categorize a list of websites or domain names."""
        results: dict[str, str] = {}
        for val in values:
            domain = _normalize_domain(val)
            if domain:
                results[domain] = self.get_category(domain)
        return results

    def process_csv(
        self,
        input_csv_path: Path,
        output_csv_path: Optional[Path] = None,
    ) -> int:
        """Process a CSV with schema (index,inputDomain,category,finalUrl,statusCode,adsTxt)
        filling in missing categories.
        """
        import csv

        input_path = Path(input_csv_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"Input CSV not found: {input_path}")

        fieldnames = ["index", "inputDomain", "category", "finalUrl", "statusCode", "adsTxt"]
        rows = []
        with input_path.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp)
            # Use reader fieldnames or default to standard schema
            actual_fieldnames = list(reader.fieldnames) if reader.fieldnames else fieldnames
            for row in reader:
                domain = row.get("inputDomain") or row.get("domain") or ""
                if domain and not row.get("category"):
                    row["category"] = self.get_category(domain)
                rows.append(row)

        out_path = Path(output_csv_path) if output_csv_path else input_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=actual_fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        self.flush_cache()
        return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify website domains into content categories."
    )
    parser.add_argument(
        "--domain", "-d",
        nargs="+",
        help="One or more domain names or URLs to categorize (space-separated list).",
    )
    parser.add_argument(
        "--input-file", "-f",
        help="Path to a text or CSV file containing a list of domains (e.g. c-websites.csv or adLikelyUrls3.csv).",
    )
    parser.add_argument(
        "--output-csv", "-o",
        help="Path to output updated CSV with populated categories.",
    )
    parser.add_argument(
        "--cache-file",
        default=str(_DEFAULT_CACHE_PATH),
        help=f"Path to domain categories JSON cache file (default: {_DEFAULT_CACHE_PATH}).",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Show browser window during lookup (headed mode to pass bot challenges).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable reading from persistent cache.",
    )

    args = parser.parse_args()

    cache_path = None if args.no_cache else Path(args.cache_file)

    with WebsiteCategorizer(
        headless=not args.show_browser,
        cache_path=cache_path,
        use_cache=not args.no_cache,
    ) as categorizer:
        if args.input_file and (args.input_file.endswith(".csv") or args.output_csv):
            in_path = Path(args.input_file)
            out_path = Path(args.output_csv) if args.output_csv else None
            # Check if it has inputDomain column or header
            sample = in_path.read_text(encoding="utf-8", errors="replace")[:1000]
            if "inputDomain" in sample or "," in sample:
                count = categorizer.process_csv(in_path, out_path)
                print(f"Processed {count} rows in CSV: {in_path} -> {out_path or in_path}")
                return

        domains: list[str] = []
        if args.input_file:
            path = Path(args.input_file)
            if path.is_file():
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    d = line.strip().split(",")[0].strip()
                    if d and not d.startswith("#") and d != "index" and d != "inputDomain":
                        domains.append(d)

        if args.domain:
            domains.extend(args.domain)

        if not domains:
            domains = [
                "cnn.com", "github.com", "espn.com", "harvard.edu",
            ]

        print(f"{'Domain':<25} | {'Category'}")
        print("-" * 50)
        results = categorizer.get_categories(domains)
        for domain, cat in results.items():
            print(f"{domain:<25} | {cat}")


if __name__ == "__main__":
    main()

