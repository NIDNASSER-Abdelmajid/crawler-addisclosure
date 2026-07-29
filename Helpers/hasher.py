import hashlib
import re
from urllib.parse import urlparse


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
