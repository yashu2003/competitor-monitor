"""Tests for exactly what the assignment says will be tested, run against fake websites (tests/mocksite.py).

  1. Real domains with active blogs -> posts must be found (several common blog setups)
  2. Run twice on the same domain   -> the second run only flags what's new
  3. No blog / blocks bots / invalid API key -> a clear message, no crash
Plus: pricing changes are detected, and post details (title, date, meta description) are extracted.
"""
import pytest

from app import storage
from app.keywords import target_keyword
from app.monitor import run_check
from app.parsing import is_post_url, normalize_url, read_post_page
from tests.mocksite import MockWeb


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T0/B0/X")
    monkeypatch.setenv("DATAFORSEO_LOGIN", "me@example.com")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "secret")
    return MockWeb()


def check(web, domain):
    return run_check(domain, transport=web.transport)


# ------------------------------------------------------------------ 1. finds posts on real-world blog setups
@pytest.mark.parametrize("domain,expected", [
    ("wp.test", 4),          # WordPress: RSS feed + sitemap index + /blog
    ("nextjs.test", 3),      # no RSS, no sitemap: only the /blog listing page ("Resources" in the nav)
    ("rootblog.test", 2),    # posts live at the site root (site.com/my-post), listed on /blog
    ("sub.test", 4),         # blog on a subdomain (blog.site.com) with its own feed
    ("half.test", 3),        # homepage blocked by Cloudflare, but the RSS feed is public
])
def test_finds_posts(web, domain, expected):
    r = check(web, domain)
    assert r.status == "ok", r.message
    assert r.total_tracked == expected


def test_extracts_title_date_and_meta_description(web):
    check(web, "wp.test")
    post = next(p for p in storage.recent_posts("wp.test") if p["url"].endswith("how-to-do-keyword-research"))
    assert post["title"] == "How to Do Keyword Research for SEO (2026 Guide)"
    assert post["description"].startswith("A step-by-step keyword research process")
    assert post["published_at"] is not None


# ------------------------------------------------------------------ 2. run twice: only what's new
def test_first_check_alerts_this_weeks_posts(web):
    r = check(web, "wp.test")
    assert r.first_check
    assert r.headline == "Wp published 1 new post this week"            # only 1 of 4 posts is < 7 days old
    assert r.new_posts[0]["keyword"] == "keyword research"
    assert r.new_posts[0]["search_volume"] == 1160
    assert len(web.slack_messages) == 1


def test_second_run_flags_only_new_posts(web):
    check(web, "wp.test")
    second = check(web, "wp.test")
    assert second.new_posts == []
    assert second.headline == "Wp published 0 new posts this week"
    assert second.alert_status == "Nothing new, so no alert was sent"
    assert len(web.slack_messages) == 1                                  # nothing repeated

    web.publish_wp("ai-overviews-seo", "How AI Overviews Change SEO", desc="What AI Overviews mean for SEO.")
    third = check(web, "wp.test")
    assert [p["url"] for p in third.new_posts] == ["https://wp.test/blog/ai-overviews-seo"]
    assert third.headline == "Wp published 1 new post this week"
    text = web.slack_messages[-1]["text"]
    assert "How AI Overviews Change SEO" in text and "https://wp.test/blog/ai-overviews-seo" in text
    assert "keyword: `ai overviews`" in text


def test_old_post_seen_for_the_first_time_is_not_announced(web):
    check(web, "wp.test")
    web.publish_wp("old-evergreen-guide", "An Old Evergreen Guide", days_ago=400)
    r = check(web, "wp.test")
    assert r.new_posts == []
    assert r.total_tracked == 5                                          # still saved


def test_www_and_plain_domain_are_the_same_competitor(web):
    check(web, "wp.test")
    assert check(web, "https://www.wp.test/blog").new_posts == []


# ------------------------------------------------------------------ pricing
def test_pricing_change_is_detected_and_alerted(web):
    first = check(web, "wp.test")
    assert first.pricing_status.startswith("Saved current pricing (3 plans)")
    web.wp_prices["Pro"] = "$129"
    r = check(web, "wp.test")
    assert r.pricing_changes == ["Pro: $99 /month → $129 /month"]
    assert "Pricing changes" in web.slack_messages[-1]["text"]


# ------------------------------------------------------------------ 3. failure cases
def test_site_with_no_blog(web):
    r = check(web, "noblog.test")
    assert r.status == "no_blog"
    assert r.headline == "No blog found on noblog.test"
    assert web.slack_messages == []


def test_site_that_blocks_bots(web):
    r = check(web, "blocked.test")
    assert r.status == "blocked"
    assert "bot protection" in r.message


def test_invalid_api_key(web):
    check(web, "wp.test")
    web.dataforseo_valid = False
    web.publish_wp("backlink-audit-guide", "Backlink Audits Explained")
    r = check(web, "wp.test")
    assert r.status == "ok" and len(r.new_posts) == 1                    # the check still works
    assert r.new_posts[0]["keyword"] and r.new_posts[0]["search_volume"] is None
    assert "DataForSEO API key is invalid" in " ".join(r.notes)
    assert "DataForSEO API key is invalid" in web.slack_messages[-1]["text"]   # alert still sent, explained


def test_missing_api_key(web, monkeypatch):
    monkeypatch.delenv("DATAFORSEO_LOGIN")
    r = check(web, "wp.test")
    assert r.new_posts and "API key not set" in " ".join(r.notes)


def test_invalid_slack_webhook(web):
    web.slack_valid = False
    r = check(web, "wp.test")
    assert "Slack rejected the alert" in r.alert_status


def test_unreachable_and_invalid_domains(web):
    assert check(web, "does-not-exist.test").status == "unreachable"
    assert check(web, "not a domain").status == "invalid"


# ------------------------------------------------------------------ small units
def test_post_url_rules():
    assert is_post_url("https://x.com/blog/how-to-write-a-brief", "x.com")
    assert is_post_url("https://blog.x.com/how-to-write-a-brief", "x.com")
    assert is_post_url("https://x.com/2026/05/my-great-post", "x.com")
    assert not is_post_url("https://x.com/blog/category/seo-tips", "x.com")
    assert not is_post_url("https://x.com/blog", "x.com")
    assert not is_post_url("https://other.com/blog/how-to-write-a-brief", "x.com")
    assert normalize_url("http://X.com/blog/post/?utm_source=a#top") == "https://x.com/blog/post"


def test_read_post_page_prefers_structured_data():
    html = """<html><head><title>Ignored | Brand</title><meta name="description" content="Meta desc">
      <script type="application/ld+json">{"@type":"BlogPosting","headline":"Real Title",
      "datePublished":"2026-03-04T10:00:00Z"}</script></head></html>"""
    post = read_post_page(html, "https://x.com/blog/real-title")
    assert (post.title, post.description, post.published_at.month) == ("Real Title", "Meta desc", 3)


def test_target_keyword():
    assert target_keyword("https://x.com/blog/keyword-research", "How to Do Keyword Research in 2026: The Complete Guide",
                          "Learn keyword research step by step.") == "keyword research"


def test_unexpected_error_shows_message_instead_of_crashing(web, monkeypatch):
    import app.monitor as monitor
    monkeypatch.setattr(monitor.storage, "known_urls", lambda d: 1 / 0)
    r = check(web, "wp.test")
    assert r.status == "error" and "Something went wrong" in r.headline


def test_one_broken_source_does_not_stop_the_others(web, monkeypatch):
    import app.scraper as scraper
    monkeypatch.setattr(scraper.Scraper, "_read_sitemaps", lambda self, res: 1 / 0)
    assert check(web, "wp.test").total_tracked == 4        # RSS + listing page still found everything
