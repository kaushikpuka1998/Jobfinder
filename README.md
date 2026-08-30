# Job Scraper — LinkedIn · Greenhouse · Ashby · Lever · Workable · Workday · Remote OK

Scrapes, deduplicates, scores and ranks job postings from seven sources. No
AI, no LLM calls, no API keys — pure HTTP, HTML/JSON parsing and deterministic
keyword arithmetic. Upload a resume and it derives the search profile for you,
still deterministically: the same resume always produces the same profile.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Then run everything with `.venv/bin/python` (or `source .venv/bin/activate` first).

## Quick start — web UI

```bash
.venv/bin/python app.py
```

Open <http://127.0.0.1:5001>, then:

1. **Upload a resume** (PDF / DOCX / TXT / MD) or paste the text, add your
   locations, hit *Analyse resume*. It shows the skills it found, your years
   of experience, and the must-have terms it will search on.
2. **Pick sources** — Greenhouse and Workday are fast; LinkedIn is rate
   limited and slow, so it is off by default.
3. **Fetch jobs.** Live progress, then a filterable result table. CSV / JSON /
   Markdown land in `output/` and are downloadable from the page.

Port 5000 is taken by AirPlay Receiver on macOS, hence 5001. Override with
`PORT=8080 .venv/bin/python app.py`.

### Layout

The controls column (Resume, Sources, Apply kit, Database) is much taller than
the results, so on a wide window the results panel **sticks to the viewport
and fills the height** — scroll down through the Apply kit and the job list
stays beside you instead of stranding you next to empty space. Below 1080px
the two columns stack and the results take the full width.

Progress is folded away by default and unfolds when a run starts; its status
line stays visible while collapsed. The job table's columns are proportional,
dropping Posted/Exp under 760px and moving company and location under the
title under 560px, so it shrinks to the space rather than scrolling sideways.

## Quick start — CLI

```bash
.venv/bin/python linkedin_job_scraper.py init-config   # writes config.json
$EDITOR config.json                                    # tune keywords + weights
.venv/bin/python linkedin_job_scraper.py scrape        # run every profile
```

Outputs land in `output/`: a CSV, a JSON dump with score breakdowns, and a
ranked Markdown digest. The CLI is LinkedIn-only; use the web UI (or import
`sources.py`) for Greenhouse and Workday.

## Sources

| Source | Endpoint | Speed | Coverage |
|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io` JSON | fast | 42 boards, descriptions inline |
| Ashby | `api.ashbyhq.com/posting-api` JSON | fast | 42 boards, descriptions inline |
| Lever | `api.lever.co/v0/postings` JSON | fast | 9 boards, descriptions inline |
| Workable | `apply.workable.com` widget JSON | medium — rate limits | 20 accounts |
| Workday | `wday/cxs/.../jobs` JSON | medium — one detail call per job | 9 employer tenants |
| [Remote OK](https://remoteok.com) | `remoteok.com/api` JSON | fast | aggregator, ~1000 jobs, all remote |
| LinkedIn | guest job search HTML | slow — rate limited, ~2s/request | broad, but bot-challenged |

Remote OK is an aggregator, not a per-employer board, so it needs no registry.
Its API returns only the newest ~100 postings per call, but each `tag` returns
a different 100 — querying a spread of tags pulls roughly 1000 distinct jobs.
Its terms ask for attribution and a followed link back, which the UI and the
Apply button honour.

Every other source is a **per-employer job board** — none has a
global "search every company" endpoint. So "everywhere" means "every board in
the registry". The registries in `sources.py` were probed live and only
working boards kept; add your own by editing those lists:

| Registry | Where the slug comes from |
|---|---|
| `GREENHOUSE_BOARDS` | `boards.greenhouse.io/<slug>` |
| `ASHBY_BOARDS` | `jobs.ashbyhq.com/<slug>` |
| `LEVER_BOARDS` | `jobs.lever.co/<slug>` |
| `WORKABLE_ACCOUNTS` | `apply.workable.com/<slug>` |
| `WORKDAY_TENANTS` | `<tenant>.wd<N>.myworkdayjobs.com/<site>` |

Workable accounts cycle in and out of hiring; most of the 20 verified accounts
have no openings on any given day.

## Remote jobs, worldwide

Tick **Remote only — worldwide** and the location box is ignored on purpose:
remote roles are kept from every board regardless of where the company sits.
LinkedIn additionally gets its native remote filter (`f_WT=2`) so it doesn't
waste rate-limited requests on onsite postings.

One detector decides "remote" for all six sources. A source's own flag
(Ashby `isRemote`, Workable `telecommuting`, Workday `remoteType`, Lever
`workplaceType`) is trusted when set; otherwise the location, title and then
the description are checked. Descriptions need an explicit statement —
"fully remote", "remote-first", "work from home" — because a bare "remote"
matches things like "remote sensing" or "our remote offices". Explicit
denials ("on-site only", "this is not a remote position") win outright.

The results panel has its own **remote only** checkbox, so you can filter the
database without re-scraping.

## Applying

The table shows an **Apply** button rather than a raw link. It points at
`/apply/<job_id>`, which looks the posting up and 302s to the real
application page — the source's apply URL when there is one (Ashby
`/application`, Lever `/apply`, Workable `/apply`), falling back to the
posting page. Non-`http(s)` targets are refused, and a missing link gives a
readable message naming the job instead of a dead link.

### Autofill (not auto-submit)

**Apply kit** keeps your details in the database (an `applicant` table
alongside the jobs), and
builds a bookmarklet with those details baked in. Drag it to your bookmarks
bar; click it on any application page and it fills what it recognises,
outlines each field it touched, and tells you what it left alone.

It stops short of submitting, deliberately:

* the final Submit stays a per-application human decision;
* these portals carry CAPTCHAs and account sign-ups that a script has no
  business defeating;
* one mis-filled field submitted 500 times misrepresents you to 500
  employers — and during testing the matcher really did try to put a city
  into an "ethnicity" box.

So it also **never touches demographic or EEO fields** (race, gender,
veteran, disability, pronouns, date of birth, salary history), and it cannot
attach your resume — browsers forbid scripts from filling file inputs.

#### Sections

The Apply kit is split to match how these forms are laid out:

| Section | Fills |
|---|---|
| Personal | name, email, phone, links, current company/title, notice, salary |
| Address | address line 1/2, city, state, postal code, country — each on its own, because Workday asks for them separately |
| Education | school, degree, field of study, GPA, from/to |
| Experience | company, title, location, from/to, "I currently work here", role description |
| Extras | how did you hear about us, skills |

Dates are stored as `MM/YYYY` and reshaped to the box being filled: Workday's
split Month and Year inputs each get their part, a `YYYY`-only box gets the
year. An experience whose end is `Present` ticks **I currently work here** and
leaves the end date empty.

#### Profile, Education and Experience blocks

Alongside the flat profile fields, Apply kit holds up to three **Education**
and three **Experience** entries, which fill the repeating
*Education (Optional)* / *Experience (Optional)* blocks these forms use.
Entry 1 fills the first block, entry 2 the second, and so on — the index is
read from the field name (`education[1][school]`, `school--1`, …).

"Start date" appears in both blocks, so those only fill when the surrounding
heading says which section the field belongs to; unambiguous labels like
*School* or *Company* fill on their own and will not cross sections.

Up to 10 entries each. Field names are normalised before matching, so
machine-generated names work as well as visible labels:
`legalNameSection_firstName` reads as "legal name section first name" and
`firstYearAttended` as "first year attended".

Workday shows a single empty block and expects you to press **Add Another**
for each further job, so the script presses it for you until there are enough
blocks, then fills them.

Entry numbering is measured from the page, not assumed. A number is only
trusted when it sits beside a block word (`workExperience-2`,
`education[1][school]`); real Workday inputs carry ids like `input-15`, and
reading those as entry numbers is what made every block repeat the first job.
When no such number exists, entries are assigned by document order — the Nth
Company box belongs to the Nth job.

A field inside an Education or Experience block only ever takes that entry's
values. If you have no entry for it, it is left empty rather than falling
back to your profile — otherwise a job's "Location" box would quietly receive
your home city.

Dropdowns are handled two ways. Native `<select>` matches on option text.
Searchable (react-select) dropdowns are opened, filtered and clicked, then
**verified** — if the choice did not stick, the typed text is cleared and the
field is reported as needing you. Typing into one of those without selecting
an option submits nothing, so an unverified fill would be worse than a blank.

### What one click does per portal

| Portal | One click on the bookmarklet |
|---|---|
| Greenhouse | Fills the form directly — name, contact, links, questions, Education/Experience, and dropdowns it can verify. |
| Lever | Same, including `org` (current company) and the `urls[…]` link fields. |
| Workable | Same on the Profile / Education / Experience sections. |
| Workday | Fills each wizard step, **but you must sign in first** — step 1 of 6 is an account wall, and the form does not exist until you are through it. Click the bookmarklet again on each step. |

Workday account creation and sign-in are yours to do: this tool does not
create accounts or enter passwords.

The details are embedded in the bookmarklet rather than fetched, so the
portal page never talks to this server and no other site can read them.
Re-save after any edit to refresh the button.

Check the matcher with:

```bash
.venv/bin/python app.py --self-check
```

## Application status

Each job carries a status: blank, `applied`, `interview`, `offer`, `rejected`
or `saved`. Filter the table by it, and see the counts in the Database card.

An untouched job shows **Apply** and a status picker. Once it has a status,
both are replaced by a single chip — there is nothing to apply to twice — and
the row dims. Click the chip to change or clear the status; clearing it brings
Apply back.

Clicking **Apply** does not mark anything — opening a posting is not applying
to it. The job goes on a short list, and when you come back to the tab the page
asks *"Did you apply to X at Y?"* with **Yes, applied** and **Not yet**. The
list is kept in the browser, so closing the page part-way through an
application does not lose the question, and several applications queue up and
are asked one at a time.

The status is yours, not the scraper's: `upsert_many` excludes it from the
columns a re-run overwrites, so re-scraping never resets a job you have already
applied to.

## Scoring — 0 to 100

Every score is reported out of 100. The underlying keyword arithmetic is
unchanged; it is divided by a **reference score** derived from the scoring
config — the points a strong match earns (the top 10 weighted terms hitting,
the heaviest also in the title, plus the best title term, full recency and the
seniority bonus).

The reference comes from the config, never from the result set, so scores are
stable: the same posting scores the same next week, and two profiles with
different weights stay comparable in one dashboard. Tuned against ~3.5k real
postings the spread lands at median 22, p90 52, p95 61, and under 1% cap
at 100.

The unnormalised total is kept in `score_raw` (and in the CSV/JSON exports),
and it breaks ties between postings that both saturate at 100. `min_score` in
`config.json` is now on the 0-100 scale — the shipped profiles use 20.

Databases written before this change are migrated in place on first open: the
old raw value moves to `score_raw` and `score` is rescaled.

## Years of experience

The resume's stated experience becomes a search band: **5 years → 4 to 6**.
Both numbers are editable in the UI before you run, and again on the results
table afterwards.

A posting matches when its requirement *overlaps* your band, so with a 4-6
band a "5+ years" role matches, a "3-8 years" role matches, and a "10+ years"
or "2-3 years" role does not. Requirements are read straight from the
description — `5+ years`, `3-5 years`, `at least 4 years`, `up to 3 years` are
all understood, while "founded 12 years ago" is correctly ignored.

Roughly a third of postings never state a requirement. **Keep jobs that don't
state a requirement** controls those; leave it on unless you want a strict
list. Run `python linkedin_job_scraper.py self-check` to exercise the parser.

## Files

| File | What it is |
|---|---|
| `linkedin_job_scraper.py` | LinkedIn scraper, scoring engine, SQLite store, exporters, CLI |
| `autofill.js` | The Apply-kit bookmarklet body, served with your details baked in |

Your application details are **stored in the database**, not in a loose file:
an `applicant` key/value table for the scalars and `applicant_entries` for the
repeating Education / Experience / Websites blocks, so the whole profile is
queryable with plain SQL and is backed up whenever the `.db` is. An existing
`applicant.json` is imported once on first read and then left alone as a
backup — nothing deletes or rewrites it.
| `sources.py` | Greenhouse + Workday adapters and their board registries |
| `resume.py` | Resume text extraction and skill/profile derivation |
| `app.py` | Flask web UI (single page, no build step) |

`sources.py` and `resume.py` each carry a `demo()` self-check — run either
file directly to verify the logic offline, no network needed.

## Commands

| Command | What it does |
|---|---|
| `init-config` | Write a starter `config.json` (`--force` to overwrite) |
| `scrape` | Search, fetch details, score, persist, export |
| `export` | Re-export from the existing DB without hitting the network |
| `stats` | Row counts, average score, top companies |
| `prune --days N` | Drop rows not seen in the last N days |

Useful `scrape` flags:

```
-p, --profile java          run one profile (repeatable)
--time-window 24h           override posting age for all profiles
--max-pages 5               cap pagination
--no-details                skip per-job detail fetch (≈5x faster, see below)
--skip-known                don't refetch jobs already in the DB
--dry-run                   scrape and score, write nothing
--format csv|json|md|all
--limit N
```

## How it works

1. **Search.** Hits `jobs-guest/jobs/api/seeMoreJobPostings/search` — the
   endpoint that serves the logged-out job search. Expands the cartesian
   product of `keywords x locations`, paginating until a page returns no new
   job ids.
2. **Detail.** For each id, hits `jobs-guest/jobs/api/jobPosting/{id}` to pull
   the full description, seniority, employment type, industry and applicant
   count. `apply_url` is now always empty — LinkedIn moved the offsite apply
   link behind a sign-in modal, so the digest falls back to `job_url`.

   `--no-details` skips this step, which leaves `description` empty. Since
   `must_have_terms` match against the description, expect ~90% of postings to
   be rejected. Use it to enumerate job ids, not to build a shortlist.
3. **Score.** Deterministic. Hard-rejects on `exclude_terms`, hard-rejects
   unless `must_have_min` of `must_have_terms` appear, then sums weighted term
   hits with logarithmic diminishing returns, a title multiplier, a recency
   bonus and an applicant-count penalty.
4. **Store.** SQLite with `job_id` as primary key, so reruns are incremental
   and `first_seen` survives updates.

## Scoring config

```jsonc
{
  "exclude_terms": ["intern", "fresher"],       // any hit -> score 0
  "must_have_terms": ["java", "spring boot"],   // need N of these
  "must_have_min": 1,
  "weighted_terms": { "java": 14, "kubernetes": 9 },
  "title_terms": { "senior software engineer": 30 },
  "title_multiplier": 2.5,        // weighted_terms hits in the title
  "recency_bonus": 12,            // decays linearly to 0
  "recency_horizon_days": 14,
  "preferred_seniority": ["Mid-Senior level"],
  "seniority_bonus": 10,
  "applicant_penalty_per_100": 2.0,
  "applicant_penalty_cap": 15,
  "repeat_factor": 0.35,          // diminishing returns on repeat hits
  "min_score": 25
}
```

Terms match on word boundaries, so `java` never matches `javascript`, and
`spring boot` also matches `Spring-Boot` and `Spring  Boot`. Tokens like `c++`
and `ci/cd` work as written.

## Rate limiting

Defaults are deliberately slow: 1.4–3.2s between requests globally, 3 detail
workers, a 25s cooldown every 120 requests, exponential backoff with jitter on
429/403/999/5xx, and User-Agent rotation. Push `min_delay` down and you will
start collecting HTTP 429s. To route through proxies:

```json
"http": { "proxies": { "http": "http://user:pass@host:port",
                       "https": "http://user:pass@host:port" } }
```

## Filter reference

`time_window`: `24h`, `48h`, `72h`, `96h`, `week`, `month`, `any`
`experience_levels`: `internship`, `entry`, `associate`, `mid-senior`, `director`, `executive`
`job_types`: `full-time`, `part-time`, `contract`, `temporary`, `internship`, `volunteer`, `other`
`workplace_types`: `onsite`, `remote`, `hybrid`
`sort_by`: `DD` (date descending) or `R` (relevance)

`geo_ids` are more reliable than location strings. A lookup table for common
cities is built in; find others by running a search on linkedin.com and reading
`geoId` out of the URL.

## Note

LinkedIn's User Agreement prohibits automated scraping, and they enforce it
with rate limits, bot challenges (HTTP 999) and IP blocks. These endpoints are
unauthenticated and public, and nothing here touches a logged-in session — but
the markup is unversioned and changes without notice, so expect to update the
CSS selectors in `parse_search_page` and `fetch_detail` periodically.
# Jobfinder
