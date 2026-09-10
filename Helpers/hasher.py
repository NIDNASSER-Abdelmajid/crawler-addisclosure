import hashlib
import re
from urllib.parse import urlparse

try:
    import tldextract
    _TLDEXTRACT_AVAILABLE = True
except ImportError:
    _TLDEXTRACT_AVAILABLE = False


def get_url_hash(url: str) -> str:
    """Return a 16-character hex hash of the URL."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def get_url_slug(url: str) -> str:
    """
    Return a filesystem-safe slug derived from the URL host.
    Examples:
        https://www.bbc.com      -> bbc_com
        https://www.nytimes.com  -> nytimes_com
    """
    host = urlparse(url).netloc.lower()
    host = re.sub(r"^www\.", "", host)          # strip leading www.
    host = re.sub(r"[^a-z0-9]+", "_", host)     # non-alnum -> underscore
    return host.strip("_") or "site"


def get_folder_name(url: str) -> str:
    """Return the per-site output folder name.

    This should be a human-readable folder based on the site's hostname.
    Example: bbc.com
    """
    host = urlparse(url).netloc.lower()
    host = re.sub(r"^www\.", "", host)
    host = host.split(":")[0]  # strip any port
    # Sanitize to a filesystem-safe form (should already be safe for domains).
    host = re.sub(r"[^a-z0-9.\-]", "_", host)
    return host or "site"


def get_registrable_domain(url_or_domain: str) -> str:
    """Extract the registrable domain (e.g., 'example.co.uk') from a URL or hostname.

    Uses tldextract for correct handling of multi-part TLDs like .co.uk, .com.au, etc.
    Falls back to intelligent 2/3-part suffix extraction if tldextract is not installed.
    """
    raw = (url_or_domain or "").strip()
    if not raw:
        return "unknown"
    if "://" not in raw:
        raw = f"http://{raw}"

    if _TLDEXTRACT_AVAILABLE:
        try:
            extracted = tldextract.extract(raw)
            registered = getattr(extracted, "top_domain_under_public_suffix", None) or getattr(extracted, "registered_domain", "")
            if registered:
                return registered.lower()
        except Exception:
            pass

    # Fallback: parse hostname and extract base registrable domain
    try:
        host = urlparse(raw).hostname or raw
    except Exception:
        host = raw

    host = re.sub(r"^www\.", "", host.lower()).strip(".")
    parts = host.split(".")
    if len(parts) <= 2:
        return host or "unknown"

    # Multi-part ccTLD heuristic (e.g. .co.uk, .com.br, .org.uk, .com.au)
    common_second_level = {"co", "com", "org", "net", "edu", "gov", "ac", "biz", "info", "mil", "nom"}
    if len(parts[-1]) == 2 and parts[-2] in common_second_level and len(parts) >= 3:
        return ".".join(parts[-3:])

    return ".".join(parts[-2:])


