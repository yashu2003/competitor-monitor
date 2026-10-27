"""Our own scraper (no scraping APIs).

find_posts(domain) looks for blog posts in 4 places and merges the results:
  1. Homepage        - links to the blog, "latest posts" links, the RSS link, the pricing link
  2. RSS/Atom feed   - most blogs have one; gives title + publish date for the latest posts
  3. XML sitemap     - the site's full list of URLs (found via robots.txt)
  4. Listing pages   - /blog, /resources, /insights ... (catches blogs with no feed or sitemap)
Using several sources means one missing or blocked source doesn't stop us finding posts.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup
from lxml import etree

from app.parsing import (BLOG_WORDS, Post, _child_text, _parse_xml, clean, is_post_url, looks_blocked,
                         normalize_url, parse_date, read_post_page, read_pricing_page, same_site, segments)

log = logging.getLogger(__name__)

HEADERS = {
    # A normal browser user-agent: many sites reject unknown bots outright.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
FEED_PATHS = ["/feed", "/rss.xml", "/blog/feed", "/blog/rss.xml", "/feed.xml", "/atom.xml", "/rss", "/blog/rss"]
SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml"]
LISTING_PATHS = ["/blog", "/resources", "/articles", "/insights", "/news", "/learn"]
PRICING_PATHS = ["/pricing", "/plans"]


@dataclass
class Page:
    url: str
    status: int
    text: str
    content: bytes
    content_type: str


@dataclass
class ScrapeResult:
    domain: str
    base_url: str = ""
    status: str = "ok"                  # ok | no_blog | blocked | unreachable
    message: str = ""
    posts: dict[str, Post] = field(default_factory=dict)
    pricing_url: Optional[str] = None
    sources: list[str] = field(default_factory=list)

    def add(self, post: Post) -> None:
        post.url = normalize_url(post.url)
        if post.url in self.posts:
            self.posts[post.url].merge(post)
        else:
            self.posts[post.url] = post


# ======================================================================== the scraper
class Scraper:
    def __init__(self, transport: httpx.BaseTransport | None = None, timeout: float = 12):
        # `transport` lets the tests plug in fake websites
        self.client = httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True, transport=transport)
        self.blocked = 0              # how many requests the site rejected as a bot
        self.blocked_in_a_row = 0
        self.robots_skipped = 0
        self._robots: dict[str, Optional[robotparser.RobotFileParser]] = {}

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------ HTTP
    def get(self, url: str, check_robots: bool = True) -> Optional[Page]:
        """GET a URL. Returns None for any failure (never raises)."""
        if self.blocked_in_a_row >= 5:
            return None                # the site keeps blocking us - stop hammering it
        if check_robots and not self._allowed(url):
            self.robots_skipped += 1
            return None
        for attempt in range(2):
            try:
                r = self.client.get(url)
            except httpx.HTTPError:
                if attempt == 0:
                    continue
                return None
            if looks_blocked(r.status_code, r.text):
                self.blocked += 1
                self.blocked_in_a_row += 1
                return None
            if r.status_code >= 500 and attempt == 0:
                continue
            if r.status_code >= 400:
                return None
            self.blocked_in_a_row = 0
            return Page(str(r.url), r.status_code, r.text, r.content, r.headers.get("content-type", "").lower())
        return None

    def _allowed(self, url: str) -> bool:
        """Respect robots.txt."""
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            rp = None
            page = self.get(origin + "/robots.txt", check_robots=False)
            if page and "<html" not in page.text[:300].lower():
                rp = robotparser.RobotFileParser()
                rp.parse(page.text.splitlines())
            self._robots[origin] = rp
        rp = self._robots[origin]
        return rp is None or rp.can_fetch("*", url)

    def _sitemaps_in_robots(self, base: str) -> list[str]:
        self._allowed(base + "/")      # makes sure robots.txt is loaded
        rp = self._robots.get(base)
        return list(rp.site_maps() or []) if rp else []

    # ------------------------------------------------------------------ 1. find posts
    def find_posts(self, domain: str) -> ScrapeResult:
        res = ScrapeResult(domain=domain)

        home = None
        for url in (f"https://{domain}/", f"https://www.{domain}/" if not domain.startswith("www.") else None):
            if url and (home := self.get(url, check_robots=False)):
                break
        if home:
            p = urlparse(home.url)
            res.base_url = f"{p.scheme}://{p.netloc}"
        elif self.blocked:
            res.base_url = f"https://{domain}"     # homepage blocked; still try the feed and sitemap
        else:
            res.status = "unreachable"
            res.message = f"Couldn't reach {domain}. Check the spelling, or the site may be down."
            return res

        listing_links, feed_links = [], []
        if home:
            listing_links, feed_links = _safe(self._read_homepage, res, home) or ([], [])
        # Each source is independent: if one breaks on an unusual page, the others still run
        _safe(self._read_feeds, res, feed_links, listing_links)
        _safe(self._read_sitemaps, res)
        _safe(self._read_listing_pages, res, listing_links)

        if res.posts:
            return res
        if self.blocked:
            res.status = "blocked"
            res.message = (f"{domain} blocks automated visitors (bot protection), and its RSS feed and sitemap "
                           f"weren't reachable either, so no posts could be collected.")
        elif self.robots_skipped:
            res.status = "blocked"
            res.message = f"{domain}'s robots.txt doesn't allow crawling its blog pages, so we respected that."
        else:
            res.status = "no_blog"
            res.message = (f"No blog found on {domain}. We checked homepage links, RSS feeds, the sitemap and "
                           f"common blog paths ({', '.join(LISTING_PATHS)}).")
        return res

    def _read_homepage(self, res: ScrapeResult, home: Page) -> tuple[list[str], list[str]]:
        soup = BeautifulSoup(home.text, "lxml")
        feeds = [urljoin(home.url, l["href"]) for l in soup.find_all("link", type=re.compile(r"(rss|atom)\+xml"))
                 if l.get("href") and "comments" not in l["href"]]
        listings = []
        for a in soup.find_all("a", href=True):
            url = urljoin(home.url, a["href"]).split("#")[0]
            if not same_site(url, res.domain):
                continue
            text = a.get_text(" ", strip=True)
            segs = segments(url)
            # Nav links such as "Blog" or "Resources" (incl. a blog on another host like blog.site.com)
            is_blog_path = len(segs) == 1 and segs[0] in BLOG_WORDS
            is_blog_text = re.fullmatch(r"(the )?(blog|resources|insights|articles|news|learn)", text, re.I)
            other_host = urlparse(url).hostname != urlparse(home.url).hostname
            if is_blog_path or (is_blog_text and len(segs) <= 1 and (segs or other_host)):
                listings.append(url)
            if not res.pricing_url and len(segs) == 1 and (segs[0] in ("pricing", "plans")
                                                           or re.fullmatch(r"pricing|plans", text, re.I)):
                res.pricing_url = url
            # "Latest from our blog" links right on the homepage
            if is_post_url(url, res.domain):
                res.add(Post(url=url, title=clean(text, 300) if len(text) > 15 else None))
        return list(dict.fromkeys(listings))[:4], feeds

    def _read_feeds(self, res: ScrapeResult, feed_links: list[str], listing_links: list[str]) -> None:
        # Guess feed URLs on the main site and on any separate blog host (blog.site.com)
        bases = [res.base_url] + [f"{urlparse(u).scheme}://{urlparse(u).netloc}" for u in listing_links]
        guesses = [b + path for b in dict.fromkeys(bases) for path in FEED_PATHS]
        for url in list(dict.fromkeys(feed_links + guesses)):
            page = self.get(url)
            if not page or ("html" in page.content_type and not re.search(r"<(rss|feed)\b", page.text[:1000])):
                continue
            feed = feedparser.parse(page.content)
            found = 0
            for e in feed.entries:
                link = e.get("link")
                if not link or not same_site(link, res.domain):
                    continue
                summary = e.get("summary", "")
                if "<" in summary:
                    summary = BeautifulSoup(summary, "lxml").get_text(" ", strip=True)
                published = parse_date(e.get("published") or e.get("updated"))
                res.add(Post(url=link, title=clean(e.get("title"), 300), published_at=published,
                             description=clean(summary, 300), date_is_exact=published is not None))
                found += 1
            if found:
                res.sources.append(f"RSS feed: {url} ({found} posts)")
                return              # one working feed is enough

    def _read_sitemaps(self, res: ScrapeResult) -> None:
        queue = self._sitemaps_in_robots(res.base_url) + [res.base_url + p for p in SITEMAP_PATHS]
        seen, total = set(), 0
        while queue and len(seen) < 15 and total < 5000:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            page = self.get(url)
            root = _parse_xml(page.content) if page else None
            if root is None:
                continue
            kind = etree.QName(root).localname
            if kind == "sitemapindex":
                children = [_child_text(s, "loc") for s in root if etree.QName(s).localname == "sitemap"]
                children = [c for c in children if c]
                # Prefer sitemaps that hold posts (post-sitemap.xml, blog-sitemap.xml ...)
                wanted = [c for c in children if re.search(r"post|blog|article|resource|news|insight", c, re.I)]
                queue = (wanted or children[:5])[:6] + queue
            elif kind == "urlset":
                post_sitemap = re.search(r"post|blog|article|news", url, re.I) is not None
                found = 0
                for u in root:
                    loc = _child_text(u, "loc")
                    if loc and is_post_url(loc, res.domain, loose=post_sitemap):
                        # <lastmod> is the last-modified date - only a stand-in until we read the post itself
                        res.add(Post(url=loc, published_at=parse_date(_child_text(u, "lastmod"))))
                        found += 1
                total += found
                if found:
                    res.sources.append(f"Sitemap: {url} ({found} posts)")

    def _read_listing_pages(self, res: ScrapeResult, listing_links: list[str]) -> None:
        targets = listing_links + ([res.base_url + p for p in LISTING_PATHS] if len(res.posts) < 20 else [])
        done = set()
        for url in targets:
            if len(done) >= 3:
                break
            page = self.get(url)
            if not page or "html" not in page.content_type or normalize_url(page.url) in done:
                continue
            found = self._posts_on_listing_page(res, page)
            if found:
                done.add(normalize_url(page.url))
                res.sources.append(f"Listing page: {page.url} ({found} posts)")

    def _posts_on_listing_page(self, res: ScrapeResult, page: Page) -> int:
        soup = BeautifulSoup(page.text, "lxml")
        listing_path = frozenset({urlparse(page.url).path.rstrip("/").lower()})
        found = set()
        for a in soup.find_all("a", href=True):
            if a.find_parent(["nav", "header", "footer"]):
                continue
            url = urljoin(page.url, a["href"]).split("#")[0]
            heading = a.find(["h1", "h2", "h3", "h4"]) or (a.parent if a.parent.name in ("h2", "h3", "h4") else None)
            in_card = heading is not None or a.find_parent("article") is not None
            # Some blogs list posts that live at the site root (site.com/my-post): accept those only from cards
            ok = is_post_url(url, res.domain, listing_path) or (in_card and len(segments(url)) == 1
                                                                 and is_post_url(url, res.domain, loose=True))
            if not ok or normalize_url(url) in found or normalize_url(url) == normalize_url(page.url):
                continue
            found.add(normalize_url(url))
            title = heading.get_text(" ", strip=True) if heading else a.get_text(" ", strip=True)
            date = None
            card = a.find_parent(["article", "li"]) or a.parent
            if card is not None and (t := card.find("time")):
                date = parse_date(t.get("datetime") or t.get_text(strip=True))
            res.add(Post(url=url, title=clean(title, 300) if title and len(title) > 10 else None, published_at=date))
        return len(found)

    # ------------------------------------------------------------------ 2. post details
    def fill_details(self, posts: list[Post]) -> None:
        """Open each post page and read its title, publish date and meta description (8 at a time)."""
        def one(post: Post) -> None:
            page = self.get(post.url)
            if page:
                found = _safe(read_post_page, page.text, post.url)
                if found is None:
                    return
                post.title = found.title or post.title                 # the page's own title wins
                post.description = found.description or post.description
                if found.published_at:
                    post.published_at, post.date_is_exact = found.published_at, True
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(one, posts))

    # ------------------------------------------------------------------ 3. pricing
    def get_pricing(self, res: ScrapeResult) -> tuple[Optional[str], dict]:
        """Returns (pricing page url, {plan name: [prices]}). Empty dict if nothing found."""
        if not res.base_url:
            return None, {}
        for url in dict.fromkeys([res.pricing_url] + [res.base_url + p for p in PRICING_PATHS]):
            page = self.get(url) if url else None
            if page and "html" in page.content_type:
                plans = _safe(read_pricing_page, page.text) or {}
                if plans:
                    return page.url, plans
        return None, {}


def _safe(fn, *args):
    """Run one scraping step; log and skip it if an unusual page makes it fail."""
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - one odd page must never break the whole check
        log.exception("Scraping step %s failed", fn.__name__)
        return None

