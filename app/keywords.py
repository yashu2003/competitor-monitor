"""Enrichment: each new post's target keyword + its monthly search volume.

Target keyword: posts are usually optimised for one phrase, and SEOs put it in the URL slug, the title and
the meta description. We pick the 1-4 word phrase from the title that best matches the slug and description.
(Brand-new posts don't rank yet, so "ranked keywords" APIs return nothing for them; on-page signals do.)

Search volume: DataForSEO (Google Ads search volume), all keywords in one request.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from app import config

STOPWORDS = set("""
a an the and or but of to in on for with by at from as is are was be it its this that your you we our how what
why when which who vs into about than can should will do does get new best top ultimate complete guide step
steps tips ways examples easy simple free everything know need more most all every
""".split())


def _words(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9+#'-]*", text or "")]


def target_keyword(url: str, title: Optional[str], description: Optional[str]) -> Optional[str]:
    slug = [w for w in re.split(r"[-_]+", (urlparse(url).path.rstrip("/").split("/") or [""])[-1].lower())
            if w and not w.isdigit()]
    slug_set, desc_set = set(slug), set(_words(description or ""))
    title_words = [w for w in _words(title or "") if not re.fullmatch(r"(19|20)\d{2}", w)]

    best, best_score = None, 0.0
    for n in (4, 3, 2, 1):
        for i in range(len(title_words) - n + 1):
            phrase = title_words[i:i + n]
            if phrase[0] in STOPWORDS or phrase[-1] in STOPWORDS or any(w.isdigit() for w in phrase):
                continue
            content = [w for w in phrase if w not in STOPWORDS]
            score = (2 * sum(w in slug_set for w in content) + sum(w in desc_set for w in content)) / len(content)
            score += {1: 0.0, 2: 0.6, 3: 0.7, 4: 0.4}[n]     # real search queries are usually 2-3 words
            if score > best_score:
                best, best_score = " ".join(phrase), score
    if not best:
        best = " ".join([w for w in slug if w not in STOPWORDS][:4])   # no usable title -> use the slug
    # Google Ads (DataForSEO) rejects keywords with symbols, so keep letters, numbers and spaces only
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", best)).strip() or None


def search_volumes(keywords: list[str], transport: httpx.BaseTransport | None = None) -> tuple[dict[str, int], Optional[str]]:
    """Returns ({keyword: monthly volume}, error message or None). Never raises."""
    keywords = list(dict.fromkeys(k for k in keywords if k))
    if not keywords:
        return {}, None
    login, password = config.dataforseo_credentials()
    if not (login and password):
        return {}, "DataForSEO API key not set, so search volume was skipped"
    try:
        with httpx.Client(timeout=40, transport=transport) as client:
            r = client.post("https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live",
                            auth=(login, password),
                            json=[{"keywords": keywords, "location_code": config.dataforseo_location(),
                                   "language_code": "en"}])
        body = r.json() if "json" in r.headers.get("content-type", "") else {}
    except (httpx.HTTPError, ValueError):
        return {}, "Couldn't reach DataForSEO, so search volume was skipped"

    code = body.get("status_code")
    if r.status_code == 401 or code == 40100:
        return {}, "DataForSEO API key is invalid, so search volume was skipped"
    if code in (40200, 40210):
        return {}, "DataForSEO account is out of credits, so search volume was skipped"
    task = (body.get("tasks") or [{}])[0]
    if code != 20000 or task.get("status_code") != 20000:
        msg = task.get("status_message") or body.get("status_message") or f"HTTP {r.status_code}"
        return {}, f"DataForSEO error ({msg}), so search volume was skipped"
    return {item["keyword"]: item.get("search_volume") for item in task.get("result") or []
            if item.get("keyword")}, None
