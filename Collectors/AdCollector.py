import asyncio
import hashlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import ElementHandle, Frame, Page, TimeoutError as PlaywrightTimeoutError
from Collectors.AdDisclosureCollector import AdDisclosureCollector
from Helpers import utils as pageUtils
from Helpers.ad_choices_matcher import find_ad_choices_in_screenshot
from Helpers.ad_disclosure import AD_DISC_LINKS_TO_COLLECT, AD_DISCLOSURE_LINKS
from Helpers.crawl_context import CrawlContext


class AdCollector:
    COLLECTOR_NAME = "AdCollector"
    MAX_ADS_PER_PAGE = 20
    MAX_IFRAMES_PER_CONTEXT = 8
    MAX_FRAME_DEPTH = 4
    MIN_PX_FOR_SCREENSHOT = 30
    SCROLL_TIMEOUT_MS = 20_000
    ELEMENT_ACTION_TIMEOUT_MS = 1_000
    EXTRACTION_TIMEOUT_MS = 1_500
    AD_SCRAPE_TIMEOUT_MS = 15_000
    DISCLOSURE_DETECTION_TIMEOUT_MS = 2_000
    PASSIVE_DISCLOSURE_FRAME_DEPTH = 2
    CONTEXT_SCREENSHOT_MARGIN_PX = 150
    AD_SCREENSHOT_MARGIN_PX = 10
    _disclosure_host_selectors = (
        [f'a[href*="{h}"]' for h in AD_DISCLOSURE_LINKS]
        + [
            'a[href*="whythisad"]',
            'a[href*="adchoice"]',
            'a[href*="adinfo"]',
            'a[href*="aboutourads"]',
            'a[href*="about-our-ads"]',
            'a[href*="privacy/adinfo"]',
            'a#abgl',
            '#abgl',
        ]
    )
    ADCHOICES_SELECTOR = f":is({', '.join(_disclosure_host_selectors)})"
    _ADCHOICES_ICON_HINTS = [
        "adchoice",
        "adchoices",
        "whythisad",
        "why-this-ad",
        "why this ad",
        "adinfo",
        "about-our-ads",
        "aboutourads",
    ]
    ADCHOICES_ICON_SELECTOR = ":is(" + ", ".join(
        [f'img[src*="{hint}" i]' for hint in _ADCHOICES_ICON_HINTS]
        + [f'img[alt*="{hint}" i]' for hint in _ADCHOICES_ICON_HINTS]
        + [f'img[aria-label*="{hint}" i]' for hint in _ADCHOICES_ICON_HINTS]
        + [f'[aria-label*="{hint}" i]' for hint in _ADCHOICES_ICON_HINTS]
        + [f'[title*="{hint}" i]' for hint in _ADCHOICES_ICON_HINTS]
        + [f'[class*="{hint}" i]' for hint in _ADCHOICES_ICON_HINTS]
        + [f'[id*="{hint}" i]' for hint in _ADCHOICES_ICON_HINTS]
    ) + ")"
    URL_IN_TEXT_RE = re.compile(r"((?:https?:)?//[^\s'\"<>\)]+)", re.IGNORECASE)
    HTML_URL_ATTR_RE = re.compile(
        r"(?:href|src|data-href|data-url|data-destination-url|data-click-url)\s*=\s*[\"']([^\"']+)[\"']",
        re.IGNORECASE,
    )
    _ADCHOICE_URL_HINTS = tuple(set(
        [
            "whythisad",
            "adchoice",
            "adchoices",
            "adinfo",
            "aboutourads",
            "about-our-ads",
        ] + [h.lower() for h in AD_DISCLOSURE_LINKS]
    ))
    _ADCHOICE_TEXT_HINTS = tuple(set(
        [
            "why this ad",
            "why this ad?",
            "why am i seeing this ad",
            "adchoice",
            "adchoices",
            "about our ads",
            "about these ads",
            "ad info",
            "ad feedback",
        ] + [t.lower() for t in AD_DISC_LINKS_TO_COLLECT]
    ))

    _DEEP_ASSET_JS = """
    (rootNode, adChoiceSelector) => {
        const out = {
            links: [],
            imageLinks: [],
            otherLinks: [],
            imgs: [],
            bgImgs: [],
            videos: [],
            scripts: [],
            iframes: [],
            adChoicesLinks: [],
        };

        const seen = new Set();

        function addUnique(kind, key, payload) {
            const token = kind + ':' + (key || '');
            if (seen.has(token)) return;
            seen.add(token);
            if (kind === 'links') out.links.push(payload);
            else if (kind === 'imageLinks') out.imageLinks.push(payload);
            else if (kind === 'otherLinks') out.otherLinks.push(payload);
            else if (kind === 'imgs') out.imgs.push(payload);
            else if (kind === 'bgImgs') out.bgImgs.push(payload);
            else if (kind === 'videos') out.videos.push(payload);
            else if (kind === 'scripts') out.scripts.push(payload);
            else if (kind === 'iframes') out.iframes.push(payload);
            else if (kind === 'adChoicesLinks') out.adChoicesLinks.push(payload);
        }

        function normalizeUrl(raw) {
            if (!raw) return '';
            if (raw.startsWith('//')) return location.protocol + raw;
            return raw;
        }

        function pickSrcset(srcset) {
            if (!srcset) return '';
            const first = srcset.split(',')[0]?.trim() || '';
            if (!first) return '';
            return first.split(/\\s+/)[0] || '';
        }

        function googAdUrl(href) {
            try {
                return new URL(href, location.href).searchParams.get('adurl');
            } catch (_) {
                return null;
            }
        }

        function walk(node) {
            if (!node) return;

            if (node.nodeType === Node.ELEMENT_NODE) {
                const el = node;
                const tag = (el.tagName || '').toUpperCase();

                if (tag === 'A') {
                    const hrefRaw = el.getAttribute('href') || '';
                    const href = normalizeUrl(hrefRaw || el.href || '');
                    const entry = [{
                        googAdUrl: googAdUrl(href),
                        href,
                        outerHTML: (el.outerHTML || '').slice(0, 2000),
                    }];
                    if (href) addUnique('links', href, entry);

                    const media = el.querySelector('img, source, video, picture img, picture source');
                    if (media) {
                        const imgSrc = normalizeUrl(
                            media.getAttribute('src') ||
                            pickSrcset(media.getAttribute('srcset') || '') ||
                            media.currentSrc ||
                            media.src ||
                            ''
                        );
                        addUnique('imageLinks', href + '|' + imgSrc, {
                            googAdUrl: googAdUrl(href),
                            href,
                            imgSrc: imgSrc || null,
                            outerHTML: (el.outerHTML || '').slice(0, 2000),
                        });
                    } else {
                        addUnique('otherLinks', href, {
                            googAdUrl: googAdUrl(href),
                            href,
                            text: (el.innerText || '').trim().slice(0, 500),
                            outerHTML: (el.outerHTML || '').slice(0, 2000),
                        });
                    }

                    const elText = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().toLowerCase();
                    const isDiscText = elText.includes('adchoice') || elText.includes('why this ad') ||
                                       elText.includes('about our ads') || elText.includes('about these ads') ||
                                       elText.includes('ad info') || elText.includes('why am i seeing');
                    if (href && ((adChoiceSelector && el.matches && el.matches(adChoiceSelector)) || isDiscText)) {
                        addUnique('adChoicesLinks', href, href);
                    }
                }

                if (tag === 'IMG' || tag === 'SOURCE') {
                    const src = normalizeUrl(
                        el.getAttribute('src') ||
                        pickSrcset(el.getAttribute('srcset') || '') ||
                        el.currentSrc ||
                        el.src ||
                        ''
                    );
                    if (src) {
                        const box = el.getBoundingClientRect();
                        addUnique('imgs', src, {
                            x: box.x,
                            y: box.y,
                            width: box.width,
                            height: box.height,
                            src,
                            outerHTML: (el.outerHTML || '').slice(0, 2000),
                        });
                    }
                }

                if (tag === 'VIDEO') {
                    const src = normalizeUrl(el.getAttribute('src') || el.currentSrc || el.src || '');
                    if (src) addUnique('videos', src, { src, width: el.videoWidth || el.width || 0, height: el.videoHeight || el.height || 0 });
                }

                if (tag === 'SCRIPT') {
                    const src = normalizeUrl(el.getAttribute('src') || el.src || '');
                    if (src) addUnique('scripts', src, src);
                }

                if (tag === 'IFRAME') {
                    const src = normalizeUrl(el.getAttribute('src') || el.src || '');
                    if (src) addUnique('iframes', src, src);

                    // Same-origin iframe documents can contain the actual ad creative.
                    // Cross-origin access will fail and is intentionally ignored.
                    try {
                        const doc = el.contentDocument;
                        if (doc) {
                            for (const a of doc.querySelectorAll('a[href]')) {
                                const href = normalizeUrl(a.getAttribute('href') || a.href || '');
                                if (!href) continue;
                                addUnique('links', href, [{
                                    googAdUrl: googAdUrl(href),
                                    href,
                                    outerHTML: (a.outerHTML || '').slice(0, 2000),
                                }]);
                            }
                            for (const img of doc.querySelectorAll('img, source')) {
                                const src2 = normalizeUrl(
                                    img.getAttribute('src') ||
                                    pickSrcset(img.getAttribute('srcset') || '') ||
                                    img.currentSrc ||
                                    img.src ||
                                    ''
                                );
                                if (!src2) continue;
                                addUnique('imgs', src2, {
                                    x: 0,
                                    y: 0,
                                    width: 0,
                                    height: 0,
                                    src: src2,
                                    outerHTML: (img.outerHTML || '').slice(0, 2000),
                                });
                            }
                        }
                    } catch (_) {}
                }

                try {
                    const bg = el.currentStyle?.backgroundImage || window.getComputedStyle(el).backgroundImage;
                    if (bg && bg !== 'none') {
                        const raw = bg.replace(/^url\\((.*)\\)$/, '$1').replace(/^['\"]|['\"]$/g, '');
                        const norm = normalizeUrl(raw);
                        if (norm) {
                            const box = el.getBoundingClientRect();
                            addUnique('bgImgs', norm, {
                                x: box.x,
                                y: box.y,
                                width: box.width,
                                height: box.height,
                                src: norm,
                                outerHTML: (el.outerHTML || '').slice(0, 2000),
                            });
                        }
                    }
                } catch (_) {}

                if (el.shadowRoot) walk(el.shadowRoot);
                if (tag === 'SLOT' && el.assignedElements) {
                    for (const assigned of el.assignedElements({ flatten: true })) walk(assigned);
                }
            }

            if (node.nodeType === Node.ELEMENT_NODE || node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
                for (const child of node.children || []) walk(child);
            }
        }

        walk(rootNode || document.documentElement);
        return out;
    }
    """

    _FIND_ADS_JS = """
    (selectors) => {
        // ── Phase 1: Flatten the full DOM tree (including shadow roots) ───────
        const allEls  = [];      // every Element in the tree
        const allRoots = [];     // every shadow root encountered

        function harvest(root) {
            const nodes = Array.from(root.querySelectorAll ? root.querySelectorAll('*') : []);
            allEls.push(...nodes);
            for (const el of nodes) {
                if (el.shadowRoot) {
                    allRoots.push(el.shadowRoot);
                    harvest(el.shadowRoot);
                }
            }
        }
        harvest(document);

        // adMap: element → first matched rule string (replaces bare adSet)
        const adMap = new Map();
        const CHUNK = 150;  // selectors per :is() call

        function addAd(el, rule) {
            if (!adMap.has(el)) adMap.set(el, rule);
        }

        // ── Phase 2a: Fast CSS selector scan via native querySelectorAll ─────
        const queryRoots = [document, ...allRoots];
        for (let i = 0; i < selectors.length; i += CHUNK) {
            const chunk = selectors.slice(i, i + CHUNK);
            const isSelector = ':is(' + chunk.join(',') + ')';
            for (const root of queryRoots) {
                try {
                    const matches = root.querySelectorAll(isSelector);
                    for (const el of matches) {
                        if (!adMap.has(el)) {
                            let matchedRule = chunk[0];
                            for (const s of chunk) {
                                try { if (el.matches(s)) { matchedRule = s; break; } } catch (_) {}
                            }
                            addAd(el, 'selector:' + matchedRule);
                        }
                    }
                } catch (_) {
                    for (const s of chunk) {
                        try {
                            const matches = root.querySelectorAll(s);
                            for (const el of matches) addAd(el, 'selector:' + s);
                        } catch (_2) {}
                    }
                }
            }
        }

        // ── Phase 2b: Explicit ad data-attribute detection ────────────────────
        const adAttrs = [
            'data-ad','data-ad-slot','data-ad-unit','data-adunit','data-ad-client',
            'data-adzone','data-zone','data-ad-id','data-is-ad','data-advertiserwho',
            'data-ad-type','data-sponsored','data-partner',
        ];
        for (const el of allEls) {
            const attr = adAttrs.find(a => el.hasAttribute(a));
            if (attr) addAd(el, 'attr:' + attr);
        }

        // ── Phase 2c: Aria-label "advertisement" / "sponsored" ────────────────
        for (const el of allEls) {
            const lbl = (el.getAttribute('aria-label') || '').trim().toLowerCase();
            if (lbl === 'advertisement' || lbl === 'sponsored' ||
                lbl === 'ad' || lbl.startsWith('sponsored by')) {
                addAd(el, 'aria-label:' + lbl);
            }
            if (el.getAttribute('role') === 'region' &&
                (lbl.includes('ad') || lbl.includes('sponsor'))) {
                addAd(el, 'role:region[' + lbl + ']');
            }
        }

        // ── Phase 2d: Class / ID name heuristic regex ─────────────────────────
        const nameRe = /\b(ad[_-]?(slot|unit|banner|container|wrapper|zone|placement|block|space|area|layout|ads)|adslot|adunit|adzone|adspace|advertisement|advert|adChoices?|adSlug|adBanner|place-?ad|placed-?ad)\b|(displayAd|display-ads|dfp-ad)/i;
        for (const el of allEls) {
            const cls = typeof el.className === 'string' ? el.className : '';
            const eid = el.id || '';
            if (nameRe.test(cls)) addAd(el, 'classname:' + cls.slice(0, 80));
            else if (nameRe.test(eid)) addAd(el, 'id:' + eid.slice(0, 80));
        }

        // ── Phase 2e: iframe source-domain hints ──────────────────────────────
        const adDoms = [
            'doubleclick','googlesyndication','bing.com','pubcenter',
            'outbrain','moatads','pubmatic','openx','adnxs','yieldmanager',
            'advertising.com','ads.msn','microsoft.com/ads',
        ];
        for (const el of allEls) {
            if (el.tagName !== 'IFRAME') continue;
            const r = el.getBoundingClientRect();
            const w = Math.round(r.width), h = Math.round(r.height);
            if (w < 50 || h < 30) continue;
            const src = (el.src || el.getAttribute('src') || '').toLowerCase();
            const domMatch = adDoms.find(d => src.includes(d));
            if (domMatch) addAd(el, 'iframe-domain:' + domMatch);
        }

        // ── Phase 2f: Visible text label sniffing ─────────────────────────────
        const adLabels = new Set(['advertisement','sponsored','paid content']);
        function scanText(root) {
            try {
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
                let node;
                while ((node = walker.nextNode())) {
                    const t = node.textContent.trim().toLowerCase();
                    if (!adLabels.has(t)) continue;
                    let parent = node.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (!parent || parent.tagName === 'BODY') break;
                        const r = parent.getBoundingClientRect();
                        if (r.width >= 100 && r.height >= 50) { addAd(parent, 'text-label:' + t); break; }
                        parent = parent.parentElement;
                    }
                }
            } catch (_) {}
        }
        scanText(document);
        for (const root of allRoots) scanText(root);

        // ── Deduplicate: structural nested ads (grids vs wrappers) ────────────
        const adSet = new Set(adMap.keys());
        
        // Build an ad tree to understand parent-child relationships among ad elements.
        const adChildren = new Map();
        for (const ad of adSet) adChildren.set(ad, []);
        
        for (const ad of adSet) {
            let cur = ad.parentNode;
            while (cur) {
                if (adSet.has(cur)) {
                    adChildren.get(cur).push(ad);
                    break; // Attach only to the closest ad ancestor
                }
                cur = cur.parentNode || cur.host;
            }
        }
        
        const toRemove = new Set();
        
        // Process tree from roots to leaves using a post-order traversal logical structure.
        function processAdNode(ad) {
            const children = adChildren.get(ad) || [];
            for (const child of children) {
                processAdNode(child);
            }
            
            // If the ad element contains MULTIPLE nested ad-elements, it is likely
            // a Grid/Feed container. In this case, we REMOVE
            // the container and KEEP the individual child ad cards.
            if (children.length > 1) {
                toRemove.add(ad);
            } 
            // If the ad element contains EXACTLY 1 nested ad-element, it is simply
            // a structural wrapper (like an extra DIV around an IFRAME).
            // We usually want to keep the parent and remove the child, 
            // EXCEPT if the child was itself determined to be a Container/Grid.
            else if (children.length === 1) {
                if (toRemove.has(children[0])) {
                    // The child was a container. Thus, this wrapper also wraps a container.
                    toRemove.add(ad);
                } else {
                    // Typical wrapper logic: keep parent, hide the inner ad.
                    toRemove.add(children[0]);
                }
            }
        }
        
        // Find roots (ads that have no ad-ancestors)
        const isRoot = new Set(adSet);
        for (const children of adChildren.values()) {
            for (const child of children) isRoot.delete(child);
        }
        
        for (const root of isRoot) {
            processAdNode(root);
        }
        
        toRemove.forEach(el => { adSet.delete(el); adMap.delete(el); });

        // ── XPath helper ──────────────────────────────────────────────────────
        // Returns a unique XPath expression for the element.
        // If the element has an id we use //*[@id="…"] (shortest/most stable).
        // Otherwise we walk up the DOM counting same-tag siblings.
        function getXPath(el) {
            if (el.id) {
                // Strip double-quotes (IDs containing " are vanishingly rare)
                const safeId = el.id.replace(/"/g, '');
                return '//*[@id="' + safeId + '"]';
            }
            const parts = [];
            let node = el;
            while (node && node.nodeType === Node.ELEMENT_NODE) {
                let idx = 1;
                let sib = node.previousSibling;
                while (sib) {
                    if (sib.nodeType === Node.ELEMENT_NODE && sib.tagName === node.tagName) idx++;
                    sib = sib.previousSibling;
                }
                parts.unshift(node.tagName.toLowerCase() + '[' + idx + ']');
                const parent = node.parentNode;
                if (!parent || parent.nodeType !== Node.ELEMENT_NODE) break;
                node = parent;
            }
            return '/' + parts.join('/');
        }

        // ── Links helper ──────────────────────────────────────────────────────
        // Collects all href/src links reachable from within the ad element's
        // subtree, including shadow-DOM descendants and same-origin iframe DOMs.
        function getLinks(el) {
            const links = new Set();
            function processEl(e) {
                if (e.tagName === 'A') {
                    try { if (e.href && !e.href.startsWith('javascript:')) links.add(e.href); } catch(_) {}
                } else if (e.tagName === 'IFRAME') {
                    const src = e.src || e.getAttribute('src') || '';
                    if (src && !src.startsWith('javascript:')) links.add(src);
                    // Try to reach into same-origin iframe documents
                    try {
                        const doc = e.contentDocument;
                        if (doc) {
                            for (const a of doc.querySelectorAll('a[href]')) {
                                try { if (a.href && !a.href.startsWith('javascript:')) links.add(a.href); } catch(_) {}
                            }
                        }
                    } catch (_) {}  // cross-origin: SecurityError is expected
                }
                if (e.shadowRoot) {
                    try {
                        for (const child of e.shadowRoot.querySelectorAll('a[href], iframe')) processEl(child);
                    } catch(_) {}
                }
            }
            // Handle the root element itself
            processEl(el);
            // Walk all descendants
            try {
                for (const child of el.querySelectorAll('a[href], iframe')) processEl(child);
            } catch(_) {}
            return Array.from(links);
        }

        // ── Collect geometry and return ───────────────────────────────────────
        const scrollX = window.scrollX;
        const scrollY = window.scrollY;

        return Array.from(adMap.entries()).map(([el, matchedRule]) => {
            const r = el.getBoundingClientRect();
            const absX = r.x + scrollX;
            const absY = r.y + scrollY;
            const fallbackSrcEl = el.querySelector('iframe[src], img[src], video[src], source[src], embed[src], object[data]');
            const fallbackSrc = fallbackSrcEl ? (fallbackSrcEl.src || fallbackSrcEl.getAttribute('src') || fallbackSrcEl.getAttribute('data') || '') : '';
            const fallbackAriaEl = el.querySelector('[aria-label]');
            const outerHTML = (el.outerHTML || '').slice(0, 8000);
            const intersectsViewPort = r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth;
            const links = getLinks(el);
            
            // Heuristic to detect even more ads based on link density or specific keywords in text
            const textContent = (el.innerText || '').toLowerCase();
            const hasSponsoredText = textContent.includes('sponsored') || textContent.includes('advertisement');
            const hasManyLinks = links.length > 2;

            return {
                id: el.id || '',
                type: el.getAttribute('type') || '',
                nodeType: el.tagName,
                name: el.getAttribute('name') || '',
                class: typeof el.className === 'string' ? el.className.slice(0, 300) : '',
                innerText: (textContent).slice(0, 4000),
                src: el.getAttribute('src') || fallbackSrc,
                ariaLabel: el.getAttribute('aria-label') || (fallbackAriaEl ? (fallbackAriaEl.getAttribute('aria-label') || '') : ''),
                placeholder: el.getAttribute('placeholder') || '',
                xpath: getXPath(el),
                borderStyle: (el.style && el.style.border) || '',
                outerHTML,
                x: absX,
                y: absY,
                width: r.width,
                height: r.height,
                intersectsViewPort,
                matchedRule: matchedRule || (hasSponsoredText ? 'heuristic:sponsored-text' : (hasManyLinks ? 'heuristic:link-density' : 'unknown')),
            };
        }).filter(d => d.width >= 30 && d.height >= 30);
    }
    """

    _SCROLL_JS = """
    async () => {
        const step = 400;
        const MAX_H = 30000;           // cap – avoid infinite-scroll pages freezing
        const MAX_TIME = 12000;        // max 12 seconds of active scroll work
        const WAIT_AFTER_PASS1 = 2000; // stay at bottom for lazy content
        const startTime = Date.now();
        const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

        const safeBreak = () => Date.now() - startTime > MAX_TIME + WAIT_AFTER_PASS1 + 800;

        // ── Pass 1: slow sweep ───────────────────────────────────────────────
        let pos = 0;
        while (pos < Math.min(document.body.scrollHeight, MAX_H)) {
            if (safeBreak()) break;
            if (Date.now() - startTime > MAX_TIME) break;
            window.scrollTo(0, pos);
            await delay(450);
            pos += step;
        }

        // a short top/bottom jitter to awaken further lazy loaders
        window.scrollTo(0, Math.max(0, document.body.scrollHeight - window.innerHeight));
        await delay(WAIT_AFTER_PASS1);

        // ── Pass 2: faster forward sweep ─────────────────────────────────────
        pos = Math.max(0, window.scrollY);
        while (pos < Math.min(document.body.scrollHeight, MAX_H)) {
            if (safeBreak()) break;
            window.scrollTo(0, pos);
            await delay(175);
            pos += step * 2;
        }

        // Return near top; this helps subsequent capture operations be consistent.
        window.scrollTo(0, 0);
        await delay(300);
    }
    """

    def init(
        self,
        output_dir: str,
        logger,
        url_hash: str,
        max_ads_captured: int | None = None,
        crawl_context: CrawlContext | None = None,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash
        self._crawl_context = crawl_context
        (self._output_dir / "ad_images").mkdir(parents=True, exist_ok=True)
        (self._output_dir / "ad_videos").mkdir(parents=True, exist_ok=True)
        (self._output_dir / "ad_disclosures").mkdir(parents=True, exist_ok=True)
        from Helpers.easylist_selectors import load_selectors
        self._selectors = load_selectors()
        self._visited_ad_urls: list[str] = []
        self._ad_disclosure_collector = AdDisclosureCollector()
        self._ad_disclosure_collector.init(str(self._output_dir), self._logger, self._url_hash, crawl_context=self._crawl_context)
        self._ad_disclosures_contents: list[dict] = []
        self._unmatched_ad_disclosure_contents: list[dict] = []
        self._n_clicked_adchoices_links = 0
        self._ad_attrs: list[dict] = []
        self._candidate_records: list[dict] = []
        if isinstance(max_ads_captured, int) and max_ads_captured > 0:
            self._max_ads_captured = max_ads_captured
        elif isinstance(self.MAX_ADS_PER_PAGE, int) and self.MAX_ADS_PER_PAGE > 0:
            self._max_ads_captured = self.MAX_ADS_PER_PAGE
        else:
            self._max_ads_captured = None

        self._detected_ads: list[dict] = []
        self._cdp = None
        self._frame_info_cache: dict[int, dict] = {}
        self._cdp_contexts: dict[int, str] = {}
        self._cdp_frame_tree: dict = {}

        self._scrape_results: dict[str, int] = {}
        self._n_small_ads = 0
        self._n_empty_ads = 0
        self._n_removed_ads = 0
        self._n_skipped_ads = 0
        self._n_timed_out_ads = 0
        self._n_ad_disclosure_matched = 0
        self._n_ad_disclosure_unmatched = 0

    def _frame_identifier(self, frame=None, url: str = "", cdp_frame_id: str | None = None) -> str:
        """Resolve a distinguishable frame identifier using real browser identifiers."""
        if cdp_frame_id:
            return cdp_frame_id
        if frame is not None:
            info = self._frame_info_cache.get(id(frame), {})
            if info.get("frameId"):
                return info["frameId"]
            return f"frame_{id(frame)}"
        return f"frame_{uuid.uuid4().hex[:8]}"


    def get_partial_results(self) -> dict:
        """Return partial ad collection results captured before a timeout or interruption."""
        n_detected = len(self._detected_ads)
        n_scraped = len(self._ad_attrs)
        n_small = self._n_small_ads
        n_empty = self._n_empty_ads
        n_removed = self._n_removed_ads
        n_skipped = self._n_skipped_ads
        n_timed_out = self._n_timed_out_ads

        # If detected ads were cut off by a stage timeout before all candidates could be evaluated,
        # attribute the unaccounted remaining candidates to timed_out so the totals reconcile.
        accounted = n_scraped + n_small + n_empty + n_removed + n_skipped + n_timed_out
        unaccounted = max(0, n_detected - accounted)
        if unaccounted > 0:
            n_timed_out += unaccounted

        candidate_records = list(self._candidate_records) if self._candidate_records else []
        if self._detected_ads and len(candidate_records) < n_detected:
            scraped_ids = {a.get("id") for a in self._ad_attrs if a.get("id")}
            scraped_cand_ids = {a.get("ad_candidate_id") for a in self._ad_attrs if a.get("ad_candidate_id")}
            records = []
            for idx, ad in enumerate(self._detected_ads):
                if not ad.get("ad_candidate_id"):
                    cand_id = (
                        self._crawl_context.next_candidate_id()
                        if self._crawl_context
                        else f"cand_{idx + 1:03d}"
                    )
                    ad["ad_candidate_id"] = cand_id

                st = ad.get("_candidate_status")
                cand_id = ad.get("ad_candidate_id")
                ad_id = ad.get("id")
                is_scraped = (
                    bool(ad.get("ad_impression_id"))
                    or (cand_id in scraped_cand_ids)
                    or (ad_id and ad_id in scraped_ids)
                    or st in ("scraped", "retained")
                )

                if is_scraped:
                    status_val = "retained"
                elif st in ("small", "empty", "removed", "skipped", "timed_out"):
                    status_val = st
                else:
                    status_val = "timed_out"
                    ad["_candidate_status"] = "timed_out"

                records.append({
                    "ad_candidate_id": cand_id,
                    "ad_impression_id": ad.get("ad_impression_id"),
                    "candidate_status": status_val,
                    "matchedRule": ad.get("matchedRule"),
                    "nodeType": ad.get("nodeType"),
                    "id": ad.get("id"),
                    "width": ad.get("width"),
                    "height": ad.get("height"),
                    "x": ad.get("x"),
                    "y": ad.get("y"),
                })
            candidate_records = records
            self._candidate_records = candidate_records

        return {
            "scrapeResults": {
                "nDetectedAds": n_detected,
                "nAdsScraped": n_scraped,
                "nSmallAds": n_small,
                "nEmptyAds": n_empty,
                "nRemovedAds": n_removed,
                "nSkippedAds": n_skipped,
                "nTimedOutAds": n_timed_out,
                "nAdDisclosureMatched": self._n_ad_disclosure_matched,
                "nAdDisclosureUnmatched": self._n_ad_disclosure_unmatched,
                "nClickedAdChoices": self._n_clicked_adchoices_links,
            },
            "adAttrs": list(self._ad_attrs),
            "candidateAds": list(self._candidate_records),
            "visitedAdUrls": list(self._visited_ad_urls),
            "unmatchedAdDisclosureContents": list(self._unmatched_ad_disclosure_contents),
        }

    async def _init_cdp_frame_tracking(self, page: Page) -> None:
        try:
            self._cdp = await page.context.new_cdp_session(page)
            def _on_ctx(ev: dict) -> None:
                ctx = ev.get("context", {})
                c_id = ctx.get("id")
                f_id = (ctx.get("auxData") or {}).get("frameId")
                if c_id is not None and f_id:
                    self._cdp_contexts[c_id] = f_id
            self._cdp.on("Runtime.executionContextCreated", _on_ctx)
            await self._cdp.send("Page.enable")
            await self._cdp.send("Runtime.enable")
            tree = await self._cdp.send("Page.getFrameTree")
            self._cdp_frame_tree = tree.get("frameTree", {})
        except Exception as exc:
            self._logger.debug(f"[AdCollector] CDP frame tracking init error: {exc}")

    async def collect(self, page: Page) -> dict:
        await self._init_cdp_frame_tracking(page)
        try:
            await self._ad_disclosure_collector.pre_crawl(page)
        except Exception as exc:
            self._logger.debug(f"[AdCollector] Could not register disclosure collector: {exc}")

        # Scroll page to trigger lazy loaded ads and dynamic ad networks
        await self._scroll_page(page)

        ads = await self._find_ads(page)
        self._detected_ads = ads
        ads.sort(key=lambda item: (item.get("y", 0), item.get("x", 0)))
        self._logger.info(f"[AdCollector] Detected {len(ads)} candidate ad element(s)")
        for idx, ad_item in enumerate(ads):
            node_tag = ad_item.get('nodeType', '')
            node_id = f"#{ad_item['id']}" if ad_item.get('id') else ""
            rule_str = ad_item.get('matchedRule', 'unknown')
            self._logger.debug(
                f"[AdCollector] Candidate {idx}: {node_tag}{node_id} ({int(ad_item.get('width', 0))}x{int(ad_item.get('height', 0))}) [rule: {rule_str}]"
            )
        try:
            ad_attrs, scrape_results = await self._capture_ads(page, ads)
            self._ad_attrs = ad_attrs
            self._scrape_results = scrape_results

            # Passive collection complete — disclosures will be interacted with in the dedicated disclosure_interaction phase
            self._logger.info(f"[AdCollector] Captured {len(ad_attrs)} ad screenshot(s) (passive collection)")
            return self.get_partial_results()
        finally:
            if self._cdp:
                try:
                    await self._cdp.detach()
                except Exception:
                    pass
                self._cdp = None

    async def _click_any_page_adchoice_fallback(self, page: Page) -> str:
        contexts: list[Page | Frame] = [page, *page.frames]

        async def _scan_handles() -> list[ElementHandle]:
            found: list[ElementHandle] = []
            for ctx in contexts:
                try:
                    handles = await ctx.query_selector_all(self.ADCHOICES_SELECTOR)
                except Exception:
                    handles = []
                if handles:
                    found.extend(handles)
            return found

        handles = await _scan_handles()

        if not handles:
            await page.wait_for_timeout(250)
            handles = await _scan_handles()

        for handle in handles:
            try:
                href = await handle.evaluate("el => el.href")
                if href and any(domain in href for domain in AD_DISCLOSURE_LINKS):
                    return href
            except Exception:
                href = ""

            if not href:
                try:
                    href = await self._extract_adchoice_href_from_handle(handle)
                except Exception:
                    href = ""

            if not href:
                continue

            disclosure = await self._ad_disclosure_collector.open_disclosure_in_new_tab(
                page,
                href,
                ad_screenshot_name="ad_disclosure_page.png",
            )
            if disclosure:
                self._n_clicked_adchoices_links += 1
                self._logger.info(f"[AdCollector] Page-level fallback opened adchoice disclosure: {href[:120]}")
                return href

        return ""

    @staticmethod
    def _disclosure_url_key(url: str) -> tuple[str, str]:
        """Return (hostname, path) for fuzzy disclosure URL matching."""
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "/").rstrip("/") or "/"
            return host, path
        except Exception:
            return "", ""

    def _match_adchoice_link(self, ads_attrs: list[dict]) -> tuple[int, int]:
        """Match disclosure pages to ads using hostname+path prefix matching.

        This avoids the old exact-URL-equality approach which broke when two
        ads shared the same disclosure host but had different query-string parameters.

        Each disclosure is assigned to at most one ad, and each ad receives
        at most one disclosure (first-match wins, preventing double-assignment).
        """
        self._unmatched_ad_disclosure_contents = []
        already_matched_ad_indices: set[int] = set()

        for disclosure in self._ad_disclosures_contents:
            disclosure_url = disclosure.get("adDiscUrl", "")
            disc_host, disc_path = self._disclosure_url_key(disclosure_url)
            matched = False

            for idx, ad_attrs in enumerate(ads_attrs):
                if idx in already_matched_ad_indices:
                    continue

                clicked = ad_attrs.get("clickedAdChoiceLink", "")
                if not clicked:
                    continue

                # Try exact match first (fastest)
                if clicked == disclosure_url:
                    matched = True
                else:
                    # Fall back to hostname + path-prefix matching
                    ad_host, ad_path = self._disclosure_url_key(clicked)
                    if ad_host and ad_host == disc_host and (
                        ad_path == disc_path or ad_path.startswith(f"{disc_path}/")
                    ):
                        matched = True

                if matched:
                    ad_attrs["adDisclosureOutLinks"] = disclosure.get("adDisclosureOutLinks", [])
                    ad_attrs["adDisclosureText"] = disclosure.get("pageText", "")
                    ad_attrs["adDisclosurePageUrl"] = disclosure.get("pageUrl", "")
                    ad_attrs["adDisclosureScreenshot"] = disclosure.get("screenshot", "")
                    already_matched_ad_indices.add(idx)
                    break

            if not matched:
                self._unmatched_ad_disclosure_contents.append(disclosure)

        n_unmatched = len(self._unmatched_ad_disclosure_contents)
        n_disclosures = len(self._ad_disclosures_contents)
        n_matched = max(0, n_disclosures - n_unmatched)

        if n_disclosures:
            self._logger.info(
                f"[AdCollector] Matched ad disclosures: {n_matched} of {n_disclosures}"
            )

        return n_matched, n_unmatched

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _scroll_page(self, page: Page) -> None:
        try:
            scroll_height = await page.evaluate("() => Math.min(document.body.scrollHeight || document.documentElement.scrollHeight, 12000)")
            scroll_height = int(scroll_height or 0)
            step = 600
            for pos in range(0, scroll_height + 1, step):
                await page.evaluate(f"() => window.scrollTo(0, {pos})")
                await page.wait_for_timeout(80)

            await page.wait_for_timeout(300)
            await page.evaluate("() => window.scrollTo(0, 0)")
            await page.wait_for_timeout(200)
        except Exception as exc:
            self._logger.warning(f"[AdCollector] Scroll error: {exc}")

    async def _find_ads(self, page: Page) -> list:
        if getattr(page, "is_closed", lambda: True)():
            self._logger.warning("[AdCollector] Target page closed; skipping ad detection")
            return []
        try:
            return await page.evaluate(self._FIND_ADS_JS, self._selectors)
        except Exception as exc:
            err_str = str(exc)
            if any(pattern in err_str for pattern in ("Target page", "closed", "Connection closed", "destroyed")):
                self._logger.warning(f"[AdCollector] DOM query skipped (page/browser closed): {exc}")
            else:
                self._logger.error(f"[AdCollector] DOM query error: {exc}")
            return []

    async def _capture_context_screenshot(self, page: Page, bbox: dict, index: int, element_handle: ElementHandle | None = None) -> tuple[str, dict]:
        viewport = await page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight, scrollX: window.scrollX, scrollY: window.scrollY })")

        # Prefer live viewport coordinates directly from element_handle if available and in-viewport
        page_left = None
        page_top = None
        bbox_w = bbox.get("width", 1)
        bbox_h = bbox.get("height", 1)
        if element_handle is not None:
            try:
                live_box = await element_handle.bounding_box()
                if live_box and live_box.get("width", 0) > 0 and live_box.get("height", 0) > 0:
                    page_left = live_box["x"]
                    page_top = live_box["y"]
                    bbox_w = live_box["width"]
                    bbox_h = live_box["height"]
            except Exception:
                pass

        if page_left is None or page_top is None:
            page_left = bbox["x"] - viewport["scrollX"]
            page_top = bbox["y"] - viewport["scrollY"]

        page_right = page_left + bbox_w
        page_bottom = page_top + bbox_h

        viewport_left = 0
        viewport_top = 0
        viewport_right = viewport["width"]
        viewport_bottom = viewport["height"]

        margin = self.CONTEXT_SCREENSHOT_MARGIN_PX
        clip_left = max(viewport_left, int(page_left) - margin)
        clip_top = max(viewport_top, int(page_top) - margin)
        clip_right = min(viewport_right, int(page_right + 0.9999) + margin)
        clip_bottom = min(viewport_bottom, int(page_bottom + 0.9999) + margin)

        context_dir = self._output_dir / "ad_context_images"
        context_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = context_dir / f"ad_{index}_{self._url_hash}_context.png"

        if clip_right <= clip_left or clip_bottom <= clip_top:
            # The ad is completely outside the viewport (e.g. an off-screen carousel slide).
            # Capture the current viewport as fallback context without raising an error.
            try:
                await page.screenshot(path=str(screenshot_path), full_page=False, timeout=1500)
                context_box = {
                    "x": max(0, min(viewport_right - 1, int(page_left))),
                    "y": max(0, min(viewport_bottom - 1, int(page_top))),
                    "width": max(1, int(bbox_w)),
                    "height": max(1, int(bbox_h)),
                    "outside_viewport": True,
                }
                return screenshot_path.name, context_box
            except Exception:
                return "", {}

        clip_width = clip_right - clip_left
        clip_height = clip_bottom - clip_top

        context_box = {
            "x": max(0, int(page_left) - clip_left),
            "y": max(0, int(page_top) - clip_top),
            "width": max(1, int(page_right + 0.9999) - int(page_left)),
            "height": max(1, int(page_bottom + 0.9999) - int(page_top)),
        }

        try:
            await page.screenshot(
                path=str(screenshot_path),
                clip={
                    "x": clip_left,
                    "y": clip_top,
                    "width": clip_width,
                    "height": clip_height,
                },
                timeout=3000,
            )
        except Exception:
            try:
                await page.screenshot(path=str(screenshot_path), full_page=False, timeout=1500)
            except Exception:
                return "", {}
        return screenshot_path.name, context_box

    def _sanitize_bbox(self, bbox: dict) -> dict | None:
        width = max(1, int(bbox.get("width", 0)))
        height = max(1, int(bbox.get("height", 0)))
        x = max(0, int(bbox.get("x", 0)))
        y = max(0, int(bbox.get("y", 0)))
        if width < self.MIN_PX_FOR_SCREENSHOT or height < self.MIN_PX_FOR_SCREENSHOT:
            return None
        return {"x": x, "y": y, "width": width, "height": height}

    def _union_bbox(self, a: dict, b: dict) -> dict | None:
        a_s = self._sanitize_bbox(a)
        b_s = self._sanitize_bbox(b)
        if not a_s and not b_s:
            return None
        if not a_s:
            return b_s
        if not b_s:
            return a_s

        left = min(a_s["x"], b_s["x"])
        top = min(a_s["y"], b_s["y"])
        right = max(a_s["x"] + a_s["width"], b_s["x"] + b_s["width"])
        bottom = max(a_s["y"] + a_s["height"], b_s["y"] + b_s["height"])
        return self._sanitize_bbox({
            "x": left,
            "y": top,
            "width": right - left,
            "height": bottom - top,
        })

    async def _scroll_bbox_into_view(self, page: Page, bbox: dict) -> None:
        """Scroll so the bbox is roughly centered vertically (and horizontally).

        This improves screenshot consistency by keeping the ad in the middle of the
        viewport instead of pinned to the top.
        """
        # Compute the desired scroll position to center the bbox in the viewport.
        # Clamp to [0, maxScroll].
        scroll_pos = await page.evaluate(
            """(bbox) => {
                const viewportH = window.innerHeight;
                const viewportW = window.innerWidth;
                const centerY = bbox.y + bbox.height / 2;
                const centerX = bbox.x + bbox.width / 2;
                const scrollingEl = document.scrollingElement || document.documentElement || document.body;
                const maxScrollY = Math.max(0, scrollingEl.scrollHeight - viewportH);
                const maxScrollX = Math.max(0, scrollingEl.scrollWidth - viewportW);
                const desiredY = Math.min(maxScrollY, Math.max(0, Math.round(centerY - viewportH / 2)));
                const targetY = desiredY;
                const targetX = Math.min(maxScrollX, Math.max(0, Math.round(centerX - viewportW / 2)));
                window.scrollTo(targetX, targetY);
                return { x: targetX, y: targetY };
            }""",
            bbox,
        )
        # Give the page a brief moment to layout after scrolling.
        await page.wait_for_timeout(50)

    async def _viewport_clip_from_bbox(self, page: Page, bbox: dict) -> dict | None:
        """Return a clip rectangle for screenshot based on the current viewport.

        If the bbox cannot be mapped into the current viewport, return None and
        let the caller fall back to element-level screenshot capture.
        """
        viewport = await page.evaluate(
            "() => ({ width: window.innerWidth, height: window.innerHeight, scrollX: window.scrollX, scrollY: window.scrollY })"
        )

        def compute_clip(vp):
            margin = self.AD_SCREENSHOT_MARGIN_PX

            left = int(bbox["x"] - vp["scrollX"]) - margin
            top = int(bbox["y"] - vp["scrollY"]) - margin
            right = int(bbox["x"] - vp["scrollX"] + bbox["width"] + 0.9999) + margin
            bottom = int(bbox["y"] - vp["scrollY"] + bbox["height"] + 0.9999) + margin

            clip_x = max(0, left)
            clip_y = max(0, top)
            if clip_x >= vp["width"] or clip_y >= vp["height"]:
                return None

            clip_right = min(vp["width"], right)
            clip_bottom = min(vp["height"], bottom)
            if clip_right <= clip_x or clip_bottom <= clip_y:
                return None
            clip_width = clip_right - clip_x
            clip_height = clip_bottom - clip_y
            if clip_width < self.MIN_PX_FOR_SCREENSHOT or clip_height < self.MIN_PX_FOR_SCREENSHOT:
                return None
            return {"x": clip_x, "y": clip_y, "width": clip_width, "height": clip_height}

        clip = compute_clip(viewport)
        if clip is not None:
            return clip

        return None

    async def _capture_bbox_screenshot(self, page: Page, bbox: dict, index: int, element_handle: ElementHandle | None = None) -> tuple[str, dict]:
        screenshot_path = self._output_dir / "ad_images" / f"ad_{index}_{self._url_hash}.png"
        
        # Center the page to the ad's coordinates.
        # This uniformly avoids elements being obscured by sticky top-headers OR sticky bottom-footers.
        await self._scroll_bbox_into_view(page, bbox)

        # If the element is inside a nested scroll container (e.g. horizontal carousel)
        # or still off-screen, bring it into view.
        if element_handle is not None:
            try:
                await element_handle.scroll_into_view_if_needed(timeout=300)
            except Exception:
                pass

        # After scrolling, the ad might have moved or resized (especially if it is a sticky element itself
        # or shifted horizontally in a carousel).
        # Re-calculate bounding box.
        if element_handle is not None:
            try:
                live_bbox = await element_handle.bounding_box()
                if live_bbox:
                    scroll_offset = await page.evaluate("() => ({ x: window.scrollX, y: window.scrollY })")
                    rel_bbox = {
                        "x": live_bbox["x"] + scroll_offset["x"],
                        "y": live_bbox["y"] + scroll_offset["y"],
                        "width": live_bbox["width"],
                        "height": live_bbox["height"],
                    }
                    bbox = self._sanitize_bbox(rel_bbox) or bbox
            except Exception:
                pass

        clip = await self._viewport_clip_from_bbox(page, bbox)
        if clip is None:
            if element_handle is not None:
                try:
                    await element_handle.screenshot(path=str(screenshot_path), timeout=1500)
                    return screenshot_path.name, bbox
                except Exception:
                    pass
            try:
                await page.screenshot(path=str(screenshot_path), full_page=False, timeout=1500)
                self._logger.debug(f"[AdCollector] Used viewport screenshot fallback for ad_{index}")
                return screenshot_path.name, bbox
            except Exception:
                raise ValueError("Ad clip could not be mapped into the viewport")

        await page.screenshot(path=str(screenshot_path), clip=clip, timeout=3000)

        return screenshot_path.name, bbox

    async def _capture_single_ad(self, page: Page, ad: dict, index: int) -> tuple[str, dict | None]:
        xpath = ad.get("xpath")
        bbox = self._sanitize_bbox(ad)
        if not bbox:
            return "removed", None

        element_handle = await self._resolve_best_element_handle(page, ad, bbox)

        extraction_target = element_handle
        preferred_iframe_used = False
        if element_handle is not None:
            preferred_iframe = await self._prefer_nested_iframe_handle(element_handle)
            if preferred_iframe is not None:
                extraction_target = preferred_iframe
                preferred_iframe_used = True
                try:
                    iframe_bbox = await preferred_iframe.bounding_box()
                    if iframe_bbox:
                        scroll_offset = await page.evaluate("() => ({ x: window.scrollX, y: window.scrollY })")
                        iframe_bbox["x"] += scroll_offset["x"]
                        iframe_bbox["y"] += scroll_offset["y"]
                        union_bbox = self._union_bbox(bbox, iframe_bbox)
                        if union_bbox:
                            bbox = union_bbox
                except Exception:
                    pass

        # Refresh bbox from the exact extraction target immediately before capture
        # so late layout shifts don't crop the ad frame.
        # This will be done again after scrolling in _capture_bbox_screenshot,
        # but we also do it here to ensure we pass a reasonably fresh bbox to it.
        if extraction_target is not None:
            try:
                live_bbox = await extraction_target.bounding_box()
                if live_bbox:
                    scroll_offset = await page.evaluate("() => ({ x: window.scrollX, y: window.scrollY })")
                    rel_bbox = {
                        "x": live_bbox["x"] + scroll_offset["x"],
                        "y": live_bbox["y"] + scroll_offset["y"],
                        "width": live_bbox["width"],
                        "height": live_bbox["height"],
                    }
                    if preferred_iframe_used and element_handle is not None:
                        try:
                            container_bbox = await element_handle.bounding_box()
                            if container_bbox:
                                container_bbox["x"] += scroll_offset["x"]
                                container_bbox["y"] += scroll_offset["y"]
                        except Exception:
                            container_bbox = None
                        union_live_bbox = self._union_bbox(container_bbox or bbox, rel_bbox)
                        if union_live_bbox:
                            bbox = union_live_bbox
                    else:
                        sanitized_live_bbox = self._sanitize_bbox(rel_bbox)
                        if sanitized_live_bbox:
                            bbox = sanitized_live_bbox
            except Exception:
                pass

        screenshot_name, final_bbox = await self._capture_bbox_screenshot(page, bbox, index, extraction_target)
        bbox = final_bbox or bbox

        ad["x"] = bbox["x"]
        ad["y"] = bbox["y"]
        ad["width"] = bbox["width"]
        ad["height"] = bbox["height"]

        context_screenshot = ""
        context_screenshot_box: dict = {}
        try:
            context_screenshot, context_screenshot_box = await self._capture_context_screenshot(page, bbox, index, extraction_target)
        except Exception as exc:
            self._logger.warning(f"[AdCollector] Context screenshot error for ad_{index}: {exc}")

        extraction_failed = False
        try:
            if extraction_target is None:
                ad_links_and_images = self._build_minimal_ad_artifacts(ad, page.url, index)
                extraction_failed = True
            else:
                ad_links_and_images = await asyncio.wait_for(
                    self._find_links_in_element(extraction_target, index, page.url),
                    timeout=self.EXTRACTION_TIMEOUT_MS / 1000,
                )
        except asyncio.TimeoutError:
            extraction_failed = True
            self._logger.warning(
                f"[AdCollector] Extraction timed out for ad_{index} after {self.EXTRACTION_TIMEOUT_MS} ms; using fallback metadata"
            )
            ad_links_and_images = self._build_minimal_ad_artifacts(ad, page.url, index)
        except Exception as exc:
            extraction_failed = True
            self._logger.warning(f"[AdCollector] Extraction error for ad_{index}: {exc}; using fallback metadata")
            ad_links_and_images = self._build_minimal_ad_artifacts(ad, page.url, index)

        if not extraction_failed and not any(item.get("containsImgsOrLinks") for item in ad_links_and_images):
            supplemental_artifacts = self._build_minimal_ad_artifacts(ad, page.url, index)
            if ad_links_and_images:
                for position, supplemental_frame in enumerate(supplemental_artifacts):
                    if position < len(ad_links_and_images):
                        self._merge_frame_artifacts(ad_links_and_images[position], supplemental_frame)
                    else:
                        ad_links_and_images.append(supplemental_frame)
            else:
                ad_links_and_images = supplemental_artifacts

        ad_attrs = {
            **ad,
            "index": index,
            "screenshot": screenshot_name,
            "contextScreenshot": context_screenshot,
            "contextBoundingBox": context_screenshot_box,
            "clickedAdChoiceLink": "",
            "adLinksAndImages": self._remove_unneeded_attrs(ad_links_and_images),
            "adDisclosureOutLinks": [],
            "adDisclosureText": "",
            "adDisclosurePageUrl": "",
            "adDisclosureScreenshot": "",
        }

        # PASSIVE COLLECTION ONLY: do NOT click disclosures during passive ad delivery!
        # Detect candidate disclosure controls and store descriptors for disclosure_interaction phase.
        # Budget-capped to avoid eating the per-ad scrape timeout.
        detected_controls = await self._detect_disclosure_controls_passive(
            ad_links_and_images, extraction_target, element_handle, page, bbox, index, screenshot_name=screenshot_name,
        )
        ad_attrs["detectedDisclosureControls"] = detected_controls
        ad_attrs["hasDisclosureControl"] = bool(detected_controls)

        await self._download_ad_videos(page, ad_attrs, index)

        matched_rule = ad_attrs.get("matchedRule", "unknown")
        node_type = ad_attrs.get("nodeType", "")
        node_id = ad_attrs.get("id", "") or ""
        self._logger.info(
            f"[AdCollector] ad_{index}: {node_type}#{node_id} "
            f"({int(ad_attrs['width'])}x{int(ad_attrs['height'])}) "
            f"[rule: {matched_rule}]"
        )
        return "scraped", ad_attrs

    async def _detect_disclosure_controls_passive(
        self,
        ad_links_and_images: list[dict],
        extraction_target: ElementHandle | None,
        element_handle: ElementHandle | None,
        page: Page,
        bbox: dict,
        index: int,
        screenshot_name: str = "",
    ) -> list[dict]:
        """Detect disclosure controls without clicking, capped by DISCLOSURE_DETECTION_TIMEOUT_MS.

        This runs during the passive ad delivery phase. It must be fast so it
        does not consume the per-ad scrape budget.
        The actual disclosure tab interaction is deferred to the disclosure_interaction phase.
        """
        try:
            return await asyncio.wait_for(
                self._detect_disclosure_controls_inner(
                    ad_links_and_images, extraction_target, element_handle, page, bbox, index, screenshot_name=screenshot_name,
                ),
                timeout=self.DISCLOSURE_DETECTION_TIMEOUT_MS / 1000,
            )
        except asyncio.TimeoutError:
            self._logger.debug(
                f"[AdCollector] Disclosure detection timed out for ad_{index} after {self.DISCLOSURE_DETECTION_TIMEOUT_MS}ms; "
                "returning partial controls"
            )
            return []
        except Exception as exc:
            self._logger.debug(f"[AdCollector] Disclosure detection error for ad_{index}: {exc}")
            return []

    async def _detect_disclosure_controls_inner(
        self,
        ad_links_and_images: list[dict],
        extraction_target: ElementHandle | None,
        element_handle: ElementHandle | None,
        page: Page,
        bbox: dict,
        index: int,
        screenshot_name: str = "",
    ) -> list[dict]:
        """Inner (uncapped) disclosure control detection logic."""
        detected_controls: list[dict] = []

        # Phase 1: Fast — scan already-extracted link data (no I/O)
        for frame_data in ad_links_and_images:
            for d_link in frame_data.get("_adChoicesLinks", []):
                if d_link and not any(c.get("href") == d_link for c in detected_controls):
                    detected_controls.append({"type": "adchoices_link", "href": d_link})

        fallback_href = self._pick_adchoice_link(ad_links_and_images)
        if fallback_href and not any(c.get("href") == fallback_href for c in detected_controls):
            detected_controls.append({"type": "fallback_link", "href": fallback_href})

        if detected_controls:
            return detected_controls

        if not extraction_target:
            return detected_controls

        # Phase 2: Medium — resolve hrefs from existing handles + shallow frame search
        try:
            searched_roots = set()
            for per_frame in ad_links_and_images:
                handles = per_frame.get("_adChoicesLinksHandles", [])
                if not handles:
                    search_root = per_frame.get("_frameHandle") or element_handle
                    if search_root is not None and id(search_root) not in searched_roots:
                        searched_roots.add(id(search_root))
                        deep_handle = await self._find_adchoice_handle(
                            search_root, self.PASSIVE_DISCLOSURE_FRAME_DEPTH,
                        )
                        if deep_handle is not None:
                            handles = [deep_handle]
                for h in handles:
                    try:
                        href = await h.evaluate("el => el.href")
                    except Exception:
                        href = ""
                    if not href:
                        try:
                            href = await self._extract_adchoice_href_from_handle(h)
                        except Exception:
                            href = ""
                    if href and not any(c.get("href") == href for c in detected_controls):
                        detected_controls.append({"type": "handle_href", "href": href})
                if detected_controls:
                    break
        except Exception as exc:
            self._logger.debug(f"[AdCollector] Handle disclosure detection error: {exc}")

        if detected_controls:
            return detected_controls

        # Phase 3: OpenCV icon matching on the already-captured ad screenshot (no extra page screenshot)
        try:
            screenshot_bytes = None
            if screenshot_name:
                ad_img_path = self._output_dir / "ad_images" / screenshot_name
                if ad_img_path.is_file() and ad_img_path.stat().st_size > 0:
                    screenshot_bytes = ad_img_path.read_bytes()

            if screenshot_bytes:
                coords = await find_ad_choices_in_screenshot(screenshot_bytes, bbox, page)
                if coords:
                    rel_x, rel_y = coords
                    detected_controls.append({
                        "type": "opencv_icon",
                        "coords": [rel_x, rel_y],
                        "href": None,
                    })
        except Exception as exc:
            self._logger.debug(f"[AdCollector] OpenCV disclosure detection error: {exc}")

        return detected_controls

    async def _download_ad_videos(self, page: Page, ad_attrs: dict, index: int) -> None:
        video_dir = self._output_dir / "ad_videos"
        downloaded: list[str] = []

        for frame_data in ad_attrs.get("adLinksAndImages", []):
            if downloaded:
                break
            for video_entry in frame_data.get("videos", []):
                if downloaded:
                    break
                src = video_entry.get("src", "") if isinstance(video_entry, dict) else ""
                if not src or not src.startswith(("http://", "https://")):
                    continue
                if src in downloaded or ".m3u8" in src.lower():
                    continue

                ext = Path(src.split("?")[0]).suffix.lower()
                if ext not in {".mp4", ".webm", ".ogg", ".mov", ".m4v"}:
                    ext = ".mp4"

                filename = f"ad_{index}_{self._url_hash}_video_{len(downloaded)}{ext}"
                filepath = video_dir / filename

                try:
                    response = await asyncio.wait_for(
                        page.context.request.get(src, timeout=2000),
                        timeout=2.0,
                    )
                    if response and response.ok:
                        body = await response.body()
                        if len(body) <= 50 * 1024 * 1024:
                            filepath.write_bytes(body)
                            downloaded.append(src)
                            video_entry["downloadedFile"] = filename
                            self._logger.info(f"[AdCollector] Downloaded video for ad_{index}: {filename}")
                except Exception as exc:
                    self._logger.debug(f"[AdCollector] Video download error for ad_{index}: {exc}")

        if downloaded:
            ad_attrs["downloadedVideos"] = downloaded

    async def _resolve_best_element_handle(self, page: Page, ad: dict, bbox: dict) -> ElementHandle | None:
        xpath = ad.get("xpath")
        if xpath:
            try:
                locator = page.locator(f"xpath={xpath}").first
                if await locator.count() > 0:
                    handle = await locator.element_handle(timeout=self.ELEMENT_ACTION_TIMEOUT_MS)
                    if handle is not None:
                        return handle
            except (PlaywrightTimeoutError, Exception):
                pass

        ad_id = ad.get("id")
        if ad_id:
            try:
                id_locator = page.locator(f"#{ad_id}").first
                if await id_locator.count() > 0:
                    handle = await id_locator.element_handle(timeout=self.ELEMENT_ACTION_TIMEOUT_MS)
                    if handle is not None:
                        return handle
            except Exception:
                pass

        try:
            point_handle = await page.evaluate_handle(
                """
                (bbox) => {
                    const x = Math.max(1, Math.floor(bbox.x + (bbox.width / 2) - window.scrollX));
                    const y = Math.max(1, Math.floor(bbox.y + (bbox.height / 2) - window.scrollY));
                    let cur = document.elementFromPoint(x, y);
                    if (!cur) return null;

                    for (let i = 0; i < 12 && cur; i++) {
                        const id = (cur.id || '').toLowerCase();
                        const cls = (typeof cur.className === 'string' ? cur.className : '').toLowerCase();
                        const hasAdAttr = !!(cur.hasAttribute && (
                            cur.hasAttribute('ad') ||
                            cur.hasAttribute('data-ad') ||
                            cur.hasAttribute('data-ad-slot') ||
                            cur.hasAttribute('data-ad-unit')
                        ));
                        if (hasAdAttr || id.includes('ad') || cls.includes('ad')) {
                            return cur;
                        }

                        const root = cur.getRootNode ? cur.getRootNode() : null;
                        cur = cur.parentElement || (root && root.host ? root.host : null);
                    }
                    return document.elementFromPoint(x, y);
                }
                """,
                bbox,
            )
            point_element = point_handle.as_element()
            if point_element is not None:
                return point_element
        except Exception:
            pass

        return None

    async def _prefer_nested_iframe_handle(self, element_handle: ElementHandle) -> ElementHandle | None:
        try:
            iframe_js_handle = await element_handle.evaluate_handle(
                """
                (root) => {
                    const seen = new Set();
                    let best = null;
                    let bestArea = 0;

                    function walk(node) {
                        if (!node || seen.has(node)) return;
                        seen.add(node);

                        if (node.nodeType === Node.ELEMENT_NODE) {
                            const el = node;
                            if (el.tagName === 'IFRAME') {
                                const r = el.getBoundingClientRect();
                                const area = Math.max(0, r.width) * Math.max(0, r.height);
                                if (area > bestArea) {
                                    bestArea = area;
                                    best = el;
                                }
                            }

                            if (el.shadowRoot) {
                                walk(el.shadowRoot);
                            }

                            if (el.tagName === 'SLOT' && el.assignedElements) {
                                for (const assigned of el.assignedElements({ flatten: true })) {
                                    walk(assigned);
                                }
                            }
                        }

                        if (node.nodeType === Node.ELEMENT_NODE || node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
                            for (const child of node.children || []) {
                                walk(child);
                            }
                        }
                    }

                    walk(root);
                    return best;
                }
                """
            )
            iframe_handle = iframe_js_handle.as_element()
            if iframe_handle is None:
                return None
            bbox = await iframe_handle.bounding_box()
            if bbox and bbox.get("width", 0) >= self.MIN_PX_FOR_SCREENSHOT and bbox.get("height", 0) >= self.MIN_PX_FOR_SCREENSHOT:
                return iframe_handle
        except Exception:
            return None
        return None

    async def _capture_ads(self, page: Page, ads: list) -> tuple[list, dict]:
        ad_details: list[dict] = []
        n_small_ads = 0
        n_empty_ads = 0
        n_removed_ads = 0
        n_skipped_ads = 0
        n_timed_out_ads = 0
        ads_to_process = ads

        for index, ad in enumerate(ads_to_process):
            cand_id = self._crawl_context.next_candidate_id() if self._crawl_context else f"cand_{index + 1:03d}"
            ad["ad_candidate_id"] = cand_id
            ad["_candidate_status"] = "pending"

        # Detection scrolling can leave the page deep down. Ads are processed in
        # ascending Y order, so reset once to top before the loop. This avoids
        # per-ad top jumps while keeping upper-page ad clips reachable.
        try:
            await page.evaluate("() => window.scrollTo(0, 0)")
            await page.wait_for_timeout(250)
        except Exception:
            pass

        for index, ad in enumerate(ads_to_process):
            if self._max_ads_captured is not None and len(ad_details) >= self._max_ads_captured:
                for rem_idx in range(index, len(ads_to_process)):
                    ads_to_process[rem_idx]["_candidate_status"] = "skipped"
                remaining_ads = len(ads_to_process) - index
                n_skipped_ads += max(0, remaining_ads)
                self._n_skipped_ads = n_skipped_ads
                self._logger.info(
                    f"[AdCollector] Reached max successful captures ({self._max_ads_captured}); skipped {remaining_ads} remaining ad(s)"
                )
                break
            if page.is_closed():
                for rem_idx in range(index, len(ads_to_process)):
                    ads_to_process[rem_idx]["_candidate_status"] = "skipped"
                remaining_ads = len(ads_to_process) - index
                n_skipped_ads += remaining_ads
                self._n_skipped_ads = n_skipped_ads
                self._logger.warning(
                    f"[AdCollector] Page closed before ad_{index}; skipped {remaining_ads} remaining ad(s)"
                )
                break
            try:
                status, ad_attrs = await asyncio.wait_for(
                    self._capture_single_ad(page, ad, index),
                    timeout=self.AD_SCRAPE_TIMEOUT_MS / 1000,
                )
                if status == "scraped" and ad_attrs is not None:
                    imp_id = self._crawl_context.next_impression_id() if self._crawl_context else f"ad_{len(ad_details) + 1:03d}"
                    ad_attrs["ad_impression_id"] = imp_id
                    ad_attrs["ad_candidate_id"] = ad["ad_candidate_id"]
                    ad["ad_impression_id"] = imp_id
                    ad["_candidate_status"] = "retained"
                    ad_details.append(ad_attrs)
                    self._ad_attrs = ad_details
                elif status == "small":
                    ad["_candidate_status"] = "small"
                    n_small_ads += 1
                    self._n_small_ads = n_small_ads
                elif status == "empty":
                    ad["_candidate_status"] = "empty"
                    n_empty_ads += 1
                    self._n_empty_ads = n_empty_ads
                elif status == "removed":
                    ad["_candidate_status"] = "removed"
                    n_removed_ads += 1
                    self._n_removed_ads = n_removed_ads
            except Exception as exc:
                if isinstance(exc, asyncio.TimeoutError) or "Timeout" in type(exc).__name__ or "Timeout" in str(exc):
                    ad["_candidate_status"] = "timed_out"
                    n_timed_out_ads += 1
                    self._n_timed_out_ads = n_timed_out_ads
                    rule_str = ad.get("matchedRule", "unknown")
                    self._logger.warning(
                        f"[AdCollector] Timed out scraping ad_{index} ({ad.get('nodeType', '')}#{ad.get('id', '')} [rule: {rule_str}]) after {self.AD_SCRAPE_TIMEOUT_MS} ms"
                    )
                    continue
                ad["_candidate_status"] = "removed"
                n_removed_ads += 1
                self._n_removed_ads = n_removed_ads
                self._logger.warning(f"[AdCollector] Screenshot error for ad_{index}: {exc}")

        # Reconcile all detected ads so nDetectedAds == sum(outcomes) even when stage budgets/timeouts hit
        accounted = len(ad_details) + n_small_ads + n_empty_ads + n_removed_ads + n_skipped_ads + n_timed_out_ads
        unaccounted = max(0, len(ads) - accounted)
        if unaccounted > 0:
            n_timed_out_ads += unaccounted
            self._n_timed_out_ads = n_timed_out_ads

        # Build candidate records covering all detected ads
        records = []
        scraped_ids = {a.get("id") for a in ad_details if a.get("id")}
        scraped_cand_ids = {a.get("ad_candidate_id") for a in ad_details if a.get("ad_candidate_id")}
        for idx, ad in enumerate(ads):
            cand_id = ad.get("ad_candidate_id") or f"cand_{idx + 1:03d}"
            st = ad.get("_candidate_status")
            if not st:
                ad_id = ad.get("id")
                if ad.get("ad_impression_id") or (cand_id in scraped_cand_ids) or (ad_id and ad_id in scraped_ids):
                    st = "retained"
                else:
                    st = "timed_out"
                    ad["_candidate_status"] = "timed_out"

            records.append({
                "ad_candidate_id": cand_id,
                "ad_impression_id": ad.get("ad_impression_id"),
                "candidate_status": st,
                "matchedRule": ad.get("matchedRule"),
                "nodeType": ad.get("nodeType"),
                "id": ad.get("id"),
                "width": ad.get("width"),
                "height": ad.get("height"),
                "x": ad.get("x"),
                "y": ad.get("y"),
            })
        self._candidate_records = records

        scrape_results = {
            "nDetectedAds": len(ads),
            "nAdsScraped": len(ad_details),
            "nSmallAds": n_small_ads,
            "nEmptyAds": n_empty_ads,
            "nRemovedAds": n_removed_ads,
            "nSkippedAds": n_skipped_ads,
            "nTimedOutAds": n_timed_out_ads,
        }
        
        self._scrape_results = scrape_results
        return ad_details, scrape_results

    async def _resolve_browser_frame_info(self, frame_or_handle: Frame | ElementHandle | None, page_url: str) -> dict[str, Any]:
        if frame_or_handle is None:
            return {
                "frameId": None,
                "loaderId": None,
                "executionContextId": None,
                "parentFrameId": None,
            }

        key = id(frame_or_handle)
        if key in self._frame_info_cache:
            return self._frame_info_cache[key]

        frame_id = None
        loader_id = None
        exec_ctx_id = None
        parent_frame_id = None

        if isinstance(frame_or_handle, Frame):
            parent_frame = frame_or_handle.parent_frame
            if parent_frame:
                parent_info = await self._resolve_browser_frame_info(parent_frame, page_url)
                parent_frame_id = parent_info.get("frameId")

            if hasattr(frame_or_handle, "page") and frame_or_handle == frame_or_handle.page.main_frame:
                main_frame = self._cdp_frame_tree.get("frame", {}) if isinstance(self._cdp_frame_tree, dict) else {}
                frame_id = main_frame.get("id")
                loader_id = main_frame.get("loaderId")
            else:
                token = uuid.uuid4().hex
                if self._cdp:
                    try:
                        await frame_or_handle.evaluate(f"() => {{ window.__ag_ftok = '{token}'; }}")
                        for c_id, f_id in list(self._cdp_contexts.items()):
                            try:
                                res = await self._cdp.send("Runtime.evaluate", {
                                    "expression": "window.__ag_ftok",
                                    "contextId": c_id,
                                    "silent": True,
                                })
                                if (res.get("result") or {}).get("value") == token:
                                    exec_ctx_id = c_id
                                    frame_id = f_id
                                    break
                            except Exception:
                                pass
                    except Exception:
                        pass

            if not frame_id:
                frame_id = f"cdp_frame_{abs(hash(frame_or_handle))}_{uuid.uuid4().hex[:8]}"
        else:
            try:
                elem_page = getattr(frame_or_handle, "page", None)
                if elem_page:
                    main_info = await self._resolve_browser_frame_info(elem_page.main_frame, page_url)
                    frame_id = main_info.get("frameId")
                    loader_id = main_info.get("loaderId")
                    parent_frame_id = None
            except Exception:
                frame_id = f"elem_frame_{abs(hash(frame_or_handle))}_{uuid.uuid4().hex[:8]}"

        info = {
            "frameId": frame_id,
            "loaderId": loader_id,
            "executionContextId": exec_ctx_id,
            "parentFrameId": parent_frame_id,
        }
        self._frame_info_cache[key] = info
        return info

    async def _eval_all(self, context: Frame | ElementHandle, selector: str, expression: str):
        try:
            return await context.eval_on_selector_all(selector, expression)
        except Exception:
            return []

    async def _extract_context_artifacts(
        self,
        context: Frame | ElementHandle,
        *,
        frame_url: str,
        frame_id: str,
        parent_frame_url: str,
        parent_frame_id: str | None,
        is_main_document: bool,
        loader_id: str | None = None,
        execution_context_id: int | None = None,
    ) -> dict:
        try:
            if isinstance(context, Frame):
                deep = await context.evaluate(
                    f"(payload) => ({self._DEEP_ASSET_JS})(document.documentElement, payload.adChoiceSelector)",
                    {"adChoiceSelector": self.ADCHOICES_SELECTOR},
                )
            else:
                deep = await context.evaluate(
                    f"(rootNode, payload) => ({self._DEEP_ASSET_JS})(rootNode, payload.adChoiceSelector)",
                    {"adChoiceSelector": self.ADCHOICES_SELECTOR},
                )
        except Exception:
            deep = None

        if not isinstance(deep, dict):
            deep = {}

        links = deep.get("links", [])
        image_links = deep.get("imageLinks", [])
        other_links = deep.get("otherLinks", [])
        gwd_links = []
        imgs = deep.get("imgs", [])
        bg_imgs = deep.get("bgImgs", [])
        videos = deep.get("videos", [])
        scripts = deep.get("scripts", [])
        iframes = deep.get("iframes", [])
        adchoices_links = deep.get("adChoicesLinks", [])

        adchoices_link_handles: list[ElementHandle] = []
        try:
            adchoices_link_handles = await context.query_selector_all(self.ADCHOICES_SELECTOR)
        except Exception:
            adchoices_link_handles = []

        return {
            "frameUrl": frame_url,
            "containsImgsOrLinks": bool(links or image_links or other_links or gwd_links or imgs or bg_imgs or videos or adchoices_links),
            "isMainDocument": is_main_document,
            "parentFrameUrl": parent_frame_url,
            "frameId": frame_id,
            "loaderId": loader_id,
            "executionContextId": execution_context_id,
            "parentFrameId": parent_frame_id,
            "links": links,
            "imageLinks": image_links,
            "otherLinks": other_links,
            "gwdLinks": gwd_links,
            "imgs": imgs,
            "bgImgs": bg_imgs,
            "videos": videos,
            "scripts": scripts,
            "iframes": iframes,
            "_adChoicesLinks": adchoices_links,
            "_adChoicesLinksHandles": adchoices_link_handles,
            "_frameHandle": context,
        }

    def _build_minimal_ad_artifacts(self, ad: dict, page_url: str, ad_index: int) -> list[dict]:
        src = ad.get("src") or ""
        imgs = []
        image_links = []
        other_links = []
        links = []
        adchoices = []
        html_blob = "\n".join([ad.get("outerHTML", ""), ad.get("innerText", "")])

        for discovered in self._extract_urls_from_text_blob(html_blob, page_url):
            lowered = discovered.lower()
            if self._looks_like_adchoice_url(discovered):
                adchoices.append(discovered)

            is_image = any(token in lowered for token in [".jpg", ".jpeg", ".png", ".webp", ".gif", "entityid/"])
            if is_image:
                image_links.append(
                    {
                        "googAdUrl": None,
                        "href": discovered,
                        "imgSrc": discovered,
                        "outerHTML": "",
                    }
                )
                imgs.append(
                    {
                        "x": ad.get("x", 0),
                        "y": ad.get("y", 0),
                        "width": ad.get("width", 0),
                        "height": ad.get("height", 0),
                        "src": discovered,
                        "outerHTML": ad.get("outerHTML", "")[:2000],
                        "origin": {
                            "kind": "url" if not discovered.startswith("data:") else "inline-data-url",
                            "sourceType": "attribute-url-scan",
                            "sourceAttribute": "unknown",
                            "tagName": ad.get("nodeType", ""),
                            "id": ad.get("id", ""),
                            "className": ad.get("class", ""),
                            "xpath": ad.get("xpath", ""),
                        },
                    }
                )
            else:
                other_links.append(
                    {
                        "googAdUrl": None,
                        "href": discovered,
                        "text": "",
                        "outerHTML": "",
                    }
                )
                links.append(
                    [
                        {
                            "googAdUrl": None,
                            "href": discovered,
                            "outerHTML": "",
                        }
                    ]
                )

        if src:
            normalized_src = self._normalize_urlish(src, page_url)
            imgs.append(
                {
                    "x": ad.get("x", 0),
                    "y": ad.get("y", 0),
                    "width": ad.get("width", 0),
                    "height": ad.get("height", 0),
                    "src": normalized_src,
                    "outerHTML": ad.get("outerHTML", "")[:2000],
                    "origin": {
                        "kind": "url" if not normalized_src.startswith("data:") else "inline-data-url",
                        "sourceType": "ad-element",
                        "sourceAttribute": "src",
                        "tagName": ad.get("nodeType", ""),
                        "id": ad.get("id", ""),
                        "className": ad.get("class", ""),
                        "xpath": ad.get("xpath", ""),
                    },
                }
            )
            image_links.append(
                {
                    "googAdUrl": None,
                    "href": normalized_src,
                    "imgSrc": normalized_src,
                    "outerHTML": ad.get("outerHTML", "")[:2000],
                }
            )

        main_info = self._cdp_frame_tree.get("frame", {}) if isinstance(self._cdp_frame_tree, dict) else {}
        main_frame_id = main_info.get("id") or f"main_{uuid.uuid4().hex[:8]}"
        main_loader_id = main_info.get("loaderId")

        return [
            {
                "frameUrl": page_url,
                "containsImgsOrLinks": bool(imgs or links or image_links or other_links or adchoices),
                "isMainDocument": False,
                "parentFrameUrl": page_url,
                "frameId": main_frame_id,
                "loaderId": main_loader_id,
                "executionContextId": None,
                "parentFrameId": main_frame_id,
                "links": links,
                "imageLinks": image_links,
                "otherLinks": other_links,
                "gwdLinks": [],
                "imgs": imgs,
                "bgImgs": [],
                "videos": [],
                "scripts": [],
                "iframes": [],
                "_adChoicesLinks": adchoices,
                "_adChoicesLinksHandles": [],
                "_frameHandle": None,
            },
            {
                "frameUrl": "",
                "containsImgsOrLinks": False,
                "isMainDocument": True,
                "parentFrameUrl": "unknown",
                "frameId": main_frame_id,
                "loaderId": main_loader_id,
                "executionContextId": None,
                "parentFrameId": None,
                "links": [],
                "imageLinks": [],
                "otherLinks": [],
                "gwdLinks": [],
                "imgs": [],
                "bgImgs": [],
                "videos": [],
                "scripts": [],
                "iframes": [],
                "_adChoicesLinks": [],
                "_adChoicesLinksHandles": [],
                "_frameHandle": None,
            },
        ]

        adchoices = self._rank_adchoice_candidates(adchoices, html_blob, page_url)

    def _merge_frame_artifacts(self, target: dict, source: dict) -> None:
        if not target or not source:
            return

        def merge_list(field: str, key_fn):
            incoming = source.get(field, [])
            if not incoming:
                return
            existing = target.setdefault(field, [])
            seen = {key_fn(item) for item in existing}
            for item in incoming:
                key = key_fn(item)
                if key in seen:
                    continue
                seen.add(key)
                existing.append(item)

        merge_list("links", lambda item: json.dumps(item, sort_keys=True))
        merge_list("imageLinks", lambda item: (item or {}).get("href", "") + "|" + str((item or {}).get("imgSrc", "")))
        merge_list("otherLinks", lambda item: (item or {}).get("href", ""))
        merge_list("gwdLinks", lambda item: json.dumps(item, sort_keys=True))
        merge_list("imgs", lambda item: (item or {}).get("src", ""))
        merge_list("bgImgs", lambda item: (item or {}).get("src", ""))
        merge_list("videos", lambda item: (item or {}).get("src", ""))
        merge_list("scripts", lambda item: item)
        merge_list("iframes", lambda item: item)
        merge_list("_adChoicesLinks", lambda item: item)

        if source.get("containsImgsOrLinks") and not target.get("containsImgsOrLinks"):
            target["containsImgsOrLinks"] = True

    def _normalize_urlish(self, candidate: str, base_url: str) -> str:
        if not candidate:
            return ""
        candidate = candidate.strip()
        if candidate.startswith("javascript:"):
            return ""
        if candidate.startswith("//"):
            parsed_base = urlparse(base_url)
            scheme = parsed_base.scheme or "https"
            return f"{scheme}:{candidate}"
        if candidate.startswith(("http://", "https://", "data:")):
            return candidate
        return urljoin(base_url, candidate)

    def _extract_urls_from_text_blob(self, text: str, base_url: str) -> list[str]:
        if not text:
            return []
        found: list[str] = []
        seen: set[str] = set()
        for match in self.URL_IN_TEXT_RE.findall(text):
            normalized = self._normalize_urlish(match, base_url)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            found.append(normalized)
        for match in self.HTML_URL_ATTR_RE.findall(text):
            normalized = self._normalize_urlish(match, base_url)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            found.append(normalized)
        return found

    def _looks_like_adchoice_url(self, candidate: str) -> bool:
        lowered = (candidate or "").lower()
        return bool(lowered) and any(token in lowered for token in self._ADCHOICE_URL_HINTS)

    def _score_adchoice_candidate(self, candidate: str, blob: str, base_url: str) -> int:
        if not candidate:
            return -1

        lowered_blob = (blob or "").lower()
        candidate_lower = candidate.lower()
        index = lowered_blob.find(candidate_lower)
        if index >= 0:
            snippet = lowered_blob[max(0, index - 700): min(len(lowered_blob), index + len(candidate_lower) + 700)]
        else:
            snippet = lowered_blob[:1400]

        urls_in_snippet = self._extract_urls_from_text_blob(snippet, base_url)
        unique_urls = list(dict.fromkeys(urls_in_snippet))

        score = 0
        if index >= 0:
            score += 1000
        if candidate_lower in lowered_blob:
            score += 100
        if self._looks_like_adchoice_url(candidate):
            score += 300
        if any(token in snippet for token in self._ADCHOICE_TEXT_HINTS):
            score += 250
        if "href=" in snippet or "data-href" in snippet or "data-url" in snippet or "onclick" in snippet:
            score += 100
        if len(unique_urls) == 1:
            score += 900
        elif candidate in unique_urls and len(unique_urls) <= 3:
            score += 500
        elif candidate in unique_urls:
            score += max(0, 300 - (len(unique_urls) * 50))
        if lowered_blob.count(candidate_lower) == 1:
            score += 50
        return score

    def _rank_adchoice_candidates(self, candidates: list[str], blob: str, base_url: str) -> list[str]:
        ranked = []
        seen = set()
        for index, candidate in enumerate(candidates):
            if not candidate or candidate.startswith("javascript:") or candidate in seen:
                continue
            seen.add(candidate)
            score = self._score_adchoice_candidate(candidate, blob, base_url)
            ranked.append((score, index, candidate))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [candidate for _, _, candidate in ranked]

    async def _resolve_content_frame(
        self,
        iframe_handle: ElementHandle,
        retries: int = 6,
        delay_ms: int = 150,
    ) -> Frame | None:
        for _ in range(retries):
            try:
                frame = await iframe_handle.content_frame()
            except Exception:
                frame = None
            if frame is not None:
                return frame
            await asyncio.sleep(delay_ms / 1000)
        return None

    async def _walk_frame_assets(self, frame: Frame, page_url: str, depth: int = 0) -> list[dict]:
        entries: list[dict] = []
        if depth >= self.MAX_FRAME_DEPTH:
            return entries

        iframe_handles = await frame.query_selector_all("iframe")
        for iframe_handle in iframe_handles[: self.MAX_IFRAMES_PER_CONTEXT]:
            child_frame = await self._resolve_content_frame(iframe_handle)
            if child_frame is not None:
                entries.extend(await self._walk_frame_assets(child_frame, page_url, depth + 1))

        frame_url = frame.url
        parent_frame = frame.parent_frame
        parent_frame_url = parent_frame.url if parent_frame else "unknown"
        frame_info = await self._resolve_browser_frame_info(frame, page_url)
        frame_id = frame_info.get("frameId")
        parent_frame_id = frame_info.get("parentFrameId")
        entries.append(
            await self._extract_context_artifacts(
                frame,
                frame_url=frame_url,
                frame_id=frame_id,
                loader_id=frame_info.get("loaderId"),
                execution_context_id=frame_info.get("executionContextId"),
                parent_frame_url=parent_frame_url,
                parent_frame_id=parent_frame_id,
                is_main_document=False,
            )
        )
        return entries

    async def _find_links_in_element(self, element_handle: ElementHandle, ad_index: int, page_url: str) -> list[dict]:
        entries: list[dict] = []

        iframe_handles = await element_handle.query_selector_all("iframe")
        for iframe_handle in iframe_handles[: self.MAX_IFRAMES_PER_CONTEXT]:
            frame = await self._resolve_content_frame(iframe_handle)
            if frame is not None:
                entries.extend(await self._walk_frame_assets(frame, page_url, 0))

        element_info = await self._resolve_browser_frame_info(element_handle, page_url)
        root_frame_id = element_info.get("frameId") or f"elem_frame_{ad_index}"
        parent_frame_id = element_info.get("parentFrameId")
        root_entry = await self._extract_context_artifacts(
            element_handle,
            frame_url=page_url,
            frame_id=root_frame_id,
            loader_id=element_info.get("loaderId"),
            execution_context_id=element_info.get("executionContextId"),
            parent_frame_url=page_url,
            parent_frame_id=parent_frame_id,
            is_main_document=False,
        )
        entries.append(root_entry)

        main_info = self._cdp_frame_tree.get("frame", {}) if isinstance(self._cdp_frame_tree, dict) else {}
        main_frame_id = main_info.get("id") or f"main_{uuid.uuid4().hex[:8]}"
        main_loader_id = main_info.get("loaderId")

        entries.append(
            {
                "frameUrl": "",
                "containsImgsOrLinks": False,
                "isMainDocument": True,
                "parentFrameUrl": "unknown",
                "frameId": main_frame_id,
                "loaderId": main_loader_id,
                "executionContextId": None,
                "parentFrameId": None,
                "links": [],
                "imageLinks": [],
                "otherLinks": [],
                "gwdLinks": [],
                "imgs": [],
                "bgImgs": [],
                "videos": [],
                "scripts": [],
                "iframes": root_entry.get("iframes", []),
                "_adChoicesLinks": [],
                "_adChoicesLinksHandles": [],
                "_frameHandle": None,
            }
        )
        return entries

    def _remove_unneeded_attrs(self, ad_links_and_images: list[dict]) -> list[dict]:
        sanitized = []
        for item in ad_links_and_images:
            clean = dict(item)
            adchoices_links = [href for href in clean.get("_adChoicesLinks", []) if href]
            clean["adChoicesLinks"] = list(dict.fromkeys(adchoices_links))
            clean.pop("_adChoicesLinks", None)
            clean.pop("_adChoicesLinksHandles", None)
            clean.pop("_frameHandle", None)
            sanitized.append(clean)
        return sanitized

    def _pick_adchoice_link(self, ad_links_and_images: list[dict]) -> str:
        best_href = ""
        best_score = -1
        best_index = len(ad_links_and_images)

        for index, item in enumerate(ad_links_and_images):
            blob = json.dumps(item, sort_keys=True, default=str)
            ranked_candidates = self._rank_adchoice_candidates(
                [href for href in item.get("_adChoicesLinks", []) if href and not href.startswith("javascript:")],
                blob,
                item.get("frameUrl", "") or "",
            )
            if not ranked_candidates:
                continue

            candidate = ranked_candidates[0]
            score = self._score_adchoice_candidate(candidate, blob, item.get("frameUrl", "") or "")
            if score > best_score or (score == best_score and index < best_index):
                best_href = candidate
                best_score = score
                best_index = index

        return best_href

    async def _click_adchoice_link_in_ad(
        self,
        ad_links_and_images: list[dict],
        page: Page,
        ad_screenshot_name: str,
        *,
        element_handle: ElementHandle | None = None,
    ) -> tuple[str, dict | None]:
        """Upstream-style adchoice click loop over pre-found link handles.

        When *element_handle* is provided, the fallback deep-search and rescan
        are scoped to that element instead of the whole page.  This prevents
        native (non-iframe) ads from accidentally picking up AdChoices links
        that belong to other ads on the same page.
        """
        for per_frame in ad_links_and_images:
            adchoice_handles = per_frame.get("_adChoicesLinksHandles", [])

            if not adchoice_handles:
                # Scope the deep search: use the per-frame handle if available,
                # otherwise the ad element_handle, and only the page as last resort.
                search_root = per_frame.get("_frameHandle") or element_handle or page
                deep_handle = await self._find_adchoice_handle(search_root, self.MAX_FRAME_DEPTH)
                if deep_handle is not None:
                    adchoice_handles = [deep_handle]

            if not adchoice_handles:
                await page.wait_for_timeout(250)
                adchoice_handles = await self._rescan_adchoice_handles(
                    per_frame, scope_element=element_handle
                )

            if not adchoice_handles:
                continue

            for adchoice_handle in adchoice_handles:
                try:
                    href = await adchoice_handle.evaluate("el => el.href")
                except Exception as exc:
                    self._logger.debug(f"[AdCollector] Error reading adchoice href: {exc}")
                    href = ""

                if not href:
                    try:
                        href = await self._extract_adchoice_href_from_handle(adchoice_handle)
                    except Exception:
                        href = ""

                if not href:
                    continue

                disclosure = await self._ad_disclosure_collector.open_disclosure_in_new_tab(
                    page,
                    href,
                    ad_screenshot_name=ad_screenshot_name,
                )
                if disclosure:
                    self._n_clicked_adchoices_links += 1
                    self._logger.debug(f"[AdCollector] Opened adchoice disclosure in a new tab: {href[:120]}")
                    return href, disclosure

        return "", None

    async def _rescan_adchoice_handles(
        self,
        per_frame: dict,
        *,
        scope_element: ElementHandle | None = None,
    ) -> list[ElementHandle]:
        """Re-scan for AdChoices link handles, scoped to the ad element.

        If *scope_element* is provided and ``per_frame`` has no frame handle,
        the search is constrained to *scope_element* rather than scanning the
        entire page DOM.
        """
        context = per_frame.get("_frameHandle") or scope_element
        if context is None:
            return []

        try:
            handles = await context.query_selector_all(self.ADCHOICES_SELECTOR)
        except Exception:
            handles = []

        per_frame["_adChoicesLinksHandles"] = handles
        return handles

    async def _click_adchoice_reveal_control(self, per_frame: dict, page: Page) -> bool:
        return False

    async def _find_adchoice_handle(self, target: ElementHandle | Frame, max_depth: int = 2) -> ElementHandle | None:
        if max_depth < 0 or target is None:
            return None
        try:
            handle = await target.query_selector(self.ADCHOICES_SELECTOR)
            if handle:
                return handle
        except Exception:
            pass

        try:
            icon_handle = await target.query_selector(self.ADCHOICES_ICON_SELECTOR)
            if icon_handle:
                clickable = await icon_handle.query_selector(
                    "xpath=ancestor-or-self::*[self::a or @onclick or @role='button' or self::button][1]"
                )
                if clickable:
                    return clickable
                return icon_handle
        except Exception:
            pass

        try:
            deep_handle = await target.evaluate_handle(
                """
                (root, payload) => {
                    const { adChoiceSelector, iconSelector } = payload;
                    const selectors = [adChoiceSelector, iconSelector].filter(Boolean);

                    const isMatch = (el) => {
                        if (!el || el.nodeType !== Node.ELEMENT_NODE) {
                            return false;
                        }

                        for (const selector of selectors) {
                            try {
                                if (el.matches(selector)) {
                                    return true;
                                }
                            } catch (_) {}
                        }

                        const text = `${el.getAttribute?.('aria-label') || ''} ${el.getAttribute?.('title') || ''} ${el.innerText || ''}`.toLowerCase();
                        return text.includes('why this ad') || text.includes('adchoices') || text.includes('ad choice');
                    };

                    const clickableAncestor = (el) => {
                        if (!el || el.nodeType !== Node.ELEMENT_NODE) {
                            return el;
                        }
                        return el.closest('a, [onclick], [role="button"], button') || el;
                    };

                    const walk = (node) => {
                        if (!node) {
                            return null;
                        }

                        if (node.nodeType === Node.ELEMENT_NODE) {
                            const el = node;
                            if (isMatch(el)) {
                                return clickableAncestor(el);
                            }

                            if (el.tagName === 'IFRAME') {
                                try {
                                    const doc = el.contentDocument;
                                    if (doc) {
                                        const fromFrame = walk(doc);
                                        if (fromFrame) {
                                            return fromFrame;
                                        }
                                    }
                                } catch (_) {}
                            }

                            if (el.shadowRoot) {
                                const fromShadow = walk(el.shadowRoot);
                                if (fromShadow) {
                                    return fromShadow;
                                }
                            }
                        }

                        for (const child of node.children || []) {
                            const found = walk(child);
                            if (found) {
                                return found;
                            }
                        }

                        return null;
                    };

                    return walk(root);
                }
                """,
                {"adChoiceSelector": self.ADCHOICES_SELECTOR, "iconSelector": self.ADCHOICES_ICON_SELECTOR},
            )
            deep_handle = deep_handle.as_element()
            if deep_handle is not None:
                return deep_handle
        except Exception:
            pass

        try:
            if isinstance(target, ElementHandle):
                tag_name = await target.evaluate("el => el.tagName.toLowerCase()")
                if tag_name == "iframe":
                    frame = await self._resolve_content_frame(target)
                    if frame:
                        return await self._find_adchoice_handle(frame, max_depth - 1)
        except Exception:
            pass

        try:
            iframes = await target.query_selector_all("iframe")
            for iframe in iframes[:3]:
                res = await self._find_adchoice_handle(iframe, max_depth - 1)
                if res:
                    return res
        except Exception:
            pass
        return None

    async def _extract_adchoice_href_from_handle(self, handle: ElementHandle) -> str:
        try:
            extracted_href = await handle.evaluate(
                r"""
                (el, adChoiceSelector) => {
                    const baseHref =
                        el?.ownerDocument?.location?.href ||
                        document?.location?.href ||
                        '';

                    const normalize = (raw) => {
                        if (!raw) return '';
                        if (String(raw).startsWith('javascript:')) return '';
                        try {
                            return new URL(String(raw), baseHref).href;
                        } catch (_) {
                            if (String(raw).startsWith('//')) {
                                return (document?.location?.protocol || 'https:') + String(raw);
                            }
                            return String(raw);
                        }
                    };

                    const firstUrlInText = (text) => {
                        if (!text) return '';
                        const match = String(text).match(/((?:https?:)?\/\/[^\s'\"<>]+)/i);
                        return match ? normalize(match[1]) : '';
                    };

                    const fromNode = (node) => {
                        if (!node) return '';

                        const direct = normalize(
                            node.getAttribute?.('href') ||
                            node.href ||
                            node.getAttribute?.('data-href') ||
                            node.getAttribute?.('data-url') ||
                            node.getAttribute?.('data-destination-url') ||
                            node.getAttribute?.('data-click-url') ||
                            ''
                        );
                        if (direct) return direct;

                        const fromOnclick = firstUrlInText(node.getAttribute?.('onclick') || '');
                        if (fromOnclick) return fromOnclick;

                        const nestedAnchor = node.querySelector?.('a[href]');
                        if (nestedAnchor) {
                            return normalize(nestedAnchor.getAttribute?.('href') || nestedAnchor.href || '');
                        }
                        return '';
                    };

                    const candidateRoots = [
                        el,
                        el.closest?.('a, [onclick], [role="button"], button') || null,
                        el.parentElement || null,
                        el.closest?.('div, span, li, section, article, aside, main') || null,
                    ].filter(Boolean);

                    for (const node of candidateRoots) {
                        const href = fromNode(node);
                        if (href) return href;
                    }

                    return '';
                }
                """,
                self.ADCHOICES_SELECTOR,
            )
            if extracted_href and not extracted_href.startswith("javascript:"):
                return extracted_href
        except Exception:
            pass
        return ""

    async def _open_adchoice_link(
        self,
        page: Page,
        extraction_target: ElementHandle | None,
        href: str,
        index: int,
        ad_screenshot_name: str,
    ) -> tuple[bool, str, dict | None]:
        if extraction_target:
            handle = await self._find_adchoice_handle(extraction_target, self.MAX_FRAME_DEPTH)
            if handle:
                if not href:
                    href = await self._extract_adchoice_href_from_handle(handle)

                if href:
                    disclosure = await self._ad_disclosure_collector.open_disclosure_in_new_tab(
                        page,
                        href,
                        ad_screenshot_name=ad_screenshot_name,
                    )
                    if disclosure:
                        self._n_clicked_adchoices_links += 1
                        return True, href, disclosure

        return False, href, None
