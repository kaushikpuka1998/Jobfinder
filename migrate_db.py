"""Move the SQLite data into MongoDB.

Run once:

    python migrate_db.py                # migrate, keep the SQLite files
    python migrate_db.py --delete-source # migrate, then remove them

The source files are kept by default. Deleting them is a separate, explicit
choice, because a migration that both moves and destroys leaves nothing to
compare against when something turns out to be missing.
"""

import argparse
import json
import os
import sqlite3
import sys

try:
    from app.database import MongoJobStore
    from app.config import Config
except Exception as exc:                                   # pragma: no cover
    print(f"Import Error: {exc}")
    sys.exit(1)

SQLITE_PATH = "linkedin_jobs.db"
APPLICANT_PATH = "applicant.json"
LIST_FIELDS = ("education", "experience", "websites")


def read_jobs(conn) -> list:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute("SELECT * FROM jobs")]


def read_profile(conn) -> tuple:
    """Profile out of the `applicant` / `applicant_entries` tables.

    This is where the profile actually lives — the earlier version of this
    script read applicant.json and looked for "scalars"/"lists" keys that file
    has never had, so it stored an empty profile and then deleted the only
    copies of the real one.
    """
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "applicant" not in names:
        return {}, {}

    scalars = {r["key"]: (r["value"] or "")
               for r in conn.execute("SELECT key, value FROM applicant")}

    by_list: dict = {}
    if "applicant_entries" in names:
        rows: dict = {}
        for r in conn.execute("SELECT list, pos, field, value FROM applicant_entries"):
            rows.setdefault(r["list"], {}).setdefault(r["pos"], {})[r["field"]] = \
                r["value"] or ""
        for name, by_pos in rows.items():
            by_list[name] = [by_pos[p] for p in sorted(by_pos)]
    return scalars, by_list


def read_profile_json() -> tuple:
    """Fallback: the flat applicant.json, split into scalars and lists."""
    if not os.path.exists(APPLICANT_PATH):
        return {}, {}
    try:
        with open(APPLICANT_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  could not read {APPLICANT_PATH}: {exc}")
        return {}, {}
    scalars = {k: v for k, v in data.items()
               if k not in LIST_FIELDS and isinstance(v, str)}
    lists = {k: data.get(k) or [] for k in LIST_FIELDS if k in data}
    return scalars, lists


def migrate(delete_source: bool = False) -> int:
    if not os.path.exists(SQLITE_PATH):
        print(f"{SQLITE_PATH} not found. Nothing to migrate.")
        return 0

    conn = sqlite3.connect(SQLITE_PATH)
    jobs = read_jobs(conn)
    scalars, lists = read_profile(conn)
    if not scalars:
        scalars, lists = read_profile_json()
        if scalars:
            print(f"  profile read from {APPLICANT_PATH}")
    else:
        print("  profile read from the SQLite tables")

    print(f"Found {len(jobs)} jobs, {len(scalars)} profile fields, "
          f"{sum(len(v) for v in lists.values())} list entries.")

    print(f"Connecting to MongoDB at {Config.MONGO_URI} ...")
    try:
        store = MongoJobStore()
        store.client.admin.command("ping")
    except Exception as exc:
        print(f"Failed to connect to MongoDB: {exc}")
        print("Start MongoDB, or set MONGO_URI in your .env file.")
        return 1

    if jobs:
        added, updated = store.upsert_many(jobs)
        print(f"Jobs: {added} inserted, {updated} updated.")

    if scalars or lists:
        store.set_profile(scalars, lists)
        print(f"Profile: {len(scalars)} fields, "
              + ", ".join(f"{len(v)} {k}" for k, v in sorted(lists.items())))

    # Read it back rather than trusting the write.
    check = store.get_profile()
    ok = (store.jobs.count_documents({}) >= len(jobs)
          and len(check.get("scalars", {})) == len(scalars))
    print(f"Verified: {store.jobs.count_documents({})} jobs and "
          f"{len(check.get('scalars', {}))} profile fields in MongoDB.")
    if not ok:
        print("Verification failed — leaving the SQLite files alone.")
        conn.close()
        return 1

    conn.close()
    if delete_source:
        for path in (SQLITE_PATH, "dry.db", APPLICANT_PATH):
            if os.path.exists(path):
                os.remove(path)
                print(f"  removed {path}")
    else:
        print("\nSource files kept. Re-run with --delete-source to remove them "
              "once you are happy the data is all there.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete-source", action="store_true",
                        help="remove the SQLite files after a verified migration")
    sys.exit(migrate(parser.parse_args().delete_source))
