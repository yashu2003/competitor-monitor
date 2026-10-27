"""Fake competitor websites + fake APIs, served through httpx.MockTransport.

Lets the whole pipeline (scrape -> diff -> enrich -> alert) run offline and deterministically:
  wp.test        WordPress-style: robots.txt -> sitemap index -> post sitemap, RSS feed, JSON-LD posts, pricing
  nextjs.test    No RSS, no sitemap: posts only discoverable from the /blog listing page (cards + <time>)
  noblog.test    Marketing site with no blog at all
  blocked.test   Cloudflare challenge (403) on every URL
  half.test      Homepage blocked, but the RSS feed is public
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

NOW = datetime.now(timezone.utc)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def rfc822(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")


CF_PAGE = ("<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
           "<div id='cf-chl-widget'>Checking your browser before accessing</div>"
           "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'></script></body></html>")


class MockWeb:
    def __init__(self):
        self.wp_posts = [
            ("how-to-do-keyword-research", "How to Do Keyword Research for SEO (2026 Guide)", 2,
             "A step-by-step keyword research process to find keywords your customers search for."),
            ("link-building-strategies", "17 Link Building Strategies That Still Work", 9,
             "Proven link building strategies to earn high-quality backlinks."),
            ("what-is-geo", "What Is Generative Engine Optimization (GEO)?", 20,
             "Generative engine optimization explained: how to get cited by AI search engines."),
            ("technical-seo-checklist", "The Technical SEO Checklist", 45,
             "A complete technical SEO checklist for crawlability and indexing."),
        ]
        self.wp_prices = {"Starter": "$29", "Pro": "$99", "Agency": "$299"}
        self.next_posts = [
            ("ai-content-workflows", "AI Content Workflows for Lean Teams", 1),
            ("email-marketing-benchmarks", "Email Marketing Benchmarks by Industry", 5),
            ("b2b-saas-content-strategy", "B2B SaaS Content Strategy Playbook", 12),
        ]
        self.slack_messages: list[dict] = []
        self.dataforseo_valid = True
        self.slack_valid = True
        self.requests: list[str] = []

    # ------------------------------------------------------------------ mutations used by tests
    def publish_wp(self, slug: str, title: str, days_ago: float = 0.1, desc: str = "Fresh post."):
        self.wp_posts.insert(0, (slug, title, days_ago, desc))

    # ------------------------------------------------------------------ sites
    def _wp(self, path: str) -> httpx.Response:
        base = "https://wp.test"
        if path == "/":
            links = "".join(f"<a href='/blog/{s}/'>{t}</a>" for s, t, *_ in self.wp_posts[:2])
            return _html(f"<html><head><title>WP Co</title>"
                         f"<link rel='alternate' type='application/rss+xml' href='{base}/feed/'></head><body>"
                         f"<nav><a href='/'>Home</a><a href='/blog/'>Blog</a><a href='/pricing/'>Pricing</a>"
                         f"<a href='/about/'>About</a></nav><section>{links}</section></body></html>")
        if path == "/robots.txt":
            return httpx.Response(200, text=f"User-agent: *\nDisallow: /wp-admin/\nSitemap: {base}/sitemap_index.xml\n")
        if path == "/sitemap_index.xml":
            return _xml("<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                        f"<sitemap><loc>{base}/post-sitemap.xml</loc><lastmod>{iso(1)}</lastmod></sitemap>"
                        f"<sitemap><loc>{base}/page-sitemap.xml</loc></sitemap>"
                        f"<sitemap><loc>{base}/category-sitemap.xml</loc></sitemap></sitemapindex>")
        if path == "/post-sitemap.xml":
            urls = "".join(f"<url><loc>{base}/blog/{s}/</loc><lastmod>{iso(d)}</lastmod></url>"
                           for s, _t, d, _ in self.wp_posts)
            return _xml(f"<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{urls}</urlset>")
        if path == "/page-sitemap.xml":
            return _xml("<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                        f"<url><loc>{base}/about/</loc></url><url><loc>{base}/pricing/</loc></url></urlset>")
        if path in ("/feed/", "/feed"):
            items = "".join(f"<item><title>{t}</title><link>{base}/blog/{s}/</link><pubDate>{rfc822(d)}</pubDate>"
                            f"<description>{desc}</description></item>" for s, t, d, desc in self.wp_posts[:10])
            return httpx.Response(200, text=f"<?xml version='1.0'?><rss version='2.0'><channel><title>WP Co</title>{items}</channel></rss>",
                                  headers={"content-type": "application/rss+xml"})
        if path in ("/blog/", "/blog"):
            cards = "".join(f"<article><a href='/blog/{s}/'><h2>{t}</h2></a></article>" for s, t, *_ in self.wp_posts)
            return _html(f"<html><body><h1>Blog</h1>{cards}<a href='/blog/category/seo/'>SEO</a></body></html>")
        if path in ("/pricing/", "/pricing"):
            cards = "".join(f"<div class='plan'><h3>{n}</h3><div class='price'><span>{p}</span><span>/month</span></div></div>"
                            for n, p in self.wp_prices.items())
            return _html(f"<html><body><h1>Pricing</h1>{cards}</body></html>")
        for slug, title, days, desc in self.wp_posts:
            if path.rstrip("/") == f"/blog/{slug}":
                ld = {"@context": "https://schema.org", "@graph": [
                    {"@type": "WebPage", "name": title},
                    {"@type": "BlogPosting", "headline": title, "datePublished": iso(days), "author": {"name": "Sam"}}]}
                return _html(f"<html><head><title>{title} | WP Co</title><meta name='description' content='{desc}'>"
                             f"<script type='application/ld+json'>{json.dumps(ld)}</script></head><body><article>"
                             f"<h1>{title}</h1><h2>Why it matters</h2><p>{'word ' * 600}</p></article></body></html>")
        return httpx.Response(404, text="<html><body>Not found</body></html>", headers={"content-type": "text/html"})

    def _next(self, path: str) -> httpx.Response:
        if path == "/":
            return _html("<html><body><header><a href='/'>Logo</a><a href='/product'>Product</a>"
                         "<a href='/blog'>Resources</a></header><h1>Next Co</h1></body></html>")
        if path == "/blog":
            cards = "".join(
                f"<div class='card'><a href='/blog/{s}'><img src='/x.png'><h3>{t}</h3></a>"
                f"<time datetime='{iso(d)[:10]}'>{iso(d)[:10]}</time></div>" for s, t, d in self.next_posts)
            return _html(f"<html><body><main><h1>Blog</h1>{cards}<a href='/blog/tag/ai'>AI</a></main></body></html>")
        for slug, title, days in self.next_posts:
            if path == f"/blog/{slug}":
                return _html(f"<html><head><meta property='og:type' content='article'><meta property='og:title' content='{title}'>"
                             f"<meta property='article:published_time' content='{iso(days)}'>"
                             f"<meta name='description' content='About {title.lower()}.'></head>"
                             f"<body><main><h1>{title}</h1><p>{'text ' * 400}</p></main></body></html>")
        return httpx.Response(404, text="Not found")

    def _noblog(self, path: str) -> httpx.Response:
        if path == "/":
            return _html("<html><body><nav><a href='/features'>Features</a><a href='/contact'>Contact</a></nav>"
                         "<h1>We sell widgets</h1></body></html>")
        return httpx.Response(404, text="Not found", headers={"content-type": "text/html"})

    def _half(self, path: str) -> httpx.Response:
        if path == "/rss.xml":
            items = "".join(f"<item><title>Half post {i}</title><link>https://half.test/insights/half-post-number-{i}</link>"
                            f"<pubDate>{rfc822(i)}</pubDate></item>" for i in range(1, 4))
            return httpx.Response(200, text=f"<rss version='2.0'><channel>{items}</channel></rss>",
                                  headers={"content-type": "application/xml"})
        return httpx.Response(403, text=CF_PAGE, headers={"content-type": "text/html", "server": "cloudflare"})

    def _rootblog(self, path: str) -> httpx.Response:
        """Backlinko-style: posts live at the root (/my-post) and are listed on /blog."""
        posts = [("seo-content-writing-tips", "SEO Content Writing Tips", 3), ("google-ranking-factors", "Google's 200 Ranking Factors", 8)]
        if path == "/":
            return _html("<html><body><nav><a href='/blog'>Blog</a><a href='/hub'>Hub</a></nav><h1>Root Co</h1></body></html>")
        if path == "/blog":
            cards = "".join(f"<article><a href='/{s}'><h2>{t}</h2></a><time datetime='{iso(d)[:10]}'></time></article>"
                            for s, t, d in posts)
            return _html(f"<html><body><nav><a href='/about-our-company-team'>About</a></nav><main>{cards}</main>"
                         "<footer><a href='/privacy-policy-and-terms'>Privacy</a></footer></body></html>")
        for s, t, d in posts:
            if path == f"/{s}":
                return _html(f"<html><head><meta property='og:type' content='article'><title>{t}</title></head>"
                             f"<body><article><h1>{t}</h1><p>{'w ' * 500}</p></article></body></html>")
        return httpx.Response(404, text="nf", headers={"content-type": "text/html"})

    def _sub(self, host: str, path: str) -> httpx.Response:
        """Main site links to a blog on a subdomain that has its own RSS feed."""
        if host == "sub.test":
            if path == "/":
                return _html("<html><body><nav><a href='https://blog.sub.test/'>Blog</a></nav></body></html>")
            return httpx.Response(404, text="nf", headers={"content-type": "text/html"})
        if path == "/":
            return _html("<html><body><h1>Sub blog</h1><p>Loading…</p></body></html>")   # JS-rendered shell
        if path == "/rss.xml":
            items = "".join(f"<item><title>Sub post {i}</title><link>https://blog.sub.test/sub-post-number-{i}</link>"
                            f"<pubDate>{rfc822(i)}</pubDate></item>" for i in range(1, 5))
            return httpx.Response(200, text=f"<rss version='2.0'><channel>{items}</channel></rss>",
                                  headers={"content-type": "application/rss+xml"})
        return httpx.Response(404, text="nf", headers={"content-type": "text/html"})

    # ------------------------------------------------------------------ APIs
    def _dataforseo(self, request: httpx.Request) -> httpx.Response:
        if not self.dataforseo_valid:
            return httpx.Response(401, json={"status_code": 40100, "status_message": "You are not authorized."})
        body = json.loads(request.content)
        kws = body[0]["keywords"]
        result = [{"keyword": k, "search_volume": 1000 + 10 * len(k), "cpc": 2.5, "competition": "MEDIUM"} for k in kws]
        return httpx.Response(200, json={"status_code": 20000, "tasks": [{"status_code": 20000, "result": result}]})

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path or "/"
        self.requests.append(f"{host}{path}")
        if host in ("wp.test", "www.wp.test"):
            return self._wp(path)
        if host == "nextjs.test":
            return self._next(path)
        if host == "noblog.test":
            return self._noblog(path)
        if host == "blocked.test":
            return httpx.Response(403, text=CF_PAGE, headers={"content-type": "text/html"})
        if host == "half.test":
            return self._half(path)
        if host == "rootblog.test":
            return self._rootblog(path)
        if host in ("sub.test", "blog.sub.test"):
            return self._sub(host, path)
        if host == "api.dataforseo.com":
            return self._dataforseo(request)
        if host == "hooks.slack.com":
            if not self.slack_valid:
                return httpx.Response(404, text="no_service")
            self.slack_messages.append(json.loads(request.content))
            return httpx.Response(200, text="ok")
        raise httpx.ConnectError(f"Unknown host {host}", request=request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def _xml(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "application/xml"})
