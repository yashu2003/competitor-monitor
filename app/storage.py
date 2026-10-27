"""Database: remembers every post we've seen, so the next check knows what's new.

Three tables:
  posts    - one row per competitor post (domain + url is unique)
  checks   - one row per check (when, result)
  pricing  - pricing snapshots (a new row only when prices change)
SQLite locally, Postgres (Supabase) in production - same code.
"""
from datetime import datetime, timezone

from sqlalchemy import (JSON, Column, DateTime, Integer, MetaData, String, Table, Text, UniqueConstraint,
                        create_engine, func, insert, select)

from app import config

metadata = MetaData()

posts = Table(
    "posts", metadata,
    Column("id", Integer, primary_key=True),
    Column("domain", String(255), nullable=False, index=True),
    Column("url", String(1000), nullable=False),
    Column("title", String(500)),
    Column("published_at", DateTime(timezone=True)),
    Column("description", Text),
    Column("keyword", String(200)),
    Column("search_volume", Integer),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("domain", "url", name="uq_posts_domain_url"),
)

checks = Table(
    "checks", metadata,
    Column("id", Integer, primary_key=True),
    Column("domain", String(255), nullable=False, index=True),
    Column("checked_at", DateTime(timezone=True), nullable=False),
    Column("status", String(30), nullable=False),
    Column("new_posts", Integer, nullable=False, default=0),
)

pricing = Table(
    "pricing", metadata,
    Column("id", Integer, primary_key=True),
    Column("domain", String(255), nullable=False, index=True),
    Column("url", String(1000)),
    Column("plans", JSON, nullable=False),
    Column("checked_at", DateTime(timezone=True), nullable=False),
)

_engine = None
_engine_url = None


def engine():
    """Create the engine once (re-created if DATABASE_URL changes, e.g. in tests)."""
    global _engine, _engine_url
    url = config.database_url()
    if _engine is None or url != _engine_url:
        _engine = create_engine(url, pool_pre_ping=True)
        _engine_url = url
        metadata.create_all(_engine)
    return _engine


def now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt):
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------- posts
def known_urls(domain: str) -> set[str]:
    with engine().connect() as conn:
        return set(conn.scalars(select(posts.c.url).where(posts.c.domain == domain)))


def save_new_posts(domain: str, rows: list[dict]) -> None:
    """Insert posts we haven't stored before. Rows already stored are skipped."""
    if not rows:
        return
    existing = known_urls(domain)
    fresh = [r for r in rows if r["url"] not in existing]
    if not fresh:
        return
    stamp = now()
    with engine().begin() as conn:
        conn.execute(insert(posts), [{
            "domain": domain, "url": r["url"], "title": (r.get("title") or "")[:500] or None,
            "published_at": r.get("published_at"), "description": r.get("description"),
            "keyword": r.get("keyword"), "search_volume": r.get("search_volume"), "first_seen_at": stamp,
        } for r in fresh])


def count_posts(domain: str) -> int:
    with engine().connect() as conn:
        return conn.scalar(select(func.count()).select_from(posts).where(posts.c.domain == domain))


def recent_posts(domain: str, limit: int = 50) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(select(posts).where(posts.c.domain == domain)
                            .order_by(posts.c.published_at.desc().nulls_last(), posts.c.id.desc())
                            .limit(limit)).mappings().all()
    return [dict(r) | {"published_at": as_utc(r["published_at"])} for r in rows]


# ---------------------------------------------------------------- checks
def last_successful_check(domain: str):
    with engine().connect() as conn:
        return as_utc(conn.scalar(select(func.max(checks.c.checked_at))
                                  .where(checks.c.domain == domain, checks.c.status == "ok")))


def record_check(domain: str, status: str, new_posts: int) -> None:
    with engine().begin() as conn:
        conn.execute(insert(checks).values(domain=domain, checked_at=now(), status=status, new_posts=new_posts))


# ---------------------------------------------------------------- pricing
def last_pricing(domain: str):
    with engine().connect() as conn:
        row = conn.execute(select(pricing.c.plans).where(pricing.c.domain == domain)
                           .order_by(pricing.c.id.desc()).limit(1)).first()
    return dict(row.plans) if row else None


def save_pricing(domain: str, url: str, plans: dict) -> None:
    with engine().begin() as conn:
        conn.execute(insert(pricing).values(domain=domain, url=url, plans=plans, checked_at=now()))
