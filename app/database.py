import datetime
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple, Set
from urllib.parse import urlparse
from pymongo import MongoClient, UpdateOne
from app.config import Config

LOG = logging.getLogger(__name__)

class MongoJobStore:
    def __init__(self, uri: str = None) -> None:
        self.uri = uri or Config.MONGO_URI
        self.client = MongoClient(self.uri)
        # The path component is the db name, e.g. "/job_scraper" -> "job_scraper".
        # A naive uri.split("/")[-1] grabbed the host instead whenever Atlas's
        # default URI (no db in the path, just "/?retryWrites=...") was used,
        # producing a dotted "db name" like "cluster.mongodb.net" that Mongo
        # rejects outright.
        db_name = urlparse(self.uri).path.lstrip('/') or 'job_scraper'
        self.db = self.client[db_name]
        self.jobs = self.db.jobs
        self.profiles = self.db.profiles

        # Ensure indexes
        self.jobs.create_index("job_id", unique=True)
        self.jobs.create_index("status")
        self.jobs.create_index("score")
        
        # We only keep one profile document, identified by _id="default"
        self.profiles.update_one(
            {"_id": "default"},
            {"$setOnInsert": {"scalars": {}, "lists": {}}},
            upsert=True
        )

    def known_ids(self) -> set:
        return {doc["job_id"] for doc in self.jobs.find({}, {"job_id": 1})}

    def upsert_many(self, jobs: Iterable) -> Tuple[int, int]:
        if not jobs:
            return 0, 0
            
        requests = []
        for job in jobs:
            # Assume job is an object with __dict__ or already a dict
            doc = job.__dict__ if hasattr(job, '__dict__') else dict(job)
            requests.append(
                UpdateOne(
                    {"job_id": doc["job_id"]},
                    {"$set": doc},
                    upsert=True
                )
            )
            
        if not requests:
            return 0, 0
            
        result = self.jobs.bulk_write(requests)
        return result.upserted_count, result.modified_count

    def _build_filter(self, filters: Dict[str, Any]) -> dict:
        and_clauses = []

        # -- status
        status = filters.get("status")
        if status is not None:
            if status.lower() == "none":
                and_clauses.append({"$or": [{"status": None}, {"status": ""}]})
            elif status.lower() == "any":
                and_clauses.append({"status": {"$nin": [None, ""]}})
            else:
                and_clauses.append({"status": status})

        # -- score
        min_score = float(filters.get("min_score") or 0)
        if min_score > 0:
            and_clauses.append({"score": {"$gte": min_score}})

        # -- ATS (Claude) score — only set on the top-N jobs from a run, so
        # this implicitly excludes everything Claude never looked at.
        min_ats_score = filters.get("min_ats_score")
        if min_ats_score is not None:
            and_clauses.append({"ats_score": {"$gte": float(min_ats_score)}})

        # -- source (derived from job_id prefix)
        source = filters.get("source")
        if source:
            source_patterns = {
                "linkedin":   {"$regex": "^(?!gh:|ab:|lv:|wk:|wd:|ro:)"},
                "greenhouse": {"$regex": "^gh:"},
                "ashby":      {"$regex": "^ab:"},
                "lever":      {"$regex": "^lv:"},
                "workable":   {"$regex": "^wk:"},
                "workday":    {"$regex": "^wd:"},
                "remoteok":   {"$regex": "^ro:"},
            }
            if source in source_patterns:
                and_clauses.append({"job_id": source_patterns[source]})
            else:
                and_clauses.append({"job_id": {"$regex": f"^{source}"}})

        # -- search text (title, company, location)
        search = filters.get("search")
        if search:
            regex = {"$regex": search, "$options": "i"}
            and_clauses.append({"$or": [
                {"title": regex},
                {"company": regex},
                {"location": regex},
            ]})

        # -- remote only
        if filters.get("remote_only"):
            and_clauses.append({"is_remote": True})

        # -- experience overlap filter
        exp_min = filters.get("exp_min")
        exp_max = filters.get("exp_max")
        include_unknown_exp = filters.get("include_unknown_exp", True)
        if exp_min is not None or exp_max is not None:
            want_min = exp_min if exp_min is not None else 0
            want_max = exp_max if exp_max is not None else 999
            range_overlap = {"$or": [
                {"$and": [
                    {"$or": [{"exp_max": None}, {"exp_max": {"$exists": False}}, {"exp_max": {"$gte": want_min}}]},
                    {"$or": [{"exp_min": None}, {"exp_min": {"$exists": False}}, {"exp_min": {"$lte": want_max}}]},
                ]},
            ]}
            if include_unknown_exp:
                and_clauses.append({"$or": [
                    {"$and": [{"$or": [{"exp_min": None}, {"exp_min": {"$exists": False}}]},
                              {"$or": [{"exp_max": None}, {"exp_max": {"$exists": False}}]}]},
                    range_overlap,
                ]})
            else:
                and_clauses.append(range_overlap)

        # -- profile
        profile = filters.get("profile")
        if profile:
            and_clauses.append({"profile": profile})

        # -- include rejected
        if not filters.get("include_rejected", False):
            and_clauses.append({"$or": [
                {"rejected_reason": None},
                {"rejected_reason": {"$exists": False}},
                {"rejected_reason": ""},
            ]})

        if not and_clauses:
            return {}
        if len(and_clauses) == 1:
            return and_clauses[0]
        return {"$and": and_clauses}

    def query(self, limit: Optional[int] = None, sort: str = "score", **filters: Any) -> List[dict]:
        query = self._build_filter(filters)
        if sort == "recent":
            # Mongo's ObjectId encodes its creation time and — because
            # upsert_many always upserts by job_id rather than re-inserting
            # — never changes on a later update, so sorting by _id is
            # exactly "when this job was first added", with no extra field
            # or migration needed. (first_seen/last_seen on Job are never
            # actually populated — a separate, pre-existing gap.)
            cursor = self.jobs.find(query).sort([("_id", -1)])
            if limit:
                cursor = cursor.limit(limit)
            return list(cursor)
        if sort == "ats":
            # Highest ATS score first; jobs Claude never scored (most of
            # them — only the top-N per run get sent) sort to the bottom
            # rather than mixing in ahead of real scores.
            pipeline = [
                {"$match": query},
                {"$addFields": {"_sort_ats": {"$ifNull": ["$ats_score", -1]}}},
                {"$sort": {"_sort_ats": -1, "score": -1}},
            ]
            if limit:
                pipeline.append({"$limit": limit})
            return list(self.jobs.aggregate(pipeline))
        if sort not in ("exp_asc", "exp_desc"):
            cursor = self.jobs.find(query).sort([("score", -1), ("posted_raw", -1)])
            if limit:
                cursor = cursor.limit(limit)
            return list(cursor)

        # Plain .sort() treats a missing/null exp_min as the lowest value,
        # which puts unstated-experience postings first on "asc" and last on
        # "desc" — inconsistent, and the low-first case buries every stated
        # year behind hundreds of unstated ones. Unstated postings always
        # belong at the bottom regardless of direction, so a computed sort
        # key substitutes an extreme sentinel for null before sorting.
        direction = 1 if sort == "exp_asc" else -1
        sentinel = 10 ** 6 if sort == "exp_asc" else -(10 ** 6)
        pipeline = [
            {"$match": query},
            {"$addFields": {"_sort_exp": {"$ifNull": ["$exp_min", sentinel]}}},
            {"$sort": {"_sort_exp": direction, "score": -1}},
        ]
        if limit:
            pipeline.append({"$limit": limit})
        return list(self.jobs.aggregate(pipeline))

    def count(self, **filters: Any) -> int:
        return self.jobs.count_documents(self._build_filter(filters))

    def get_profile(self) -> Dict[str, Any]:
        doc = self.profiles.find_one({"_id": "default"}) or {}
        return {
            "scalars": doc.get("scalars", {}),
            "lists": doc.get("lists", {})
        }

    def set_profile(self, scalars: Dict[str, str], lists: Dict[str, List[Dict[str, str]]]) -> None:
        self.profiles.update_one(
            {"_id": "default"},
            {"$set": {"scalars": scalars, "lists": lists}},
            upsert=True
        )

    def has_profile(self) -> bool:
        doc = self.get_profile()
        return bool(doc.get("scalars")) or bool(doc.get("lists"))

    # The search profile (resume-derived keywords/scoring) plus the source
    # picks and filters from the last manual run — a distinct document from
    # the applicant autofill profile above. The cron job replays this one.
    def save_search_profile(self, profile: Dict[str, Any], options: Dict[str, Any]) -> None:
        self.profiles.update_one(
            {"_id": "search"},
            {"$set": {"profile": profile, "options": options}},
            upsert=True
        )

    def get_search_profile(self) -> Optional[Dict[str, Any]]:
        doc = self.profiles.find_one({"_id": "search"})
        if not doc or not doc.get("profile"):
            return None
        return {"profile": doc["profile"], "options": doc.get("options", {})}

    # The one static resume file this deployment runs on — its S3 location,
    # not the derived profile above. Single fixed key: uploading a new
    # resume replaces this one, matching "one user, one static resume".
    def save_resume_file(self, s3_key: str, filename: str, content_type: str) -> None:
        self.profiles.update_one(
            {"_id": "resume_file"},
            {"$set": {"s3_key": s3_key, "filename": filename, "content_type": content_type,
                     "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}},
            upsert=True
        )

    def get_resume_file(self) -> Optional[Dict[str, Any]]:
        doc = self.profiles.find_one({"_id": "resume_file"})
        if not doc:
            return None
        return {k: doc.get(k) for k in ("s3_key", "filename", "content_type", "uploaded_at")}

    # Where you are with each job. "" means untouched.
    STATUSES = ("applied", "interview", "offer", "rejected", "saved")

    def set_status(self, job_id: str, status: str) -> bool:
        """Record where a job stands. An unknown status is refused rather than
        written, so a typo cannot quietly create a new state."""
        status = (status or "").strip().lower()
        if status and status not in self.STATUSES:
            raise ValueError(f"unknown status {status!r}; "
                             f"expected one of {', '.join(self.STATUSES)} or empty")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        result = self.jobs.update_one(
            {"job_id": job_id},
            {"$set": {"status": status, "status_at": now if status else ""}}
        )
        # matched, not modified: setting the same status again is a no-op, not
        # a missing job.
        return result.matched_count > 0

    def status_counts(self) -> Dict[str, int]:
        pipeline = [
            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
        ]
        counts: Dict[str, int] = {}
        for row in self.jobs.aggregate(pipeline):
            key = row["_id"] or ""
            counts[key] = counts.get(key, 0) + row["count"]
        return counts

    def stats(self) -> Dict[str, Any]:
        total = self.jobs.count_documents({})
        rejected = self.jobs.count_documents({"rejected_reason": {"$gt": ""}})
        pipeline = [
            {"$match": {"score": {"$gt": 0}}},
            {"$group": {"_id": None, "avg": {"$avg": "$score"}, "max": {"$max": "$score"}}}
        ]
        agg = list(self.jobs.aggregate(pipeline))
        avg_score = agg[0]["avg"] if agg else None
        max_score = agg[0]["max"] if agg else None
        return {
            "total": total,
            "rejected": rejected,
            "avg_score": avg_score,
            "max_score": max_score,
            "statuses": self.status_counts()
        }

    def prune(self, days: int) -> int:
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        # simplistic prune based on posted_raw, assuming ISO format string or just dropping old
        # A more robust system would parse dates.
        result = self.jobs.delete_many({
            "status": {"$in": [None, ""]},
            "$or": [
                # Need an actual datetime field to do proper pruning in Mongo.
                # Assuming 'added_at' exists, if not we fallback to zero deleted.
                {"added_at": {"$lt": cutoff.isoformat()}}
            ]
        })
        return result.deleted_count

    def close(self) -> None:
        self.client.close()
