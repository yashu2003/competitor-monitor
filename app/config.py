"""Settings, read from environment variables (or a local .env file)."""
import os

from dotenv import load_dotenv

load_dotenv()


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip() or "sqlite:///data.db"
    # Supabase gives "postgresql://..." -> tell SQLAlchemy to use the psycopg (v3) driver
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def slack_webhook_url() -> str:
    return os.getenv("SLACK_WEBHOOK_URL", "").strip()


def dataforseo_credentials() -> tuple[str, str]:
    return os.getenv("DATAFORSEO_LOGIN", "").strip(), os.getenv("DATAFORSEO_PASSWORD", "").strip()


def dataforseo_location() -> int:
    # 2840 = United States, 2356 = India
    return int(os.getenv("DATAFORSEO_LOCATION_CODE", "2840"))
