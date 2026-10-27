"""Reading pages: URL rules, dates, and pulling post details and pricing out of HTML/XML."""
from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from lxml import etree

BLOG_WORDS = {"blog", "blogs", "resources", "articles", "article", "insights", "news", "learn", "guides",
              "guide", "posts", "post", "stories", "academy", "library", "journal", "magazine", "hub", "content"}
NOT_POSTS = {"tag", "tags", "category", "categories", "author", "authors", "page", "search", "feed", "rss",
             "wp-content", "wp-json", "cdn-cgi", "login", "signup", "cart", "account", "privacy", "terms",
             "careers", "jobs", "contact", "about", "topics", "topic", "archive", "archives", "amp"}
FILE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".mp4", ".xml", ".json",
                   ".css", ".js", ".txt", ".gz")
BLOCK_SIGNS = ("just a moment...", "cf-chl", "challenge-platform", "attention required! | cloudflare",
               "checking your browser", "captcha-delivery", "px-captcha", "access denied")
DATE_IN_URL = re.compile(r"/((?:19|20)\d{2})/(0?[1-9]|1[0-2])(?:/(0?[1-9]|[12]\d|3[01]))?/")
PRICE = re.compile(r"(?:\$|€|£|₹|Rs\.?\s?|USD\s?|INR\s?)\s?\d[\d,]*(?:\.\d{1,2})?")
PERIOD = re.compile(r"/\s?(?:mo|month|yr|year|user|seat)\b|per\s+(?:month|year|user|seat)|monthly|annually|yearly",
                    re.I)


# ======================================================================== URLs, dates, text
def normalize_domain(raw: str) -> str:
    """'https://www.Ahrefs.com/blog' -> 'www.ahrefs.com'. Raises ValueError for junk input."""
    raw = (raw or "").strip()
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").lower().strip(".")
    if "." not in host or " " in host:
        raise ValueError("Please enter a valid domain, e.g. ahrefs.com")
    return host


def normalize_url(url: str) -> str:
    """One canonical form per post, so the same post is never stored twice."""
    p = urlparse(url.strip())
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = "&".join(q for q in p.query.split("&") if q and not q.startswith(("utm_", "ref=", "fbclid", "gclid")))
    return urlunparse(("https", (p.hostname or "").lower(), path, "", query, ""))


def root_domain(host: str) -> str:
    parts = host.lower().split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "ac"}:
        return ".".join(parts[-3:])          # example.co.uk / example.co.in
    return ".".join(parts[-2:])


def same_site(url: str, host: str) -> bool:
    h = urlparse(url).hostname or ""
    return bool(h) and root_domain(h) == root_domain(host)


def segments(url: str) -> list[str]:
    return [s for s in urlparse(url).path.lower().split("/") if s]


def looks_like_slug(s: str) -> bool:
    """Post URLs end in a readable slug like 'how-to-do-keyword-research'."""
    return ("-" in s and len(s) >= 8) or len(s) >= 16


def is_post_url(url: str, host: str, listing_paths: frozenset[str] = frozenset(), loose: bool = False) -> bool:
    """Does this URL look like a single blog post?  loose=True for URLs from a 'post' sitemap."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not same_site(url, host) or p.path.lower().endswith(FILE_EXTENSIONS):
        return False
    segs = segments(url)
    if not segs or any(s in NOT_POSTS for s in segs) or not looks_like_slug(segs[-1]):
        return False
    if loose:
        return True
    lang_prefix = re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", segs[0]) is not None
    return (
        (segs[0] in BLOG_WORDS and len(segs) >= 2)                              # /blog/my-post
        or (lang_prefix and len(segs) >= 3 and segs[1] in BLOG_WORDS)           # /en/blog/my-post
        or DATE_IN_URL.search(p.path + "/") is not None                         # /2026/10/my-post
        or (p.hostname or "").split(".")[0] in {"blog", "blogs", "news", "resources"}  # blog.site.com/my-post
        or any(p.path.lower().startswith(lp + "/") for lp in listing_paths if lp)      # under a listing page
    )


def parse_date(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else dateparser.parse(str(value).strip()[:60])
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.year < 2000 or dt > datetime.now(timezone.utc) + timedelta(days=2):
        return None        # junk or future date
    return dt


def looks_blocked(status: int, text: str) -> bool:
    """Bot protection pages (Cloudflare, DataDome, ...) or 403/429 responses."""
    if status in (403, 429):
        return True
    head = text[:4000].lower()
    if status in (401, 503):
        return any(s in head for s in BLOCK_SIGNS)
    # Some challenge pages come back as "200 OK"
    return status == 200 and len(text) < 15000 and any(s in head for s in ("cf-chl", "challenge-platform",
                                                                            "captcha-delivery"))


def clean(text, limit: int = 500) -> Optional[str]:
    if not text:
        return None
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text[:limit] or None

# ======================================================================== a blog post
@dataclass
class Post:
    url: str
    title: Optional[str] = None
    published_at: Optional[datetime] = None
    description: Optional[str] = None
    date_is_exact: bool = False      # True when the date came from the post itself or the RSS feed

    def merge(self, other: "Post") -> None:
        self.title = self.title or other.title
        self.description = self.description or other.description
        if other.published_at and (not self.published_at or (other.date_is_exact and not self.date_is_exact)):
            self.published_at, self.date_is_exact = other.published_at, other.date_is_exact


# ======================================================================== page readers
def read_post_page(html: str, url: str) -> Post:
    """Title, publish date and meta description from a post page.
    Most reliable first: JSON-LD structured data -> meta tags -> <time> tag -> date in the URL."""
    soup = BeautifulSoup(html, "lxml")
    post = Post(url=url)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        nodes += [n for d in nodes if isinstance(d, dict) for n in d.get("@graph", [])]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if any(t in ("Article", "BlogPosting", "NewsArticle", "TechArticle") for t in types):
                post.title = post.title or clean(node.get("headline"), 300)
                post.published_at = post.published_at or parse_date(node.get("datePublished"))
                post.description = post.description or clean(node.get("description"))

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"property": n})
            if tag and tag.get("content", "").strip():
                return tag["content"].strip()
        return None

    post.description = clean(meta("description")) or post.description or clean(meta("og:description"))
    post.title = post.title or clean(meta("og:title"), 300)
    if not post.title and soup.title and soup.title.string:
        post.title = clean(re.sub(r"\s+[|\-–—]\s+[^|\-–—]+$", "", soup.title.string), 300)
    post.published_at = (post.published_at
                         or parse_date(meta("article:published_time", "datePublished", "publish-date", "date"))
                         or parse_date((soup.find("time", datetime=True) or {}).get("datetime")))
    if not post.published_at and (m := DATE_IN_URL.search(urlparse(url).path + "/")):
        post.published_at = parse_date(f"{m.group(1)}-{m.group(2)}-{m.group(3) or 1}")
    return post


def read_pricing_page(html: str) -> dict[str, list[str]]:
    """{'Pro': ['$99 /month'], ...}: every price on the page, grouped under the nearest plan heading."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()
    plans: dict[str, set[str]] = {}
    for text in soup.find_all(string=PRICE):
        box = text.parent.parent or text.parent
        context = re.sub(r"\s+", " ", box.get_text(" ", strip=True))
        m = PRICE.search(context)
        if not m:
            continue
        period = PERIOD.search(context[m.end(): m.end() + 40])
        price = re.sub(r"\s+", "", m.group(0)) + (f" {period.group(0).lower()}" if period else "")
        plans.setdefault(_plan_name(text.parent), set()).add(price)
    if sum(len(v) for v in plans.values()) > 40:
        return {}                   # not a pricing table (e.g. a product catalogue)
    return {name: sorted(prices) for name, prices in sorted(plans.items())}


def _plan_name(el) -> str:
    """Walk up from the price to the plan's card and use its heading as the plan name."""
    for _ in range(6):
        el = el.parent
        if el is None or el.name in ("body", "html"):
            break
        h = el.find(["h1", "h2", "h3", "h4", "h5"])
        if h and 0 < len(h.get_text(strip=True)) <= 50 and not PRICE.search(h.get_text()):
            return h.get_text(" ", strip=True)
    return "Plan"


def _parse_xml(data: bytes):
    if data[:2] == b"\x1f\x8b":                      # .xml.gz sitemaps
        try:
            data = gzip.decompress(data)
        except OSError:
            return None
    try:
        return etree.fromstring(data.strip(), etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    except (etree.XMLSyntaxError, ValueError):
        return None


def _child_text(el, name: str) -> Optional[str]:
    for child in el:
        if isinstance(child.tag, str) and etree.QName(child).localname == name and child.text:
            return child.text.strip()
    return None
