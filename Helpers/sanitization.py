"""Helpers/sanitization.py — Centralized Privacy Sanitization for URLs, Headers, Tokens, and DOM Fields.

Ensures that raw authentication tokens, passwords, secrets, session cookies,
and script/executable injections are never persisted to disk or leaked across data boundaries.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

SENSITIVE_PARAM_NAMES = {
    "token", "auth", "session", "key", "secret", "password", "passwd",
    "bearer", "sig", "signature", "access_token", "id_token", "api_key",
    "apikey", "session_id", "sessionid", "sid", "phpsessid", "jsessionid"
}

SENSITIVE_HEADER_NAMES = {
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "x-auth-token", "x-api-key", "x-csrf-token", "x-xsrf-token",
}

SCRIPT_TAG_REGEX = re.compile(r"<\s*script[^>]*>.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL)
STYLE_TAG_REGEX = re.compile(r"<\s*style[^>]*>.*?<\s*/\s*style\s*>", re.IGNORECASE | re.DOTALL)


def sanitize_url(url: str | None, redact_tokens_only: bool = True) -> str:
    """Sanitize URL by removing or redacting sensitive query tokens and passwords."""
    if not url or not isinstance(url, str):
        return ""

    try:
        parsed = urlparse(url)
        # 1. Remove password from netloc (e.g. user:pass@host)
        netloc = parsed.netloc
        if "@" in netloc:
            auth_part, host_part = netloc.split("@", 1)
            if ":" in auth_part:
                user = auth_part.split(":", 1)[0]
                netloc = f"{user}:[REDACTED]@{host_part}"

        # 2. Sanitize query string
        if not parsed.query:
            return urlunparse(parsed._replace(netloc=netloc))

        if not redact_tokens_only:
            # Complete strip of query string
            return urlunparse(parsed._replace(netloc=netloc, query=""))

        qs = parse_qs(parsed.query, keep_blank_values=True)
        sanitized_qs = []
        for k, vals in qs.items():
            k_lower = k.lower()
            if any(s in k_lower for s in SENSITIVE_PARAM_NAMES):
                sanitized_qs.append(f"{k}=[REDACTED]")
            else:
                for v in vals:
                    sanitized_qs.append(f"{k}={v}")

        new_query = "&".join(sanitized_qs)
        return urlunparse(parsed._replace(netloc=netloc, query=new_query))
    except Exception:
        return url.split("?")[0].split("#")[0]


def sanitize_headers(headers: dict[str, Any] | None) -> dict[str, Any]:
    """Redact sensitive headers (Authorization, Cookie, Set-Cookie, etc.)."""
    if not headers or not isinstance(headers, dict):
        return {}

    sanitized: dict[str, Any] = {}
    for k, v in headers.items():
        k_str = str(k)
        if k_str.lower() in SENSITIVE_HEADER_NAMES:
            sanitized[k_str] = "[REDACTED]"
        else:
            sanitized[k_str] = v
    return sanitized


def sanitize_dom_text(text: str | None, max_length: int = 2000) -> str:
    """Sanitize DOM HTML/text by stripping executable tags, redacting secrets, and truncating."""
    if not text or not isinstance(text, str):
        return ""

    # 1. Strip script and style tags
    clean = SCRIPT_TAG_REGEX.sub("", text)
    clean = STYLE_TAG_REGEX.sub("", clean)

    # 2. Redact token-like patterns: e.g. token=xyz, bearer xyz
    clean = re.sub(r"(?i)\b(bearer\s+)[a-zA-Z0-9_\-\.]{10,}", r"\1[REDACTED]", clean)
    clean = re.sub(r"(?i)\b(password|passwd|secret)\s*[:=]\s*['\"]?[^\s'\"]+['\"]?", r"\1=[REDACTED]", clean)

    # 3. Truncate to maximum bound
    if len(clean) > max_length:
        clean = clean[:max_length] + " [TRUNCATED]"

    return clean.strip()


def sanitize_token_value(token_str: str | None) -> str:
    """Redact or format token safely."""
    if not token_str:
        return ""
    if len(token_str) <= 6:
        return "[REDACTED]"
    return f"{token_str[:2]}...[REDACTED]...{token_str[-2:]}"
