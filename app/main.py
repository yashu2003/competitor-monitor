"""Web app: one page. Enter a competitor domain -> run a check -> show the result."""
import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import storage
from app.monitor import run_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="Competitor Monitoring Alert Tool")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _date(value) -> str:
    return value.strftime("%d %b %Y") if value else "—"


templates.env.filters["date"] = _date


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {"result": None, "domain": ""})


@app.post("/", response_class=HTMLResponse)
async def check(request: Request, domain: str = Form(...)):
    # Scraping takes a few seconds; run it in a worker thread so the server stays responsive
    result = await run_in_threadpool(run_check, domain)
    tracked = []
    if result.status not in ("invalid", "error"):
        try:
            tracked = storage.recent_posts(result.domain, limit=100)
        except Exception:  # noqa: BLE001 - the list is extra info; never fail the page over it
            logging.exception("Couldn't load tracked posts")
    return templates.TemplateResponse(request, "index.html", {"result": result, "domain": result.domain,
                                                              "tracked": tracked})


@app.get("/health")
def health():
    return {"ok": True}
