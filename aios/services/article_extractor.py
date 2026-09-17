"""Article body extraction.

Direct migration of the original ``fetch_article_text``: fetch the page, drop
chrome elements, keep paragraphs of reasonable length. Kept deliberately simple
and dependency-light; a failure here degrades an article to snippet-only rather
than failing the run.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional
from urllib.parse import urlparse, urlunparse

import requests

from ..config import USER_AGENT

logger = logging.getLogger(__name__)

#: Aggregator redirects whose HTML carries no usable article text.
SKIP_HOSTS = ("news.google.com", "news.yahoo.com/rss")

#: Query parameters that only exist for analytics and break URL identity.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "spm", "from", "ref", "oc",
}

_MIN_PARAGRAPH = 40


def canonicalize_url(url: str) -> str:
    """Normalise a URL for identity.

    Drops tracking parameters, the fragment, a ``www.`` prefix and a trailing
    slash, and folds ``http`` into ``https`` - otherwise the same article
    reached over both schemes would be stored twice. Only used for identity;
    the original URL is always preserved separately.
    """
    if not url:
        return ""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return url.strip()

    query_pairs = []
    for chunk in (parts.query or "").split("&"):
        if not chunk:
            continue
        name = chunk.split("=", 1)[0].lower()
        if name in TRACKING_PARAMS:
            continue
        query_pairs.append(chunk)

    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"

    scheme = parts.scheme.lower()
    if scheme in ("http", "https", ""):
        scheme = "https"

    return urlunparse((scheme, netloc, path, "", "&".join(query_pairs), ""))


def url_hash(url: str) -> str:
    """Stable identity for an article URL."""
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    """Hash of normalised body text, used for near-duplicate detection."""
    normalised = re.sub(r"\s+", " ", (text or "")).strip().lower()
    if not normalised:
        return ""
    return hashlib.sha256(normalised[:4000].encode("utf-8")).hexdigest()


class ArticleExtractor:
    """Fetches and cleans article bodies."""

    def __init__(
        self,
        max_chars: int = 7000,
        timeout: int = 20,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.max_chars = max_chars
        self.timeout = timeout
        self._http = session or requests.Session()

    def fetch(self, url: str) -> str:
        """Return extracted body text, or '' when extraction is not possible."""
        if not url or any(host in url for host in SKIP_HOSTS):
            return ""
        try:
            response = self._http.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.debug("Body fetch failed for %s: %s", url[:80], exc)
            return ""

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            return ""
        return self.extract(response.text)

    def extract(self, html_text: str) -> str:
        """Pull readable paragraphs out of raw HTML."""
        if not html_text:
            return ""
        try:
            from bs4 import BeautifulSoup
        except Exception:  # pragma: no cover - bs4 is a hard requirement in practice
            text = re.sub(r"<[^>]+>", " ", html_text)
            return re.sub(r"\s+", " ", text).strip()[: self.max_chars]

        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
            tag.decompose()
        paragraphs = [
            re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
            for p in soup.find_all("p")
        ]
        body = "\n".join(p for p in paragraphs if len(p) >= _MIN_PARAGRAPH)
        if not body:
            # Fall back to the whole document when the page has no <p> structure.
            body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        return body[: self.max_chars]
