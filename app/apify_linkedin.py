#!/usr/bin/env python3
"""
apify_linkedin.py
==================

LinkedIn jobs via Apify's hosted `valig/linkedin-jobs-scraper` actor, run
synchronously through Apify's REST API — no Apify SDK, no polling loop,
just one POST that blocks until the run's dataset is ready.

This is a separate path from app.sources / app.scraper's own LinkedIn
client: that one drives linkedin.com directly and is rate-limited; this one
pays Apify per result and is meant for the "last 24 hours" cron sweep
against the one saved resume profile.

Results are cached in Redis per (keywords, location, datePosted) — see
app.cache — so an accidental duplicate call (a double click, a redeploy
mid-run) reuses the recent result instead of paying Apify again.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Callable, Dict, List, Optional, Sequence

import requests

from app import cache
from app.config import Config
from app.scraper import Job, clean_text, detect_remote, normalise_date, parse_salary

LOG = logging.getLogger(__name__)

ACTOR_ID = "valig~linkedin-jobs-scraper"
RUN_SYNC_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"

# LinkedIn's own f_TPR search-age codes, which this actor passes through
# directly rather than a friendlier string like the old actor used.
DATE_POSTED_24H = "r86400"

Progress = Optional[Callable[[str], None]]


def _noop(_msg: str) -> None:
    pass


def _cache_key(keywords: str, location: str, date_posted: str) -> str:
    raw = f"{ACTOR_ID}|{keywords}|{location}|{date_posted}"
    return "apify_cache:" + hashlib.sha256(raw.encode()).hexdigest()


def _map_job(item: dict, profile_name: str) -> Optional[Job]:
    job_id = item.get("id")
    if not job_id:
        return None
    title = clean_text(item.get("title") or "")
    description = clean_text(item.get("description") or "")
    location = clean_text(item.get("location") or "")
    url = item.get("url") or item.get("applyUrl") or ""
    lo, hi, cur = parse_salary(item.get("salary") or f"{title} {description}")
    work_type = clean_text(item.get("workType") or "")

    return Job(
        # Namespaced so an Apify-sourced posting never collides with the
        # same job's id from the direct LinkedIn scraper, but still tags as
        # "linkedin" in the UI — see SOURCE_PREFIXES in app.scraper.
        job_id=f"apli:{job_id}",
        title=title,
        company=clean_text(item.get("companyName") or ""),
        company_url=item.get("companyUrl") or "",
        location=location,
        job_url=url,
        apply_url=item.get("applyUrl") or url,
        posted_date=normalise_date(item.get("postedDate")) or "",
        posted_raw=clean_text(item.get("postedDate") or item.get("postedTimeAgo") or ""),
        description=description,
        seniority=clean_text(item.get("experienceLevel") or ""),
        employment_type=clean_text(item.get("contractType") or ""),
        job_function="",
        industries=clean_text(item.get("sector") or ""),
        applicants=_as_int(item.get("applicationsCount")),
        salary_min=lo, salary_max=hi, salary_currency=cur or "",
        is_remote=detect_remote(location=location, title=title, description=description,
                                workplace=work_type),
        profile=profile_name,
    )


def _as_int(value) -> Optional[int]:
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def fetch_linkedin_jobs(token: str,
                        keywords: str,
                        locations: Sequence[str] = (),
                        profile_name: str = "",
                        date_posted: str = DATE_POSTED_24H,
                        limit_per_search: int = 100,
                        timeout: int = 240,
                        progress: Progress = None) -> List[Job]:
    """Run the actor once per location (or once, unfiltered, if none given).

    Runs synchronously via Apify's run-sync-get-dataset-items endpoint — the
    call blocks until the actor finishes, so `timeout` needs real headroom.
    Each per-location search checks the Redis cache first (see app.cache)
    and stores a fresh result after a successful call.
    """
    say = progress or _noop
    if not token:
        say("Apify: no APIFY_TOKEN configured, skipping")
        return []

    searches = list(locations) or [""]
    out: Dict[str, Job] = {}
    sess = requests.Session()
    for location in searches:
        label = f"apify-linkedin/{location or 'anywhere'}"
        cache_key = _cache_key(keywords, location, date_posted)
        items = cache.get(cache_key)
        if items is not None:
            say(f"{label}: reusing cached result from a recent search (avoids paying Apify twice)")
        else:
            body = {
                "keywords": keywords,
                "location": location,
                "datePosted": date_posted,
                "limit": limit_per_search,
                "under10Applicants": False,
            }
            try:
                resp = sess.post(RUN_SYNC_URL, params={"token": token}, json=body, timeout=timeout)
            except requests.RequestException as exc:
                say(f"{label}: network error ({exc.__class__.__name__})")
                continue
            if resp.status_code not in (200, 201):
                # run-sync-get-dataset-items answers 201 (Created) on a
                # normal successful run, not 200 — treating only 200 as
                # success silently discarded every real result.
                say(f"{label}: HTTP {resp.status_code}, skipping — {resp.text[:200]}")
                continue
            try:
                items = resp.json()
            except ValueError:
                say(f"{label}: bad JSON, skipping")
                continue
            cache.set(cache_key, items, Config.APIFY_CACHE_TTL_SECONDS)

        found = 0
        for item in items:
            job = _map_job(item, profile_name)
            if job:
                out.setdefault(job.job_id, job)
                found += 1
        say(f"{label}: {found} postings")

    return list(out.values())
