"""One check of one competitor: scrape -> find what's new -> enrich -> pricing -> alert -> save.

What counts as "new":
  * First check of a domain: everything is saved as the starting point, and the alert covers
    posts published in the last 7 days ("published 3 new posts this week").
  * Every later check: only URLs that aren't in the database yet.
    (If a URL is new to us but was published months ago - e.g. it only now appeared in the sitemap -
    it's saved quietly instead of being announced as new.)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app import storage
from app.alert import build_message, send_slack
from app.keywords import search_volumes, target_keyword
from app.parsing import Post, normalize_domain
from app.scraper import Scraper

log = logging.getLogger(__name__)

MAX_PAGES_TO_OPEN = 30        # post pages opened per check (titles, dates, meta descriptions)
OLD_POST_DAYS = 30


@dataclass
class CheckResult:
    domain: str
    status: str                                   # ok | no_blog | blocked | unreachable | invalid | error
    headline: str
    message: str = ""
    first_check: bool = False
    new_posts: list[dict] = field(default_factory=list)
    total_tracked: int = 0
    pricing_url: Optional[str] = None
    pricing_status: str = ""
    pricing_changes: list[str] = field(default_factory=list)
    alert_status: str = ""
    notes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def company_name(domain: str) -> str:
    return domain.removeprefix("www.").split(".")[0].capitalize()


def run_check(raw_domain: str, transport: httpx.BaseTransport | None = None) -> CheckResult:
    """`transport` is only used by the tests (fake websites and APIs)."""
    try:
        domain = normalize_domain(raw_domain)
    except ValueError as e:
        return CheckResult(domain=raw_domain, status="invalid", headline=str(e))
    if domain.startswith("www."):
        domain = domain[4:]                       # ahrefs.com and www.ahrefs.com are the same competitor
    name = company_name(domain)

    scraper = Scraper(transport=transport)
    try:
        return _check(domain, name, scraper, transport)
    except Exception:  # noqa: BLE001 - show a clear message instead of a server error page
        log.exception("Check of %s failed", domain)
        return CheckResult(domain=domain, status="error", headline=f"Something went wrong while checking {domain}",
                           message="Please try again. If it keeps happening, the details are in the server log.")
    finally:
        scraper.close()


def _check(domain: str, name: str, scraper: Scraper, transport) -> CheckResult:
    known = storage.known_urls(domain)
    first_check = not known
    previous_check = storage.last_successful_check(domain)
    scraped = scraper.find_posts(domain)
    result = CheckResult(domain=domain, status=scraped.status, headline="", message=scraped.message,
                         first_check=first_check, sources=scraped.sources)
    if scraper.blocked and scraped.posts:
        result.notes.append(f"{scraper.blocked} page(s) were blocked by bot protection; "
                            "posts were collected from the sources that weren't")

    # ---- 1. Which posts are new?
    new = [p for url, p in scraped.posts.items() if url not in known]
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    new.sort(key=lambda p: p.published_at or oldest, reverse=True)

    # ---- 2. Open the newest post pages for exact title, publish date and meta description
    opened = new[:MAX_PAGES_TO_OPEN]
    scraper.fill_details(opened)

    # Only announce posts whose date we trust (from the post itself or the RSS feed). A sitemap <lastmod>
    # is just "last edited", so an old post that was updated yesterday isn't announced as new.
    now = datetime.now(timezone.utc)

    def age(p: Post) -> timedelta:
        return now - p.published_at

    if first_check:
        to_announce = [p for p in new if p.date_is_exact and age(p) <= timedelta(days=7)]
    else:
        to_announce = [p for p in new if (p.date_is_exact and age(p) <= timedelta(days=OLD_POST_DAYS))
                       or (p in opened and not p.date_is_exact)]   # opened, but the page shows no date
    to_announce = to_announce[:MAX_PAGES_TO_OPEN]

    # ---- 3. Enrich the posts we announce: target keyword + search volume
    rows = {p.url: _row(p) for p in new}
    for p in to_announce:
        rows[p.url]["keyword"] = target_keyword(p.url, p.title, p.description)
    volumes, volume_error = search_volumes([rows[p.url]["keyword"] for p in to_announce], transport=transport)
    for p in to_announce:
        rows[p.url]["search_volume"] = volumes.get(rows[p.url]["keyword"])
    if volume_error and to_announce:
        result.notes.append(volume_error)
    result.new_posts = [rows[p.url] for p in to_announce]

    # ---- 4. Pricing page
    if scraped.base_url and scraped.status != "unreachable":
        _check_pricing(domain, scraper, scraped, result)

    # ---- 5. Save, then alert
    storage.save_new_posts(domain, list(rows.values()))
    result.total_tracked = storage.count_posts(domain)
    period = _period(first_check, previous_check, now)
    count = len(result.new_posts)
    if scraped.status == "ok":
        result.headline = f"{name} published {count} new post{'' if count == 1 else 's'} {period}"
    else:
        result.headline = {"no_blog": f"No blog found on {domain}", "blocked": f"{domain} blocked the scraper",
                           "unreachable": f"Couldn't reach {domain}"}[scraped.status]

    if result.new_posts or result.pricing_changes:
        _sent, result.alert_status = send_slack(
            build_message(result.headline if result.new_posts else f"{name} changed its pricing",
                          result.new_posts, result.pricing_changes, result.notes), transport=transport)
        if not _sent:
            result.notes.append(result.alert_status)
    elif scraped.status == "ok":
        result.alert_status = "Nothing new, so no alert was sent"

    storage.record_check(domain, scraped.status, count)
    return result


def _row(p: Post) -> dict:
    return {"url": p.url, "title": p.title or _title_from_url(p.url), "published_at": p.published_at,
            "description": p.description, "keyword": None, "search_volume": None}


def _title_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1].replace("-", " ").capitalize()


def _period(first_check: bool, previous: Optional[datetime], now: datetime) -> str:
    if first_check or previous is None or now - previous <= timedelta(days=7):
        return "this week"
    return f"since {previous.strftime('%d %b')}"


def _check_pricing(domain: str, scraper: Scraper, scraped, result: CheckResult) -> None:
    url, plans = scraper.get_pricing(scraped)
    result.pricing_url = url
    if not plans:
        result.pricing_status = "No pricing page with prices found"
        return
    old = storage.last_pricing(domain)
    if old is None:
        result.pricing_status = f"Saved current pricing ({len(plans)} plans); changes will be flagged next time"
        storage.save_pricing(domain, url, plans)
        return
    for plan, prices in plans.items():
        if plan not in old:
            result.pricing_changes.append(f"New plan {plan}: {', '.join(prices)}")
        elif sorted(old[plan]) != prices:
            result.pricing_changes.append(f"{plan}: {', '.join(old[plan])} → {', '.join(prices)}")
    for plan, prices in old.items():
        if plan not in plans:
            result.pricing_changes.append(f"Plan removed: {plan} (was {', '.join(prices)})")
    if result.pricing_changes:
        result.pricing_status = f"{len(result.pricing_changes)} pricing change(s)"
        storage.save_pricing(domain, url, plans)
    else:
        result.pricing_status = "No pricing changes"
