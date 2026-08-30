import datetime
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple, Set
from pymongo import MongoClient, UpdateOne
from app.config import Config

LOG = logging.getLogger(__name__)

class MongoJobStore:
    def __init__(self, uri: str = None) -> None:
        self.uri = uri or Config.MONGO_URI
        self.client = MongoClient(self.uri)
        # Parse db name from URI or use default
        db_name = self.uri.split('/')[-1].split('?')[0]
        if not db_name:
            db_name = 'job_scraper'
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
        query = {}
        status = filters.get("status")
        if status is not None:
            if status.lower() == "none":
                query["$or"] = [{"status": None}, {"status": ""}]
            elif status.lower() == "any":
                query["status"] = {"$nin": [None, ""]}
            else:
                query["status"] = status
                
        if filters.get("min_score", 0.0) > 0:
            query["score"] = {"$gte": float(filters["min_score"])}
            
        # Simplified profile/text matching, if needed
        # In a real scenario we'd use full text search or regex
        return query

    def query(self, limit: Optional[int] = None, **filters: Any) -> List[dict]:
        query = self._build_filter(filters)
        cursor = self.jobs.find(query).sort([("score", -1), ("posted_raw", -1)])
        if limit:
            cursor = cursor.limit(limit)
        return list(cursor)

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

    def set_status(self, job_id: str, status: str) -> bool:
        now = datetime.datetime.utcnow().isoformat()
        result = self.jobs.update_one(
            {"job_id": job_id},
            {"$set": {"status": status, "status_at": now}}
        )
        return result.modified_count > 0

    def status_counts(self) -> Dict[str, int]:
        pipeline = [
            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
        ]
        counts = {}
        for row in self.jobs.aggregate(pipeline):
            key = row["_id"] or ""
            counts[key] = row["count"]
        return counts

    def stats(self) -> Dict[str, Any]:
        return {
            "total_jobs": self.jobs.count_documents({}),
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
