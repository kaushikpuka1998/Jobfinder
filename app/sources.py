#!/usr/bin/env python3
"""
sources.py
==========

Extra job sources beyond LinkedIn: Greenhouse and Workday.

Both expose public, unauthenticated JSON APIs, so there is no HTML scraping
here — just JSON in, `Job` objects out. Everything downstream (scoring,
dedup, SQLite persistence, exports) is the machinery already in
linkedin_job_scraper.py; these adapters only produce `Job` rows for it.

Neither Greenhouse nor Workday has a global "search all companies" endpoint —
they are per-employer job boards. So "everywhere" means "every board in the
registry below". The registries were probed live and only working boards were
kept; add your own with the `boards=` / `tenants=` arguments or by editing
GREENHOUSE_BOARDS / WORKDAY_TENANTS.
"""

from __future__ import annotations

import html as html_mod
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from app.scraper import (
    Job,
    clean_text,
    detect_remote,
    make_soup,
    matches_any,
    normalise_date,
    parse_salary,
)

# --------------------------------------------------------------------------
# Registries — every entry below returned HTTP 200 with jobs when probed.
# --------------------------------------------------------------------------

GREENHOUSE_BOARDS = [
    "databricks", "stripe", "anthropic", "datadog", "hellofresh", "mongodb",
    "elastic", "cloudflare", "brex", "verkada", "celonis", "samsara",
    "scaleai", "gitlab", "affirm", "coinbase", "airbnb", "flexport", "figma",
    "reddit", "twilio", "robinhood", "asana", "instacart", "vercel",
    "mixpanel", "gusto", "duolingo", "monzo", "postman", "faire", "chime",
    "n26", "getyourguide", "launchdarkly", "discord", "dropbox", "amplitude",
    "betterment", "wise", "airtable", "circleci",
    # Added — probed live, all returning postings at add time.
    "algolia", "calendly", "carta", "checkr", "cockroachlabs", "coursera",
    "druva", "honor", "imc", "khanacademy", "lattice", "lyft", "masterclass",
    "netlify", "newrelic", "nextdoor", "okta", "pinterest", "splice",
    "squarespace", "tanium", "toast", "twitch", "udemy", "upstart",
    "webflow", "zocdoc",
]

# (tenant, workday cluster, career-site name)
WORKDAY_TENANTS = [
    ("nvidia", "wd5", "NVIDIAExternalCareerSite"),
    ("abbott", "wd5", "abbottcareers"),
    ("salesforce", "wd12", "External_Career_Site"),
    ("mastercard", "wd1", "CorporateCareers"),
    ("philips", "wd3", "jobs-and-careers"),
    ("hp", "wd5", "ExternalCareerSite"),
    ("adobe", "wd5", "external_experienced"),
    ("workday", "wd5", "Workday"),
    ("blackrock", "wd1", "BlackRock_Professional"),
    # Added — probed live. Workday has no guessable slug pattern (tenant,
    # cluster, and site name are independent per company), so unlike the
    # boards above, growing this list means probing each company by hand.
    ("pfizer", "wd1", "PfizerCareers"),
    ("intel", "wd1", "External"),
    ("target", "wd5", "targetcareers"),
]

# Ashby boards — slug from jobs.ashbyhq.com/<slug>
ASHBY_BOARDS = [
    "openai", "crusoe", "harvey", "sierra", "saronic", "decagon", "ramp",
    "notion", "zip", "cursor", "vanta", "perplexity", "1x", "supabase",
    "attio", "abridge", "radiant", "sardine", "thinkingmachines", "render",
    "gamma", "cartesia", "modal", "watershed", "linear", "workos", "hex",
    "omni", "dust", "column", "tennr", "posthog", "resend", "pylon",
    "browserbase", "railway", "openevidence", "neon", "paradigm", "stytch",
    "unit", "knock",
    # Added — probed live, all returning postings at add time.
    "airbyte", "anyscale", "baseten", "clickhouse", "cohere", "eightsleep",
    "elevenlabs", "fireworks", "lambda", "langchain", "mercor", "meter",
    "montecarlodata", "pinecone", "replit", "runway", "suno", "synthesia",
    "temporal", "weaviate", "writer",
]

# Lever boards — slug from jobs.lever.co/<slug>
LEVER_BOARDS = [
    "gopuff", "shieldai", "spotify", "ro", "qonto", "angellist", "swile",
    "tala", "ledger",
]

# Workable accounts — slug from apply.workable.com/<slug>. These were all
# verified to exist; most cycle between having openings and having none.
WORKABLE_ACCOUNTS = [
    "zego", "hotjar", "typeform", "payhawk", "nexthink", "tide", "docplanner",
    "glovo", "factorial", "travelperk", "cabify", "satispay", "ankorstore",
    "beamery", "onfido", "cleo", "marshmallow", "zopa", "wagestream",
    "deliverect",
]

# RemoteOK is an aggregator, not a per-company board, so it has no registry.
# Its public API returns only the latest ~100 postings per call, but the `tag`
# parameter gives a different 100 per tag — querying a spread of tags pulls
# roughly 1000 distinct jobs instead of 100.
REMOTEOK_TAGS = [
    "python", "javascript", "typescript", "react", "node", "java", "golang",
    "ruby", "django", "backend", "frontend", "full stack", "devops", "api",
    "engineer", "senior", "sql", "docker", "kubernetes", "php", "rust",
    "data", "machine learning", "security", "mobile", "android", "ios",
    "design", "aws", "cloud", "saas", "startup", "developer",
]

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
REMOTEOK_API = "https://remoteok.com/api"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{board}"
LEVER_API = "https://api.lever.co/v0/postings/{board}"
WORKABLE_API = "https://apply.workable.com/api/v1/widget/accounts/{board}"
WORKDAY_SEARCH = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
WORKDAY_DETAIL = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
WORKDAY_PUBLIC = "https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

Progress = Optional[Callable[[str], None]]


def _noop(_msg: str) -> None:
    pass


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    return s


def _strip_html(raw: str) -> str:
    """Greenhouse/Workday ship descriptions as escaped HTML."""
    if not raw:
        return ""
    text = html_mod.unescape(raw)
    if "<" in text:
        text = make_soup(text).get_text(separator=" ")
    return clean_text(text)


def _location_ok(location: str, wanted: Sequence[str]) -> bool:
    """Keep a job if its location mentions any wanted place, or if we do not care."""
    if not wanted:
        return True
    blob = (location or "").lower()
    if not blob:
        return False
    for place in wanted:
        for token in re.split(r"[,/]", place):
            token = token.strip().lower()
            # Skip noise tokens like "india" inside "Bengaluru, Karnataka, India"?
            # No — a country match is a legitimate hit, we only skip empties.
            if token and token in blob:
                return True
    return False


def _fresh_enough(posted_date: str, since_days: Optional[int]) -> bool:
    if not since_days or not posted_date:
        # No date parsed -> keep it; scoring simply gives it no recency bonus.
        return True
    try:
        posted = datetime.fromisoformat(posted_date).date()
    except ValueError:
        return True
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).date()
    return posted >= cutoff


def _board_scan(boards: Sequence[str],
                one_board: Callable[[str], List[Job]],
                label: str,
                workers: int,
                limit: Optional[int],
                say: Callable[[str], None]) -> List[Job]:
    """Run one fetch per board concurrently and dedup the results.

    Greenhouse, Ashby, Lever and Workable all work the same way — one request
    per company board — so the concurrency and dedup live here once.
    """
    out: Dict[str, Job] = {}
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one_board, b) for b in boards]
        for future in as_completed(futures):
            try:
                found = future.result()
            except Exception as exc:  # noqa: BLE001
                say(f"{label}: board failed ({exc.__class__.__name__})")
                continue
            with lock:
                for job in found:
                    out.setdefault(job.job_id, job)
    jobs = list(out.values())
    say(f"{label}: {len(jobs)} postings from {len(boards)} boards")
    return jobs[:limit] if limit else jobs


def _get_json(sess: requests.Session, url: str, label: str,
              say: Callable[[str], None], params=None) -> Optional[Any]:
    try:
        resp = sess.get(url, params=params, timeout=30)
    except requests.RequestException as exc:
        say(f"{label}: network error ({exc.__class__.__name__})")
        return None
    if resp.status_code != 200:
        say(f"{label}: HTTP {resp.status_code}, skipping")
        return None
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        say(f"{label}: bad JSON, skipping")
        return None


def _relevant(job: Job, terms: Sequence[str], locations: Sequence[str],
              since_days: Optional[int], remote_only: bool = False) -> bool:
    """Shared keep/drop test applied by every board-style source."""
    if terms and not matches_any(terms, f"{job.title} {job.description}"):
        return False
    if remote_only:
        # "Remote, worldwide" — a location filter would defeat the point, so
        # remote roles are kept wherever the company happens to be based.
        if not job.is_remote:
            return False
    elif not _location_ok(job.location, locations):
        return False
    return _fresh_enough(job.posted_date, since_days)


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------

def fetch_greenhouse(terms: Sequence[str],
                     locations: Sequence[str] = (),
                     since_days: Optional[int] = None,
                     boards: Optional[Sequence[str]] = None,
                     workers: int = 8,
                     limit: Optional[int] = None,
                     remote_only: bool = False,
                     progress: Progress = None) -> List[Job]:
    """One request per board returns every posting *with* its description."""
    say = progress or _noop
    boards = list(boards or GREENHOUSE_BOARDS)
    sess = _session()

    def one_board(board: str) -> List[Job]:
        payload = _get_json(sess, GREENHOUSE_API.format(board=board),
                            f"greenhouse/{board}", say, params={"content": "true"})
        if not payload:
            return []
        found: List[Job] = []
        for item in payload.get("jobs", []):
            title = clean_text(item.get("title") or "")
            description = _strip_html(item.get("content") or "")
            location = clean_text((item.get("location") or {}).get("name") or "")
            posted = (normalise_date(item.get("first_published"))
                      or normalise_date(item.get("updated_at")) or "")
            url = item.get("absolute_url") or ""
            lo, hi, cur = parse_salary(f"{title} {description}")

            job = Job(
                job_id=f"gh:{board}:{item.get('id')}",
                title=title,
                company=clean_text(item.get("company_name") or board.title()),
                location=location,
                job_url=url,
                apply_url=url,
                posted_date=posted,
                posted_raw=clean_text(item.get("updated_at") or ""),
                description=description,
                job_function=clean_text(
                    ", ".join(d.get("name", "") for d in item.get("departments") or [])),
                salary_min=lo, salary_max=hi, salary_currency=cur or "",
                is_remote=detect_remote(location, title, description),
                search_location=location,
            )
            if _relevant(job, terms, locations, since_days, remote_only):
                found.append(job)
        say(f"greenhouse/{board}: {len(found)} relevant of {len(payload.get('jobs', []))}")
        return found

    return _board_scan(boards, one_board, "greenhouse", workers, limit, say)


# --------------------------------------------------------------------------
# RemoteOK
# --------------------------------------------------------------------------

def fetch_remoteok(terms: Sequence[str],
                   locations: Sequence[str] = (),
                   since_days: Optional[int] = None,
                   tags: Optional[Sequence[str]] = None,
                   workers: int = 4,
                   limit: Optional[int] = None,
                   remote_only: bool = False,
                   progress: Progress = None) -> List[Job]:
    """Every posting is remote by definition, so `remote_only` costs nothing.

    RemoteOK's terms ask for attribution and a followed link back; the UI
    credits them and the Apply button sends users to the RemoteOK posting.
    """
    say = progress or _noop
    tags = list(tags or REMOTEOK_TAGS)
    sess = _session()

    def one_tag(tag: str) -> List[Job]:
        payload = _get_json(sess, REMOTEOK_API, f"remoteok/{tag}", say,
                            params={"tag": tag} if tag else None)
        if not isinstance(payload, list):
            return []
        found: List[Job] = []
        for item in payload:
            # The first element is a legal/ToS notice, not a posting.
            if not isinstance(item, dict) or not item.get("id"):
                continue
            title = clean_text(item.get("position") or "")
            description = _strip_html(item.get("description") or "")
            tag_list = [t for t in (item.get("tags") or []) if t]
            location = clean_text(item.get("location") or "") or "Remote"
            url = item.get("url") or ""

            lo = item.get("salary_min") or None
            hi = item.get("salary_max") or None
            if not lo or not hi:
                lo, hi, cur = parse_salary(f"{title} {description}")
            else:
                cur = "USD"

            found.append(Job(
                job_id=f"ro:{item.get('id')}",
                title=title,
                company=clean_text(item.get("company") or ""),
                location=location,
                job_url=url,
                apply_url=item.get("apply_url") or url,
                posted_date=normalise_date(item.get("date")) or "",
                posted_raw=clean_text(str(item.get("date") or "")),
                description=description,
                job_function=", ".join(tag_list[:6]),
                salary_min=float(lo) if lo else None,
                salary_max=float(hi) if hi else None,
                salary_currency=cur or "",
                is_remote=1,
                search_keyword=tag,
                search_location=location,
            ))
        say(f"remoteok/{tag}: {len(found)} postings")
        return found

    out: Dict[str, Job] = {}
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in as_completed([pool.submit(one_tag, t) for t in tags]):
            try:
                batch = future.result()
            except Exception as exc:  # noqa: BLE001
                say(f"remoteok: tag failed ({exc.__class__.__name__})")
                continue
            with lock:
                for job in batch:
                    out.setdefault(job.job_id, job)

    kept = [j for j in out.values()
            if _relevant(j, terms, locations, since_days, remote_only)]
    say(f"remoteok: {len(kept)} relevant of {len(out)} across {len(tags)} tags")
    return kept[:limit] if limit else kept


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------

def fetch_ashby(terms: Sequence[str],
                locations: Sequence[str] = (),
                since_days: Optional[int] = None,
                boards: Optional[Sequence[str]] = None,
                workers: int = 8,
                limit: Optional[int] = None,
                remote_only: bool = False,
                progress: Progress = None) -> List[Job]:
    """One request per board; descriptions and remote flags come inline."""
    say = progress or _noop
    boards = list(boards or ASHBY_BOARDS)
    sess = _session()

    def one_board(board: str) -> List[Job]:
        payload = _get_json(sess, ASHBY_API.format(board=board), f"ashby/{board}", say,
                            params={"includeCompensation": "true"})
        if not payload:
            return []
        items = payload.get("jobs") or []
        found: List[Job] = []
        for item in items:
            if item.get("isListed") is False:
                continue
            title = clean_text(item.get("title") or "")
            description = (clean_text(item.get("descriptionPlain") or "")
                           or _strip_html(item.get("descriptionHtml") or ""))
            location = clean_text(item.get("location") or "")
            url = item.get("jobUrl") or ""
            lo, hi, cur = parse_salary(f"{title} {description}")

            job = Job(
                job_id=f"ab:{board}:{item.get('id')}",
                title=title,
                company=clean_text(payload.get("name") or board.title()),
                location=location,
                job_url=url,
                apply_url=item.get("applyUrl") or url,
                posted_date=normalise_date(item.get("publishedAt")) or "",
                posted_raw=clean_text(item.get("publishedAt") or ""),
                description=description,
                employment_type=clean_text(item.get("employmentType") or ""),
                job_function=clean_text(item.get("department") or item.get("team") or ""),
                salary_min=lo, salary_max=hi, salary_currency=cur or "",
                is_remote=detect_remote(location, title, description,
                                        explicit=item.get("isRemote"),
                                        workplace=item.get("workplaceType") or ""),
                search_location=location,
            )
            if _relevant(job, terms, locations, since_days, remote_only):
                found.append(job)
        say(f"ashby/{board}: {len(found)} relevant of {len(items)}")
        return found

    return _board_scan(boards, one_board, "ashby", workers, limit, say)


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------

def fetch_lever(terms: Sequence[str],
                locations: Sequence[str] = (),
                since_days: Optional[int] = None,
                boards: Optional[Sequence[str]] = None,
                workers: int = 8,
                limit: Optional[int] = None,
                remote_only: bool = False,
                progress: Progress = None) -> List[Job]:
    """`?mode=json` returns the whole board including plain-text descriptions."""
    say = progress or _noop
    boards = list(boards or LEVER_BOARDS)
    sess = _session()

    def one_board(board: str) -> List[Job]:
        payload = _get_json(sess, LEVER_API.format(board=board), f"lever/{board}", say,
                            params={"mode": "json"})
        if not isinstance(payload, list):
            return []
        found: List[Job] = []
        for item in payload:
            title = clean_text(item.get("text") or "")
            description = (clean_text(item.get("descriptionPlain") or "")
                           or _strip_html(item.get("description") or ""))
            lists = item.get("lists") or []
            if lists:
                # Requirements usually live in these bullet blocks, and that is
                # where the years-of-experience line hides.
                extra = " ".join(_strip_html(b.get("content") or "") for b in lists)
                description = f"{description} {extra}".strip()

            categories = item.get("categories") or {}
            location = clean_text(categories.get("location") or "")
            url = item.get("hostedUrl") or ""
            lo, hi, cur = parse_salary(f"{title} {description}")
            workplace = (item.get("workplaceType") or "").lower()

            job = Job(
                job_id=f"lv:{board}:{item.get('id')}",
                title=title,
                company=board.replace("-", " ").title(),
                location=location,
                job_url=url,
                apply_url=item.get("applyUrl") or url,
                posted_date=_epoch_ms_to_date(item.get("createdAt")),
                description=description,
                employment_type=clean_text(categories.get("commitment") or ""),
                job_function=clean_text(categories.get("team") or ""),
                salary_min=lo, salary_max=hi, salary_currency=cur or "",
                is_remote=detect_remote(location, title, description, workplace=workplace),
                search_location=location,
            )
            if _relevant(job, terms, locations, since_days, remote_only):
                found.append(job)
        say(f"lever/{board}: {len(found)} relevant of {len(payload)}")
        return found

    return _board_scan(boards, one_board, "lever", workers, limit, say)


def _epoch_ms_to_date(value: Any) -> str:
    """Lever timestamps are epoch milliseconds."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


# --------------------------------------------------------------------------
# Workable
# --------------------------------------------------------------------------

def fetch_workable(terms: Sequence[str],
                   locations: Sequence[str] = (),
                   since_days: Optional[int] = None,
                   boards: Optional[Sequence[str]] = None,
                   workers: int = 5,
                   limit: Optional[int] = None,
                   remote_only: bool = False,
                   progress: Progress = None) -> List[Job]:
    """`details=true` returns descriptions inline, so one request per account.

    Workable rate-limits aggressively, hence the lower default worker count.
    """
    say = progress or _noop
    boards = list(boards or WORKABLE_ACCOUNTS)
    sess = _session()

    def one_board(board: str) -> List[Job]:
        payload = _get_json(sess, WORKABLE_API.format(board=board), f"workable/{board}", say,
                            params={"details": "true"})
        if not payload:
            return []
        items = payload.get("jobs") or []
        company = clean_text(payload.get("name") or board.title())
        found: List[Job] = []
        for item in items:
            title = clean_text(item.get("title") or "")
            description = _strip_html(item.get("description") or "")
            location = ", ".join(p for p in (clean_text(item.get("city") or ""),
                                             clean_text(item.get("country") or "")) if p)
            url = item.get("url") or item.get("shortlink") or ""
            lo, hi, cur = parse_salary(f"{title} {description}")

            job = Job(
                job_id=f"wk:{board}:{item.get('shortcode')}",
                title=title,
                company=company,
                location=location,
                job_url=url,
                apply_url=item.get("application_url") or url,
                posted_date=normalise_date(item.get("published_on")
                                           or item.get("created_at")) or "",
                description=description,
                employment_type=clean_text(item.get("employment_type") or ""),
                job_function=clean_text(item.get("department") or item.get("function") or ""),
                salary_min=lo, salary_max=hi, salary_currency=cur or "",
                is_remote=detect_remote(location, title, description,
                                        explicit=item.get("telecommuting")),
                search_location=location,
            )
            if _relevant(job, terms, locations, since_days, remote_only):
                found.append(job)
        say(f"workable/{board}: {len(found)} relevant of {len(items)}")
        return found

    return _board_scan(boards, one_board, "workable", workers, limit, say)


# --------------------------------------------------------------------------
# Workday
# --------------------------------------------------------------------------

def _workday_search(sess: requests.Session, tenant: str, wd: str, site: str,
                    text: str, max_results: int, say: Callable[[str], None]) -> List[dict]:
    url = WORKDAY_SEARCH.format(tenant=tenant, wd=wd, site=site)
    postings: List[dict] = []
    offset = 0
    page_size = 20
    while offset < max_results:
        try:
            resp = sess.post(url, json={"appliedFacets": {}, "limit": page_size,
                                        "offset": offset, "searchText": text},
                             headers={"Content-Type": "application/json"}, timeout=30)
        except requests.RequestException as exc:
            say(f"workday/{tenant}: network error ({exc.__class__.__name__})")
            break
        if resp.status_code != 200:
            say(f"workday/{tenant}: HTTP {resp.status_code}")
            break
        try:
            payload = resp.json()
        except ValueError:
            break
        batch = payload.get("jobPostings") or []
        if not batch:
            break
        postings.extend(batch)
        total = payload.get("total") or 0
        offset += page_size
        if offset >= total:
            break
    return postings


def fetch_workday(terms: Sequence[str],
                  locations: Sequence[str] = (),
                  since_days: Optional[int] = None,
                  tenants: Optional[Sequence[Tuple[str, str, str]]] = None,
                  per_search: int = 60,
                  workers: int = 6,
                  limit: Optional[int] = None,
                  fetch_details: bool = True,
                  remote_only: bool = False,
                  progress: Progress = None) -> List[Job]:
    """Search each tenant for each term, then pull descriptions for the hits."""
    say = progress or _noop
    tenants = list(tenants or WORKDAY_TENANTS)
    search_terms = [t for t in (terms or []) if t.strip()] or [""]
    sess = _session()
    out: Dict[str, Job] = {}
    lock = threading.Lock()

    def one_tenant(entry: Tuple[str, str, str]) -> List[Job]:
        tenant, wd, site = entry
        seen: Dict[str, Job] = {}
        for text in search_terms:
            for posting in _workday_search(sess, tenant, wd, site, text, per_search, say):
                path = posting.get("externalPath") or ""
                if not path:
                    continue
                req = (posting.get("bulletFields") or [path])[0]
                job_id = f"wd:{tenant}:{req}"
                if job_id in seen:
                    continue

                title = clean_text(posting.get("title") or "")
                location = clean_text(posting.get("locationsText") or "")
                posted = normalise_date(posting.get("postedOn") or "") or ""

                seen[job_id] = Job(
                    job_id=job_id,
                    title=title,
                    company=tenant.title(),
                    location=location,
                    job_url=WORKDAY_PUBLIC.format(tenant=tenant, wd=wd, site=site, path=path),
                    posted_date=posted,
                    posted_raw=clean_text(posting.get("postedOn") or ""),
                    is_remote=detect_remote(location, title,
                                            workplace=posting.get("remoteType") or ""),
                    search_location=location,
                    # stash for the detail pass
                    search_keyword=f"{tenant}|{wd}|{site}|{path}",
                )
        say(f"workday/{tenant}: {len(seen)} postings")
        return list(seen.values())

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in as_completed([pool.submit(one_tenant, t) for t in tenants]):
            for job in future.result():
                with lock:
                    out.setdefault(job.job_id, job)

    jobs = list(out.values())

    if fetch_details and jobs:
        say(f"workday: fetching {len(jobs)} descriptions")
        jobs = _workday_details(sess, jobs, workers, say)

    # Filter only after details land — the list view has no description and a
    # coarse posted-on string, so filtering earlier throws away good rows.
    kept = []
    for job in jobs:
        job.search_keyword = ""
        if _relevant(job, terms, locations, since_days, remote_only):
            kept.append(job)

    say(f"workday: {len(kept)} relevant of {len(jobs)}")
    return kept[:limit] if limit else kept


def _workday_details(sess: requests.Session, jobs: List[Job], workers: int,
                     say: Callable[[str], None]) -> List[Job]:
    def one(job: Job) -> Job:
        try:
            tenant, wd, site, path = job.search_keyword.split("|", 3)
        except ValueError:
            return job
        url = WORKDAY_DETAIL.format(tenant=tenant, wd=wd, site=site, path=path)
        try:
            resp = sess.get(url, timeout=30)
            if resp.status_code != 200:
                return job
            info = resp.json().get("jobPostingInfo") or {}
        except (requests.RequestException, ValueError):
            return job

        job.description = _strip_html(info.get("jobDescription") or "")
        job.location = clean_text(info.get("location") or job.location)
        job.employment_type = clean_text(info.get("timeType") or "")
        job.posted_date = normalise_date(info.get("startDate")) or job.posted_date
        if info.get("externalUrl"):
            job.apply_url = info["externalUrl"]
        job.is_remote = detect_remote(job.location, job.title, job.description,
                                      explicit=bool(job.is_remote),
                                      workplace=info.get("remoteType") or "")
        lo, hi, cur = parse_salary(f"{job.title} {job.description}")
        job.salary_min, job.salary_max = lo, hi
        job.salary_currency = cur or ""
        return job

    done = 0
    results: List[Job] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, j): j for j in jobs}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception:  # noqa: BLE001
                results.append(futures[future])
            done += 1
            if done % 50 == 0:
                say(f"workday: {done}/{len(jobs)} descriptions")
    return results


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------

def demo() -> None:
    """Offline checks for the pure logic. No network."""
    assert _strip_html("<p>Ruby &amp; Rails</p>") == "Ruby & Rails"
    assert _strip_html("") == ""

    assert _location_ok("Bengaluru, Karnataka, India", ["India"])
    assert _location_ok("Remote - US", [])
    assert not _location_ok("Tokyo, Japan", ["India", "Ireland"])
    assert not _location_ok("", ["India"])
    assert _location_ok("Dublin, Ireland", ["Dublin, Ireland"])

    today = datetime.now(timezone.utc).date().isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    assert _fresh_enough(today, 7)
    assert not _fresh_enough(old, 7)
    assert _fresh_enough(old, None)
    assert _fresh_enough("", 7)          # unknown date -> keep
    assert _fresh_enough("garbage", 7)   # unparseable -> keep

    # Relevance uses the same matcher as the scorer: java != javascript.
    assert matches_any(["java"], "Senior Java Engineer")
    assert not matches_any(["java"], "Senior JavaScript Engineer")
    assert matches_any(["spring boot"], "we use Spring-Boot here")

    # Lever ships epoch-millisecond timestamps.
    assert _epoch_ms_to_date(1755000000000).startswith("2025-")
    assert _epoch_ms_to_date(None) == ""
    assert _epoch_ms_to_date("nonsense") == ""

    # The shared keep/drop test, used by all four board sources.
    job = Job(job_id="x", title="Senior Ruby Engineer",
              description="We use Rails", location="Bengaluru, India",
              posted_date=datetime.now(timezone.utc).date().isoformat())
    assert _relevant(job, ["ruby"], ["India"], 30)
    assert not _relevant(job, ["golang"], ["India"], 30)
    assert not _relevant(job, ["ruby"], ["Japan"], 30)

    # Every registry entry must be a usable slug.
    for registry in (GREENHOUSE_BOARDS, ASHBY_BOARDS, LEVER_BOARDS, WORKABLE_ACCOUNTS):
        assert registry and all(isinstance(b, str) and b.strip() for b in registry)
        assert len(registry) == len(set(registry)), "duplicate board slug"

    print("sources.py self-check OK "
          f"({len(GREENHOUSE_BOARDS)} greenhouse, {len(ASHBY_BOARDS)} ashby, "
          f"{len(LEVER_BOARDS)} lever, {len(WORKABLE_ACCOUNTS)} workable, "
          f"{len(WORKDAY_TENANTS)} workday)")


if __name__ == "__main__":
    demo()
