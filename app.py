"""
FastAPI service exposing bug details as JSON, e.g.:

    GET /get_bug_details.html?bug_id=9340

Reuses the BugzillaFetcher / load_config logic already in get_bug_details.py
so there's only one place that knows how to talk to Bugzilla.

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8000

Then:
    curl "http://localhost:8000/get_bug_details.html?bug_id=9340"
"""

import logging
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from get_bug_details import BugzillaFetcher, load_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("bug_details_api")

CONFIG_PATH = "config.json"

app = FastAPI(
    title="Bugzilla Bug Details API",
    description="Fetches a bug (and its dependency tree) from Bugzilla as JSON.",
    version="1.0.0",
)


@lru_cache(maxsize=1)
def get_fetcher() -> BugzillaFetcher:
    """
    Build one BugzillaFetcher for the process lifetime. Config (including
    credentials) is loaded once from config.json / env vars and never
    included in any response.
    """
    config = load_config(CONFIG_PATH)
    return BugzillaFetcher(config)


@app.get("/get_bug_details")
def get_bug_details(
    bug_id: str = Query(..., description="Bugzilla bug id to fetch, e.g. 9340"),
):
    fetcher = get_fetcher()
    try:
        details = fetcher.fetch_bug_details(bug_id)
    except ValueError as exc:
        # e.g. "No bug found for bug id 9340"
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to fetch bug %s", bug_id)
        raise HTTPException(
            status_code=502, detail=f"Failed to fetch bug {bug_id}: {exc}"
        ) from exc

    document = fetcher.build_output_document(bug_id, details)
    return JSONResponse(content=document)


@app.get("/health")
def health():
    return {"status": "ok"}
