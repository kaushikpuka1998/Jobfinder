#!/usr/bin/env python3
"""
resume.py
=========

Turn an uploaded resume into a search profile. Deterministic and offline:
no AI, no LLM, no API keys — same resume always yields the same profile,
which is the whole premise of this project.

Text extraction:
  .pdf        -> pypdf
  .docx       -> stdlib zipfile (a .docx is a zip of XML)
  .txt / .md  -> read it

Skills come from a fixed vocabulary matched on the same word boundaries the
scorer uses, so `java` never matches `javascript` here either.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.scraper import term_hits, term_pattern

# --------------------------------------------------------------------------
# Skill vocabulary: term -> weight when it appears in a job posting.
# "core" terms are the ones eligible to become must-have search terms.
# --------------------------------------------------------------------------

CORE_SKILLS: Dict[str, float] = {
    # languages
    "java": 14, "python": 14, "golang": 14, "go": 10, "ruby": 13, "rust": 13,
    "typescript": 13, "javascript": 11, "c#": 12, "c++": 12, "kotlin": 12,
    "scala": 12, "swift": 12, "php": 10, "elixir": 12, "clojure": 12, "r": 8,
    # frameworks / runtimes
    "spring boot": 14, "spring": 9, "node.js": 14, "nodejs": 14, "express": 7,
    "nestjs": 9, "django": 12, "flask": 10, "fastapi": 11, "rails": 13,
    "ruby on rails": 15, "react": 10, "next.js": 9, "vue": 8, "angular": 9,
    "svelte": 8, ".net": 11, "asp.net": 11, "laravel": 10, "hibernate": 6,
    "jpa": 5, "graphql": 7, "grpc": 6,
}

BONUS_SKILLS: Dict[str, float] = {
    # cloud / infra
    "aws": 8, "gcp": 7, "azure": 7, "kubernetes": 9, "docker": 6,
    "terraform": 7, "ansible": 5, "jenkins": 4, "ci/cd": 4, "github actions": 4,
    "serverless": 5, "lambda": 4, "helm": 4, "argocd": 4,
    # data
    "postgresql": 6, "postgres": 6, "mysql": 5, "mongodb": 5, "redis": 5,
    "elasticsearch": 7, "kafka": 8, "rabbitmq": 5, "cassandra": 6,
    "dynamodb": 5, "snowflake": 6, "spark": 7, "airflow": 6, "clickhouse": 6,
    "sql": 4, "nosql": 4,
    # practice
    "microservices": 8, "distributed systems": 9, "system design": 7,
    "rest api": 6, "scalability": 5, "observability": 5, "unit testing": 4,
    "tdd": 4, "agile": 3, "scrum": 3, "code review": 3, "linux": 4,
    "git": 3, "performance tuning": 5, "security": 4, "oauth": 4,
    # ml-ish, kept small on purpose
    "machine learning": 7, "pytorch": 6, "tensorflow": 6, "llm": 6,
}

ALL_SKILLS: Dict[str, float] = {**BONUS_SKILLS, **CORE_SKILLS}

# Job titles worth extra points, reused as the default title_terms.
TITLE_TERMS: Dict[str, float] = {
    "senior software engineer": 30, "staff engineer": 26, "principal engineer": 26,
    "software engineer ii": 26, "software engineer 2": 26, "sde 2": 26,
    "sde ii": 26, "software engineer iii": 24, "sde 3": 24,
    "senior backend engineer": 30, "backend engineer": 20,
    "senior engineer": 24, "full stack engineer": 16, "lead engineer": 14,
    "engineering manager": 10, "software engineer": 14, "developer": 8,
}

DEFAULT_EXCLUDES = [
    "intern", "internship", "fresher", "trainee", "unpaid", "commission only",
    "bpo", "voice process", "telecaller", "field sales",
]

# Seniority buckets keyed off years of experience.
SENIORITY_BANDS: List[Tuple[int, List[str], List[str]]] = [
    (0,  ["entry", "associate"],       ["Entry level", "Associate"]),
    (3,  ["associate", "mid-senior"],  ["Associate", "Mid-Senior level"]),
    (6,  ["mid-senior"],               ["Mid-Senior level"]),
    (10, ["mid-senior", "director"],   ["Mid-Senior level", "Director"]),
]

YEARS_PATTERNS = [
    re.compile(r"(\d{1,2})\+?\s*(?:\.\d+)?\s*years?\s+(?:of\s+)?(?:professional\s+|industry\s+|relevant\s+)?experience", re.I),
    re.compile(r"experience\s*(?:of|:)?\s*(\d{1,2})\+?\s*years?", re.I),
]


# --------------------------------------------------------------------------
# Text extraction
# --------------------------------------------------------------------------

class ResumeError(ValueError):
    """Raised when a resume cannot be read."""


def extract_text(data: bytes, filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return _from_pdf(data)
    if name.endswith(".docx"):
        return _from_docx(data)
    if name.endswith((".txt", ".md", ".text")):
        return data.decode("utf-8", errors="replace")
    raise ResumeError(f"Unsupported file type: {filename or '(no name)'}. "
                      "Use PDF, DOCX, TXT or MD.")


def _from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise ResumeError("pypdf is not installed — run: pip install pypdf") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - pypdf raises a wide range
        raise ResumeError(f"Could not read PDF: {exc}") from exc
    text = "\n".join(pages).strip()
    if not text:
        raise ResumeError("No text found in the PDF — it looks like a scan. "
                          "Export a text-based PDF, or upload DOCX/TXT.")
    return text


def _from_docx(data: bytes) -> str:
    # ponytail: a .docx is a zip of XML; stdlib beats adding python-docx.
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ResumeError(f"Could not read DOCX: {exc}") from exc
    xml = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    if not text.strip():
        raise ResumeError("No text found in the DOCX.")
    return text


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def detect_years(text: str) -> Optional[int]:
    """Highest plausible 'N years of experience' claim in the resume."""
    best: Optional[int] = None
    for pattern in YEARS_PATTERNS:
        for match in pattern.finditer(text):
            try:
                years = int(match.group(1))
            except ValueError:
                continue
            if 0 <= years <= 40:
                best = years if best is None else max(best, years)
    return best


def detect_skills(text: str) -> List[Tuple[str, int]]:
    """Vocabulary terms present in the resume, most-mentioned first."""
    hits = [(term, term_hits(term, text)) for term in ALL_SKILLS]
    found = [(t, n) for t, n in hits if n]
    found.sort(key=lambda pair: (-pair[1], -ALL_SKILLS[pair[0]], pair[0]))
    return found


def experience_band(years: Optional[int], spread: int = 1) -> Tuple[Optional[int], Optional[int]]:
    """Turn "N years of experience" into the band to search.

    5 years -> 4 to 6. The floor is clamped at 0, and an unknown resume gives
    an open band so nothing is filtered out on a guess.
    """
    if years is None:
        return None, None
    return max(0, years - spread), years + spread


def detect_titles(text: str) -> List[str]:
    head = text[:1500]
    return [t for t in TITLE_TERMS if term_pattern(t).search(head)]


def build_profile(text: str, name: str = "resume",
                  locations: Optional[Sequence[str]] = None,
                  time_window: str = "week",
                  max_core: int = 5,
                  max_terms: int = 28) -> Dict[str, Any]:
    """Resume text -> a config-shaped profile dict the scraper understands."""
    if not text or not text.strip():
        raise ResumeError("Resume text is empty.")

    skills = detect_skills(text)
    if not skills:
        raise ResumeError("No known skills found in this resume. Add the terms "
                          "you want to search for manually.")

    core = [t for t, _ in skills if t in CORE_SKILLS][:max_core]
    if not core:
        core = [t for t, _ in skills[:3]]

    weighted: Dict[str, float] = {}
    for term, count in skills[:max_terms]:
        weight = ALL_SKILLS[term]
        # A skill mentioned a lot in the resume is one you actually want to use.
        if count >= 3:
            weight *= 1.25
        weighted[term] = round(weight, 1)

    years = detect_years(text)
    levels, seniority = _band(years)
    exp_lo, exp_hi = experience_band(years)

    keywords = _keywords(core, years)

    return {
        "name": name,
        "keywords": keywords,
        "locations": list(locations or []),
        "geo_ids": [],
        "time_window": time_window,
        "experience_levels": levels,
        "job_types": ["full-time"],
        "workplace_types": [],
        "sort_by": "DD",
        "max_pages": 10,
        "max_results": 400,
        "fetch_details": True,
        "scoring": {
            "exclude_terms": list(DEFAULT_EXCLUDES),
            "must_have_terms": core,
            "must_have_min": 1,
            "weighted_terms": weighted,
            "title_terms": dict(TITLE_TERMS),
            "title_multiplier": 2.5,
            "recency_bonus": 12,
            "recency_horizon_days": 14,
            "preferred_seniority": seniority,
            "seniority_bonus": 10,
            "applicant_penalty_per_100": 2.0,
            "applicant_penalty_cap": 15,
            "repeat_factor": 0.35,
            "min_score": 20,   # out of 100
        },
        "_detected": {
            "years": years,
            "exp_min": exp_lo,
            "exp_max": exp_hi,
            "skills": [{"term": t, "count": n} for t, n in skills[:max_terms]],
            "titles": detect_titles(text),
            "chars": len(text),
        },
    }


def _band(years: Optional[int]) -> Tuple[List[str], List[str]]:
    if years is None:
        return ["associate", "mid-senior"], ["Associate", "Mid-Senior level"]
    chosen = SENIORITY_BANDS[0]
    for band in SENIORITY_BANDS:
        if years >= band[0]:
            chosen = band
    return list(chosen[1]), list(chosen[2])


def _keywords(core: Sequence[str], years: Optional[int]) -> List[str]:
    """Search phrases built from the strongest resume skills."""
    prefix = "Senior " if (years or 0) >= 4 else ""
    pretty = {"node.js": "Node.js", "nodejs": "Node.js", "c#": "C#", "c++": "C++",
              "ruby on rails": "Ruby on Rails", "spring boot": "Spring Boot",
              ".net": ".NET", "next.js": "Next.js", "fastapi": "FastAPI"}
    out: List[str] = []
    for term in core[:3]:
        label = pretty.get(term, term.title())
        out.append(f"{prefix}Software Engineer {label}".strip())
        out.append(f"{prefix}Backend Engineer {label}".strip())
    # Keep it tight: every keyword multiplies the LinkedIn request count.
    seen, unique = set(), []
    for kw in out:
        if kw.lower() not in seen:
            seen.add(kw.lower())
            unique.append(kw)
    return unique[:6]


# --------------------------------------------------------------------------
# Self-check
# --------------------------------------------------------------------------

def demo() -> None:
    sample = """
    Jane Doe — Senior Software Engineer
    7 years of professional experience building backend systems.
    Skills: Java, Spring Boot, Kubernetes, AWS, Kafka, PostgreSQL, Docker.
    Built Java microservices on AWS. Java again. Spring Boot again.
    Also dabbled in JavaScript.
    """
    assert detect_years(sample) == 7
    assert detect_years("no numbers here") is None

    skills = dict(detect_skills(sample))
    assert skills["java"] == 3, skills.get("java")
    assert "javascript" in skills and skills["javascript"] == 1
    assert "spring boot" in skills

    profile = build_profile(sample, name="test", locations=["India"])
    assert profile["scoring"]["must_have_terms"], profile["scoring"]
    assert "java" in profile["scoring"]["must_have_terms"]
    assert profile["experience_levels"] == ["mid-senior"], profile["experience_levels"]
    assert profile["keywords"] and all(profile["keywords"])
    assert profile["scoring"]["weighted_terms"]["java"] > CORE_SKILLS["java"]  # repeat boost
    assert profile["locations"] == ["India"]

    # Bands
    assert _band(None)[0] == ["associate", "mid-senior"]
    assert _band(1)[0] == ["entry", "associate"]
    assert _band(12)[0] == ["mid-senior", "director"]

    # 5 years of experience -> search 4 to 6 years.
    assert experience_band(5) == (4, 6)
    assert experience_band(0) == (0, 1)
    assert experience_band(1) == (0, 2)          # floor clamps at 0
    assert experience_band(None) == (None, None)  # unknown -> no filtering
    assert experience_band(5, spread=2) == (3, 7)
    # The sample resume above says 7 years, so the band is 6-8.
    assert profile["_detected"]["exp_min"] == 6, profile["_detected"]
    assert profile["_detected"]["exp_max"] == 8, profile["_detected"]

    # docx extraction round-trip through a real zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml",
                    "<w:p><w:t>Python</w:t></w:p><w:p><w:t>Django</w:t></w:p>")
    assert "Python" in extract_text(buf.getvalue(), "cv.docx")
    assert "Django" in extract_text(buf.getvalue(), "cv.docx")

    for bad in ("cv.rtf", "cv.pages", ""):
        try:
            extract_text(b"x", bad)
        except ResumeError:
            pass
        else:
            raise AssertionError(f"expected ResumeError for {bad!r}")

    print("resume.py self-check OK")


if __name__ == "__main__":
    demo()
