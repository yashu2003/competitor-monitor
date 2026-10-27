# Competitor Monitoring Alert Tool

Enter a competitor's domain. The tool:

1. **Scrapes the site** with its own scraper (no scraping API): the homepage, the blog/resource listing pages, the RSS feed, the sitemap, and the recent post pages.
2. **Detects new content**: it extracts each post's URL, title, publish date and meta description, and stores them in a database, so every later check knows what's new. It also **tracks pricing changes** on the pricing page.
3. **Enriches each new post** with its target keyword and monthly search volume (DataForSEO).
4. **Sends a Slack alert**, for example: *"Ahrefs published 3 new posts this week"*, with titles, URLs and keyword data.

**Live:** `<Render URL>` · **Demo:** `<Loom link>`

---

## What happens in the cases you'll test

| Test | Result |
|---|---|
| Domain with an active blog | Posts are collected from 4 sources and merged: homepage links, RSS feed, sitemap, listing pages (`/blog`, `/resources`, …). This covers WordPress, headless/JS sites with only a listing page, blogs on `blog.site.com`, and posts living at the site root. |
| Run twice on the same domain | **First check:** all posts are saved, and the alert covers posts published in the last 7 days. **Second check:** only URLs not already in the database are flagged. If nothing is new, the page says *"published 0 new posts"* and no alert is sent. |
| Site has no blog | *"No blog found on x.com"*, listing what was checked. |
| Site blocks bots | It sends browser-like headers and respects robots.txt. If the homepage is blocked, it still tries the RSS feed and sitemap, which are often not blocked. If everything is blocked: *"x.com blocked the scraper"*. It stops after 5 blocked requests in a row instead of hammering the site. |
| Invalid API key | The check still finishes and the alert is still sent. Search volume shows *n/a*, and the page and the alert both say *"DataForSEO API key is invalid"*. A missing key and an invalid Slack webhook are reported the same way. |

None of these cases crash the app. If something unexpected fails, the page shows a clear message.

## How it works

```
domain ──► scraper.py   find posts: homepage → RSS feed → sitemap → listing pages (merged, de-duplicated)
       ──► monitor.py   compare with the database → which posts are new?
       ──► scraper.py   open the new post pages → title, publish date, meta description
       ──► keywords.py  target keyword (from slug + title + meta description) → DataForSEO search volume
       ──► scraper.py   pricing page → {plan: price} → compare with the last saved pricing
       ──► storage.py   save new posts + pricing
       ──► alert.py     Slack message
```

| File | What it does |
|---|---|
| `app/main.py` | The web page (FastAPI): a form for the domain and the result |
| `app/monitor.py` | Runs one check, step by step (above) |
| `app/scraper.py` | Downloads pages and finds posts in the 4 sources |
| `app/parsing.py` | Reads pages: is this URL a post? publish date? meta description? prices? |
| `app/keywords.py` | Target keyword + DataForSEO search volume |
| `app/alert.py` | Builds and sends the Slack message |
| `app/storage.py` | Database tables: `posts`, `checks`, `pricing` |

**Design decisions**
- **Why 4 sources?** No single source works on every site. Some blogs have no RSS feed, some have no sitemap, and some block their HTML pages but not their feed.
- **Publish dates:** dates are taken from the post itself (JSON-LD `datePublished` → `article:published_time` → `<time>`) or from the RSS feed. A sitemap `<lastmod>` is only the *last edited* date, so an old post that was edited yesterday is never announced as new.
- **Target keyword:** posts are optimized for one phrase, which SEOs put in the URL slug, the title and the meta description. The tool picks the title phrase that best matches the slug and description. Brand-new posts don't rank yet, so "ranked keywords" APIs return nothing for them.
- **Storage:** Postgres (Supabase) in production, so the history survives restarts. A local SQLite file is used for development.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                 # add your Slack webhook + DataForSEO login
uvicorn app.main:app --reload        # open http://127.0.0.1:8000
pytest                               # 22 tests, run offline against fake websites
```

## Deploy (free)

1. **Supabase:** create a project → **Connect** → copy the **Session pooler** connection string.
2. **Slack:** go to api.slack.com/apps → Create app → Incoming Webhooks → add to a channel → copy the URL.
3. **DataForSEO:** sign up (free trial credit) → API Dashboard → copy the API login and password.
4. **Render:** New → Blueprint → choose this repo, then fill in `DATABASE_URL`, `SLACK_WEBHOOK_URL`, `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD`.
