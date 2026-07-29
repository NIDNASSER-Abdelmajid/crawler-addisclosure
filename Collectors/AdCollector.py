import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import ElementHandle, Frame, Page, TimeoutError as PlaywrightTimeoutError
from Collectors.AdDisclosureCollector import AdDisclosureCollector
from Helpers import utils as pageUtils
from Helpers.ad_choices_matcher import find_ad_choices_in_screenshot


class AdCollector:
    COLLECTOR_NAME = "AdCollector"
    MAX_ADS_PER_PAGE = None
    MAX_IFRAMES_PER_CONTEXT = 8
    MAX_FRAME_DEPTH = 4
    MIN_PX_FOR_SCREENSHOT = 30
    SCROLL_TIMEOUT_MS = 20_000
    ELEMENT_ACTION_TIMEOUT_MS = 5_000
    EXTRACTION_TIMEOUT_MS = 8_000
    AD_SCRAPE_TIMEOUT_MS = 20_000
    CONTEXT_SCREENSHOT_MARGIN_PX = 150
    AD_SCREENSHOT_MARGIN_PX = 10
    ADCHOICES_SELECTOR = ':is(a[href*="whythisad"], a[href*="adchoice"], a[href*="adssettings.google.com"], a#abgl, #abgl)'
    _ADCHOICES_ICON_HINTS = [
        "adchoice",
        "adchoices",
        "whythisad",
        "why-this-ad",
        "why this ad",
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
    _ADCHOICE_URL_HINTS = (
        "whythisad",
        "adchoice",
        "adchoices",
        # "privacy/adinfo",
        "adssettings.google.com",
        "privacy.us.criteo.com",
        "privacy.eu.criteo.com",
        # "criteo",
        # "taboola",
        # "outbrain",
    )
    _ADCHOICE_TEXT_HINTS = (
        "why this ad",
        "see more ads by this advertiser",
        "report this ad",
        "adchoices",
    ) # https://legal.yahoo.com/us/en/yahoo/privacy/adinfo/index.html

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

                    if (adChoiceSelector && el.matches && el.matches(adChoiceSelector) && href) {
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

    # Extra selectors not in the EasyList file.
    # Covers Google AdSense, Microsoft/MSN Prism, Outbrain,
    # DFP/GPT containers, generic ad-framework patterns, and ad iframes.
    EXTRA_SELECTORS = [
        # ── Google AdSense / generic ──────────────────────────────────────────
        "ins.adsbygoogle",
        '[data-google-query-id]',
        '[data-ad-client]',
        '[data-ad-slot]',
        '[data-ad-unit]',
        '[data-ad-id]',
        '[data-ad]',
        'iframe[id^="google_ads_iframe"]',
        # ── MSN / Microsoft Prism ad architecture ─────────────────────────────
        '[class*="prism-ad"]',
        '[class*="grid-ad"]',
        '[class*="ad-spot"]',
        '[class*="ad-slot"]',
        '[class*="adslot"]',
        '[class*="ad-unit"]',
        '[class*="adunit"]',
        '[class*="ad-zone"]',
        '[class*="adzone"]',
        '[class*="ad-container"]',
        '[class*="ad-wrapper"]',
        '[class*="ad-placement"]',
        '[class*="ad-placeholder"]',
        '[class*="ad-layout"]',
        '[class*="ad-area"]',
        '[class*="ad-block"]',
        '[class*="advertisement"]',
        '[id*="ad-slot"]',
        '[id*="adslot"]',
        '[id*="ad_slot"]',
        '[id*="ad-unit"]',
        '[data-ad-zone]',
        '[data-is-ad]',
        '[data-advertiserwho]',
        '[data-ad-type]',
        # ── MSN shadow-DOM ad elements (discovered via live inspection) ────────
        '.displayAdContainer',
        '.displayAdWCContainer',
        '.display-ads-container',
        '.ad-banner-wrapper',
        '.displayAdCard',
        '.adSlug',
        '.adChoices',
        '.adChoicesOutside',
        '[id^="banner"][class*="displayAd"]',
        '[id^="rectangle"][class*="displayAd"]',
        # ── Sponsored / paid content ──────────────────────────────────────────
        '.sponsored',
        '[class*="sponsored-content"]',
        '[class*="paid-content"]',
        '[class*="promoted"]',
        '[data-sponsored]',
        # ── Taboola ───────────────────────────────────────────────────────────
        '[id^="taboola"]',
        '[class^="trc_"]',
        '[id^="trc_"]',
        '.trc-content-sponsored',
        '[data-taboola-item-id]',
        # ── Outbrain ──────────────────────────────────────────────────────────
        '.ob-widget',
        '.OUTBRAIN',
        '[data-widget-id]',
        '[id^="rc_widget"]',
        # ── Ad-serving iframes ────────────────────────────────────────────────
        'iframe[src*="doubleclick.net"]',
        'iframe[src*="googlesyndication.com"]',
        'iframe[src*="bing.com"]',
        'iframe[src*="microsoft.com/ads"]',
        'iframe[src*="ads.msn.com"]',
        'iframe[src*="pubcenter.microsoft.com"]',
        'iframe[src*="moatads.com"]',
        'iframe[src*="adnxs.com"]',
        'iframe[src*="pubmatic.com"]',
        'iframe[src*="openx.net"]',
        # ── DFP / GPT ad-tag containers ───────────────────────────────────────
        '[class*="dfp-"]',
        '[id*="dfp-"]',
        '[class*="gpt-"]',
        '[id*="gpt-"]',
        '.ad-tag',
        '.ad-label',
        # ── NYTimes ad containers ─────────────────────────────────────────────
        '.place-ad',
        '.placed-ad',
        '[class*="place-ad"]',
        '[class*="placed-ad"]',
        # Hashed CSS wrapper around every NYT ad slot (wraps the dfp- divs)
        '[class*="dfp-ad-"]',
        '[id*="dfp-ad-"]',
        '.ad-container',
        '.ad-wrapper',
        '.ad-placement',
        '.ad-placeholder',
        '.sponsored-link',
        '.sponsored-post',
        '.sponsored-label',
        '.advertisement-label',
        '[id*="google_ads_iframe"]',
        '[id*="re-ad-"]',
        '[class*="re-ad-"]',
    ]

    # JavaScript run inside the browser to find ad elements and return their geometry/metadata.
    #
    # MSN (and many modern news sites) render their content entirely inside Web
    # Component shadow roots.  Standard document.querySelectorAll('*') therefore
    # returns only the thin outer skeleton (~100 elements) while the actual page
    # content — including ad containers — lives inside shadow DOM subtrees.
    #
    # This implementation uses a two-phase strategy:
    #   Phase 1 – Harvest: recursively walk every shadow root and collect ALL
    #             elements into a flat list.
    #   Phase 2 – Detect:  run all detection strategies (CSS selector matching
    #             via :is() batching, attribute heuristics, aria-label, class/id
    #             name regex, iframe-domain hints, text-label sniffing) over that
    #             flat list so shadow DOM elements are never missed.
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

        // Find the first CSS selector (from the full list) that matches el.
        // Uses batched :is() to avoid thousands of individual calls, then
        // identifies the exact matching selector within the winning chunk.
        function findMatchingSelector(el, sels) {
            for (let i = 0; i < sels.length; i += CHUNK) {
                const chunk = sels.slice(i, i + CHUNK);
                let batchHit = false;
                try {
                    batchHit = el.matches(':is(' + chunk.join(',') + ')');
                } catch (_) {
                    // Batch :is() failed (invalid selector in chunk) — try individually
                    for (const s of chunk) {
                        try { if (el.matches(s)) return s; } catch (_2) {}
                    }
                    continue;
                }
                if (batchHit) {
                    for (const s of chunk) {
                        try { if (el.matches(s)) return s; } catch (_2) {}
                    }
                    return chunk[0]; // fallback (shouldn't happen)
                }
            }
            return null;
        }

        // ── Phase 2a: CSS selector scan over ALL elements ─────────────────────
        for (const el of allEls) {
            const rule = findMatchingSelector(el, selectors);
            if (rule) addAd(el, 'selector:' + rule);
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

    # Slow multi-pass scroll to trigger lazy-loading on JS-heavy pages.
    # Pass 1 — slow downward sweep (500 ms / step, 400 px) to wake lazy-loaders.
    # Pass 2 — faster re-sweep to catch late arrivals.
    # A 2.5 s pause at the bottom lets ads finish rendering before pass 2.
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
    ) -> None:
        self._output_dir = Path(output_dir)
        self._logger = logger
        self._url_hash = url_hash
        (self._output_dir / "ad_images").mkdir(parents=True, exist_ok=True)
        (self._output_dir / "ad_videos").mkdir(parents=True, exist_ok=True)
        (self._output_dir / "ad_disclosures").mkdir(parents=True, exist_ok=True)
        from Helpers.easylist_selectors import load_selectors
        self._selectors = load_selectors() + self.EXTRA_SELECTORS
        self._visited_ad_urls: list[str] = []
        self._ad_disclosure_collector = AdDisclosureCollector()
        self._ad_disclosure_collector.init(str(self._output_dir), self._logger, self._url_hash)
        self._ad_disclosures_contents: list[dict] = []
        self._unmatched_ad_disclosure_contents: list[dict] = []
        self._n_clicked_adchoices_links = 0
        self._max_ads_captured = max_ads_captured if isinstance(max_ads_captured, int) and max_ads_captured > 0 else None

    async def collect(self, page: Page) -> dict:
        try:
            await self._ad_disclosure_collector.pre_crawl(page)
        except Exception as exc:
            self._logger.debug(f"[AdCollector] Could not register disclosure collector: {exc}")

        try:
            await page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:
            pass

        # await self._scroll_page(page)

        try:
            await page.wait_for_timeout(1500)
        except Exception:
            pass

        ads = await self._find_ads(page)
        ads.sort(key=lambda item: (item.get("y", 0), item.get("x", 0)))
        self._logger.info(f"[AdCollector] Detected {len(ads)} candidate ad element(s)")
        ad_attrs, scrape_results = await self._capture_ads(page, ads)

        self._ad_disclosures_contents = await self._ad_disclosure_collector.collect(page)

        n_matched, n_unmatched = self._match_adchoice_link(ad_attrs)
        scrape_results["nAdDisclosureMatched"] = n_matched
        scrape_results["nAdDisclosureUnmatched"] = n_unmatched
        scrape_results["nClickedAdChoices"] = self._n_clicked_adchoices_links

        self._logger.info(f"[AdCollector] Captured {len(ad_attrs)} ad screenshot(s)")
        return {
            "scrapeResults": scrape_results,
            "adAttrs": ad_attrs,
            "visitedAdUrls": self._visited_ad_urls,
            "unmatchedAdDisclosureContents": self._unmatched_ad_disclosure_contents,
        }

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
            handles = await _scan_handles()

        for handle in handles:
            try:
                href = await handle.evaluate("el => el.href")
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
        ads shared the same disclosure host (e.g. adssettings.google.com) but
        had different query-string parameters.

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
        start_ms = asyncio.get_event_loop().time() * 1000
        max_time_ms = self.SCROLL_TIMEOUT_MS
        step = 400
        max_height = 30000
        delay_pass1 = 500
        delay_pass2 = 200
        wait_after_pass1 = 2500

        def elapsed_ms() -> float:
            return asyncio.get_event_loop().time() * 1000 - start_ms

        timed_out = False

        try:
            pos = 0
            scroll_height = await page.evaluate("() => Math.min(document.body.scrollHeight, %s)" % max_height)
            scroll_height = int(scroll_height or 0)

            # Pass 1: slow downward sweep
            while pos <= scroll_height and elapsed_ms() < max_time_ms:
                await page.evaluate(f"() => window.scrollTo(0, {pos})")
                await page.wait_for_timeout(delay_pass1)
                pos += step
                scroll_height = int(await page.evaluate("() => Math.min(document.body.scrollHeight, %s)" % max_height) or 0)

            # Wait for lazy content triggered in pass 1
            await page.wait_for_timeout(wait_after_pass1)

            # Pass 2: faster forward sweep starting from current scroll position
            pos = int(await page.evaluate("() => window.scrollY") or 0)
            scroll_height = int(await page.evaluate("() => Math.min(document.body.scrollHeight, %s)" % max_height) or 0)
            while pos <= scroll_height and elapsed_ms() < max_time_ms:
                await page.evaluate(f"() => window.scrollTo(0, {pos})")
                await page.wait_for_timeout(delay_pass2)
                pos += step * 2
                scroll_height = int(await page.evaluate("() => Math.min(document.body.scrollHeight, %s)" % max_height) or 0)

            # Reset back to top so capture operations are consistent
            await page.evaluate("() => window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)

            if elapsed_ms() >= max_time_ms:
                timed_out = True

        except Exception as exc:
            self._logger.warning(f"[AdCollector] Scroll error: {exc}")
            return

        if timed_out:
            self._logger.warning(
                f"[AdCollector] Scroll timed out after {self.SCROLL_TIMEOUT_MS} ms; proceeding with current page state"
            )

    async def _find_ads(self, page: Page) -> list:
        try:
            return await page.evaluate(self._FIND_ADS_JS, self._selectors)
        except Exception as exc:
            self._logger.error(f"[AdCollector] DOM query error: {exc}")
            return []

    async def _capture_context_screenshot(self, page: Page, bbox: dict, index: int) -> tuple[str, dict]:
        viewport = await page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight, scrollX: window.scrollX, scrollY: window.scrollY })")

        # Convert page coordinates (relative to document) to viewport-relative coordinates
        # by subtracting the current scroll offset.
        page_left = bbox["x"] - viewport["scrollX"]
        page_top = bbox["y"] - viewport["scrollY"]
        page_right = page_left + bbox["width"]
        page_bottom = page_top + bbox["height"]

        viewport_left = 0
        viewport_top = 0
        viewport_right = viewport["width"]
        viewport_bottom = viewport["height"]

        margin = self.CONTEXT_SCREENSHOT_MARGIN_PX
        clip_left = max(viewport_left, int(page_left) - margin)
        clip_top = max(viewport_top, int(page_top) - margin)
        clip_right = min(viewport_right, int(page_right + 0.9999) + margin)
        clip_bottom = min(viewport_bottom, int(page_bottom + 0.9999) + margin)

        clip_width = max(1, clip_right - clip_left)
        clip_height = max(1, clip_bottom - clip_top)

        if clip_width <= 0 or clip_height <= 0:
            raise ValueError("Context clip is empty or outside the viewport")

        context_box = {
            "x": max(0, int(page_left) - clip_left),
            "y": max(0, int(page_top) - clip_top),
            "width": max(1, int(page_right + 0.9999) - int(page_left)),
            "height": max(1, int(page_bottom + 0.9999) - int(page_top)),
        }

        context_dir = self._output_dir / "ad_context_images"
        context_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = context_dir / f"ad_{index}_{self._url_hash}_context.png"
        try:
            await page.screenshot(
                path=str(screenshot_path),
                clip={
                    "x": clip_left,
                    "y": clip_top,
                    "width": clip_width,
                    "height": clip_height,
                },
            )
        except Exception:
            await page.screenshot(path=str(screenshot_path), full_page=False)
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
                const maxScrollY = Math.max(0, document.documentElement.scrollHeight - viewportH);
                const maxScrollX = Math.max(0, document.documentElement.scrollWidth - viewportW);
                const desiredY = Math.min(maxScrollY, Math.max(0, Math.round(centerY - viewportH / 2)));
                const targetY = desiredY;
                const targetX = Math.min(maxScrollX, Math.max(0, Math.round(centerX - viewportW / 2)));
                window.scrollTo(targetX, targetY);
                return { x: targetX, y: targetY };
            }""",
            bbox,
        )
        # Give the page some time to layout after scrolling.
        await page.wait_for_timeout(250)

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
            clip_width = max(1, clip_right - clip_x)
            clip_height = max(1, clip_bottom - clip_y)
            if clip_width < self.MIN_PX_FOR_SCREENSHOT or clip_height < self.MIN_PX_FOR_SCREENSHOT:
                return None
            return {"x": clip_x, "y": clip_y, "width": clip_width, "height": clip_height}

        clip = compute_clip(viewport)
        if clip is not None:
            return clip

        return None

    async def _capture_bbox_screenshot(self, page: Page, bbox: dict, index: int, element_handle: ElementHandle | None = None) -> tuple[str, dict]:
        screenshot_path = self._output_dir / "ad_images" / f"ad_{index}_{self._url_hash}.png"
        
        # We always use exact vertical/horizontal centering logic.
        # This uniformly avoids elements being obscured by sticky top-headers OR sticky bottom-footers,
        # without randomly pushing ads out of the viewport.
        await self._scroll_bbox_into_view(page, bbox)
        await page.wait_for_timeout(1000)

        # After scrolling, the ad might have moved or resized (especially if it is a sticky element itself).
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
                    await element_handle.screenshot(path=str(screenshot_path), timeout=self.ELEMENT_ACTION_TIMEOUT_MS)
                    return screenshot_path.name, bbox
                except Exception:
                    pass
            try:
                await page.screenshot(path=str(screenshot_path), full_page=False)
                self._logger.debug(f"[AdCollector] Used viewport screenshot fallback for ad_{index}")
                return screenshot_path.name, bbox
            except Exception:
                raise ValueError("Ad clip could not be mapped into the viewport")

        await page.screenshot(path=str(screenshot_path), clip=clip)

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
            context_screenshot, context_screenshot_box = await self._capture_context_screenshot(page, bbox, index)
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

        if extraction_target:
            clicked_link, disclosure = await self._click_adchoice_link_in_ad(
                ad_links_and_images,
                page,
                screenshot_name,
                element_handle=element_handle,
            )

            if not clicked_link:
                fallback_href = self._pick_adchoice_link(ad_links_and_images)
                fallback_disclosure = await self._ad_disclosure_collector.open_disclosure_in_new_tab(
                    page,
                    fallback_href,
                    ad_screenshot_name=screenshot_name,
                )
                if fallback_href:
                    clicked_link = fallback_href
                if fallback_disclosure and not disclosure:
                    disclosure = fallback_disclosure

            if not clicked_link:
                try:
                    if extraction_target:
                        element_screenshot = await extraction_target.screenshot(type="png")
                        coords = await find_ad_choices_in_screenshot(
                            element_screenshot, page
                        )
                        if coords:
                            rel_x, rel_y = coords
                            self._logger.info(
                                f"[AdCollector] OpenCV fallback: clicking AdChoices icon "
                                f"at relative ({rel_x:.1f}, {rel_y:.1f}) for ad_{index}"
                            )
                            async with page.expect_popup(timeout=3000) as popup_info:
                                await extraction_target.click(position={"x": rel_x, "y": rel_y}, force=True)
                            try:
                                popup_page = await popup_info.value
                                cv_disclosure = await self._ad_disclosure_collector.capture_disclosure_page(
                                    popup_page,
                                    ad_screenshot_name=screenshot_name,
                                )
                                if cv_disclosure:
                                    disclosure = cv_disclosure
                                    clicked_link = popup_page.url
                                    self._n_clicked_adchoices_links += 1
                            except Exception:
                                cv_disclosures = await self._ad_disclosure_collector.capture_context_disclosures(
                                    page, ad_screenshot_name=screenshot_name, settle_ms=1000
                                )
                                if cv_disclosures:
                                    disclosure = cv_disclosures[0]
                                    clicked_link = disclosure.get("pageUrl", "")
                                    self._n_clicked_adchoices_links += 1
                except Exception as exc:
                    self._logger.debug(f"[AdCollector] OpenCV fallback error for ad_{index}: {exc}")

            if clicked_link and not clicked_link.startswith("javascript:"):
                ad_attrs["clickedAdChoiceLink"] = clicked_link

            if disclosure:
                ad_attrs["adDisclosureOutLinks"] = disclosure.get("adDisclosureOutLinks", [])
                ad_attrs["adDisclosureText"] = disclosure.get("pageText", "")
                ad_attrs["adDisclosurePageUrl"] = disclosure.get("pageUrl", "")
                ad_attrs["adDisclosureScreenshot"] = disclosure.get("screenshot", "")
                self._ad_disclosures_contents.append(disclosure)

        await self._download_ad_videos(page, ad_attrs, index)

        self._logger.info(
            f"[AdCollector] ad_{index}: {ad_attrs['nodeType']}#{ad_attrs['id'] or ''} "
            f"({int(ad_attrs['width'])}x{int(ad_attrs['height'])})"
        )
        return "scraped", ad_attrs

    async def _download_ad_videos(self, page: Page, ad_attrs: dict, index: int) -> None:
        video_dir = self._output_dir / "ad_videos"
        downloaded: list[str] = []

        for frame_data in ad_attrs.get("adLinksAndImages", []):
            for video_entry in frame_data.get("videos", []):
                src = video_entry.get("src", "") if isinstance(video_entry, dict) else ""
                if not src or not src.startswith(("http://", "https://")):
                    continue
                if src in downloaded:
                    continue

                ext = Path(src.split("?")[0]).suffix.lower()
                if ext not in {".mp4", ".webm", ".ogg", ".mov", ".m4v"}:
                    ext = ".mp4"

                filename = f"ad_{index}_{self._url_hash}_video_{len(downloaded)}{ext}"
                filepath = video_dir / filename

                try:
                    response = await page.context.request.get(src, timeout=10000)
                    if response and response.ok:
                        body = await response.body()
                        filepath.write_bytes(body)
                        downloaded.append(src)
                        video_entry["downloadedFile"] = filename
                        self._logger.info(f"[AdCollector] Downloaded video for ad_{index}: {filename}")
                    else:
                        self._logger.debug(f"[AdCollector] Video download failed (HTTP {getattr(response, 'status', '?')}): {src[:120]}")
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
                remaining_ads = len(ads_to_process) - index
                n_skipped_ads += max(0, remaining_ads)
                self._logger.info(
                    f"[AdCollector] Reached max successful captures ({self._max_ads_captured}); skipped {remaining_ads} remaining ad(s)"
                )
                break
            if page.is_closed():
                remaining_ads = len(ads_to_process) - index
                n_skipped_ads += remaining_ads
                self._logger.warning(
                    f"[AdCollector] Page closed before ad_{index}; skipped {remaining_ads} remaining ad(s)"
                )
                break
            try:
                status, ad_attrs = await self._capture_single_ad(page, ad, index)
                if status == "scraped" and ad_attrs is not None:
                    ad_details.append(ad_attrs)
                elif status == "small":
                    n_small_ads += 1
                elif status == "empty":
                    n_empty_ads += 1
                elif status == "removed":
                    n_removed_ads += 1
            except Exception as exc:
                if "Timeout" in type(exc).__name__ or "Timeout" in str(exc):
                    n_timed_out_ads += 1
                    self._logger.warning(
                        f"[AdCollector] Timed out scraping ad_{index} after {self.AD_SCRAPE_TIMEOUT_MS} ms"
                    )
                    continue
                n_removed_ads += 1
                self._logger.warning(f"[AdCollector] Screenshot error for ad_{index}: {exc}")

        scrape_results = {
            "nDetectedAds": len(ads),
            "nAdsScraped": len(ad_details),
            "nSmallAds": n_small_ads,
            "nEmptyAds": n_empty_ads,
            "nRemovedAds": n_removed_ads,
            "nSkippedAds": n_skipped_ads,
            "nTimedOutAds": n_timed_out_ads,
        }
        return ad_details, scrape_results

    def _frame_identifier(self, seed: str) -> str:
        if not seed:
            seed = f"frame:{self._url_hash}"
        return hashlib.md5(seed.encode("utf-8"), usedforsecurity=False).hexdigest().upper()[:24]

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
    ) -> dict:
        links = await self._eval_all(
            context,
            "a",
            """
            (links) => links.map((link) => [{
                googAdUrl: (() => { try { return new URL(link.href).searchParams.get('adurl'); } catch (_) { return null; } })(),
                href: link.href,
                outerHTML: link.outerHTML.slice(0, 2000),
            }])
            """,
        )
        image_links = await self._eval_all(
            context,
            "a",
            """
            (links) => links
                .filter((link) => !!link.querySelector('img, picture, svg, canvas, video'))
                .map((link) => ({
                    googAdUrl: (() => { try { return new URL(link.href).searchParams.get('adurl'); } catch (_) { return null; } })(),
                    href: link.href,
                    imgSrc: (() => {
                        const media = link.querySelector('img, source, video');
                        if (!media) return null;
                        return media.currentSrc || media.src || media.getAttribute('src') || null;
                    })(),
                    outerHTML: link.outerHTML.slice(0, 2000),
                }))
            """,
        )
        other_links = await self._eval_all(
            context,
            "a",
            """
            (links) => links
                .filter((link) => !link.querySelector('img, picture, svg, canvas, video'))
                .map((link) => ({
                    googAdUrl: (() => { try { return new URL(link.href).searchParams.get('adurl'); } catch (_) { return null; } })(),
                    href: link.href,
                    text: (link.innerText || '').trim().slice(0, 500),
                    outerHTML: link.outerHTML.slice(0, 2000),
                }))
            """,
        )
        gwd_links = await self._eval_all(
            context,
            "gwd-taparea",
            """
            (areas) => areas.map((area) => [{
                googAdUrl: (() => {
                    try {
                        const raw = area.getAttribute('exit-override-url') || '';
                        return new URLSearchParams(raw.split('?')[1] || '').get('adurl');
                    } catch (_) {
                        return null;
                    }
                })(),
                href: area.getAttribute('exit-override-url'),
                outerHTML: area.outerHTML.slice(0, 2000),
            }])
            """,
        )
        imgs = await self._eval_all(
            context,
            "img",
            """
            (imgs) => {
                function getXPath(el) {
                    if (el.id) {
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

                return imgs.map((img) => {
                const box = img.getBoundingClientRect();
                const src = img.currentSrc || img.src;
                return {
                    x: box.x,
                    y: box.y,
                    width: box.width,
                    height: box.height,
                    src,
                    outerHTML: img.outerHTML.slice(0, 2000),
                    origin: {
                        kind: src && src.startsWith('data:') ? 'inline-data-url' : 'url',
                        sourceType: 'img-element',
                        sourceAttribute: 'src',
                        tagName: img.tagName,
                        id: img.id || '',
                        className: typeof img.className === 'string' ? img.className.slice(0, 300) : '',
                        xpath: getXPath(img),
                    },
                };
                });
            }
            """,
        )
        bg_imgs = await self._eval_all(
            context,
            "*",
            """
            (elements) => {
                function getXPath(el) {
                    if (el.id) {
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

                return elements.map((el) => {
                const bg = el.currentStyle?.backgroundImage || window.getComputedStyle(el).backgroundImage;
                if (!bg || bg === 'none') {
                    return null;
                }
                const url = bg.replace(/^url\\((.*)\\)$/,'$1').replace(/^['\\"]|['\\"]$/g, '');
                const box = el.getBoundingClientRect();
                return {
                    x: box.x,
                    y: box.y,
                    width: box.width,
                    height: box.height,
                    src: url,
                    outerHTML: el.outerHTML.slice(0, 2000),
                    origin: {
                        kind: url && url.startsWith('data:') ? 'inline-data-url' : 'url',
                        sourceType: 'css-background-image',
                        sourceAttribute: 'background-image',
                        tagName: el.tagName,
                        id: el.id || '',
                        className: typeof el.className === 'string' ? el.className.slice(0, 300) : '',
                        xpath: getXPath(el),
                    },
                };
                }).filter(Boolean);
            }
            """,
        )
        videos = await self._eval_all(
            context,
            "video",
            "(videos) => videos.map((video) => ({ src: video.src, width: video.width, height: video.height })).filter((item) => item.src)",
        )
        scripts = await self._eval_all(
            context,
            "script",
            "(scripts) => scripts.map((script) => script.src).filter(Boolean)",
        )
        iframes = await self._eval_all(
            context,
            "iframe",
            "(iframes) => iframes.map((iframe) => iframe.src).filter(Boolean)",
        )
        adchoices_links = await self._eval_all(
            context,
            self.ADCHOICES_SELECTOR,
            "(links) => links.map((link) => link.href).filter(href => href && !href.startsWith('javascript:'))",
        )
        adchoices_icon_links = await self._eval_all(
            context,
            self.ADCHOICES_ICON_SELECTOR,
            """
            (icons) => {
                const normalize = (raw) => {
                    if (!raw) return '';
                    if (raw.startsWith('//')) return location.protocol + raw;
                    return raw;
                };
                const firstUrlInText = (text) => {
                    if (!text) return '';
                    const match = String(text).match(/((?:https?:)?\\/\\/[^\\s'\"<>]+)/i);
                    return match ? normalize(match[1]) : '';
                };

                return icons
                    .map((icon) => {
                        const clickable = icon.closest('a, [onclick], [role="button"], button');
                        const node = clickable || icon;
                        const href = normalize(
                            node?.getAttribute?.('href') ||
                            node?.href ||
                            node?.getAttribute?.('data-href') ||
                            node?.getAttribute?.('data-url') ||
                            node?.getAttribute?.('data-destination-url') ||
                            node?.getAttribute?.('data-click-url') ||
                            ''
                        );
                        if (href && !href.startsWith('javascript:')) {
                            return href;
                        }

                        const fromOnclick = firstUrlInText(node?.getAttribute?.('onclick') || '');
                        if (fromOnclick && !fromOnclick.startsWith('javascript:')) {
                            return fromOnclick;
                        }

                        return '';
                    })
                    .filter(Boolean);
            }
            """,
        )

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

        def _merge_unique(existing, incoming, key_fn):
            seen = {key_fn(item) for item in existing if item is not None}
            for item in incoming or []:
                k = key_fn(item)
                if k in seen:
                    continue
                seen.add(k)
                existing.append(item)

        _merge_unique(adchoices_links, adchoices_icon_links, lambda x: x)

        if isinstance(deep, dict):
            _merge_unique(links, deep.get("links", []), lambda x: json.dumps(x, sort_keys=True))
            _merge_unique(image_links, deep.get("imageLinks", []), lambda x: (x or {}).get("href", "") + "|" + str((x or {}).get("imgSrc", "")))
            _merge_unique(other_links, deep.get("otherLinks", []), lambda x: (x or {}).get("href", ""))
            _merge_unique(imgs, deep.get("imgs", []), lambda x: (x or {}).get("src", ""))
            _merge_unique(bg_imgs, deep.get("bgImgs", []), lambda x: (x or {}).get("src", ""))
            _merge_unique(videos, deep.get("videos", []), lambda x: (x or {}).get("src", ""))
            _merge_unique(scripts, deep.get("scripts", []), lambda x: x)
            _merge_unique(iframes, deep.get("iframes", []), lambda x: x)
            _merge_unique(adchoices_links, deep.get("adChoicesLinks", []), lambda x: x)

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

        return [
            {
                "frameUrl": page_url,
                "containsImgsOrLinks": bool(imgs or links or image_links or other_links or adchoices),
                "isMainDocument": False,
                "parentFrameUrl": page_url,
                "frameId": self._frame_identifier(f"{page_url}:ad:{ad_index}"),
                "parentFrameId": self._frame_identifier(page_url),
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
                "frameId": self._frame_identifier(f"main:{page_url}"),
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
        frame_id = self._frame_identifier(frame_url or page_url)
        parent_frame_id = self._frame_identifier(parent_frame_url) if parent_frame else None
        entries.append(
            await self._extract_context_artifacts(
                frame,
                frame_url=frame_url,
                frame_id=frame_id,
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

        root_frame_id = self._frame_identifier(f"{page_url}:ad:{ad_index}")
        root_entry = await self._extract_context_artifacts(
            element_handle,
            frame_url=page_url,
            frame_id=root_frame_id,
            parent_frame_url=page_url,
            parent_frame_id=self._frame_identifier(page_url),
            is_main_document=False,
        )
        entries.append(root_entry)
        entries.append(
            {
                "frameUrl": "",
                "containsImgsOrLinks": False,
                "isMainDocument": True,
                "parentFrameUrl": "unknown",
                "frameId": self._frame_identifier(f"main:{page_url}"),
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
            for iframe in iframes:
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
                        const match = String(text).match(/((?:https?:)?\\/\\/[^\\s'\"<>]+)/i);
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

                                        const urlRegex = /((?:https?:)?\/\/[^\s'\"<>]+)/gi;
                                        const textHints = [
                                            'why this ad',
                                            'whythisad',
                                            'adchoice',
                                            'adchoices',
                                            'privacy/adinfo',
                                            'see more ads by this advertiser',
                                            'report this ad',
                                            'criteo',
                                            'taboola',
                                            'outbrain',
                                        ];
                                        const urlHints = [
                                            'adssettings.google.com',
                                            'privacy.us.criteo.com',
                                            'privacy.eu.criteo.com',
                                            'criteo',
                                            'taboola',
                                            'outbrain',
                                        ];

                                        const candidateRoots = [
                                            el,
                                            el.closest?.('a, [onclick], [role="button"], button') || null,
                                            el.parentElement || null,
                                            el.closest?.('div, span, li, section, article, aside, main') || null,
                                        ].filter(Boolean);

                                        const scoreNode = (node, href) => {
                                            const html = `${node?.outerHTML || ''}\n${node?.innerText || ''}`.toLowerCase();
                                            const hrefLower = String(href || '').toLowerCase();
                                            const urls = Array.from(new Set((html.match(urlRegex) || []).map((raw) => normalize(raw)).filter(Boolean)));
                                            let score = 0;
                                            if (html.includes(hrefLower)) score += 1000;
                                            if (urlHints.some((hint) => hrefLower.includes(hint))) score += 300;
                                            if (urls.length === 1) score += 900;
                                            else if (urls.length > 1) score += Math.max(0, 500 - (urls.length * 60));
                                            if (textHints.some((hint) => html.includes(hint))) score += 250;
                                            if (html.includes('href=') || html.includes('data-href') || html.includes('data-url') || html.includes('onclick')) score += 100;
                                            if (html.includes('<iframe')) score += 25;
                                            if (hrefLower && html.split(hrefLower).length === 2) score += 50;
                                            return score;
                                        };
                        const fromOnclick = firstUrlInText(node.getAttribute?.('onclick') || '');
                        if (fromOnclick) return fromOnclick;

                        const nestedAnchor = node.querySelector?.('a[href]');

                                        const scored = new Map();
                                        const consider = (node) => {
                                            if (!node) return;
                                            const hrefs = [];
                                            const add = (raw) => {
                                                const href = normalize(raw);
                                                if (!href || href.startsWith('javascript:') || hrefs.includes(href)) return;
                                                hrefs.push(href);
                                            };

                                            add(node.getAttribute?.('href') || node.href || '');
                                            add(node.getAttribute?.('data-href') || node.getAttribute?.('data-url') || node.getAttribute?.('data-destination-url') || node.getAttribute?.('data-click-url') || '');
                                            add(firstUrlInText(node.getAttribute?.('onclick') || ''));

                                            const nestedAnchor = node.querySelector?.('a[href]');
                                            if (nestedAnchor) {
                                                add(nestedAnchor.getAttribute?.('href') || nestedAnchor.href || '');
                                            }

                                            for (const href of hrefs) {
                                                const score = scoreNode(node, href);
                                                const current = scored.get(href);
                                                if (!current || score > current.score) {
                                                    scored.set(href, { href, score });
                                                }
                                            }
                                        };

                                        for (const node of candidateRoots) {
                                            consider(node);
                                        }
                        if (nestedAnchor) {
                        el.parentElement || null,
                                        const docMatchNodes = [
                                            ...doc.querySelectorAll(adChoiceSelector),
                                            ...doc.querySelectorAll('a[href*="whythisad" i], a[href*="adchoice" i], a[href*="privacy/adinfo" i], a[href*="criteo" i], a[href*="taboola" i], a[href*="outbrain" i]'),
                                        ];
                                        for (const node of docMatchNodes) {
                                            consider(node);
                                        }

                                        const best = Array.from(scored.values()).sort((a, b) => b.score - a.score)[0];
                                        if (best) {
                                            return best.href;
                                        }

                                        for (const node of candidateRoots) {
                                            const href = fromNode(node);
                                            if (href) return href;
                                        }

                                        const docMatch = docMatchNodes[0] || null;
                                        return fromNode(docMatch);
                        const href = fromNode(node);
                        if (href) return href;
                    }

                    const doc = el?.ownerDocument || document;
                    const docMatch =
                        doc.querySelector(adChoiceSelector) ||
                        doc.querySelector('a[href*="whythisad" i], a[href*="adchoice" i], a[href*="privacy/adinfo" i]');
                    return fromNode(docMatch);
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
