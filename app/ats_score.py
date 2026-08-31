#!/usr/bin/env python3
"""
ats_score.py
============

A second, deeper scoring pass on top of app.scraper.Scorer's free keyword
matching. Sends the resume text and a job's title/description to Claude and
asks for a 0-100 fit score plus a one-line reason.

Only ever called on a small top-N slice of a run's kept jobs (see
Config.ATS_TOP_N in do_run) — Claude bills per token, so every scraped job
is never sent, only the ones the free scorer already ranked highest.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, List, Optional

from anthropic import Anthropic, APIStatusError

from app.config import Config

LOG = logging.getLogger(__name__)

Progress = Optional[Callable[[str], None]]


def _noop(_msg: str) -> None:
    pass


PROMPT = """You are an ATS (applicant tracking system) evaluating how well a candidate's resume matches a job posting.

Resume:
{resume}

Job title: {title}
Job description:
{description}

Score the match from 0 to 100, the way a strict but fair recruiter would — \
weigh required skills, seniority, and domain relevance more than keyword overlap.

Reply with ONLY a JSON object, no other text: {{"score": <integer 0-100>, "reason": "<one sentence, under 25 words>"}}"""


def _parse_response(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if "score" not in data:
        return None
    try:
        score = max(0, min(100, int(data["score"])))
    except (TypeError, ValueError):
        return None
    return {"score": score, "reason": str(data.get("reason") or "")[:300]}


def score_jobs(jobs: List, resume_text: str, progress: Progress = None) -> None:
    """Mutates job.ats_score / job.ats_reason in place for each job given."""
    say = progress or _noop
    if not Config.ANTHROPIC_API_KEY:
        say("ATS (Claude): no ANTHROPIC_API_KEY configured, skipping")
        return
    if not jobs:
        return

    client = Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    say(f"ATS (Claude): scoring {len(jobs)} jobs against your resume…")
    scored = 0
    for job in jobs:
        prompt = PROMPT.format(
            resume=resume_text[:8000],
            title=job.title,
            description=(job.description or "")[:4000],
        )
        try:
            resp = client.messages.create(
                model=Config.CLAUDE_MODEL,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            parsed = _parse_response(text)
            if parsed:
                job.ats_score = parsed["score"]
                job.ats_reason = parsed["reason"]
                scored += 1
            else:
                say(f"ATS (Claude): couldn't parse a score for \"{job.title}\", skipping")
        except APIStatusError as exc:
            # Auth/billing/permission/config errors apply to every remaining
            # job too (Anthropic returns even a zero-credit account as a
            # generic 400) — no point burning through the rest of the batch
            # to hit the same wall N more times. Per-job problems (a job
            # description that trips content filtering, say) are rare
            # enough that stopping early here is the safer default.
            detail = (exc.body or {}).get("error", {}).get("message", str(exc)) \
                if isinstance(exc.body, dict) else str(exc)
            say(f"ATS (Claude): {exc.status_code} — {detail}")
            say("ATS (Claude): stopping the rest of this batch, same error would likely repeat")
            break
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the batch
            say(f"ATS (Claude): {exc.__class__.__name__} on \"{job.title}\", skipping")
    say(f"ATS (Claude): {scored} of {len(jobs)} scored")
