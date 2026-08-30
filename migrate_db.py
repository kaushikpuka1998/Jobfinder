import sqlite3
import os
import sys

# Try importing the new MongoJobStore
try:
    from app.database import MongoJobStore
    from app.config import Config
except Exception as e:
    print(f"Import Error: {e}")
    sys.exit(1)

def migrate():
    sqlite_db_path = 'linkedin_jobs.db'
    if not os.path.exists(sqlite_db_path):
        print(f"{sqlite_db_path} not found. Nothing to migrate.")
        return

    # 1. Read all jobs from SQLite
    print("Connecting to SQLite...")
    conn = sqlite3.connect(sqlite_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs")
    rows = cursor.fetchall()
    
    jobs_to_insert = [dict(row) for row in rows]
    print(f"Found {len(jobs_to_insert)} jobs in SQLite.")

    # 2. Try to connect to MongoDB
    print(f"Connecting to MongoDB at {Config.MONGO_URI} ...")
    try:
        mongo_store = MongoJobStore()
        # Test connection
        mongo_store.client.admin.command('ping')
    except Exception as e:
        print(f"Failed to connect to MongoDB: {e}")
        print("Please ensure MongoDB is running or set a valid MONGO_URI in your .env file.")
        sys.exit(1)

    # 3. Upsert into MongoDB
    if jobs_to_insert:
        print("Migrating jobs to MongoDB...")
        # Since upsert_many in MongoJobStore expects Iterable[Job] or Iterable[dict], we can pass the dicts
        added, updated = mongo_store.upsert_many(jobs_to_insert)
        print(f"Migration complete: {added} inserted, {updated} updated.")
    
    # 4. Migrate Profile / Config if present (optional, but good to have)
    import json
    applicant_path = 'applicant.json'
    if os.path.exists(applicant_path):
        try:
            with open(applicant_path, 'r') as f:
                data = json.load(f)
                mongo_store.set_profile(data.get("scalars", {}), data.get("lists", {}))
                print("Migrated applicant.json to MongoDB.")
        except Exception as e:
            print(f"Could not migrate applicant.json: {e}")

    # 5. Remove old SQLite databases
    conn.close()
    print("Removing old SQLite database files...")
    os.remove(sqlite_db_path)
    if os.path.exists('dry.db'):
        os.remove('dry.db')
    if os.path.exists(applicant_path):
        os.remove(applicant_path)
    print("Cleanup complete.")

if __name__ == '__main__':
    migrate()
