#!/usr/bin/env python3
"""
app.py
======

Web frontend for the job scraper.

    pip install -r requirements.txt
    python app.py            # -> http://127.0.0.1:5000

What it does:
  * upload a resume (PDF / DOCX / TXT / MD) -> a search profile, derived
    deterministically from a skill vocabulary (no AI, same as the scraper)
  * run LinkedIn + Greenhouse + Workday in one pass, with live progress
  * score, dedup and persist into the same SQLite DB the CLI uses
  * browse, filter and export the results

Single-user local tool: one run at a time, state in memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

from flask import Flask, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException

import app.resume as resume_mod
import app.sources as sources
from app.config import Config
from app.database import MongoJobStore
from app.s3_service import s3_service
from app.scraper import (
    AppConfig,
    DEFAULT_CONFIG,
    JobScraper,
    MongoJobStore,
    LinkedInClient,
    SearchProfile,
    Scorer,
    default_config,
    experience_overlaps,
    parse_experience,
    LOG,
    resolve_targets,
    source_of,
    write_exports,
)

# Board-style sources all share one call signature, so the run loop treats
# them as data instead of four near-identical blocks.
BOARD_SOURCES = {
    "greenhouse": sources.fetch_greenhouse,
    "ashby": sources.fetch_ashby,
    "lever": sources.fetch_lever,
    "workable": sources.fetch_workable,
    "remoteok": sources.fetch_remoteok,
}

app = Flask(__name__)

# Scanned / image-heavy resumes blow past a small cap easily, and the browser
# reports an over-limit upload as an unparseable failure, so keep it generous.
MAX_UPLOAD_MB = 25
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

CONFIG_PATH = os.environ.get("SCRAPER_CONFIG", DEFAULT_CONFIG)


@app.errorhandler(HTTPException)
def _http_error_as_json(exc: HTTPException):
    """Flask serves HTML error pages by default; the UI parses JSON.

    Without this, a 413 (resume too big) or a 404 reaches the browser as
    "<!doctype html>..." and every fetch in the page dies on JSON.parse.
    """
    if exc.code == 413:
        message = (f"That file is larger than the {MAX_UPLOAD_MB} MB limit. "
                   "Export a smaller PDF, or paste the text instead.")
    else:
        message = exc.description or exc.name
    return jsonify(error=message), exc.code or 500


@app.errorhandler(Exception)
def _crash_as_json(exc: Exception):
    """Any unhandled bug still answers JSON, so the UI can show the reason."""
    app.logger.exception("Unhandled error")
    return jsonify(error=f"{exc.__class__.__name__}: {exc}"), 500

# ponytail: one run at a time, in-process. This is a local single-user tool;
# swap for a task queue only if it ever needs to serve more than one person.
RUN: Dict[str, Any] = {
    "running": False,
    "log": [],
    "started": None,
    "finished": None,
    "found": 0,
    "kept": 0,
    "inserted": 0,
    "updated": 0,
    "error": None,
    "per_source": {},
}
RUN_LOCK = threading.Lock()


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    with RUN_LOCK:
        RUN["log"].append(f"{stamp}  {msg}")
        del RUN["log"][:-400]  # keep the tail bounded


def load_config() -> AppConfig:
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(default_config(), fh, indent=2, ensure_ascii=False)
    return AppConfig.load(CONFIG_PATH)


# --------------------------------------------------------------------------
# The run itself
# --------------------------------------------------------------------------

def do_run(profile_raw: Dict[str, Any], opts: Dict[str, Any]) -> None:
    cfg = load_config()
    profile = SearchProfile.from_dict(profile_raw)
    target = int(opts.get("target") or 100)
    use = set(opts.get("sources") or ["linkedin", "greenhouse", "workday"])
    since_days = opts.get("since_days")
    remote_only = bool(opts.get("remote_only"))
    if remote_only:
        # Worldwide by definition — a location filter would fight the point.
        profile.locations = []
        log("Remote-only: searching every board worldwide, ignoring locations")

    jobs: List[Any] = []
    per_source: Dict[str, int] = {}
    store = None

    def note(source: str, found: List[Any]) -> None:
        per_source[source] = len(found)
        with RUN_LOCK:
            RUN["per_source"] = dict(per_source)
            RUN["found"] = sum(per_source.values())

    try:
        # Connecting lives inside the try now — a bad/missing MONGO_URI used
        # to raise here, outside any handler, and leave the run stuck at
        # "running" forever with no error shown (the UI just froze at 0).
        store = MongoJobStore()
        # Remembered so the cron thread can replay this exact search
        # unattended — the only place a search profile is persisted.
        store.save_search_profile(profile_raw, opts)

        # -- Board sources first: they are fast and unmetered, and usually
        # clear the target alone, so a slow LinkedIn pass is never the blocker.
        for name, fetch in BOARD_SOURCES.items():
            if name not in use:
                continue
            log(f"{name.title()}: scanning boards…")
            found = fetch(
                terms=profile.scoring.must_have_terms,
                locations=profile.locations,
                since_days=since_days,
                remote_only=remote_only,
                progress=log,
            )
            for job in found:
                job.profile = profile.name
            jobs.extend(found)
            note(name, found)
            log(f"{name.title()}: {len(found)} postings")

        if "workday" in use:
            log("Workday: searching tenants…")
            found = sources.fetch_workday(
                terms=profile.scoring.must_have_terms,
                locations=profile.locations,
                since_days=since_days,
                per_search=int(opts.get("workday_per_search") or 60),
                remote_only=remote_only,
                progress=log,
            )
            for job in found:
                job.profile = profile.name
            jobs.extend(found)
            note("workday", found)
            log(f"Workday: {len(found)} postings")

        if "linkedin" in use:
            if remote_only:
                # LinkedIn can filter server-side (f_WT=2), which is far more
                # productive than fetching everything and discarding onsite roles.
                profile.workplace_types = ["remote"]
            log("LinkedIn: searching (rate limited, this is the slow one)…")
            client = LinkedInClient(cfg.http)
            scraper = JobScraper(client, store)
            collected: Dict[str, Any] = {}
            for keyword in profile.keywords:
                for location, geo_id in resolve_targets(profile):
                    for job in scraper.search(keyword, location, geo_id, profile):
                        collected.setdefault(job.job_id, job)
                    log(f"LinkedIn: {len(collected)} unique so far")
                    if len(collected) >= profile.max_results:
                        break
                if len(collected) >= profile.max_results:
                    break
            found = list(collected.values())
            if profile.fetch_details and found:
                log(f"LinkedIn: fetching {len(found)} descriptions…")
                found = scraper.enrich(found, cfg.http.concurrency)
            for job in found:
                job.profile = profile.name
            jobs.extend(found)
            note("linkedin", found)
            log(f"LinkedIn: {len(found)} postings  ({json.dumps(client.stats)})")

        # -- score + dedup across sources
        log(f"Scoring {len(jobs)} postings…")
        scorer = Scorer(profile.scoring)
        unique: Dict[str, Any] = {}
        for job in jobs:
            # One place where every source gets its experience requirement
            # parsed, so the filter behaves identically across all of them.
            if job.exp_min is None and job.exp_max is None:
                job.exp_min, job.exp_max = parse_experience(f"{job.title}. {job.description}")
            scorer.score(job)
            unique.setdefault(job.job_id, job)
        jobs = list(unique.values())
        kept = [j for j in jobs if not j.rejected_reason and j.score >= profile.scoring.min_score]
        if remote_only:
            # Board sources already filtered; this catches LinkedIn, whose
            # remote flag is only known once the description is fetched.
            before = len(kept)
            kept = [j for j in kept if j.is_remote]
            log(f"Remote-only: {len(kept)} of {before} are remote")

        # -- years-of-experience filter
        want_min, want_max = opts.get("exp_min"), opts.get("exp_max")
        include_unknown = opts.get("include_unknown_exp", True)
        if want_min is not None or want_max is not None:
            before = len(kept)
            stated = unstated = 0
            matched = []
            for job in kept:
                verdict = experience_overlaps(job.exp_min, job.exp_max, want_min, want_max)
                if verdict is None:
                    unstated += 1
                    if include_unknown:
                        matched.append(job)
                elif verdict:
                    stated += 1
                    matched.append(job)
            kept = matched
            log(f"Experience filter {want_min or 0}-{want_max or 'any'} yrs: "
                f"{len(kept)} of {before} kept "
                f"({stated} stated a matching range, {unstated} did not say"
                f"{', kept' if include_unknown else ', dropped'})")

        kept.sort(key=lambda j: j.score, reverse=True)
        with RUN_LOCK:
            RUN["kept"] = len(kept)
        log(f"{len(kept)} passed filters out of {len(jobs)} scraped")
        if len(kept) < target:
            log(f"NOTE: {len(kept)} matches < target {target}. Widen locations, "
                f"the year range, or the time window, or lower min_score.")

        inserted, updated = store.upsert_many(jobs)
        with RUN_LOCK:
            RUN["inserted"], RUN["updated"] = inserted, updated
        log(f"Database: {inserted} new, {updated} updated")

        if opts.get("export"):
            rows = store.query(min_score=profile.scoring.min_score,
                               exp_min=want_min, exp_max=want_max,
                               include_unknown_exp=include_unknown,
                               remote_only=remote_only)
            write_exports(rows, cfg.output_dir, "csv")
            log(f"Exported {len(rows)} rows to {cfg.output_dir}/")

    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        with RUN_LOCK:
            RUN["error"] = f"{exc.__class__.__name__}: {exc}"
        log(f"ERROR: {exc}")
        log(traceback.format_exc(limit=3))
    finally:
        if store is not None:
            store.close()
        with RUN_LOCK:
            RUN["running"] = False
            RUN["finished"] = time.time()
        log("Run finished.")


def _auto_run_loop() -> None:
    """The cron: replays the last manually-run search on a fixed interval.

    ponytail: one in-process thread with a plain sleep loop, matching the
    "single run, in memory" model the rest of this file already uses — swap
    for a real scheduler (APScheduler, system cron) only if this ever needs
    to survive across multiple worker processes.
    """
    while True:
        time.sleep(Config.AUTO_RUN_INTERVAL_SECONDS)
        try:
            with RUN_LOCK:
                if RUN["running"]:
                    continue
            store = MongoJobStore()
            try:
                saved = store.get_search_profile()
            finally:
                store.close()
            if not saved:
                continue  # nothing analysed yet — nothing to replay
            with RUN_LOCK:
                if RUN["running"]:
                    continue
                RUN.update(running=True, log=[], started=time.time(), finished=None,
                           found=0, kept=0, inserted=0, updated=0, error=None, per_source={})
            log("Cron: starting scheduled run")
            do_run(saved["profile"], saved["options"])
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            app.logger.exception("Auto-run tick failed")


# Started once at import time; gunicorn's default single worker (see
# railway.json) means one thread, matching the rest of this app's
# single-user, in-memory design.
threading.Thread(target=_auto_run_loop, daemon=True).start()


def _is_admin(req) -> bool:
    return bool(Config.ADMIN_TOKEN) and req.headers.get("X-Admin-Token") == Config.ADMIN_TOKEN


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.post("/api/resume")
def api_resume():
    upload = request.files.get("file")
    pasted = (request.form.get("text") or "").strip()

    try:
        if upload and upload.filename:
            file_bytes = upload.read()
            text = resume_mod.extract_text(file_bytes, upload.filename)
            origin = upload.filename
            
            # Save uploaded resume to S3
            import io
            file_obj = io.BytesIO(file_bytes)
            s3_service.upload_fileobj(file_obj, f"resumes/{upload.filename}")
        elif pasted:
            text, origin = pasted, "pasted text"
        else:
            return jsonify(error="Upload a resume file or paste your resume text."), 400

        locations = [s.strip() for s in (request.form.get("locations") or "").split(",") if s.strip()]
        profile = resume_mod.build_profile(
            text,
            name=request.form.get("name") or "resume",
            locations=locations,
            time_window=request.form.get("time_window") or "week",
        )
    except resume_mod.ResumeError as exc:
        return jsonify(error=str(exc)), 400

    return jsonify(profile=profile, source=origin)


@app.post("/api/run")
def api_run():
    with RUN_LOCK:
        if RUN["running"]:
            return jsonify(error="A run is already in progress."), 409
        RUN.update(running=True, log=[], started=time.time(), finished=None,
                   found=0, kept=0, inserted=0, updated=0, error=None, per_source={})

    body = request.get_json(silent=True) or {}
    profile = body.get("profile")
    if not profile:
        with RUN_LOCK:
            RUN["running"] = False
        return jsonify(error="No profile supplied — analyse a resume first."), 400

    threading.Thread(target=do_run, args=(profile, body.get("options") or {}),
                     daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/status")
def api_status():
    with RUN_LOCK:
        state = dict(RUN)
    state["elapsed"] = round((state["finished"] or time.time()) - state["started"], 1) \
        if state["started"] else 0
    if not _is_admin(request):
        # Everyone gets the summary pill (found/kept/elapsed); only an admin
        # sees the line-by-line scrape log and per-source error detail.
        state["log"] = []
        state["per_source"] = {}
        state["error"] = None
    return jsonify(state)


@app.get("/api/jobs")
def api_jobs():
    cfg = load_config()
    store = MongoJobStore()
    try:
        def as_int(name):
            raw = request.args.get(name)
            return int(raw) if raw not in (None, "") else None

        # Every filter runs in SQL, so `total` is the true match count rather
        # than whatever survived the page limit.
        filters = dict(
            min_score=float(request.args.get("min_score") or 0),
            profile=request.args.get("profile") or None,
            since_days=as_int("since_days"),
            include_rejected=request.args.get("include_rejected") == "1",
            exp_min=as_int("exp_min"),
            exp_max=as_int("exp_max"),
            include_unknown_exp=request.args.get("include_unknown_exp", "1") == "1",
            search=request.args.get("q") or None,
            source=request.args.get("source") or None,
            remote_only=request.args.get("remote") == "1",
            status=request.args.get("status") or None,
        )
        limit = as_int("limit")
        sort = request.args.get("sort") or "score"
        total = store.count(**filters)
        rows = store.query(limit=limit, sort=sort, **filters)

        out = []
        for row in rows:
            item = {k: row[k] for k in row.keys()}
            # Mongo hands back an ObjectId that json cannot encode.
            item.pop("_id", None)
            item["source"] = source_of(item["job_id"])
            item.pop("description", None)  # keep the payload small
            item.pop("score_breakdown", None)
            out.append(item)
        return jsonify(jobs=out, count=len(out), total=total)
    finally:
        store.close()




@app.get("/api/stats")
def api_stats():
    cfg = load_config()
    store = MongoJobStore()
    try:
        data = store.stats()
        by_source: Dict[str, int] = {}
        for (job_id,) in [(d["job_id"],) for d in store.jobs.find({}, {"job_id": 1})]:
            key = source_of(job_id)
            by_source[key] = by_source.get(key, 0) + 1
        data["by_source"] = by_source
        data["by_status"] = store.status_counts()
        data["database"] = cfg.database
        data["output_dir"] = cfg.output_dir
        return jsonify(data)
    finally:
        store.close()


@app.post("/api/export")
def api_export():
    body = request.get_json(silent=True) or {}
    cfg = load_config()
    store = MongoJobStore()
    try:
        rows = store.query(min_score=float(body.get("min_score") or 0),
                           limit=int(body["limit"]) if body.get("limit") else None)
        if not rows:
            return jsonify(error="Nothing to export."), 400
        before = set(os.listdir(cfg.output_dir)) if os.path.isdir(cfg.output_dir) else set()
        write_exports(rows, cfg.output_dir, body.get("format") or "csv")
        new = sorted(set(os.listdir(cfg.output_dir)) - before)
        uploaded = []
        for file in new:
            file_path = os.path.join(cfg.output_dir, file)
            if s3_service.upload_file(file_path):
                uploaded.append(file)
        return jsonify(ok=True, rows=len(rows), files=new, uploaded_to_s3=uploaded)
    finally:
        store.close()


@app.post("/api/prune")
def api_prune():
    body = request.get_json(silent=True) or {}
    cfg = load_config()
    store = MongoJobStore()
    try:
        removed = store.prune(int(body.get("days") or 45))
        return jsonify(ok=True, removed=removed)
    finally:
        store.close()


@app.get("/api/files")
def api_files():
    cfg = load_config()
    if not os.path.isdir(cfg.output_dir):
        return jsonify(files=[])
    files = sorted(os.listdir(cfg.output_dir), reverse=True)
    return jsonify(files=[{"name": f,
                           "size": os.path.getsize(os.path.join(cfg.output_dir, f))}
                          for f in files if not f.startswith(".")])


@app.get("/download/<path:name>")
def download(name: str):
    cfg = load_config()
    # send_from_directory rejects traversal outside the directory itself.
    return send_from_directory(os.path.abspath(cfg.output_dir), name, as_attachment=True)


@app.get("/apply/<path:job_id>")
def apply(job_id: str):
    """Send the user to the real application page.

    The table links here instead of embedding the destination, so the row does
    not carry a raw external URL. Prefers the source's apply link and falls
    back to the posting page when there isn't one.
    """
    cfg = load_config()
    store = MongoJobStore()
    try:
        row = store.jobs.find_one(
            {"job_id": job_id},
            {"title": 1, "company": 1, "apply_url": 1, "job_url": 1, "status": 1})

        if row is None:
            return jsonify(error=f"Unknown job {job_id}."), 404

        target = (row.get("apply_url") or "").strip() or (row.get("job_url") or "").strip()
        # Only ever bounce to a real http(s) page — never a javascript:/data:
        # URL that happened to land in a scraped field.
        if not target or urlparse(target).scheme not in ("http", "https"):
            return jsonify(
                error=f"No application link stored for “{row.get('title') or job_id}”"
                      f"{' at ' + row['company'] if row.get('company') else ''}. "
                      "Re-run the scrape for this source to pick one up."), 404

        # Deliberately does not mark the job applied. Opening a posting is not
        # applying to it — the page asks when you come back, and records what
        # you answer.
    finally:
        store.close()

    return redirect(target, code=302)


# --------------------------------------------------------------------------
# Apply kit — autofill, but you press Submit
# --------------------------------------------------------------------------
#
# This deliberately stops short of submitting applications for you:
#   * the final Submit stays a human decision, per application;
#   * Greenhouse/Lever/Ashby/Workday forms carry CAPTCHAs and account
#     sign-ups that a script has no business defeating;
#   * a mis-filled field submitted 500 times misrepresents you to 500
#     employers, and burns the accounts doing it.
# So: it fills what it can recognise, highlights what it filled, and leaves
# the rest — including the resume upload, which browsers forbid scripts from
# populating — to you.

APPLICANT_PATH = os.environ.get("APPLICANT_PROFILE", "applicant.json")

APPLICANT_FIELDS = ["first_name", "last_name", "full_name", "email", "phone",
                    "linkedin", "github", "twitter", "portfolio", "location",
                    # Workday splits the address into its own section.
                    "address_line_1", "address_line_2", "city", "state",
                    "postal_code", "country",
                    "current_company", "current_title",
                    "work_authorisation", "notice_period", "salary_expectation",
                    "heard_about_us", "skills"] + [
                    # Screening questions. Each holds "Yes", "No" or "" — blank
                    # means leave the question alone. They are your answers,
                    # set once here; nothing is inferred on your behalf.
                    "q_work_authorised", "q_needs_sponsorship", "q_non_compete",
                    "q_worked_here_before", "q_related_to_employee",
                    "q_government_employee", "q_outside_employment",
                    "q_negotiates_contracts", "q_over_18", "q_consent_terms"]

# Shown in the UI, in this order, with the question they answer.
SCREENING_QUESTIONS = [
    ("q_work_authorised", "Legally authorised to work in the job's country?"),
    ("q_needs_sponsorship", "Require visa sponsorship, now or in future?"),
    ("q_worked_here_before", "Ever worked for this company before?"),
    ("q_non_compete", "Bound by a non-compete / NDA / restrictive covenant?"),
    ("q_related_to_employee", "Related to, or close to, an employee there?"),
    ("q_government_employee", "You or a relative a government official?"),
    ("q_outside_employment", "Other employment you'd continue if hired?"),
    ("q_negotiates_contracts", "Your current role negotiates contracts with them?"),
    ("q_over_18", "Are you 18 or older?"),
    ("q_consent_terms", "Consent to the terms / privacy notice"),
]

# Repeating "Education" / "Work Experience" blocks.
EDUCATION_FIELDS = ["school", "degree", "field_of_study", "gpa", "start", "end"]
EXPERIENCE_FIELDS = ["company", "title", "location", "start", "end",
                     "current", "description"]
# Workday's "Websites 1..N", each block one URL — kept as its own list so the
# order and the count are yours, not whatever the four link fields happen to
# hold. Blank rows are dropped on save, which is what stops the form ending up
# with a spare empty block it then marks "URL is required".
WEBSITE_FIELDS = ["url"]
MAX_ENTRIES = 10


def _blank_applicant() -> Dict[str, Any]:
    return {**{k: "" for k in APPLICANT_FIELDS},
            "education": [], "experience": [], "websites": []}


def _clean_entries(raw: Any, fields: List[str]) -> List[Dict[str, str]]:
    """Keep well-formed entries only; drop rows the user left entirely blank."""
    out: List[Dict[str, str]] = []
    for item in (raw or [])[:MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        entry = {f: str(item.get(f) or "").strip() for f in fields}
        if any(entry.values()):
            out.append(entry)
    return out


PROFILE_LISTS = {"education": EDUCATION_FIELDS,
                 "experience": EXPERIENCE_FIELDS,
                 "websites": WEBSITE_FIELDS}


def _shape(saved: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce whatever was stored into the current field set.

    Accepts both shapes: the store's {"scalars": ..., "lists": ...} document
    and a flat dict such as applicant.json.
    """
    scalars = saved.get("scalars") if isinstance(saved.get("scalars"), dict) else saved
    lists = saved.get("lists") if isinstance(saved.get("lists"), dict) else saved
    data: Dict[str, Any] = {k: str(scalars.get(k) or "") for k in APPLICANT_FIELDS}
    for name, fields in PROFILE_LISTS.items():
        data[name] = _clean_entries(lists.get(name), fields)
    return data


def _read_json_file() -> Optional[Dict[str, Any]]:
    if not os.path.exists(APPLICANT_PATH):
        return None
    try:
        with open(APPLICANT_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def load_applicant() -> Dict[str, Any]:
    """Read the profile from the database.

    A profile still sitting in applicant.json is imported once, so an existing
    setup carries over without retyping. The file is left where it is as a
    backup — nothing deletes it.
    """
    cfg = load_config()
    store = MongoJobStore()
    try:
        if not store.has_profile():
            legacy = _read_json_file()
            if legacy:
                data = _shape(legacy)
                _write_profile(store, data)
                LOG.info("Imported profile from %s into %s",
                         APPLICANT_PATH, cfg.database)
                return data
            return _blank_applicant()
        return _shape(store.get_profile())
    finally:
        store.close()


def _write_profile(store: MongoJobStore, data: Dict[str, Any]) -> None:
    scalars = {k: data.get(k, "") for k in APPLICANT_FIELDS}
    lists = {name: data.get(name, []) for name in PROFILE_LISTS}
    store.set_profile(scalars, lists)


@app.get("/api/applicant")
def api_applicant_get():
    return jsonify(applicant=load_applicant())


@app.post("/api/applicant")
def api_applicant_save():
    body = request.get_json(silent=True) or {}
    data: Dict[str, Any] = {k: str(body.get(k) or "").strip() for k in APPLICANT_FIELDS}
    if data["full_name"] == "":
        data["full_name"] = " ".join(p for p in (data["first_name"],
                                                 data["last_name"]) if p)
    for name, fields in PROFILE_LISTS.items():
        data[name] = _clean_entries(body.get(name), fields)

    cfg = load_config()
    store = MongoJobStore()
    try:
        _write_profile(store, data)
    finally:
        store.close()
    return jsonify(ok=True, applicant=data, stored_in=os.path.abspath(cfg.database))


# Never autofill these, whatever else they look like. Equal-opportunity and
# demographic questions are legally sensitive, often optional, and a wrong
# guess here is worse than a blank — a location pattern once matched an
# "ethnicity" field because the EEO blurb mentioned relocation.
# Every alternative is word-bounded. Unanchored fragments matched inside
# ordinary words — "age" fired on "gradeAverage" and on "language", so a GPA
# box and a language question were both treated as demographic questions.
AUTOFILL_SKIP = (r"\bethnic\w*\b|\brace\b|\bracial\b|\bgender\b|\bsex\b"
                 r"|\bveteran\b|\bdisabilit\w*\b|\bdisabled\b"
                 r"|\bhispanic\b|\blatino\b|\bpronouns?\b|\blgbt\w*\b"
                 r"|\bsexual[\s_-]*orientation\b|\bdemographic\w*\b"
                 r"|\beeo\b|\bequal[\s_-]*opportunity\b|\bself[\s_-]*identif\w*\b"
                 r"|\bdate[\s_-]*of[\s_-]*birth\b|\bdob\b|\bage\b|\bage[\s_-]*range\b"
                 r"|\bsalary[\s_-]*history\b")

# Matched against a field's name / id / placeholder / aria-label / <label>.
# Word-boundaried so "relocation" does not read as "location".
AUTOFILL_RULES = [
    # Screening questions first: they are specific Yes/No questions, and the
    # generic work_authorisation rule below would otherwise claim them and put
    # free text ("Indian citizen") into a Yes/No dropdown.
    ("q_needs_sponsorship", r"require[\s_-]*sponsorship|need[\s_-]*sponsorship"
                            r"|sponsorship[\s_-]*for[\s_-]*an?[\s_-]*employment"
                            r"|\bvisa[\s_-]*sponsorship\b|will you.*require.*visa"),
    ("q_work_authorised", r"legally[\s_-]*authori[sz]ed|authori[sz]ed[\s_-]*to[\s_-]*work"
                          r"|\bright[\s_-]*to[\s_-]*work\b|eligible[\s_-]*to[\s_-]*work"),
    ("q_non_compete", r"non[\s_-]?compete|restrictive[\s_-]*covenant"
                      r"|non[\s_-]?disclosure[\s_-]*agreement"),
    ("q_worked_here_before", r"ever[\s_-]*worked[\s_-]*(for|at)|previously[\s_-]*worked"
                             r"|previously[\s_-]*employed|former[\s_-]*employee"
                             r"|worked[\s_-]*(for|at).*as[\s_-]*an?[\s_-]*employee"),
    ("q_related_to_employee", r"related[\s_-]*to[\s_-]*anyone|close[\s_-]*personal[\s_-]*relationship"
                              r"|\brelative\b.*\bemployee\b|family[\s_-]*member.*employee"),
    ("q_government_employee", r"government[\s_-]*(office|agency|official|department)"
                              r"|public[\s_-]*official"),
    ("q_outside_employment", r"outside[\s_-]*employment|outside[\s_-]*activit"),
    ("q_negotiates_contracts", r"negotiate.*contract|sign[\s_-]*commercial[\s_-]*contract"),
    ("q_over_18", r"\b18[\s_-]*(years)?[\s_-]*(or[\s_-]*older|or[\s_-]*above)\b"
                  r"|are[\s_-]*you[\s_-]*(at[\s_-]*least[\s_-]*)?18\b"),
    ("q_consent_terms", r"consent[\s_-]*to[\s_-]*the[\s_-]*above[\s_-]*terms"
                        r"|i[\s_-]*consent[\s_-]*to|agree[\s_-]*to[\s_-]*the[\s_-]*terms"
                        r"|acknowledge.*privacy[\s_-]*notice|accept[\s_-]*the[\s_-]*terms"),
    ("first_name", r"\bfirst[\s_-]*name\b|\bfname\b|\bgiven[\s_-]*name\b|given-name"),
    ("last_name", r"\blast[\s_-]*name\b|\blname\b|\bsurname\b|\bfamily[\s_-]*name\b|family-name"),
    ("full_name", r"^name$|\bfull[\s_-]*name\b|\byour[\s_-]*name\b|\bcandidate[\s_-]*name\b"),
    ("email", r"\be-?mail\b"),
    ("phone", r"\bphone\b|\bmobile\b|\btelephone\b|\btel\b|\bcontact[\s_-]*number\b"),
    ("linkedin", r"\blinked-?in\b"),
    ("github", r"\bgit-?hub\b"),
    ("portfolio", r"\bportfolio\b|\bpersonal[\s_-]*(site|website)\b|\bwebsite\b"),
    ("twitter", r"\btwitter\b|\bx\.com\b"),
    # Address parts come before the generic "location" rule so a City box gets
    # the city rather than "Bengaluru, India".
    ("address_line_2", r"\baddress[\s_-]*line[\s_-]*2\b|\baddress[\s_-]*2\b"),
    ("address_line_1", r"\baddress[\s_-]*line[\s_-]*1\b|\baddress[\s_-]*1\b"
                       r"|\bstreet[\s_-]*address\b|^address$"),
    ("city", r"\bcity\b|\btown\b|\bcity[\s_-]*name\b"),
    ("postal_code", r"\bpostal[\s_-]*code\b|\bpost[\s_-]*code\b|\bzip\b|\bpin[\s_-]*code\b"),
    ("state", r"\bstate\b|\bprovince\b|\bregion\b|\bcounty\b"),
    ("country", r"\bcountry\b|\bcountry[\s_-]*region\b|\bnation\b"),
    ("heard_about_us", r"how did you hear|\bhear about us\b|\bsource\b|\breferral[\s_-]*source\b"),
    ("skills", r"\bskills?\b"),
    ("location", r"(?<!re)\blocation\b|\bcurrent[\s_-]*address\b"
                 r"|where are you (based|located)"),
    ("current_company", r"\bcurrent[\s_-]*(company|employer)\b|^org$|\bpresent[\s_-]*employer\b"),
    ("current_title", r"\bcurrent[\s_-]*(title|role|position|job[\s_-]*title)\b"),
    ("work_authorisation", r"\bwork[\s_-]*(authoriz|authoris|permit)\w*\b"
                           r"|\bvisa\b|\bsponsorship\b|\bright to work\b"),
    ("notice_period", r"\bnotice[\s_-]*period\b|\bearliest.*start\b"),
    ("salary_expectation", r"\bsalary[\s_-]*expect\w*\b|\bexpected[\s_-]*(salary|compensation|pay)\b"
                           r"|\bcompensation[\s_-]*expect\w*\b|\bdesired[\s_-]*salary\b"
                           r"|\bcurrent[\s_-]*ctc\b|\bexpected[\s_-]*ctc\b"),
]

# Repeating Education / Experience blocks.
#
# Each rule is [list, field, pattern, needs_section]. "Start date" appears in
# both blocks, so those rules only fire when the surrounding heading says
# which section we are in; unambiguous labels like "School" do not need it.
AUTOFILL_LIST_RULES = [
    ["education", "school",
     r"\bschool\b|\buniversity\b|\bcollege\b|\binstitution\b|\balma[\s_-]*mater\b", False],
    ["education", "degree", r"\bdegree\b|\bqualification\b|\blevel of education\b", False],
    ["education", "field_of_study",
     r"\bdiscipline\b|\bfield[\s_-]*of[\s_-]*study\b|\bmajor\b|\bspecial[ie]sation\b|\bstream\b", False],
    ["education", "gpa", r"\bgpa\b|\boverall[\s_-]*result\b|\bgrade\b|\bpercentage\b", False],
    # Workday names these firstYearAttended / lastYearAttended.
    ["education", "start", r"\bfirst[\s_-]*year\b|\battended[\s_-]*from\b", False],
    ["education", "end", r"\blast[\s_-]*year\b|\bgraduat\w*\b|\bcompletion\b", False],
    # Anchored to the start of the visible label: a bare "to" once matched
    # "Type to Add Skills" and pasted a graduation year into it.
    ["education", "start", r"^from\b|^start\b|\bstart[\s_-]*date\b", True],
    ["education", "end", r"^to\b|^end\b|^graduat|\bend[\s_-]*date\b", True],
    ["experience", "company",
     r"\bcompany\b|\bemployer\b|\borgani[sz]ation\b|\bfirm\b", False],
    # Description first: "Role Description" contains "role", so the title rule
    # would otherwise claim it and paste the job title into the description.
    ["experience", "description",
     r"\brole[\s_-]*description\b|\bjob[\s_-]*description\b|\bdescription\b"
     r"|\bresponsibilit\w*\b|\bsummary\b|\bwhat you did\b", False],
    ["experience", "title",
     r"\bjob[\s_-]*title\b|\btitle\b|\brole\b|\bposition\b|\bdesignation\b", False],
    ["experience", "location", r"^location\b", True],
    ["experience", "start", r"^from\b|^start\b|\bstart[\s_-]*date\b", True],
    ["experience", "end", r"^to\b|^end\b|^until\b|\bend[\s_-]*date\b", True],
    # Workday's Websites section: repeating blocks each holding one URL.
    ["websites", "url", r"^url\b|^website\b|^link\b|\bweb[\s_-]*address\b", True],
]

# Headings that tell the script which repeating block a field belongs to.
SECTION_PATTERNS = {
    "education": r"education|academic|qualification",
    "experience": r"experience|employment|work[\s_-]*history",
    "websites": r"websites?|social[\s_-]*links?|online[\s_-]*presence",
}

# Order the links go into a "Websites" block list.
WEBSITE_ORDER = ["linkedin", "github", "portfolio", "twitter"]


def website_entries(applicant: Dict[str, Any]) -> List[Dict[str, str]]:
    """Your links as a repeating list, for Workday's Websites 1..N blocks.

    The explicit `websites` list comes first and in its own order; the four
    link fields then top it up, so an existing profile keeps working without
    retyping anything and a hand-ordered list is not silently reshuffled.
    """
    seen, out = set(), []
    rows = [str(r.get("url") or "") for r in (applicant.get("websites") or [])
            if isinstance(r, dict)]
    for url in rows + [str(applicant.get(k) or "") for k in WEBSITE_ORDER]:
        url = url.strip()
        if url and url.lower() not in seen:
            seen.add(url.lower())
            out.append({"url": url})
    return out


@app.get("/api/bookmarklet")
def api_bookmarklet():
    """A javascript: URL that fills the form on whatever page you're on.

    The data is baked into the bookmarklet rather than fetched from this
    server, so the portal page never talks to localhost and your details are
    not exposed to any site that happens to be open.
    """
    return _build_bookmarklet(inspect=False)


def _build_bookmarklet(inspect: bool = False):
    applicant = load_applicant()
    # Short build id so you can tell at a glance whether the bookmark in your
    # bar is the current one — the logic is baked in, so a stale bookmark
    # keeps running old, already-fixed behaviour.
    build = hashlib.sha1(
        (_AUTOFILL_JS + json.dumps(AUTOFILL_RULES) + json.dumps(AUTOFILL_LIST_RULES)
         + AUTOFILL_SKIP).encode("utf-8")).hexdigest()[:6]
    if not inspect:
        applicant = dict(applicant, websites=website_entries(applicant))
    payload = json.dumps({"data": {} if inspect else applicant,
                          "rules": AUTOFILL_RULES,
                          "skip": AUTOFILL_SKIP,
                          "listRules": AUTOFILL_LIST_RULES,
                          "sections": SECTION_PATTERNS,
                          "build": build,
                          "inspect": inspect}, separators=(",", ":"))
    script = _AUTOFILL_JS.replace("__PAYLOAD__", payload)
    # Collapse to one line; a bookmarklet cannot contain raw newlines. A "//"
    # comment would then swallow everything after it, so the source uses only
    # /* */ comments and this guard keeps it that way.
    assert "//" not in _AUTOFILL_JS, "line comments break the collapsed bookmarklet"
    compact = " ".join(line.strip() for line in script.splitlines() if line.strip())
    return jsonify(bookmarklet="javascript:" + quote(compact, safe=""),
                   build=build,
                   filled=sum(1 for v in applicant.values() if isinstance(v, str) and v))


# The autofill script lives in autofill.js so it can be read and linted as
# JavaScript rather than hidden inside a Python string.
AUTOFILL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autofill.js")

with open(AUTOFILL_PATH, "r", encoding="utf-8") as _fh:
    _AUTOFILL_JS = _fh.read()


@app.post("/api/jobs/<path:job_id>/status")
def api_set_status(job_id: str):
    """Change where a job stands — or clear it with an empty status."""
    body = request.get_json(silent=True) or {}
    cfg = load_config()
    store = MongoJobStore()
    try:
        try:
            ok = store.set_status(job_id, body.get("status", ""))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if not ok:
            return jsonify(error=f"Unknown job {job_id}."), 404
        return jsonify(ok=True, counts=store.status_counts())
    finally:
        store.close()


@app.get("/api/inspector")
def api_inspector():
    """Same script in report-only mode: describes the form, changes nothing.

    Field names and labels only — never values — so the report can be shared
    to diagnose a portal whose markup does not match expectations.
    """
    return _build_bookmarklet(inspect=True)


@app.get("/api/registry")
def api_registry():
    return jsonify(greenhouse=sources.GREENHOUSE_BOARDS,
                   workday=[t[0] for t in sources.WORKDAY_TENANTS])


@app.get("/api/meta")
def api_meta():
    """Everything the React app would otherwise hardcode twice."""
    return jsonify(max_upload_mb=MAX_UPLOAD_MB,
                   screening=SCREENING_QUESTIONS,
                   applicant_fields=APPLICANT_FIELDS,
                   lists=PROFILE_LISTS,
                   max_entries=MAX_ENTRIES)


# The React build, when there is one. Without it the old inline pages still
# serve, so the app runs straight from a clone with no npm step.
DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "frontend", "dist")


@app.get("/assets/<path:name>")
def spa_assets(name: str):
    return send_from_directory(os.path.join(DIST, "assets"), name)


def render(template: str) -> str:
    """Both pages share the stylesheet and the fetch helpers."""
    return (template
            .replace("__CSS__", _CSS)
            .replace("__COMMON_JS__", _COMMON_JS)
            .replace("__MAX_UPLOAD_MB__", str(MAX_UPLOAD_MB))
            .replace("__SCREENING__", json.dumps(SCREENING_QUESTIONS)))


@app.get("/")
def index():
    if os.path.exists(os.path.join(DIST, "index.html")):
        return send_from_directory(DIST, "index.html")
    return render(PAGE)


@app.get("/profile")
def profile_page():
    """All the applicant input fields, on their own page.

    They outgrew the sidebar — the form is taller than the results it sat
    next to — so the dashboard now links here instead.
    """
    if os.path.exists(os.path.join(DIST, "index.html")):
        return send_from_directory(DIST, "index.html")
    return render(PAGE_PROFILE)


# --------------------------------------------------------------------------
# UI — one page, no build step, no framework.
# --------------------------------------------------------------------------

# The two pages share one stylesheet and one set of fetch helpers.
# Same stylesheet the React app imports, so both stay in step.
_CSS = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "frontend", "src", "app.css"), encoding="utf-8").read()

_COMMON_JS = r"""
const MAX_UPLOAD_MB = __MAX_UPLOAD_MB__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// Every request goes through here. Reading the body as text first means an
// HTML error page or a dead server produces a readable message instead of
// "Unexpected token '<'" or an unhandled "Failed to fetch".
async function api(url, opts) {
  let r;
  try {
    r = await fetch(url, opts);
  } catch (e) {
    throw new Error("Cannot reach the server — is app.py still running? "
                    + "Restart it with: .venv/bin/python app.py");
  }
  const body = await r.text();
  let data = null;
  try { data = body ? JSON.parse(body) : null; } catch (e) { /* not JSON */ }
  if (!r.ok) {
    throw new Error((data && data.error)
                    || `Server returned HTTP ${r.status}. ${body.slice(0, 160)}`);
  }
  if (data === null) throw new Error("Server sent an empty or invalid response.");
  return data;
}
function debounce(fn, ms) { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; }

// A throw inside an async click handler is otherwise swallowed: the button
// simply does nothing. Say so instead.
window.addEventListener("unhandledrejection", e => {
  const box = $("rerr") || $("ap_err") || $("count");
  if (box) box.textContent = "Something went wrong: " + (e.reason && e.reason.message || e.reason);
  console.error(e.reason);
});
"""

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Scraper</title>
<style>__CSS__</style></head><body>

<header>
  <h1>Job Scraper</h1>
  <span class="sub">LinkedIn · Greenhouse · Ashby · Lever · Workable · Workday ·
    <a href="https://remoteok.com">Remote OK</a> —
    resume-driven, deterministic scoring, no AI</span>
</header>

<main>
<div class="grid">
  <!-- ============ LEFT: controls ============ -->
  <div>
    <div class="card">
      <h2>1 · Resume</h2>
      <input type="file" id="file" accept=".pdf,.docx,.txt,.md">
      <div class="hint">PDF, DOCX, TXT or MD — up to __MAX_UPLOAD_MB__ MB.
        A scanned PDF has no text layer; paste the text instead.</div>
      <label>…or paste the text</label>
      <textarea id="text" placeholder="Paste your resume here if you'd rather not upload a file"></textarea>
      <label>Locations (comma separated, blank = anywhere)</label>
      <input type="text" id="locations" placeholder="Bengaluru, India, Dublin, Remote">
      <div class="row" style="margin-top:14px">
        <button id="analyse">Analyse resume</button>
        <span id="rstatus" class="muted"></span>
      </div>
      <div id="rerr" class="err"></div>
      <div id="detected"></div>
    </div>

    <div class="card">
      <h2>2 · Sources &amp; filters</h2>
      <label class="chk"><input type="checkbox" id="s_gh" checked> Greenhouse <span class="muted">(42 boards)</span></label>
      <label class="chk"><input type="checkbox" id="s_ab" checked> Ashby <span class="muted">(42 boards)</span></label>
      <label class="chk"><input type="checkbox" id="s_lv" checked> Lever <span class="muted">(9 boards)</span></label>
      <label class="chk"><input type="checkbox" id="s_wk" checked> Workable <span class="muted">(20 accounts)</span></label>
      <label class="chk"><input type="checkbox" id="s_ro" checked>
        <a href="https://remoteok.com" target="_blank" rel="noopener">Remote OK</a>
        <span class="muted">(aggregator, all remote)</span></label>
      <label class="chk"><input type="checkbox" id="s_wd" checked> Workday <span class="muted">(9 employers)</span></label>
      <label class="chk"><input type="checkbox" id="s_li"> LinkedIn <span class="muted">(slow, rate limited)</span></label>

      <label class="chk" style="margin-top:14px"><input type="checkbox" id="remote_only">
        <b>Remote only — worldwide</b></label>
      <div class="hint" style="margin-top:2px">Ignores the locations above and keeps
        every remote role from every board.</div>

      <label>Years of experience</label>
      <div class="row">
        <input type="number" id="exp_min" placeholder="min" min="0" max="40" style="width:78px">
        <span class="muted">to</span>
        <input type="number" id="exp_max" placeholder="max" min="0" max="40" style="width:78px">
        <span class="muted" id="expnote" style="font-size:11.5px"></span>
      </div>
      <label class="chk" style="margin-top:8px"><input type="checkbox" id="exp_unknown" checked>
        Keep jobs that don't state a requirement</label>

      <label>Posted within (days, blank = any)</label>
      <input type="number" id="since" value="30" min="1" max="365">
      <label>Target number of matches</label>
      <input type="number" id="target" value="100" min="1">
      <label class="chk" style="margin-top:12px"><input type="checkbox" id="doexport" checked> Write CSV/JSON/Markdown when done</label>
      <div class="row" style="margin-top:14px">
        <button id="run" disabled>Fetch jobs</button>
        <span id="rundot" class="muted"></span>
      </div>
      <div class="hint">Greenhouse, Ashby, Lever, Workable and Workday are per-employer
        boards — "everywhere" means every board in the built-in registry.</div>
    </div>

    <div class="card">
      <h2>3 · Apply kit</h2>
      <div class="hint" style="margin:0 0 12px">Your details, education, experience
        and links — used by the autofill bookmarklet.</div>
      <div id="ap_summary" class="muted" style="font-size:12.5px">loading…</div>
      <div class="row" style="margin-top:13px">
        <a class="btnlink" href="/profile">Open Apply kit →</a>
      </div>
    </div>

    <div class="card">
      <h2>Database</h2>
      <div id="stats" class="muted">loading…</div>
      <div class="row" style="margin-top:13px">
        <button class="ghost" id="export">Export now</button>
        <button class="ghost" id="prune">Prune &gt;45d</button>
      </div>
      <div id="dberr" class="err"></div>
      <div id="dbok" class="ok"></div>
      <div id="files"></div>
    </div>
  </div>

  <!-- ============ RIGHT: progress + results ============ -->
  <div class="col-right">
    <div class="card progress-card">
      <details id="progress">
        <summary><h2 style="display:inline;margin:0">Progress</h2>
          <span id="rundot2" class="muted"></span></summary>
        <pre id="log">Idle. Analyse a resume, pick your sources, then hit “Fetch jobs”.</pre>
      </details>
    </div>

    <div class="card results-card">
      <h2>Results</h2>
      <div class="row">
        <input type="text" id="q" placeholder="filter title / company / location" style="flex:1;min-width:170px">
        <select id="fsource" style="width:auto">
          <option value="">all sources</option>
          <option value="linkedin">LinkedIn</option>
          <option value="greenhouse">Greenhouse</option>
          <option value="ashby">Ashby</option>
          <option value="lever">Lever</option>
          <option value="workable">Workable</option>
          <option value="remoteok">Remote OK</option>
          <option value="workday">Workday</option>
        </select>
        <input type="number" id="fmin" placeholder="min score" style="width:98px">
      </div>
      <div class="row" style="margin-top:8px">
        <span class="muted" style="font-size:12px">Years of experience</span>
        <input type="number" id="fexp_min" placeholder="min" min="0" max="40" style="width:72px">
        <span class="muted">to</span>
        <input type="number" id="fexp_max" placeholder="max" min="0" max="40" style="width:72px">
        <label class="chk" style="margin:0"><input type="checkbox" id="fexp_unknown" checked>
          <span class="muted" style="font-size:12px">incl. unstated</span></label>
        <label class="chk" style="margin:0"><input type="checkbox" id="fremote">
          <span class="muted" style="font-size:12px">remote only</span></label>
        <select id="fstatus" style="width:auto">
          <option value="">any status</option>
          <option value="none">not applied</option>
          <option value="any">tracked</option>
          <option value="applied">applied</option>
          <option value="interview">interview</option>
          <option value="offer">offer</option>
          <option value="rejected">rejected</option>
          <option value="saved">saved</option>
        </select>
        <select id="flimit" style="width:auto">
          <option value="300">show 300</option>
          <option value="1000">show 1000</option>
          <option value="5000">show all</option>
        </select>
        <button class="ghost" id="refresh">Refresh</button>
      </div>
      <div id="pending" class="askbar" style="display:none"></div>
      <div id="count" class="muted" style="margin:11px 0 8px"></div>
      <div class="scroll results-scroll">
        <table><thead><tr>
          <th class="c-score">Score</th><th class="c-title">Title</th>
          <th class="c-company">Company</th><th class="c-loc">Location</th>
          <th class="c-exp">Exp</th><th class="c-posted">Posted</th>
          <th class="c-source">Src</th><th class="c-link"></th>
        </tr></thead><tbody id="rows">
          <tr><td colspan="8" class="muted" style="padding:22px">No jobs yet.</td></tr>
        </tbody></table>
      </div>
    </div>
  </div>
</div>
</main>

<script>__COMMON_JS__
// Dashboard state: the analysed profile, and the status poll handle.
let profile = null, poll = null;

// ---------- 1. resume ----------
$("analyse").onclick = async () => {
  $("rerr").textContent = ""; $("rstatus").textContent = "";
  const f = $("file").files[0];

  // Check the size here rather than letting the server reject it mid-upload —
  // an aborted upload surfaces in the browser as an unhelpful fetch failure.
  if (f && f.size > MAX_UPLOAD_MB * 1024 * 1024) {
    $("rerr").textContent =
      `"${f.name}" is ${(f.size / 1048576).toFixed(1)} MB, over the ${MAX_UPLOAD_MB} MB limit. `
      + "Export a smaller PDF, or paste the text below instead.";
    return;
  }
  if (!f && !$("text").value.trim()) {
    $("rerr").textContent = "Choose a resume file, or paste your resume text below.";
    return;
  }

  $("rstatus").innerHTML = '<span class="spin"></span>reading…';
  $("analyse").disabled = true;
  const fd = new FormData();
  if (f) fd.append("file", f);
  fd.append("text", $("text").value);
  fd.append("locations", $("locations").value);
  try {
    const d = await api("/api/resume", {method:"POST", body:fd});
    profile = d.profile;
    $("rstatus").textContent = "read " + d.source;
    showDetected(d.profile);
    $("run").disabled = false;
  } catch (e) {
    $("rerr").textContent = e.message; $("rstatus").textContent = "";
  } finally { $("analyse").disabled = false; }
};

function showDetected(p) {
  const d = p._detected || {};
  const chips = (d.skills || []).slice(0, 22)
    .map(s => `<span class="chip"><b>${esc(s.term)}</b> ×${s.count}</span>`).join("");

  // Prefill the year band from the resume: 5 years detected -> search 4 to 6.
  if (d.exp_min != null) $("exp_min").value = d.exp_min;
  if (d.exp_max != null) $("exp_max").value = d.exp_max;
  $("fexp_min").value = d.exp_min != null ? d.exp_min : "";
  $("fexp_max").value = d.exp_max != null ? d.exp_max : "";
  $("expnote").textContent = d.years != null
    ? `from ${d.years} yrs on your resume`
    : "no years found — set these yourself";

  $("detected").innerHTML =
    `<div class="hint" style="margin-top:15px">
       Experience: <b>${d.years != null ? d.years + " years" : "not stated"}</b>
       ${d.exp_min != null ? `· searching <b>${d.exp_min}–${d.exp_max} yrs</b>` : ""} ·
       Must-have: <b>${esc((p.scoring.must_have_terms || []).join(", "))}</b>
     </div>
     <div class="chips">${chips}</div>
     <div class="hint">Search keywords: ${esc((p.keywords || []).join(" · "))}</div>`;
}

// ---------- 2. run ----------
$("run").onclick = async () => {
  if (!profile) {
    $("rerr").textContent =
      "Analyse a resume first — the search terms come from it.";
    $("rerr").scrollIntoView({block: "center"});
    return;
  }
  const sources = [];
  for (const [id, name] of [["s_gh","greenhouse"], ["s_ab","ashby"], ["s_lv","lever"],
                            ["s_wk","workable"], ["s_ro","remoteok"],
                            ["s_wd","workday"], ["s_li","linkedin"]]) {
    if ($(id).checked) sources.push(name);
  }
  if (!sources.length) { alert("Pick at least one source."); return; }

  const eMin = $("exp_min").value === "" ? null : +$("exp_min").value;
  const eMax = $("exp_max").value === "" ? null : +$("exp_max").value;
  if (eMin != null && eMax != null && eMin > eMax) {
    alert("Minimum years cannot be greater than maximum years."); return;
  }

  $("run").disabled = true; $("log").textContent = "";
  $("progress").open = true;      // unfold so the run is visible
  const body = {profile, options:{
    sources, target: +$("target").value || 100,
    since_days: $("since").value ? +$("since").value : null,
    remote_only: $("remote_only").checked,
    exp_min: eMin, exp_max: eMax,
    include_unknown_exp: $("exp_unknown").checked,
    export: $("doexport").checked}};
  try {
    await api("/api/run", {method:"POST", headers:{"Content-Type":"application/json"},
                           body: JSON.stringify(body)});
  } catch (e) {
    $("log").textContent = e.message; $("run").disabled = false; return;
  }
  poll = setInterval(status, 1000); status();
};

async function status() {
  let d;
  try {
    d = await api("/api/status");
  } catch (e) {
    // Stop polling instead of throwing once a second forever.
    if (poll) { clearInterval(poll); poll = null; }
    $("log").textContent += "\n" + e.message;
    $("run").disabled = !profile;
    return;
  }
  if (d.log && d.log.length) {
    const el = $("log"), stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = d.log.join("\n");
    if (stick) el.scrollTop = el.scrollHeight;
  }
  const src = Object.entries(d.per_source || {}).map(([k,v]) => `${k} ${v}`).join(" · ");
  const status = d.running
    ? `<span class="spin"></span>${d.found} found${src ? " — " + src : ""} · ${d.elapsed}s`
    : (d.finished ? `done — ${d.kept} matches in ${d.elapsed}s` : "");
  $("rundot").innerHTML = status;
  $("rundot2").innerHTML = status;   // visible while Progress is folded
  if (!d.running) {
    clearInterval(poll); poll = null;
    // Only usable once a resume has been analysed — this runs on load too.
    $("run").disabled = !profile;
    loadJobs(); loadStats(); loadFiles();
  } else if (d.found > 0) {
    // Refresh job list periodically during the run so results appear live.
    loadJobs();
  }
}

// ---------- results ----------
async function loadJobs() {
  const p = new URLSearchParams({limit: $("flimit").value});
  if ($("q").value.trim()) p.set("q", $("q").value.trim());
  if ($("fsource").value) p.set("source", $("fsource").value);
  if ($("fmin").value) p.set("min_score", $("fmin").value);
  if ($("fexp_min").value !== "") p.set("exp_min", $("fexp_min").value);
  if ($("fexp_max").value !== "") p.set("exp_max", $("fexp_max").value);
  p.set("include_unknown_exp", $("fexp_unknown").checked ? "1" : "0");
  if ($("fremote").checked) p.set("remote", "1");
  if ($("fstatus").value) p.set("status", $("fstatus").value);
  let d;
  try { d = await api("/api/jobs?" + p); }
  catch (e) { $("count").textContent = e.message; return; }

  const band = [$("fexp_min").value, $("fexp_max").value];
  // Say plainly when the table is only showing part of the matches — the
  // export writes all of them, and the two numbers disagreeing is confusing.
  $("count").textContent =
    (d.total > d.count ? `showing ${d.count} of ${d.total} matching jobs`
                       : `${d.total} job${d.total === 1 ? "" : "s"}`)
    + ($("fremote").checked ? "  ·  remote only" : "")
    + ((band[0] !== "" || band[1] !== "")
       ? `  ·  ${band[0] || 0}–${band[1] || "any"} yrs experience` : "");

  $("rows").innerHTML = d.jobs.length ? d.jobs.map(j => `
    <tr>
      <td class="c-score score">${(j.score ?? 0).toFixed(0)}</td>
      <td class="c-title">${esc(j.title)}
        <div class="rowsub">${esc([j.company, j.location].filter(Boolean).join(" · "))}</div>
        <div class="terms">${esc(shortTerms(j.matched_terms))}</div></td>
      <td class="c-company">${esc(j.company)}</td>
      <td class="c-loc muted">${esc(j.location)}${j.is_remote ? ' <span class="tag">remote</span>' : ""}</td>
      <td class="c-exp muted">${expLabel(j)}</td>
      <td class="c-posted muted">${esc(j.posted_date || "—")}</td>
      <td class="c-source"><span class="tag ${j.source}"><span class="full">${j.source}</span
        ><span class="abbr">${SOURCE_ABBR[j.source] || j.source.slice(0,2)}</span></span></td>
      <td class="c-link" data-status="${j.status || ""}">
        <a class="apply" href="/apply/${encodeURIComponent(j.job_id)}"
           target="_blank" rel="noopener noreferrer"
           data-job="${encodeURIComponent(j.job_id)}"
           data-title="${esc(j.title || "")}" data-company="${esc(j.company || "")}">Apply</a>
        <span class="statuschip" title="Click to change">${esc(j.status || "")}</span>
        <select class="statussel" data-job="${encodeURIComponent(j.job_id)}">
          ${["","applied","interview","offer","rejected","saved"].map(v =>
            `<option value="${v}"${(j.status||"") === v ? " selected" : ""}>${v || "not applied"}</option>`).join("")}
        </select></td>
    </tr>`).join("")
    : `<tr><td colspan="8" class="muted" style="padding:22px">No jobs match.</td></tr>`;
  bindStatusSelects();
}

// Rows are re-rendered wholesale, so the selects are rebound after each draw.
function bindStatusSelects() {
  document.querySelectorAll(".statussel").forEach(sel => {
    sel.dataset.prev = sel.value;
    paintRow(sel);
    sel.onchange = () => setStatus(sel);
  });
  document.querySelectorAll(".statuschip").forEach(chip => chip.onclick = () => {
    const cell = chip.closest("td");
    cell.classList.add("editing");
    cell.querySelector(".statussel").focus();
  });
  document.querySelectorAll("a.apply").forEach(a => a.onclick = () => {
    queueApply({id: decodeURIComponent(a.dataset.job),
                title: a.dataset.title, company: a.dataset.company});
  });
}

const SOURCE_ABBR = {greenhouse:"GH", ashby:"AB", lever:"LV", workable:"WK",
                     workday:"WD", linkedin:"LI", remoteok:"ROK"};

// Full term lists made rows several lines tall; show a handful and a count.
function shortTerms(terms) {
  const list = (terms || "").split(",").map(t => t.trim()).filter(Boolean);
  if (!list.length) return "";
  const shown = list.slice(0, 5).join(", ");
  return list.length > 5 ? `${shown} +${list.length - 5}` : shown;
}

// "5+", "3–5", "≤3", or "—" when the posting never says.
function expLabel(j) {
  const lo = j.exp_min, hi = j.exp_max;
  if (lo == null && hi == null) return '<span class="muted">—</span>';
  if (lo != null && hi != null) return `${lo}–${hi} yrs`;
  if (lo != null) return `${lo}+ yrs`;
  return `≤${hi} yrs`;
}

/* Opening a posting is not the same as applying to it, so the job is put on
   a short list and you are asked when you come back to this tab. The list
   lives in localStorage, so closing the page mid-application does not lose
   the question. */
const PENDING_KEY = "applyPending";
const loadPending = () => {
  try { return JSON.parse(localStorage.getItem(PENDING_KEY) || "[]"); }
  catch (e) { return []; }
};
const savePending = v => localStorage.setItem(PENDING_KEY, JSON.stringify(v));

function queueApply(job) {
  const p = loadPending().filter(x => x.id !== job.id);
  p.push(job);
  savePending(p);
}

function renderPending() {
  const host = $("pending"), queue = loadPending();
  if (!queue.length) { host.style.display = "none"; host.innerHTML = ""; return; }
  const job = queue[0];
  host.style.display = "";
  host.innerHTML =
    `<span>Did you apply to <b>${esc(job.title || job.id)}</b>`
    + `${job.company ? " at " + esc(job.company) : ""}?</span>
     <button class="ghost" data-ans="applied">Yes, applied</button>
     <button class="ghost" data-ans="">Not yet</button>
     ${queue.length > 1 ? `<span class="muted">+${queue.length - 1} more</span>` : ""}`;
  host.querySelectorAll("button").forEach(b => b.onclick = () => answerPending(job, b.dataset.ans));
}

async function answerPending(job, status) {
  savePending(loadPending().filter(x => x.id !== job.id));
  if (status) {
    try {
      await api(`/api/jobs/${encodeURIComponent(job.id)}/status`,
                {method:"POST", headers:{"Content-Type":"application/json"},
                 body: JSON.stringify({status})});
    } catch (e) { $("count").textContent = e.message; }
    loadJobs(); loadStats();
  }
  renderPending();
}

function paintRow(sel) {
  const tr = sel.closest("tr"), cell = sel.closest("td");
  if (tr) tr.classList.toggle("done", !!sel.value);
  sel.classList.toggle("set", !!sel.value);
  if (cell) {
    // Drives the CSS: a set status hides Apply and folds the picker away.
    cell.dataset.status = sel.value;
    cell.classList.remove("editing");
    const chip = cell.querySelector(".statuschip");
    if (chip) { chip.textContent = sel.value; chip.className = "statuschip " + sel.value; }
  }
}
async function setStatus(sel) {
  const prev = sel.dataset.prev || "";
  try {
    await api(`/api/jobs/${sel.dataset.job}/status`, {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({status: sel.value})});
    sel.dataset.prev = sel.value;
    paintRow(sel);
    loadStats();
  } catch (e) {
    sel.value = prev;                 // put it back rather than lie about it
    $("count").textContent = e.message;
  }
}

// Coming back from the portal is the moment to ask.
window.addEventListener("focus", renderPending);

$("refresh").onclick = loadJobs;
$("q").oninput = debounce(loadJobs, 300);
$("fsource").onchange = loadJobs;
$("fmin").oninput = debounce(loadJobs, 300);
$("fexp_min").oninput = debounce(loadJobs, 400);
$("fexp_max").oninput = debounce(loadJobs, 400);
$("fexp_unknown").onchange = loadJobs;
$("fremote").onchange = loadJobs;
$("fstatus").onchange = loadJobs;
$("flimit").onchange = loadJobs;
// Ticking remote-only for the scrape pre-selects it in the results too.
$("remote_only").onchange = () => { $("fremote").checked = $("remote_only").checked; loadJobs(); };
// ---------- database ----------
async function loadStats() {
  let d;
  try { d = await api("/api/stats"); }
  catch (e) { $("stats").textContent = e.message; return; }
  const bySrc = Object.entries(d.by_source || {})
    .map(([k,v]) => `<div class="stat"><span class="muted">${k}</span><b>${v}</b></div>`).join("");
  $("stats").innerHTML =
    `<div class="stat"><span class="muted">total</span><b>${d.total || 0}</b></div>
     <div class="stat"><span class="muted">rejected</span><b>${d.rejected || 0}</b></div>
     <div class="stat"><span class="muted">avg score</span><b>${d.avg_score ? d.avg_score.toFixed(1) : "—"}</b></div>
     <div class="stat"><span class="muted">best score</span><b>${d.max_score ? d.max_score.toFixed(1) : "—"}</b></div>
     ${bySrc}
     ${Object.entries(d.by_status || {}).filter(([k]) => k !== "none")
        .map(([k, v]) => `<div class="stat"><span class="muted">${k}</span><b>${v}</b></div>`).join("")}`;
}

async function loadFiles() {
  let d;
  try { d = await api("/api/files"); }
  catch (e) { return; }
  $("files").innerHTML = d.files.length
    ? `<div class="hint" style="margin-top:13px">Exports</div>` + d.files.slice(0, 20).map(f =>
        `<div class="stat"><a href="/download/${encodeURIComponent(f.name)}">${esc(f.name)}</a>
         <span class="muted">${(f.size/1024).toFixed(0)} KB</span></div>`).join("")
    : "";
}

$("export").onclick = async () => {
  $("dberr").textContent = ""; $("dbok").textContent = "";
  try {
    const d = await api("/api/export", {method:"POST", headers:{"Content-Type":"application/json"},
                                        body: JSON.stringify({format:"all"})});
    $("dbok").textContent = `Exported ${d.rows} rows.`; loadFiles();
  } catch (e) { $("dberr").textContent = e.message; }
};

$("prune").onclick = async () => {
  if (!confirm("Delete rows not seen in the last 45 days?")) return;
  $("dberr").textContent = ""; $("dbok").textContent = "";
  try {
    const d = await api("/api/prune", {method:"POST", headers:{"Content-Type":"application/json"},
                                       body: JSON.stringify({days:45})});
    $("dbok").textContent = `Removed ${d.removed} rows.`; loadStats(); loadJobs();
  } catch (e) { $("dberr").textContent = e.message; }
};


async function loadApplicantSummary() {
  try {
    const d = await api("/api/applicant");
    const a = d.applicant;
    const filled = Object.keys(a).filter(k => typeof a[k] === "string" && a[k]).length;
    $("ap_summary").innerHTML =
      `${filled} detail${filled === 1 ? "" : "s"} saved · `
      + `${a.experience.length} job${a.experience.length === 1 ? "" : "s"} · `
      + `${a.education.length} education`;
  } catch (e) { $("ap_summary").textContent = e.message; }
}

loadJobs(); loadStats(); loadFiles(); loadApplicantSummary(); status();
renderPending();
</script></body></html>
"""

PAGE_PROFILE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Apply kit — Job Scraper</title>
<style>__CSS__</style></head><body>

<header>
  <h1>Apply kit</h1>
  <span class="sub">Your details for autofilling application forms —
    saved locally to <code>applicant.json</code></span>
  <a class="btnlink ghostlink" href="/" style="margin-left:auto">← Back to jobs</a>
</header>

<main>
<div class="formgrid">
    <div class="card">
      <h2>Personal</h2>
      <div class="hint" style="margin:0 0 12px">Your details, saved locally to
        <code>applicant.json</code>. The bookmarklet fills these into whatever
        application form you're looking at. <b>It never submits</b> — you check
        the values and press Submit yourself.</div>
      <div class="grid2">
        <div><label>First name</label><input type="text" id="ap_first_name"></div>
        <div><label>Last name</label><input type="text" id="ap_last_name"></div>
      </div>
      <label>Email</label><input type="text" id="ap_email">
      <div class="grid2">
        <div><label>Phone</label><input type="text" id="ap_phone"></div>
        <div><label>Location</label><input type="text" id="ap_location"></div>
      </div>
      <label>LinkedIn</label><input type="text" id="ap_linkedin">
      <div class="grid2">
        <div><label>GitHub</label><input type="text" id="ap_github"></div>
        <div><label>Portfolio</label><input type="text" id="ap_portfolio"></div>
      </div>
      <div class="grid2">
        <div><label>Twitter / X</label><input type="text" id="ap_twitter"></div>
        <div><label>Current company</label><input type="text" id="ap_current_company"></div>
      </div>
      <div class="grid2">
        <div><label>Current title</label><input type="text" id="ap_current_title"></div>
        <div><label>Work authorisation</label><input type="text" id="ap_work_authorisation"></div>
      </div>
      <div class="grid2">
        <div><label>Notice period</label><input type="text" id="ap_notice_period"></div>
        <div><label>Salary expectation</label><input type="text" id="ap_salary_expectation"></div>
      </div>
      <input type="hidden" id="ap_full_name">

      <div style="margin-top:20px;border-top:1px solid var(--line);padding-top:14px">
        <h2 style="margin-bottom:4px">Address</h2>
        <div class="hint" style="margin:0 0 10px">Workday asks for these separately —
          City on its own, not "Bengaluru, India".</div>
        <label>Address line 1</label><input type="text" id="ap_address_line_1">
        <label>Address line 2</label><input type="text" id="ap_address_line_2">
        <div class="grid2">
          <div><label>City</label><input type="text" id="ap_city"></div>
          <div><label>State / Province</label><input type="text" id="ap_state"></div>
        </div>
        <div class="grid2">
          <div><label>Postal code</label><input type="text" id="ap_postal_code"></div>
          <div><label>Country</label><input type="text" id="ap_country"></div>
        </div>
      </div>

      <div style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
        <h2 style="margin-bottom:4px">Extras</h2>
        <label>How did you hear about us?</label>
        <input type="text" id="ap_heard_about_us" placeholder="LinkedIn">
        <label>Skills (comma separated)</label>
        <input type="text" id="ap_skills" placeholder="Python, Django, AWS">
      </div>

      <div style="margin-top:20px;border-top:1px solid var(--line);padding-top:14px">
        <h2 style="margin-bottom:4px">Education</h2>
        <div class="hint" style="margin:0 0 10px">For the
          <i>Education (Optional)</i> block on Greenhouse / Workable / Lever forms.</div>
        <div id="ap_education"></div>
        <button class="ghost" id="ap_add_edu" style="margin-top:8px">+ Add education</button>
      </div>

      <div style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
        <h2 style="margin-bottom:4px">Experience</h2>
        <div class="hint" style="margin:0 0 10px">Most recent first — entry 1 fills
          the first block on the form, entry 2 the second.</div>
        <div id="ap_experience"></div>
        <button class="ghost" id="ap_add_exp" style="margin-top:8px">+ Add experience</button>
      </div>

      <div style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
        <h2 style="margin-bottom:4px">Screening questions</h2>
        <div class="hint" style="margin:0 0 10px">The Yes/No questions these
          forms ask — <i>Application Questions</i>, <i>Conflict of Interest</i>,
          <i>Voluntary Disclosures</i>. Leave one blank and it is left alone on
          the form. Nothing here is guessed for you.</div>
        <div id="ap_screening"></div>
        <div class="hint">Gender, race, veteran and disability questions are
          never answered, whatever you put here.</div>
      </div>

      <div style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
        <h2 style="margin-bottom:4px">Websites</h2>
        <div class="hint" style="margin:0 0 10px">Fills Workday's
          <i>Websites 1..N</i> blocks, one URL each, in this order. Leave it
          empty and your LinkedIn / GitHub / Portfolio / Twitter above are used
          instead.</div>
        <div id="ap_websites"></div>
        <button class="ghost" id="ap_add_web" style="margin-top:8px">+ Add website</button>
      </div>

      <div class="row" style="margin-top:16px">
        <button id="ap_save">Save details</button>
        <span id="ap_status" class="muted"></span>
      </div>
      <div id="ap_err" class="err"></div>
      <div id="ap_drag" style="margin-top:14px"></div>
    </div>

</div>
</main>

<script>__COMMON_JS__
// ---------- apply kit ----------
const SCREENING = __SCREENING__;

const AP_FIELDS = ["first_name","last_name","email","phone","location","linkedin",
                   "github","twitter","portfolio","current_company","current_title",
                   "work_authorisation","notice_period","salary_expectation",
                   "address_line_1","address_line_2","city","state","postal_code",
                   "country","heard_about_us","skills"]
                  .concat(SCREENING.map(q => q[0]));

const EDU_COLS = [["school","School / University"], ["degree","Degree"],
                  ["field_of_study","Field of study"], ["gpa","GPA / result"],
                  ["start","From (YYYY)"], ["end","To (YYYY)"]];
const EXP_COLS = [["company","Company"], ["title","Job title"],
                  ["location","Location"], ["start","From (MM/YYYY)"],
                  ["end","To (MM/YYYY or Present)"],
                  ["description","Role description", "area"]];
const WEB_COLS = [["url","URL", true]];
/* One place that knows a list's columns and heading, so a new list is two
   lines here instead of a ternary in every handler. */
const AP_COLS = {education: EDU_COLS, experience: EXP_COLS, websites: WEB_COLS};
const AP_TITLES = {education: "Education", experience: "Work experience",
                   websites: "Websites"};

function entryRow(listName, cols, idx, values) {
  const title = AP_TITLES[listName] || listName;
  const inputs = cols.map(([key, label, full]) =>
    `<div${full ? ' class="span2"' : ""}><label>${esc(label)}</label>
       ${full === "area"
          ? `<textarea data-list="${listName}" data-idx="${idx}" data-key="${key}"
                       rows="2">${esc(values[key] || "")}</textarea>`
          : `<input type="text" data-list="${listName}" data-idx="${idx}" data-key="${key}"
                    value="${esc(values[key] || "")}">`}
     </div>`).join("");
  return `<div class="entry">
      <div class="entry-head">
        <b>${esc(title)} ${idx + 1}</b>
        <a href="#" data-remove="${listName}:${idx}">Remove</a>
      </div>
      <div class="grid2">${inputs}</div>
    </div>`;
}

function renderEntries(listName, cols, entries) {
  const host = $("ap_" + listName);
  host.innerHTML = entries.length
    ? entries.map((v, i) => entryRow(listName, cols, i, v)).join("")
    : `<div class="hint">None yet.</div>`;
  host.querySelectorAll("[data-remove]").forEach(a => a.onclick = ev => {
    ev.preventDefault();
    const [ln, i] = a.dataset.remove.split(":");
    AP_LISTS[ln].splice(+i, 1);
    renderEntries(ln, AP_COLS[ln], AP_LISTS[ln]);
  });
  host.querySelectorAll("[data-key]").forEach(inp => inp.oninput = () => {
    AP_LISTS[inp.dataset.list][+inp.dataset.idx][inp.dataset.key] = inp.value;
  });
}

const AP_LISTS = {education: [], experience: [], websites: []};

function addRow(listName) {
  if (AP_LISTS[listName].length >= 10) return;
  AP_LISTS[listName].push(Object.fromEntries(AP_COLS[listName].map(([k]) => [k, ""])));
  renderEntries(listName, AP_COLS[listName], AP_LISTS[listName]);
}
$("ap_add_edu").onclick = () => addRow("education");
$("ap_add_exp").onclick = () => addRow("experience");
$("ap_add_web").onclick = () => addRow("websites");

/* Pull every list out of one response and paint them all. */
function paintLists(applicant) {
  for (const ln in AP_LISTS) {
    AP_LISTS[ln] = applicant[ln] || [];
    renderEntries(ln, AP_COLS[ln], AP_LISTS[ln]);
  }
}


function renderScreening(values) {
  $("ap_screening").innerHTML = SCREENING.map(([key, label]) => `
    <div class="qrow">
      <label for="ap_${key}">${esc(label)}</label>
      <select id="ap_${key}">
        <option value=""${!values[key] ? " selected" : ""}>— leave blank —</option>
        <option value="Yes"${values[key] === "Yes" ? " selected" : ""}>Yes</option>
        <option value="No"${values[key] === "No" ? " selected" : ""}>No</option>
      </select>
    </div>`).join("");
}

async function loadApplicant() {
  let d;
  try { d = await api("/api/applicant"); }
  catch (e) { $("ap_err").textContent = e.message; return; }
  renderScreening(d.applicant);
  for (const f of AP_FIELDS) if ($("ap_" + f)) $("ap_" + f).value = d.applicant[f] || "";
  paintLists(d.applicant);
  showBookmarklet();
}

$("ap_save").onclick = async () => {
  $("ap_err").textContent = ""; $("ap_status").textContent = "";
  const body = {education: AP_LISTS.education, experience: AP_LISTS.experience,
                websites: AP_LISTS.websites};
  for (const f of AP_FIELDS) body[f] = $("ap_" + f).value.trim();
  try {
    const d = await api("/api/applicant", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    paintLists(d.applicant);
    $("ap_status").textContent = "saved";
    showBookmarklet();
  } catch (e) { $("ap_err").textContent = e.message; }
};

async function showBookmarklet() {
  let d;
  try { d = await api("/api/bookmarklet"); }
  catch (e) { return; }
  if (!d.filled) {
    $("ap_drag").innerHTML =
      `<div class="hint">Fill in your details and save to get the autofill button.</div>`;
    return;
  }
  // Rebuilt on every save so the button always carries the current details.
  const a = document.createElement("a");
  a.className = "bm";
  a.textContent = "↧ Fill this application";
  a.href = d.bookmarklet;
  a.title = "Drag me to your bookmarks bar";
  a.onclick = e => { e.preventDefault();
    alert("Drag this button to your bookmarks bar. Then, on any application "
        + "page, click the bookmark to fill your details in."); };
  $("ap_drag").innerHTML = `<div class="hint" style="margin-bottom:7px">
      <b>Drag this to your bookmarks bar</b>, then click it on any application
      page to fill the form. Resume uploads and Submit stay yours.<br>
      <b style="color:var(--warn)">Replace the old bookmark whenever you save</b>
      — your details and the matching logic are baked into it. It reports
      build <code>${esc(d.build || "?")}</code> when it runs; if the popup shows
      a different build, you clicked a stale bookmark.</div>`;
  $("ap_drag").appendChild(a);

  // Re-dragging is fiddly, so offer the URL for pasting over the old bookmark.
  const copy = document.createElement("button");
  copy.className = "ghost";
  copy.style.marginLeft = "8px";
  copy.textContent = "Copy link";
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(d.bookmarklet);
      copy.textContent = "Copied — paste over the old bookmark's URL";
      setTimeout(() => { copy.textContent = "Copy link"; }, 4000);
    } catch (e) { copy.textContent = "Copy failed — drag the button instead"; }
  };
  $("ap_drag").appendChild(copy);

  // Diagnostic: reports what a form looks like without touching or reading it.
  let insp;
  try { insp = await api("/api/inspector"); } catch (e) { return; }
  const b = document.createElement("a");
  b.className = "bm";
  b.style.cssText = "background:var(--panel2);color:var(--fg);border:1px solid var(--line);margin-left:8px";
  b.textContent = "⌕ Inspect form";
  b.href = insp.bookmarklet;
  b.onclick = e => { e.preventDefault();
    alert("Drag this to your bookmarks bar too. On a form that fills wrongly, "
        + "click it: it copies a description of the form's fields (names and "
        + "labels only — no values) so the mismatch can be diagnosed."); };
  $("ap_drag").appendChild(b);
}


loadApplicant();
</script></body></html>
"""



def self_check() -> int:
    """Offline checks for the autofill matcher. No network, no browser."""
    import re as _re

    skip = _re.compile(AUTOFILL_SKIP, _re.I)
    rules = [(k, _re.compile(p, _re.I)) for k, p in AUTOFILL_RULES]

    def match(haystack: str) -> Optional[str]:
        """What the bookmarklet would put in a field with this label text."""
        hay = haystack.lower()
        if skip.search(hay):
            return None
        for key, pattern in rules:
            if pattern.search(hay):
                return key
        return None

    # Demographic / EEO fields are never touched. This is the bug that shipped
    # a location into an "ethnicity" box during testing.
    for label in ["hispanic_ethnicity", "Are you Hispanic/Latino?", "Gender",
                  "Race", "Veteran status", "Disability status",
                  "Voluntary Self-Identification", "EEO survey",
                  "Date of birth", "What is your salary history?",
                  "What is your age range?"]:
        assert match(label) is None, f"{label!r} must not be autofilled"

    # …but ordinary fields that merely contain those letters must survive.
    # "gradeAverage" and "language" both end in "age".
    for label in ["gradeAverage", "Overall Result (GPA)", "Language",
                  "Preferred language", "Package details", "Manage team size"]:
        assert not skip.search(label.lower()), f"{label!r} wrongly skipped as demographic"

    # Ordinary identity fields are.
    expected = {
        "First Name*": "first_name",
        "given-name": "first_name",
        "Last Name*": "last_name",
        "family-name": "last_name",
        "Email*": "email",
        "Phone": "phone",
        "LinkedIn Profile": "linkedin",
        "GitHub URL": "github",
        "Website": "portfolio",
        "Portfolio": "portfolio",
        "Current location": "location",
        "Work authorization status": "work_authorisation",
        # Screening questions win over the generic rule: these are Yes/No
        # dropdowns, and free text would simply not match any option.
        "Do you require visa sponsorship?": "q_needs_sponsorship",
        "Do you now, or will you in the future, require sponsorship for an "
        "employment visa in the country where the position is located?": "q_needs_sponsorship",
        "Are you NOW legally authorized to work in the country where the "
        "position you are applying for is located?": "q_work_authorised",
        "Have you entered into any restrictive covenant, non-compete agreement, "
        "or non-disclosure agreement?": "q_non_compete",
        "Have you ever worked for Mastercard?": "q_worked_here_before",
        "Are you related to anyone who is an employee of a government office "
        "or agency that has oversight over Mastercard?": "q_related_to_employee",
        "Are you currently engaged in any outside employment or activity that "
        "you would like to continue if you are hired?": "q_outside_employment",
        "Except where consent is not required by applicable privacy law, I "
        "consent to the above terms": "q_consent_terms",
        "Notice period": "notice_period",
        "Expected salary": "salary_expectation",
        # Workday's Address section — each part on its own, so a City box does
        # not receive the whole "Bengaluru, India" location string.
        "Address Line 1": "address_line_1",
        "Address Line 2": "address_line_2",
        "City": "city",
        "addressSection city": "city",
        "Postal Code": "postal_code",
        "Zip": "postal_code",
        "State": "state",
        "Country": "country",
        "How Did You Hear About Us?": "heard_about_us",
        "Type to Add Skills": "skills",
    }
    for label, want in expected.items():
        got = match(label)
        assert got == want, f"{label!r} -> {got}, want {want}"

    # "relocation" must not read as "location".
    assert match("Are you willing to relocate?") is None, "relocation matched location"

    # -- repeating Education / Experience blocks -----------------------------
    list_rules = [(ln, f, _re.compile(p, _re.I), need)
                  for ln, f, p, need in AUTOFILL_LIST_RULES]

    def list_match(haystack: str, section: str = "",
                   label: Optional[str] = None) -> Optional[str]:
        """What the bookmarklet would fill, given the surrounding section.

        Anchored rules (needs_section) are matched against the visible label
        only, mirroring the bookmarklet.
        """
        hay = haystack.lower()
        lab = (label if label is not None else haystack).lower()
        if skip.search(hay):
            return None
        # Inside a known section the entry's own rules win over the profile.
        if section:
            for name, field, pattern, needs in list_rules:
                if name != section:
                    continue
                if pattern.search(lab if needs else hay):
                    return f"{name}.{field}"
            return None
        if match(haystack):
            return None  # a scalar rule claims it first
        for name, field, pattern, needs in list_rules:
            if needs or not pattern.search(hay):
                continue
            return f"{name}.{field}"
        return None

    assert list_match("School") == "education.school"
    assert list_match("University / College") == "education.school"
    assert list_match("Degree") == "education.degree"
    assert list_match("Discipline") == "education.field_of_study"
    assert list_match("Field of Study") == "education.field_of_study"
    assert list_match("Company") == "experience.company"
    assert list_match("Job Title") == "experience.title"
    assert list_match("School or University") == "education.school"
    assert list_match("Overall Result (GPA)") == "education.gpa"

    # "Role Description" contains "role"; the title rule used to claim it and
    # paste the job title into the description box.
    assert list_match("Role Description") == "experience.description"
    assert list_match("Role Description", "experience") == "experience.description"
    assert list_match("Job Description", "experience") == "experience.description"
    assert list_match("Job Title", "experience") == "experience.title"
    assert list_match("Role", "experience") == "experience.title"

    # "Start date" is ambiguous, so it only fills with a section to anchor it.
    assert list_match("Start date") is None, "ambiguous start filled without a section"
    assert list_match("Start date", "education") == "education.start"
    assert list_match("Start date", "experience") == "experience.start"
    assert list_match("End date", "education") == "education.end"
    assert list_match("End date", "experience") == "experience.end"

    # An unambiguous label in the wrong section must not cross over.
    assert list_match("School", "experience") is None, "school leaked into experience"

    # Workday's From / To boxes.
    assert list_match("From", "education") == "education.start"
    assert list_match("To (Actual or Expected)", "education") == "education.end"
    assert list_match("From", "experience") == "experience.start"
    assert list_match("Location", "experience") == "experience.location"

    # Workday's Websites 1..N blocks, each holding a single URL.
    assert list_match("URL", "websites") == "websites.url"
    assert list_match("Website", "websites") == "websites.url"
    assert list_match("URL", "experience") is None, "a job block took a website URL"
    links = website_entries({"linkedin": "https://li", "github": "https://gh",
                             "portfolio": "", "twitter": "https://x"})
    assert [w["url"] for w in links] == ["https://li", "https://gh", "https://x"], links
    assert website_entries({"linkedin": "https://a", "github": "https://a"}) == \
        [{"url": "https://a"}], "duplicate links should collapse"
    assert website_entries({}) == []
    # The explicit list leads, in its own order, and the link fields top it up
    # without repeating a URL that is already in the list.
    explicit = website_entries({"websites": [{"url": "https://port"}, {"url": ""},
                                             {"url": "https://gh"}],
                                "github": "https://gh", "linkedin": "https://li"})
    assert [w["url"] for w in explicit] == \
        ["https://port", "https://gh", "https://li"], explicit
    assert _clean_entries([{"url": " "}, {"url": "https://a"}], WEBSITE_FIELDS) == \
        [{"url": "https://a"}], "a blank URL row would become a required-field error"

    # Regression: a bare "to" used to match "Type to Add Skills" and paste a
    # graduation year into the skills box.
    assert list_match("Type to Add Skills", "education") is None, "skills box took a date"
    assert list_match("Type to Add Skills") is None
    assert match("Type to Add Skills") == "skills"
    # …and these must still not be read as dates.
    for label in ["Tell us about yourself", "Torch experience", "Custom question"]:
        got = list_match(label, "experience")
        assert got is None, f"{label!r} matched {got}"

    # Section headings are recognised.
    for heading, want in [("Education (Optional)", "education"),
                          ("Experience (Optional)", "experience"),
                          ("Employment history", "experience"),
                          ("Academic background", "education")]:
        hits = [s for s, p in SECTION_PATTERNS.items() if _re.search(p, heading, _re.I)]
        assert hits == [want], f"{heading!r} -> {hits}, want {want}"

    # Entries round-trip and blank rows are dropped.
    cleaned = _clean_entries(
        [{"school": "IIT", "degree": "B.Tech"}, {}, {"school": ""}], EDUCATION_FIELDS)
    assert len(cleaned) == 1 and cleaned[0]["school"] == "IIT", cleaned
    assert set(cleaned[0]) == set(EDUCATION_FIELDS)
    assert len(_clean_entries([{"company": str(i)} for i in range(MAX_ENTRIES + 5)],
                              EXPERIENCE_FIELDS)) == MAX_ENTRIES

    # A bookmarklet is one line, so a // comment would swallow the rest of it.
    assert "//" not in _AUTOFILL_JS, "line comment would break the bookmarklet"

    # The saved profile round-trips through the store. It runs against a
    # scratch database that is dropped afterwards, so the real one is never
    # touched; skipped entirely when MongoDB is not reachable.
    scratch = Config.MONGO_URI.rsplit("/", 1)[0] + "/job_scraper_selfcheck"
    try:
        probe_store = MongoJobStore(scratch)
        probe_store.client.admin.command("ping")
    except Exception as exc:
        print(f"  (profile store check skipped — MongoDB unreachable: "
              f"{exc.__class__.__name__})")
    else:
        try:
            probe_store.db.profiles.delete_many({})
            probe_store.set_profile(
                {"first_name": "A", "email": "a@b.c"},
                {"experience": [{"company": "One"}, {"company": "Two"}],
                 "education": [], "websites": [{"url": "https://x"}]})
            assert probe_store.has_profile()
            raw = probe_store.get_profile()
            assert [e["company"] for e in raw["lists"]["experience"]] == ["One", "Two"], raw
            assert raw["lists"]["websites"] == [{"url": "https://x"}], raw
            assert raw["scalars"]["first_name"] == "A"
            # Saving again replaces what was there rather than merging into it.
            probe_store.set_profile({"first_name": "B"},
                                    {"experience": [], "education": [], "websites": []})
            again = probe_store.get_profile()
            assert again["scalars"] == {"first_name": "B"}, again
            assert again["lists"]["experience"] == [], again
        finally:
            probe_store.client.drop_database(probe_store.db.name)
            probe_store.close()

    applicant = load_applicant()
    assert set(applicant) == set(APPLICANT_FIELDS) | {"education", "experience",
                                                      "websites"}
    assert isinstance(applicant["education"], list)
    assert isinstance(applicant["experience"], list)
    assert isinstance(applicant["websites"], list)
    # The Websites editor and the save handler must agree on the list names.
    for name in ("education", "experience", "websites"):
        assert f'id="ap_{name}"' in PAGE_PROFILE, f"no editor for {name}"
        assert f"{name}: AP_LISTS.{name}" in PAGE_PROFILE or \
            f"{name}: []" in PAGE_PROFILE, f"{name} never saved"

    print(f"app.py self-check OK ({len(expected)} autofill rules, "
          f"{len(AUTOFILL_SKIP.split('|'))} skip patterns)")
    return 0


if __name__ == "__main__" and "--self-check" in sys.argv:
    raise SystemExit(self_check())

if __name__ == "__main__":
    # 5000 is taken by AirPlay Receiver on macOS, so default one above it.
    port = int(os.environ.get("PORT", 5001))
    print(f"\n  Job scraper UI  ->  http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
