#!/usr/bin/env python3
"""
linkedin_job_scraper.py
=======================

A production-grade LinkedIn job scraper and ranker. No AI, no LLM calls,
no API keys. Pure HTTP + HTML parsing + deterministic keyword scoring.

It uses LinkedIn's public *guest* job endpoints (the ones that serve the
logged-out job search at /jobs/search). No login, no cookies, no session
hijacking.

Features
--------
  * Multi-profile search (e.g. one profile per resume variant)
  * Cartesian expansion over keywords x locations
  * Full pagination with automatic end-of-results detection
  * Per-job detail fetch (description, seniority, employment type, industry,
    applicant count, external apply URL)
  * Polite concurrency with a shared token-bucket rate limiter
  * Exponential backoff with jitter on 429 / 5xx, User-Agent rotation
  * SQLite persistence with dedup, so runs are resumable and incremental
  * Deterministic scoring engine: must-have / exclude / weighted terms,
    title multipliers, recency decay, seniority matching
  * Salary extraction via regex (LPA, INR, USD, EUR, GBP ranges)
  * Exports to CSV, JSON, and a ranked Markdown digest

Usage
-----
    python linkedin_job_scraper.py init-config          # write config.json
    python linkedin_job_scraper.py scrape               # run all profiles
    python linkedin_job_scraper.py scrape -p java       # run one profile
    python linkedin_job_scraper.py export --format all  # export from DB
    python linkedin_job_scraper.py stats                # DB summary
    python linkedin_job_scraper.py prune --days 45      # drop stale rows

Dependencies
------------
    pip install requests beautifulsoup4 lxml python-dateutil
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random
import re
from app.database import MongoJobStore
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

try:
    from dateutil import parser as date_parser  # type: ignore
    HAVE_DATEUTIL = True
except ImportError:  # pragma: no cover
    HAVE_DATEUTIL = False


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
PUBLIC_JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}/"

DEFAULT_DB = "linkedin_jobs.db"
DEFAULT_CONFIG = "config.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# LinkedIn filter code reference (used to build query params)
EXPERIENCE_CODES = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid-senior": "4",
    "director": "5",
    "executive": "6",
}

JOB_TYPE_CODES = {
    "full-time": "F",
    "part-time": "P",
    "contract": "C",
    "temporary": "T",
    "internship": "I",
    "volunteer": "V",
    "other": "O",
}

WORKPLACE_CODES = {
    "onsite": "1",
    "remote": "2",
    "hybrid": "3",
}

TIME_WINDOWS = {
    "24h": 86400,
    "48h": 172800,
    "72h": 259200,
    "96h": 345600,
    "week": 604800,
    "month": 2592000,
    "any": 0,
}

# Common geoIds. Passing a geoId is far more reliable than a location string.
GEO_IDS = {
    "india": "102713980",
    "bengaluru": "105214831",
    "bangalore": "105214831",
    "hyderabad": "105556991",
    "pune": "114806696",
    "chennai": "106888327",
    "mumbai": "106164952",
    "delhi ncr": "115018733",
    "gurugram": "106442238",
    "noida": "115383763",
    "dublin": "104738515",
    "ireland": "104738515",
    "london": "102257491",
    "united kingdom": "101165590",
    "berlin": "106967730",
    "germany": "101282230",
    "netherlands": "102890719",
    "amsterdam": "102011674",
    "united states": "103644278",
    "european union": "91000000",
}

LOG = logging.getLogger("linkedin_scraper")

# Scores are reported 0-100. The raw keyword arithmetic is divided by a
# reference "strong match" total derived from the scoring config — see
# ScoringConfig.reference_score.
MAX_SCORE = 100.0
REFERENCE_TOP_TERMS = 10

# Only used to convert databases written before scores were normalised; it is
# the reference the shipped profiles produce, so legacy rows land on roughly
# the right scale instead of staying on the old 0-270 one.
LEGACY_REFERENCE = 170.0

# job_id prefix -> source name. LinkedIn ids are bare numbers, so it is the
# fallback rather than an entry here.
SOURCE_PREFIXES = {"greenhouse": "gh:", "ashby": "ab:", "lever": "lv:",
                   "workable": "wk:", "workday": "wd:", "remoteok": "ro:",
                   # Apify-sourced LinkedIn postings — namespaced so they
                   # never collide with the direct scraper's bare numeric
                   # ids, but still labelled "linkedin" in the UI.
                   "linkedin": "apli:"}


def source_of(job_id: str) -> str:
    for name, prefix in SOURCE_PREFIXES.items():
        if job_id.startswith(prefix):
            return name
    return "linkedin"


# --------------------------------------------------------------------------
# Configuration model
# --------------------------------------------------------------------------

@dataclass
class ScoringConfig:
    """Deterministic scoring rules. Pure keyword arithmetic, no models."""

    # A job is rejected outright if ANY of these appear anywhere.
    exclude_terms: List[str] = field(default_factory=list)
    # A job is rejected unless AT LEAST `must_have_min` of these appear.
    must_have_terms: List[str] = field(default_factory=list)
    must_have_min: int = 1
    # term -> points. Matched case-insensitively on word boundaries.
    weighted_terms: Dict[str, float] = field(default_factory=dict)
    # Terms that are worth extra when they land in the job title.
    title_terms: Dict[str, float] = field(default_factory=dict)
    # Multiplier applied to weighted_terms hits found in the title.
    title_multiplier: float = 2.5
    # Points added for a job posted today, decaying linearly to 0 at
    # `recency_horizon_days`.
    recency_bonus: float = 12.0
    recency_horizon_days: int = 14
    # Points for matching seniority level exactly.
    preferred_seniority: List[str] = field(default_factory=list)
    seniority_bonus: float = 10.0
    # Penalty applied per 100 applicants (crowded postings rank lower).
    applicant_penalty_per_100: float = 2.0
    applicant_penalty_cap: float = 15.0
    # Diminishing returns: a term matched N times scores
    # weight * (1 + log1p(N-1) * repeat_factor)
    repeat_factor: float = 0.35
    # Minimum score (0-100) to keep a job in exports.
    min_score: float = 0.0

    def reference_score(self, top_n: int = REFERENCE_TOP_TERMS) -> float:
        """Raw points a strong match earns — the divisor for the 0-100 score.

        Derived from the config alone, never from the result set, so a job's
        score is stable: the same posting scores the same today and next week,
        and two profiles with different weights stay comparable.

        A "strong match" is taken as the top `top_n` weighted terms hitting,
        with the single heaviest also landing in the title, plus the best title
        term, full recency and the seniority bonus. Tuned against ~3.5k real
        postings: median lands near 22, p95 near 61, and under 1% cap at 100.
        """
        weights = sorted(self.weighted_terms.values(), reverse=True)[:top_n]
        total = float(sum(weights))
        if weights:
            total += weights[0] * (self.title_multiplier - 1.0)
        if self.title_terms:
            total += max(self.title_terms.values())
        total += self.recency_bonus + self.seniority_bonus
        return max(1.0, total)


@dataclass
class SearchProfile:
    """One named search configuration — typically one per resume variant."""

    name: str
    keywords: List[str] = field(default_factory=list)
    locations: List[str] = field(default_factory=list)
    geo_ids: List[str] = field(default_factory=list)
    time_window: str = "72h"
    experience_levels: List[str] = field(default_factory=list)
    job_types: List[str] = field(default_factory=list)
    workplace_types: List[str] = field(default_factory=list)
    sort_by: str = "DD"          # DD = date descending, R = relevance
    max_pages: int = 12          # per keyword x location combination
    max_results: int = 400       # hard cap per profile
    fetch_details: bool = True
    scoring: ScoringConfig = field(default_factory=ScoringConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SearchProfile":
        scoring_raw = data.get("scoring", {}) or {}
        known = {f for f in ScoringConfig.__dataclass_fields__}
        scoring = ScoringConfig(**{k: v for k, v in scoring_raw.items() if k in known})
        payload = {k: v for k, v in data.items() if k != "scoring"}
        allowed = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in payload.items() if k in allowed}
        return cls(scoring=scoring, **payload)


@dataclass
class HttpConfig:
    min_delay: float = 1.4          # seconds between requests (global)
    max_delay: float = 3.2
    timeout: float = 25.0
    max_retries: int = 5
    backoff_base: float = 2.0
    backoff_cap: float = 90.0
    concurrency: int = 3            # detail-fetch workers
    proxies: Dict[str, str] = field(default_factory=dict)
    cooldown_after: int = 120       # requests before a longer pause
    cooldown_seconds: float = 25.0


@dataclass
class AppConfig:
    profiles: List[SearchProfile] = field(default_factory=list)
    http: HttpConfig = field(default_factory=HttpConfig)
    database: str = DEFAULT_DB
    output_dir: str = "output"

    @classmethod
    def load(cls, path: str) -> "AppConfig":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        http_raw = raw.get("http", {}) or {}
        http_allowed = {f for f in HttpConfig.__dataclass_fields__}
        http = HttpConfig(**{k: v for k, v in http_raw.items() if k in http_allowed})
        profiles = [SearchProfile.from_dict(p) for p in raw.get("profiles", [])]
        return cls(
            profiles=profiles,
            http=http,
            database=raw.get("database", DEFAULT_DB),
            output_dir=raw.get("output_dir", "output"),
        )


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

class RateLimiter:
    """Thread-safe minimum-interval gate with jitter and periodic cooldowns."""

    def __init__(self, min_delay: float, max_delay: float,
                 cooldown_after: int = 0, cooldown_seconds: float = 0.0) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.cooldown_after = cooldown_after
        self.cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._count = 0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            gap = random.uniform(self.min_delay, self.max_delay)
            self._count += 1
            if self.cooldown_after and self._count % self.cooldown_after == 0:
                gap += self.cooldown_seconds
                LOG.info("Cooldown: pausing an extra %.1fs after %d requests",
                         self.cooldown_seconds, self._count)
            self._next_allowed = max(now, self._next_allowed) + gap
        if wait > 0:
            time.sleep(wait)

    def penalise(self, seconds: float) -> None:
        """Push the whole schedule back — used after a 429."""
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic()) + seconds


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class LinkedInClient:
    """Session wrapper with retries, backoff, UA rotation and rate limiting."""

    def __init__(self, cfg: HttpConfig) -> None:
        self.cfg = cfg
        self.limiter = RateLimiter(
            cfg.min_delay, cfg.max_delay, cfg.cooldown_after, cfg.cooldown_seconds
        )
        self._local = threading.local()
        self.stats = {"requests": 0, "retries": 0, "rate_limited": 0, "errors": 0}
        self._stats_lock = threading.Lock()

    def _session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Referer": "https://www.linkedin.com/jobs/search/",
            })
            if self.cfg.proxies:
                sess.proxies.update(self.cfg.proxies)
            self._local.session = sess
        return sess

    def _bump(self, key: str) -> None:
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + 1

    def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Return response text, or None if the resource is genuinely gone."""
        attempt = 0
        while attempt <= self.cfg.max_retries:
            self.limiter.acquire()
            sess = self._session()
            sess.headers["User-Agent"] = random.choice(USER_AGENTS)
            try:
                self._bump("requests")
                resp = sess.get(url, params=params, timeout=self.cfg.timeout)
            except requests.RequestException as exc:
                attempt += 1
                self._bump("retries")
                delay = self._backoff(attempt)
                LOG.warning("Network error (%s). Retry %d/%d in %.1fs",
                            exc.__class__.__name__, attempt, self.cfg.max_retries, delay)
                time.sleep(delay)
                continue

            status = resp.status_code

            if status == 200:
                return resp.text

            if status in (404, 410):
                LOG.debug("Not found: %s", resp.url)
                return None

            if status == 429:
                self._bump("rate_limited")
                attempt += 1
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = float(retry_after)
                else:
                    delay = self._backoff(attempt) * 2
                LOG.warning("HTTP 429 rate limited. Backing off %.1fs (attempt %d/%d)",
                            delay, attempt, self.cfg.max_retries)
                self.limiter.penalise(delay)
                time.sleep(delay)
                continue

            if status in (500, 502, 503, 504):
                attempt += 1
                self._bump("retries")
                delay = self._backoff(attempt)
                LOG.warning("HTTP %d. Retry %d/%d in %.1fs",
                            status, attempt, self.cfg.max_retries, delay)
                time.sleep(delay)
                continue

            if status in (403, 999):
                # 999 is LinkedIn's "we think you're a bot" status.
                attempt += 1
                self._bump("rate_limited")
                delay = min(self.cfg.backoff_cap, self._backoff(attempt) * 3)
                LOG.warning("HTTP %d (bot challenge). Cooling down %.1fs", status, delay)
                self.limiter.penalise(delay)
                time.sleep(delay)
                continue

            self._bump("errors")
            LOG.error("Unhandled HTTP %d for %s", status, resp.url)
            return None

        self._bump("errors")
        LOG.error("Giving up on %s after %d attempts", url, self.cfg.max_retries)
        return None

    def _backoff(self, attempt: int) -> float:
        raw = self.cfg.backoff_base ** attempt
        return min(self.cfg.backoff_cap, raw) * random.uniform(0.7, 1.3)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


JOB_ID_RE = re.compile(r"(?:jobPosting:|/view/(?:[^/?]*-)?)(\d{6,})")


def extract_job_id(card) -> Optional[str]:
    """Pull the numeric posting id from a search result card."""
    for node in (card, card.find("div", class_="base-card"),
                 card.find("div", attrs={"data-entity-urn": True})):
        if node is None:
            continue
        urn = node.get("data-entity-urn") or node.get("data-id")
        if urn:
            match = JOB_ID_RE.search(urn)
            if match:
                return match.group(1)
            digits = re.search(r"(\d{6,})", urn)
            if digits:
                return digits.group(1)

    link = card.find("a", class_="base-card__full-link") or card.find("a", href=True)
    if link and link.get("href"):
        match = JOB_ID_RE.search(link["href"])
        if match:
            return match.group(1)
        qs = parse_qs(urlparse(link["href"]).query)
        for key in ("currentJobId", "jobId"):
            if key in qs and qs[key]:
                return qs[key][0]
    return None


def parse_relative_date(text: str, reference: Optional[datetime] = None) -> Optional[str]:
    """Turn '3 days ago' / '2 weeks ago' into an ISO date string."""
    if not text:
        return None
    ref = reference or datetime.now(timezone.utc)
    text = text.lower().strip()
    if "just now" in text or "minute" in text or "hour" in text or "today" in text:
        return ref.date().isoformat()
    match = re.search(r"(\d+)\s*(day|week|month|year)", text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * amount
    return (ref - timedelta(days=days)).date().isoformat()


def normalise_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    if HAVE_DATEUTIL:
        try:
            return date_parser.parse(value).date().isoformat()
        except (ValueError, OverflowError):
            pass
    return parse_relative_date(value)


SALARY_PATTERNS = [
    # ₹12,00,000 - ₹18,00,000  /  INR 1200000-1800000
    (re.compile(r"(?:₹|inr|rs\.?)\s*([\d,]+(?:\.\d+)?)\s*(?:-|–|to)\s*(?:₹|inr|rs\.?)?\s*([\d,]+(?:\.\d+)?)", re.I), "INR"),
    # 25 - 40 LPA  /  25-40 lakhs
    (re.compile(r"([\d.]+)\s*(?:-|–|to)\s*([\d.]+)\s*(?:lpa|lakhs?|lacs?)", re.I), "INR_LPA"),
    (re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:-|–|to)\s*\$?\s*([\d,]+(?:\.\d+)?)", re.I), "USD"),
    (re.compile(r"(?:€|eur)\s*([\d,]+(?:\.\d+)?)\s*(?:-|–|to)\s*(?:€|eur)?\s*([\d,]+(?:\.\d+)?)", re.I), "EUR"),
    (re.compile(r"(?:£|gbp)\s*([\d,]+(?:\.\d+)?)\s*(?:-|–|to)\s*(?:£|gbp)?\s*([\d,]+(?:\.\d+)?)", re.I), "GBP"),
]


def parse_salary(text: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """Best-effort salary range extraction. Returns (min, max, currency)."""
    if not text:
        return None, None, None
    snippet = text[:4000]
    for pattern, currency in SALARY_PATTERNS:
        match = pattern.search(snippet)
        if not match:
            continue
        try:
            low = float(match.group(1).replace(",", ""))
            high = float(match.group(2).replace(",", ""))
        except (ValueError, IndexError):
            continue
        if currency == "INR_LPA":
            return low * 100000, high * 100000, "INR"
        if low > high:
            low, high = high, low
        return low, high, currency
    return None, None, None


APPLICANT_RE = re.compile(r"([\d,]+)\s*(?:\+\s*)?applicants?", re.I)


def parse_applicants(text: str) -> Optional[int]:
    if not text:
        return None
    match = APPLICANT_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Years-of-experience extraction
# --------------------------------------------------------------------------
#
# Postings state the requirement half a dozen ways. Each pattern yields
# (min, max); None on either side means open-ended.
#
#   "3-5 years"            -> (3, 5)
#   "5+ years"             -> (5, None)
#   "at least 4 years"     -> (4, None)
#   "up to 3 years"        -> (None, 3)
#   "4 years of experience"-> (4, None)   plain number, needs context
#
# Bare numbers are the dangerous case ("founded 5 years ago", "5 years of
# double-digit growth"), so a plain match only counts when an experience word
# sits right next to it.

EXPERIENCE_CONTEXT = re.compile(
    r"experience|exp\.|professional|industry|working|work|hands[\s-]?on|"
    r"background|expertise|developing|building|engineering|relevant",
    re.I)

_EXP_RANGE = re.compile(
    r"(\d{1,2})\s*(?:-|–|—|\bto\b|\bthru\b)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I)
_EXP_PLUS = re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)\b", re.I)
_EXP_ATLEAST = re.compile(
    r"(?:at\s+least|minimum(?:\s+of)?|min\.?|no\s+less\s+than|over)\s*"
    r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I)
_EXP_UPTO = re.compile(r"(?:up\s+to|less\s+than|under|below)\s*(\d{1,2})\s*(?:years?|yrs?)\b", re.I)
_EXP_PLAIN = re.compile(r"(\d{1,2})\s*(?:years?|yrs?)\b", re.I)

MAX_SANE_YEARS = 30


def parse_experience(text: str) -> Tuple[Optional[int], Optional[int]]:
    """Best-effort '(min_years, max_years)' required by a posting.

    Returns (None, None) when the posting never says. Scans left to right and
    takes the first credible statement, because requirements lead and later
    mentions are usually nice-to-haves ("2+ years of Kubernetes").
    """
    if not text:
        return None, None
    snippet = text[:6000]

    candidates: List[Tuple[int, Optional[int], Optional[int], bool]] = []

    for match in _EXP_RANGE.finditer(snippet):
        lo, hi = int(match.group(1)), int(match.group(2))
        if lo <= hi <= MAX_SANE_YEARS:
            candidates.append((match.start(), lo, hi, False))
    for match in _EXP_ATLEAST.finditer(snippet):
        lo = int(match.group(1))
        if lo <= MAX_SANE_YEARS:
            candidates.append((match.start(), lo, None, False))
    for match in _EXP_UPTO.finditer(snippet):
        hi = int(match.group(1))
        if hi <= MAX_SANE_YEARS:
            candidates.append((match.start(), None, hi, False))
    for match in _EXP_PLUS.finditer(snippet):
        lo = int(match.group(1))
        if lo <= MAX_SANE_YEARS:
            candidates.append((match.start(), lo, None, True))
    for match in _EXP_PLAIN.finditer(snippet):
        lo = int(match.group(1))
        if lo <= MAX_SANE_YEARS:
            candidates.append((match.start(), lo, None, True))

    if not candidates:
        return None, None

    candidates.sort(key=lambda c: c[0])
    for start, lo, hi, needs_context in candidates:
        if needs_context:
            window = snippet[max(0, start - 60):start + 90]
            if not EXPERIENCE_CONTEXT.search(window):
                continue
        return lo, hi
    return None, None


# --------------------------------------------------------------------------
# Remote detection
# --------------------------------------------------------------------------
#
# Every source words this differently, so one detector serves all six. A bare
# "remote" anywhere in a description is far too loose ("remote sensing", "our
# remote offices"), so the description only counts when it says so plainly.

# Location / workplace fields are terse and reliable.
REMOTE_PLACE_RE = re.compile(
    r"\b(remote|anywhere|work\s?from\s?home|wfh|virtual|home[\s-]?based|telecommute)\b", re.I)

# Descriptions need an explicit statement.
REMOTE_STRONG_RE = re.compile(
    r"\b(?:fully|100%|entirely|permanently|completely)[\s-]*remote\b"
    r"|\bremote[\s-](?:first|friendly|position|role|job|opportunity|work)\b"
    r"|\bwork\s+(?:from\s+home|remotely|from\s+anywhere)\b"
    r"|\bthis\s+(?:is\s+a|role\s+is)\s+(?:fully\s+)?remote\b"
    r"|\b(?:position|role)\s+is\s+(?:fully\s+)?remote\b", re.I)

# Explicit denials beat everything else.
NOT_REMOTE_RE = re.compile(
    r"\bnot\s+(?:a\s+)?remote\b|\bno\s+remote\b|\bon[\s-]?site\s+only\b"
    r"|\bnot\s+(?:a\s+)?(?:remote|work[\s-]from[\s-]home)\s+(?:role|position)\b"
    r"|\bremote\s+work\s+is\s+not\b", re.I)


def detect_remote(location: str = "", title: str = "", description: str = "",
                  explicit: Optional[bool] = None, workplace: str = "") -> int:
    """Is this posting remote? Returns 1/0.

    `explicit` is a source's own flag (Ashby isRemote, Workable telecommuting,
    …). True is trusted outright; False falls through to the text, because
    several boards leave the flag unset on genuinely remote roles.
    """
    if explicit:
        return 1
    if workplace and REMOTE_PLACE_RE.search(workplace):
        return 1
    if location and REMOTE_PLACE_RE.search(location):
        return 1
    if title and REMOTE_PLACE_RE.search(title):
        return 1
    if description:
        head = description[:4000]
        if NOT_REMOTE_RE.search(head):
            return 0
        if REMOTE_STRONG_RE.search(head):
            return 1
    return 0


def experience_overlaps(job_min: Optional[int], job_max: Optional[int],
                        want_min: Optional[int], want_max: Optional[int]) -> Optional[bool]:
    """Does a posting's requirement overlap the band the candidate wants?

    None means the posting never stated a requirement, so the caller decides
    whether to keep it — that is a policy choice, not a match result.
    """
    if want_min is None and want_max is None:
        return True
    if job_min is None and job_max is None:
        return None
    lo = job_min if job_min is not None else 0
    hi = job_max if job_max is not None else 99
    want_lo = want_min if want_min is not None else 0
    want_hi = want_max if want_max is not None else 99
    return lo <= want_hi and hi >= want_lo


# LinkedIn ships the external apply link two different ways:
#   1. <code id="applyUrl" ...><!--"https://..."--></code>
#   2. inline JSON:  "applyUrl":"https://..."
APPLY_URL_PATTERNS = [
    re.compile(r'id=["\']applyUrl["\'][^>]*>\s*<!--\s*"([^"]+)"\s*-->', re.I | re.S),
    re.compile(r'id=["\']applyUrl["\'][^>]*>\s*"?(https?://[^"\'<\s]+)', re.I),
    re.compile(r'"applyUrl"\s*:\s*"([^"]+)"'),
]


def _decode_escapes(value: str) -> str:
    """Undo the \\u0026 / &amp; style escaping LinkedIn embeds in URLs."""
    value = re.sub(r"\\u([0-9a-fA-F]{4})",
                   lambda m: chr(int(m.group(1), 16)), value)
    return value.replace("\\/", "/").replace("&amp;", "&")


def extract_apply_url(html: str) -> Optional[str]:
    # ponytail: dead as of 2026-08 — the guest jobPosting HTML no longer carries
    # an applyUrl anywhere; offsite apply sits behind a sign-in modal. Patterns
    # kept because callers already fall back to job_url and LinkedIn's markup
    # churns. Delete the field + column if it hasn't come back in a few months.
    for pattern in APPLY_URL_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        url = _decode_escapes(match.group(1)).strip()
        if url.startswith("http"):
            return url
    return None


# --------------------------------------------------------------------------
# Job model
# --------------------------------------------------------------------------

@dataclass
class Job:
    job_id: str
    title: str = ""
    company: str = ""
    company_url: str = ""
    location: str = ""
    job_url: str = ""
    apply_url: str = ""
    posted_date: str = ""
    posted_raw: str = ""
    description: str = ""
    seniority: str = ""
    employment_type: str = ""
    job_function: str = ""
    industries: str = ""
    applicants: Optional[int] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: str = ""
    exp_min: Optional[int] = None
    exp_max: Optional[int] = None
    is_remote: int = 0
    profile: str = ""
    search_keyword: str = ""
    search_location: str = ""
    score: float = 0.0          # 0-100, free keyword scorer
    score_raw: float = 0.0      # unnormalised keyword points
    score_breakdown: str = ""
    matched_terms: str = ""
    # Second, deeper pass from Claude — set only for the top-scoring jobs
    # from a run (see Config.ATS_TOP_N), null for the rest.
    ats_score: Optional[int] = None
    ats_reason: str = ""
    rejected_reason: str = ""
    status: str = ""          # "", applied, interview, offer, rejected
    status_at: str = ""
    first_seen: str = ""
    last_seen: str = ""

    def to_row(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Search + detail scraping
# --------------------------------------------------------------------------

class JobScraper:
    def __init__(self, client: LinkedInClient, store: "MongoJobStore") -> None:
        self.client = client
        self.store = store

    # -- query construction ------------------------------------------------

    @staticmethod
    def build_params(keyword: str, location: str, geo_id: str,
                     profile: SearchProfile, start: int) -> Dict[str, Any]:
        params: Dict[str, Any] = {"keywords": keyword, "start": start}
        if geo_id:
            params["geoId"] = geo_id
        if location:
            params["location"] = location

        seconds = TIME_WINDOWS.get(profile.time_window.lower())
        if seconds is None:
            match = re.fullmatch(r"r?(\d+)", profile.time_window)
            seconds = int(match.group(1)) if match else 0
        if seconds:
            params["f_TPR"] = f"r{seconds}"

        levels = [EXPERIENCE_CODES[e.lower()] for e in profile.experience_levels
                  if e.lower() in EXPERIENCE_CODES]
        if levels:
            params["f_E"] = ",".join(levels)

        types = [JOB_TYPE_CODES[t.lower()] for t in profile.job_types
                 if t.lower() in JOB_TYPE_CODES]
        if types:
            params["f_JT"] = ",".join(types)

        places = [WORKPLACE_CODES[w.lower()] for w in profile.workplace_types
                  if w.lower() in WORKPLACE_CODES]
        if places:
            params["f_WT"] = ",".join(places)

        if profile.sort_by:
            params["sortBy"] = profile.sort_by
        return params

    # -- search page -------------------------------------------------------

    def parse_search_page(self, html: str) -> List[Job]:
        soup = make_soup(html)
        cards = soup.find_all("li")
        if not cards:
            cards = soup.find_all("div", class_="base-card")

        jobs: List[Job] = []
        for card in cards:
            job_id = extract_job_id(card)
            if not job_id:
                continue

            title_node = (card.find("h3", class_="base-search-card__title")
                          or card.find("h3"))
            company_node = (card.find("h4", class_="base-search-card__subtitle")
                            or card.find("a", class_="hidden-nested-link")
                            or card.find("h4"))
            location_node = card.find("span", class_="job-search-card__location")
            time_node = card.find("time")
            link_node = card.find("a", class_="base-card__full-link") or card.find("a", href=True)
            salary_node = card.find("span", class_="job-search-card__salary-info")

            company_link = ""
            company_anchor = card.find("a", class_="hidden-nested-link")
            if company_anchor and company_anchor.get("href"):
                company_link = company_anchor["href"].split("?")[0]

            job_url = ""
            if link_node and link_node.get("href"):
                job_url = link_node["href"].split("?")[0]
            if not job_url:
                job_url = PUBLIC_JOB_URL.format(job_id=job_id)

            posted_raw = ""
            posted_date = ""
            if time_node is not None:
                posted_raw = clean_text(time_node.get_text())
                posted_date = normalise_date(time_node.get("datetime")) or \
                    normalise_date(posted_raw) or ""

            location = clean_text(location_node.get_text()) if location_node else ""

            job = Job(
                job_id=job_id,
                title=clean_text(title_node.get_text()) if title_node else "",
                company=clean_text(company_node.get_text()) if company_node else "",
                company_url=company_link,
                location=location,
                job_url=job_url,
                posted_date=posted_date,
                posted_raw=posted_raw,
                is_remote=detect_remote(location, title=clean_text(
                    title_node.get_text()) if title_node else ""),
            )

            if salary_node:
                lo, hi, cur = parse_salary(clean_text(salary_node.get_text()))
                job.salary_min, job.salary_max = lo, hi
                job.salary_currency = cur or ""

            jobs.append(job)
        return jobs

    def search(self, keyword: str, location: str, geo_id: str,
               profile: SearchProfile) -> List[Job]:
        collected: Dict[str, Job] = {}
        start = 0
        empty_streak = 0

        for page in range(profile.max_pages):
            params = self.build_params(keyword, location, geo_id, profile, start)
            LOG.info("[%s] search '%s' @ '%s' page %d (start=%d)",
                     profile.name, keyword, location or geo_id, page + 1, start)

            html = self.client.get(SEARCH_URL, params=params)
            if html is None:
                LOG.info("[%s] no response, stopping pagination", profile.name)
                break

            if not html.strip():
                LOG.info("[%s] empty page, end of results", profile.name)
                break

            page_jobs = self.parse_search_page(html)
            if not page_jobs:
                empty_streak += 1
                if empty_streak >= 2:
                    LOG.info("[%s] two empty pages, end of results", profile.name)
                    break
                start += 10
                continue

            empty_streak = 0
            new_on_page = 0
            for job in page_jobs:
                if job.job_id in collected:
                    continue
                job.profile = profile.name
                job.search_keyword = keyword
                job.search_location = location or geo_id
                collected[job.job_id] = job
                new_on_page += 1

            LOG.info("[%s] page %d -> %d cards (%d new, %d total)",
                     profile.name, page + 1, len(page_jobs), new_on_page, len(collected))

            if new_on_page == 0:
                LOG.info("[%s] no new ids on this page, stopping", profile.name)
                break

            if len(collected) >= profile.max_results:
                LOG.info("[%s] hit max_results=%d", profile.name, profile.max_results)
                break

            start += len(page_jobs)

        return list(collected.values())

    # -- detail page -------------------------------------------------------

    def fetch_detail(self, job: Job) -> Job:
        html = self.client.get(DETAIL_URL.format(job_id=job.job_id))
        if not html:
            return job

        soup = make_soup(html)

        desc_node = (soup.find("div", class_="show-more-less-html__markup")
                     or soup.find("div", class_="description__text")
                     or soup.find("section", class_="description"))
        if desc_node:
            job.description = clean_text(desc_node.get_text(separator=" "))

        for item in soup.find_all("li", class_="description__job-criteria-item"):
            header_node = item.find("h3")
            value_node = item.find("span")
            if not header_node or not value_node:
                continue
            header = clean_text(header_node.get_text()).lower()
            value = clean_text(value_node.get_text())
            if "seniority" in header:
                job.seniority = value
            elif "employment" in header:
                job.employment_type = value
            elif "function" in header:
                job.job_function = value
            elif "industr" in header:
                job.industries = value

        applicants_node = (soup.find("span", class_="num-applicants__caption")
                           or soup.find("figcaption", class_="num-applicants__caption"))
        if applicants_node:
            job.applicants = parse_applicants(clean_text(applicants_node.get_text()))

        posted_node = soup.find("span", class_="posted-time-ago__text")
        if posted_node and not job.posted_date:
            job.posted_raw = clean_text(posted_node.get_text())
            job.posted_date = normalise_date(job.posted_raw) or ""

        if not job.title:
            title_node = soup.find("h2", class_="top-card-layout__title") or soup.find("h1")
            if title_node:
                job.title = clean_text(title_node.get_text())

        if not job.company:
            company_node = soup.find("a", class_="topcard__org-name-link")
            if company_node:
                job.company = clean_text(company_node.get_text())
                job.company_url = (company_node.get("href") or "").split("?")[0]

        apply_url = extract_apply_url(html)
        if apply_url:
            job.apply_url = apply_url

        if job.salary_min is None:
            haystack = " ".join([job.description, job.title])
            lo, hi, cur = parse_salary(haystack)
            job.salary_min, job.salary_max = lo, hi
            job.salary_currency = cur or job.salary_currency

        job.is_remote = detect_remote(job.location, job.title, job.description,
                                      explicit=bool(job.is_remote))
        job.exp_min, job.exp_max = parse_experience(f"{job.title}. {job.description}")

        return job

    def enrich(self, jobs: Sequence[Job], workers: int) -> List[Job]:
        if not jobs:
            return []
        results: List[Job] = []
        LOG.info("Fetching details for %d jobs with %d workers", len(jobs), workers)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self.fetch_detail, job): job for job in jobs}
            done = 0
            for future in as_completed(futures):
                original = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("Detail fetch failed for %s: %s", original.job_id, exc)
                    results.append(original)
                done += 1
                if done % 25 == 0:
                    LOG.info("  ... %d/%d details fetched", done, len(jobs))
        return results


# --------------------------------------------------------------------------
# Deterministic scoring engine
# --------------------------------------------------------------------------

@lru_cache(maxsize=4096)
def term_pattern(term: str) -> re.Pattern:
    """Word-boundary matcher for one scoring/search term.

    Escape each whitespace-separated word, then join with a flexible separator
    so "spring boot" also matches "Spring-Boot"/"Spring  Boot". (re.escape no
    longer escapes spaces on Python 3.7+, so we cannot rely on replacing a
    backslash-space sequence after the fact.)

    Shared by the Scorer and by the Greenhouse/Workday adapters in sources.py,
    so relevance filtering and scoring always agree on what "matches".
    """
    words = [re.escape(w) for w in term.strip().lower().split() if w]
    if not words:
        return re.compile(r"(?!x)x")  # never matches
    body = r"[\s\-_/]+".join(words)
    return re.compile(rf"(?<![a-z0-9+#]){body}(?![a-z0-9+#])", re.I)


def term_hits(term: str, text: str) -> int:
    return len(term_pattern(term).findall(text))


def matches_any(terms: Iterable[str], text: str) -> bool:
    return any(term_pattern(t).search(text) for t in terms)


class Scorer:
    """Pure keyword arithmetic. Same input always yields the same score."""

    def __init__(self, cfg: ScoringConfig) -> None:
        self.cfg = cfg
        self.reference = cfg.reference_score()

    def _count(self, term: str, text: str) -> int:
        return term_hits(term, text)

    def score(self, job: Job) -> Job:
        cfg = self.cfg
        title = job.title.lower()
        body = " ".join([job.title, job.description, job.job_function,
                         job.industries, job.employment_type]).lower()

        for term in cfg.exclude_terms:
            if self._count(term, body):
                job.score = 0.0
                job.rejected_reason = f"excluded term: {term}"
                job.score_breakdown = json.dumps({"rejected": term})
                return job

        if cfg.must_have_terms:
            hits = [t for t in cfg.must_have_terms if self._count(t, body)]
            if len(hits) < cfg.must_have_min:
                job.score = 0.0
                job.rejected_reason = (
                    f"only {len(hits)}/{cfg.must_have_min} must-have terms matched"
                )
                job.score_breakdown = json.dumps({"must_have_hits": hits})
                return job

        breakdown: Dict[str, float] = {}
        matched: List[str] = []
        total = 0.0

        for term, weight in cfg.weighted_terms.items():
            body_hits = self._count(term, body)
            if not body_hits:
                continue
            title_hits = self._count(term, title)
            repeat = 1.0 + math.log1p(max(0, body_hits - 1)) * cfg.repeat_factor
            points = float(weight) * repeat
            if title_hits:
                points *= cfg.title_multiplier
            breakdown[f"term:{term}"] = round(points, 2)
            matched.append(term)
            total += points

        for term, weight in cfg.title_terms.items():
            if self._count(term, title):
                breakdown[f"title:{term}"] = float(weight)
                matched.append(term)
                total += float(weight)

        if cfg.preferred_seniority and job.seniority:
            wanted = {s.lower() for s in cfg.preferred_seniority}
            if job.seniority.lower() in wanted:
                breakdown["seniority"] = cfg.seniority_bonus
                total += cfg.seniority_bonus

        if job.posted_date and cfg.recency_bonus:
            try:
                posted = datetime.fromisoformat(job.posted_date).date()
                age = (datetime.now(timezone.utc).date() - posted).days
                if age <= cfg.recency_horizon_days:
                    ratio = 1.0 - (age / max(1, cfg.recency_horizon_days))
                    bonus = round(cfg.recency_bonus * max(0.0, ratio), 2)
                    if bonus:
                        breakdown["recency"] = bonus
                        total += bonus
            except ValueError:
                pass

        if job.applicants and cfg.applicant_penalty_per_100:
            penalty = min(cfg.applicant_penalty_cap,
                          (job.applicants / 100.0) * cfg.applicant_penalty_per_100)
            penalty = round(penalty, 2)
            if penalty:
                breakdown["applicants"] = -penalty
                total -= penalty

        raw = max(0.0, total)
        job.score_raw = round(raw, 2)
        job.score = round(min(MAX_SCORE, MAX_SCORE * raw / self.reference), 1)
        # Breakdown stays in raw points — it explains the arithmetic — so record
        # the divisor alongside it, otherwise the parts look inconsistent with
        # the 0-100 total.
        breakdown["_raw_total"] = round(raw, 2)
        breakdown["_reference"] = round(self.reference, 2)
        job.score_breakdown = json.dumps(breakdown, sort_keys=True)
        job.matched_terms = ", ".join(sorted(set(matched)))
        job.rejected_reason = ""
        return job


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    title            TEXT,
    company          TEXT,
    company_url      TEXT,
    location         TEXT,
    job_url          TEXT,
    apply_url        TEXT,
    posted_date      TEXT,
    posted_raw       TEXT,
    description      TEXT,
    seniority        TEXT,
    employment_type  TEXT,
    job_function     TEXT,
    industries       TEXT,
    applicants       INTEGER,
    salary_min       REAL,
    salary_max       REAL,
    salary_currency  TEXT,
    exp_min          INTEGER,
    exp_max          INTEGER,
    is_remote        INTEGER DEFAULT 0,
    profile          TEXT,
    search_keyword   TEXT,
    search_location  TEXT,
    score            REAL DEFAULT 0,
    score_raw        REAL DEFAULT 0,
    score_breakdown  TEXT,
    matched_terms    TEXT,
    rejected_reason  TEXT,
    status           TEXT,
    status_at        TEXT,
    first_seen       TEXT,
    last_seen        TEXT
);
-- Indexes stay on columns the base table has always had, so that this script
-- still runs against a database created before the newer columns existed.
-- Your application details, alongside the jobs rather than in a loose file.
-- Scalars are key/value; the repeating blocks are one row per field so the
-- whole profile stays queryable with plain SQL.
CREATE TABLE IF NOT EXISTS applicant (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
CREATE TABLE IF NOT EXISTS applicant_entries (
    list   TEXT NOT NULL,
    pos    INTEGER NOT NULL,
    field  TEXT NOT NULL,
    value  TEXT,
    PRIMARY KEY (list, pos, field)
);

CREATE INDEX IF NOT EXISTS idx_jobs_score   ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_posted  ON jobs(posted_date DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_profile ON jobs(profile);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
"""

COLUMNS = [
    "job_id", "title", "company", "company_url", "location", "job_url",
    "apply_url", "posted_date", "posted_raw", "description", "seniority",
    "employment_type", "job_function", "industries", "applicants",
    "salary_min", "salary_max", "salary_currency", "exp_min", "exp_max",
    "is_remote", "profile",
    "search_keyword", "search_location", "score", "score_raw",
    "score_breakdown", "matched_terms", "rejected_reason",
    "first_seen", "last_seen",
]


# The columns the CSV export carries, in order. Lost when this module was
# split out of linkedin_job_scraper.py, which broke every export path.
EXPORT_FIELDS = [
    "score", "score_raw", "title", "company", "location", "posted_date", "seniority",
    "exp_min", "exp_max", "employment_type", "is_remote", "applicants",
    "salary_min", "salary_max", "salary_currency", "profile", "matched_terms",
    "status", "status_at", "job_url", "apply_url", "company_url", "job_id",
]


def export_csv(rows: Sequence[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in EXPORT_FIELDS})
    LOG.info("Wrote %d rows -> %s", len(rows), path)


def export_json(rows: Sequence[dict], path: str) -> None:
    payload = []
    for row in rows:
        item = {k: row[k] for k in row.keys()}
        # Mongo documents carry an ObjectId that json cannot encode.
        item.pop("_id", None)
        try:
            item["score_breakdown"] = json.loads(item.get("score_breakdown") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["score_breakdown"] = {}
        payload.append(item)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    LOG.info("Wrote %d rows -> %s", len(rows), path)


def format_salary(row: dict) -> str:
    lo, hi, cur = row["salary_min"], row["salary_max"], row["salary_currency"]
    if not lo or not hi:
        return "—"
    if cur == "INR" and lo >= 100000:
        return f"₹{lo / 100000:.1f}–{hi / 100000:.1f} LPA"
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(cur or "", "")
    return f"{symbol}{lo:,.0f}–{symbol}{hi:,.0f}"


def export_markdown(rows: Sequence[dict], path: str, top_n: int = 60) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Job digest — {now}",
        "",
        f"{len(rows)} matching postings. Showing the top {min(top_n, len(rows))} by score.",
        "",
        "| # | Score | Title | Company | Location | Posted | Salary | Link |",
        "|---|-------|-------|---------|----------|--------|--------|------|",
    ]
    for idx, row in enumerate(rows[:top_n], start=1):
        title = (row["title"] or "").replace("|", "/")
        company = (row["company"] or "").replace("|", "/")
        location = (row["location"] or "").replace("|", "/")
        lines.append(
            f"| {idx} | {row['score']:.1f} | {title} | {company} | {location} | "
            f"{row['posted_date'] or '—'} | {format_salary(row)} | [open]({row['job_url']}) |"
        )

    lines += ["", "---", "", "## Match detail", ""]
    for idx, row in enumerate(rows[:top_n], start=1):
        lines.append(f"### {idx}. {row['title']} — {row['company']}")
        lines.append("")
        lines.append(f"- **Score:** {row['score']:.1f}  ·  **Profile:** {row['profile']}")
        lines.append(f"- **Location:** {row['location'] or '—'}  ·  "
                     f"**Remote:** {'yes' if row['is_remote'] else 'no'}")
        lines.append(f"- **Seniority:** {row['seniority'] or '—'}  ·  "
                     f"**Type:** {row['employment_type'] or '—'}")
        if row["applicants"] is not None:
            lines.append(f"- **Applicants:** {row['applicants']}")
        lines.append(f"- **Matched:** {row['matched_terms'] or '—'}")
        lines.append(f"- **Apply:** {row['apply_url'] or row['job_url']}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    LOG.info("Wrote digest -> %s", path)


# --------------------------------------------------------------------------
# Default config generator
# --------------------------------------------------------------------------

def default_config() -> Dict[str, Any]:
    shared_exclude = [
        "intern", "internship", "fresher", "0-1 years", "unpaid",
        "commission only", "sales executive", "bpo", "voice process",
        "sap abap", "mainframe", "cobol",
    ]
    shared_titles = {
        "senior software engineer": 30,
        "software engineer ii": 26,
        "software engineer 2": 26,
        "sde 2": 26,
        "sde ii": 26,
        "software engineer iii": 24,
        "sde 3": 24,
        "senior backend engineer": 30,
        "backend engineer": 20,
        "full stack engineer": 16,
        "staff engineer": 14,
        "lead engineer": 12,
    }
    common_bonus = {
        "microservices": 8, "rest api": 6, "system design": 7,
        "kubernetes": 9, "docker": 6, "aws": 7, "gcp": 5, "azure": 4,
        "kafka": 8, "redis": 5, "elasticsearch": 8, "graphql": 6,
        "terraform": 7, "ci/cd": 4, "postgresql": 5, "mysql": 4,
        "distributed systems": 9, "product based": 6, "scalability": 5,
        "unit testing": 4, "observability": 5, "grpc": 5,
    }

    profiles = [
        {
            "name": "java",
            "keywords": [
                "Senior Software Engineer Java",
                "Backend Engineer Spring Boot",
                "SDE 2 Java",
                "Java Microservices Engineer",
            ],
            "locations": ["Bengaluru, Karnataka, India", "India", "Dublin, Ireland",
                          "London, United Kingdom", "Berlin, Germany"],
            "geo_ids": [],
            "time_window": "96h",
            "experience_levels": ["mid-senior", "associate"],
            "job_types": ["full-time"],
            "workplace_types": [],
            "sort_by": "DD",
            "max_pages": 10,
            "max_results": 250,
            "fetch_details": True,
            "scoring": {
                "exclude_terms": shared_exclude + ["php", "wordpress", ".net"],
                "must_have_terms": ["java", "spring", "spring boot", "kotlin"],
                "must_have_min": 1,
                "weighted_terms": dict(common_bonus, **{
                    "java": 14, "spring boot": 14, "spring": 8, "hibernate": 6,
                    "jpa": 5, "maven": 3, "gradle": 3, "junit": 4, "jvm": 5,
                }),
                "title_terms": shared_titles,
                "title_multiplier": 2.5,
                "recency_bonus": 12,
                "recency_horizon_days": 14,
                "preferred_seniority": ["Mid-Senior level", "Associate"],
                "seniority_bonus": 10,
                "applicant_penalty_per_100": 2.0,
                "applicant_penalty_cap": 15,
                "repeat_factor": 0.35,
                "min_score": 20,   # out of 100
            },
        },
        {
            "name": "node",
            "keywords": [
                "Senior Backend Engineer Node.js",
                "TypeScript Backend Engineer",
                "SDE 2 Node.js",
                "Full Stack Engineer TypeScript",
            ],
            "locations": ["Bengaluru, Karnataka, India", "India", "Dublin, Ireland",
                          "London, United Kingdom", "Berlin, Germany"],
            "geo_ids": [],
            "time_window": "96h",
            "experience_levels": ["mid-senior", "associate"],
            "job_types": ["full-time"],
            "workplace_types": [],
            "sort_by": "DD",
            "max_pages": 10,
            "max_results": 250,
            "fetch_details": True,
            "scoring": {
                "exclude_terms": shared_exclude + ["php", "wordpress", "drupal"],
                "must_have_terms": ["node.js", "nodejs", "typescript", "express", "nestjs"],
                "must_have_min": 1,
                "weighted_terms": dict(common_bonus, **{
                    "node.js": 14, "nodejs": 14, "typescript": 12, "express": 7,
                    "nestjs": 8, "react": 5, "next.js": 5, "mongodb": 4,
                    "prisma": 4, "jest": 3,
                }),
                "title_terms": shared_titles,
                "title_multiplier": 2.5,
                "recency_bonus": 12,
                "recency_horizon_days": 14,
                "preferred_seniority": ["Mid-Senior level", "Associate"],
                "seniority_bonus": 10,
                "applicant_penalty_per_100": 2.0,
                "applicant_penalty_cap": 15,
                "repeat_factor": 0.35,
                "min_score": 20,   # out of 100
            },
        },
        {
            "name": "ruby",
            "keywords": [
                "Senior Ruby on Rails Engineer",
                "Backend Engineer Ruby",
                "SDE 2 Ruby on Rails",
            ],
            "locations": ["Bengaluru, Karnataka, India", "India", "Dublin, Ireland",
                          "London, United Kingdom", "Berlin, Germany"],
            "geo_ids": [],
            "time_window": "week",
            "experience_levels": ["mid-senior", "associate"],
            "job_types": ["full-time"],
            "workplace_types": [],
            "sort_by": "DD",
            "max_pages": 8,
            "max_results": 150,
            "fetch_details": True,
            "scoring": {
                "exclude_terms": shared_exclude,
                "must_have_terms": ["ruby", "rails", "ruby on rails"],
                "must_have_min": 1,
                "weighted_terms": dict(common_bonus, **{
                    "ruby on rails": 15, "ruby": 12, "rails": 12,
                    "sidekiq": 6, "rspec": 5, "activerecord": 5, "puma": 3,
                }),
                "title_terms": shared_titles,
                "title_multiplier": 2.5,
                "recency_bonus": 12,
                "recency_horizon_days": 14,
                "preferred_seniority": ["Mid-Senior level", "Associate"],
                "seniority_bonus": 10,
                "applicant_penalty_per_100": 2.0,
                "applicant_penalty_cap": 15,
                "repeat_factor": 0.35,
                "min_score": 20,   # out of 100
            },
        },
    ]

    return {
        "database": DEFAULT_DB,
        "output_dir": "output",
        "http": {
            "min_delay": 1.4,
            "max_delay": 3.2,
            "timeout": 25.0,
            "max_retries": 5,
            "backoff_base": 2.0,
            "backoff_cap": 90.0,
            "concurrency": 3,
            "proxies": {},
            "cooldown_after": 120,
            "cooldown_seconds": 25.0,
        },
        "profiles": profiles,
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def resolve_targets(profile: SearchProfile) -> List[Tuple[str, str]]:
    """Return a list of (location_string, geo_id) pairs to search."""
    targets: List[Tuple[str, str]] = []
    for geo in profile.geo_ids:
        targets.append(("", str(geo)))
    for loc in profile.locations:
        geo = GEO_IDS.get(loc.split(",")[0].strip().lower(), "")
        targets.append((loc, geo))
    if not targets:
        targets.append(("", ""))
    return targets


def run_profile(profile: SearchProfile, scraper: JobScraper, store: MongoJobStore,
                http_cfg: HttpConfig, skip_known: bool) -> List[Job]:
    LOG.info("=" * 72)
    LOG.info("PROFILE: %s  (%d keywords x %d locations)",
             profile.name, len(profile.keywords),
             max(1, len(profile.locations) + len(profile.geo_ids)))
    LOG.info("=" * 72)

    collected: Dict[str, Job] = {}
    for keyword in profile.keywords:
        for location, geo_id in resolve_targets(profile):
            found = scraper.search(keyword, location, geo_id, profile)
            for job in found:
                if job.job_id not in collected:
                    collected[job.job_id] = job
            if len(collected) >= profile.max_results:
                break
        if len(collected) >= profile.max_results:
            LOG.info("[%s] reached max_results, stopping keyword loop", profile.name)
            break

    jobs = list(collected.values())[: profile.max_results]
    LOG.info("[%s] %d unique postings from search", profile.name, len(jobs))

    if skip_known:
        known = store.known_ids()
        before = len(jobs)
        jobs = [j for j in jobs if j.job_id not in known]
        LOG.info("[%s] skipping %d already in DB, %d new",
                 profile.name, before - len(jobs), len(jobs))

    if profile.fetch_details and jobs:
        jobs = scraper.enrich(jobs, http_cfg.concurrency)
    elif jobs and profile.scoring.must_have_terms:
        # Without the detail fetch there is no description, so must-have terms
        # can only match the title — which rejects nearly everything.
        LOG.warning("[%s] details disabled but must_have_terms is set; matching "
                    "on titles only, expect most postings to be rejected",
                    profile.name)

    scorer = Scorer(profile.scoring)
    for job in jobs:
        scorer.score(job)

    kept = [j for j in jobs if not j.rejected_reason]
    LOG.info("[%s] scored %d jobs — %d passed filters, %d rejected",
             profile.name, len(jobs), len(kept), len(jobs) - len(kept))
    return jobs


def cmd_scrape(args: argparse.Namespace) -> int:
    cfg = AppConfig.load(args.config)
    os.makedirs(cfg.output_dir, exist_ok=True)

    profiles = cfg.profiles
    if args.profile:
        wanted = {p.lower() for p in args.profile}
        profiles = [p for p in profiles if p.name.lower() in wanted]
        if not profiles:
            LOG.error("No profiles matched %s", ", ".join(args.profile))
            return 1

    if args.time_window:
        for p in profiles:
            p.time_window = args.time_window
    if args.max_pages:
        for p in profiles:
            p.max_pages = args.max_pages
    if args.no_details:
        for p in profiles:
            p.fetch_details = False

    store = MongoJobStore()
    client = LinkedInClient(cfg.http)
    scraper = JobScraper(client, store)

    started = time.time()
    all_jobs: List[Job] = []
    try:
        for profile in profiles:
            jobs = run_profile(profile, scraper, store, cfg.http, args.skip_known)
            all_jobs.extend(jobs)
            if jobs and not args.dry_run:
                inserted, updated = store.upsert_many(jobs)
                LOG.info("[%s] DB: %d inserted, %d updated",
                         profile.name, inserted, updated)
    except KeyboardInterrupt:
        LOG.warning("Interrupted — saving what we have")
        if all_jobs and not args.dry_run:
            store.upsert_many(all_jobs)

    elapsed = time.time() - started
    kept = [j for j in all_jobs if not j.rejected_reason]
    LOG.info("-" * 72)
    LOG.info("Done in %.1fs — %d scraped, %d passed filters",
             elapsed, len(all_jobs), len(kept))
    LOG.info("HTTP stats: %s", json.dumps(client.stats))

    if not args.dry_run:
        min_score = min((p.scoring.min_score for p in profiles), default=0.0)
        rows = store.query(min_score=min_score, limit=args.limit)
        write_exports(rows, cfg.output_dir, args.format)

    store.close()
    return 0


def write_exports(rows: Sequence[dict], output_dir: str, fmt: str) -> None:
    if not rows:
        LOG.warning("Nothing to export.")
        return
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if fmt in ("csv", "all"):
        export_csv(rows, os.path.join(output_dir, f"jobs_{stamp}.csv"))
    if fmt in ("json", "all"):
        export_json(rows, os.path.join(output_dir, f"jobs_{stamp}.json"))
    if fmt in ("md", "markdown", "all"):
        export_markdown(rows, os.path.join(output_dir, f"digest_{stamp}.md"))


def cmd_export(args: argparse.Namespace) -> int:
    cfg = AppConfig.load(args.config)
    store = MongoJobStore()
    rows = store.query(
        min_score=args.min_score,
        profile=args.profile,
        limit=args.limit,
        since_days=args.since_days,
        include_rejected=args.include_rejected,
    )
    LOG.info("Query returned %d rows", len(rows))
    write_exports(rows, cfg.output_dir, args.format)
    store.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = AppConfig.load(args.config)
    store = MongoJobStore()
    data = store.stats()
    print(f"\nDatabase: {cfg.database}")
    print(f"  Total postings : {data.get('total') or 0}")
    print(f"  Rejected       : {data.get('rejected') or 0}")
    avg = data.get("avg_score")
    print(f"  Average score  : {avg:.1f}" if avg else "  Average score  : n/a")
    mx = data.get("max_score")
    print(f"  Highest score  : {mx:.1f}" if mx else "  Highest score  : n/a")
    print("\n  By profile:")
    for row in data.get("by_profile", []):
        print(f"    {row['profile'] or '(none)':<12} {row['n']:>5}  avg {row['avg_score']}")
    print("\n  Top companies:")
    for row in data.get("top_companies", []):
        print(f"    {row['company'] or '(unknown)':<40} {row['n']:>4}")
    print()
    store.close()
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    cfg = AppConfig.load(args.config)
    store = MongoJobStore()
    removed = store.prune(args.days)
    LOG.info("Removed %d rows not seen in the last %d days", removed, args.days)
    store.close()
    return 0


def cmd_init_config(args: argparse.Namespace) -> int:
    if os.path.exists(args.config) and not args.force:
        LOG.error("%s already exists. Use --force to overwrite.", args.config)
        return 1
    with open(args.config, "w", encoding="utf-8") as fh:
        json.dump(default_config(), fh, indent=2, ensure_ascii=False)
    LOG.info("Wrote starter config -> %s", args.config)
    LOG.info("Edit the keywords, locations and scoring weights, then run: scrape")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linkedin_job_scraper",
        description="Scrape, score and rank LinkedIn job postings. No AI involved.",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                        help=f"path to config JSON (default: {DEFAULT_CONFIG})")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings only")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-config", help="write a starter config file")
    p_init.add_argument("--force", action="store_true", help="overwrite existing config")
    p_init.set_defaults(func=cmd_init_config)

    p_scrape = sub.add_parser("scrape", help="run the scraper")
    p_scrape.add_argument("-p", "--profile", action="append",
                          help="run only this profile (repeatable)")
    p_scrape.add_argument("--time-window", choices=sorted(TIME_WINDOWS),
                          help="override the posting age filter for all profiles")
    p_scrape.add_argument("--max-pages", type=int,
                          help="override max pages per keyword/location pair")
    p_scrape.add_argument("--no-details", action="store_true",
                          help="skip per-job detail fetches (much faster, less data)")
    p_scrape.add_argument("--skip-known", action="store_true",
                          help="do not re-fetch details for jobs already in the DB")
    p_scrape.add_argument("--dry-run", action="store_true",
                          help="scrape and score but write nothing")
    p_scrape.add_argument("--format", default="all",
                          choices=["csv", "json", "md", "markdown", "all"])
    p_scrape.add_argument("--limit", type=int, help="cap exported rows")
    p_scrape.set_defaults(func=cmd_scrape)

    p_export = sub.add_parser("export", help="export from the existing database")
    p_export.add_argument("--min-score", type=float, default=0.0)
    p_export.add_argument("-p", "--profile")
    p_export.add_argument("--limit", type=int)
    p_export.add_argument("--since-days", type=int,
                          help="only postings dated within the last N days")
    p_export.add_argument("--include-rejected", action="store_true")
    p_export.add_argument("--format", default="all",
                          choices=["csv", "json", "md", "markdown", "all"])
    p_export.set_defaults(func=cmd_export)

    p_stats = sub.add_parser("stats", help="print database summary")
    p_stats.set_defaults(func=cmd_stats)

    p_prune = sub.add_parser("prune", help="delete rows not seen recently")
    p_prune.add_argument("--days", type=int, default=45)
    p_prune.set_defaults(func=cmd_prune)

    p_check = sub.add_parser("self-check", help="run offline logic checks")
    p_check.set_defaults(func=cmd_self_check)

    return parser


def cmd_self_check(_args: argparse.Namespace) -> int:
    """Offline checks for the parsing logic. No network, no database."""
    exp_cases = [
        ("We need 3-5 years of professional experience", (3, 5)),
        ("5+ years of backend experience required", (5, None)),
        ("At least 4 years of experience building APIs", (4, None)),
        ("Minimum of 7 years experience", (7, None)),
        ("up to 3 years of experience", (None, 3)),
        ("4 years of experience with Rails", (4, None)),
        ("3 to 6 years of relevant industry experience", (3, 6)),
        ("2+ yrs experience", (2, None)),
        ("You will own the roadmap.", (None, None)),
        ("Our company was founded 12 years ago and we love pizza", (None, None)),
        ("Requirements: 5+ years of engineering experience. "
         "Nice to have: 2+ years of Kubernetes experience", (5, None)),
        ("8-10 years of hands-on experience", (8, 10)),
    ]
    for text, want in exp_cases:
        got = parse_experience(text)
        assert got == want, f"parse_experience({text!r}) -> {got}, want {want}"

    # Candidate wants 4-6 years.
    overlap_cases = [
        ((5, None), True), ((10, None), False), ((2, 3), False), ((3, 8), True),
        ((None, None), None), ((6, None), True), ((7, None), False), ((None, 3), False),
    ]
    for (job_lo, job_hi), want in overlap_cases:
        got = experience_overlaps(job_lo, job_hi, 4, 6)
        assert got is want, f"overlap({job_lo},{job_hi}) -> {got}, want {want}"
    assert experience_overlaps(9, None, None, None) is True  # no band = no filter

    # Term matching still distinguishes look-alikes.
    assert term_hits("java", "Senior Java Engineer") == 1
    assert term_hits("java", "Senior JavaScript Engineer") == 0
    assert matches_any(["spring boot"], "we use Spring-Boot")

    assert parse_salary("25 - 40 LPA")[0] == 2500000
    assert normalise_date("2026-01-05") == "2026-01-05"

    # Scores are reported 0-100 and never exceed it.
    cfg = ScoringConfig(
        weighted_terms={"java": 14, "spring boot": 14, "kubernetes": 9, "aws": 8},
        title_terms={"senior software engineer": 30},
        must_have_terms=["java"], recency_bonus=12, seniority_bonus=10,
    )
    scorer = Scorer(cfg)
    assert scorer.reference > 0

    strong = scorer.score(Job(
        job_id="1", title="Senior Software Engineer",
        description="Java Java Java Spring Boot Kubernetes AWS " * 5))
    weak = scorer.score(Job(job_id="2", title="Analyst",
                            description="Some java somewhere"))
    for job in (strong, weak):
        assert 0.0 <= job.score <= MAX_SCORE, job.score
    assert strong.score > weak.score
    assert strong.score_raw > strong.score  # raw points kept alongside

    # An absurdly good match saturates at exactly 100, never above.
    huge = Scorer(cfg).score(Job(
        job_id="3", title="Senior Software Engineer",
        description=("java spring boot kubernetes aws " * 200)))
    assert huge.score == MAX_SCORE, huge.score

    # Rejected postings score zero, not a normalised value.
    rejected = Scorer(ScoringConfig(exclude_terms=["intern"],
                                    weighted_terms={"java": 10})).score(
        Job(job_id="4", title="Intern", description="java intern role"))
    assert rejected.score == 0.0 and rejected.rejected_reason

    # The reference depends only on the config, never on the result set.
    assert cfg.reference_score() == ScoringConfig(
        weighted_terms=dict(cfg.weighted_terms), title_terms=dict(cfg.title_terms),
        must_have_terms=["java"], recency_bonus=12, seniority_bonus=10,
    ).reference_score()
    assert ScoringConfig().reference_score() >= 1.0  # empty config cannot divide by zero

    # Remote detection: terse location fields are trusted, descriptions are not.
    remote_cases = [
        (("Remote",), 1), (("Remote - US",), 1), (("Anywhere",), 1),
        (("San Francisco, CA",), 0),
        (("London, UK", "Senior Engineer", "We are a remote-first company"), 1),
        (("London, UK", "Senior Engineer", "This role is fully remote"), 1),
        (("London, UK", "Senior Engineer", "Work from home allowed"), 1),
        (("Berlin", "Engineer", "You will work on remote sensing satellites"), 0),
        (("Berlin", "Engineer", "Our remote offices span Europe"), 0),
        (("Berlin", "Engineer", "This is not a remote position"), 0),
        (("Berlin", "Engineer", "On-site only, no remote work"), 0),
        (("Berlin", "Remote Software Engineer", ""), 1),
    ]
    for args, want in remote_cases:
        got = detect_remote(*args)
        assert got == want, f"detect_remote{args} -> {got}, want {want}"
    assert detect_remote("Berlin", "Eng", "", explicit=True) == 1     # source flag wins
    assert detect_remote("Remote", "Eng", "", explicit=False) == 1    # but False falls through
    assert detect_remote("Berlin", "Eng", "", workplace="Remote") == 1

    print("linkedin_job_scraper self-check OK "
          f"({len(exp_cases)} experience + {len(overlap_cases)} overlap cases)")
    return 0


def configure_logging(verbose: bool, quiet: bool) -> None:
    level = logging.INFO
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose, args.quiet)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        LOG.error("%s — run `init-config` first.", exc)
        return 1
    except json.JSONDecodeError as exc:
        LOG.error("Config is not valid JSON: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
