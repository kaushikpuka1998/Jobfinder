import os
import secrets
from dotenv import load_dotenv

load_dotenv()

class Config:
    PORT = int(os.environ.get('PORT', 5000))
    MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/job_scraper')
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')
    AWS_BUCKET_NAME = os.environ.get('AWS_BUCKET_NAME')
    AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
    # Redis-backed Apify cache (see app/cache.py). Unset means caching is
    # simply skipped — every Apify call goes through, same as before Redis.
    REDIS_URL = os.environ.get('REDIS_URL', '')
    # Real login now (see api_login in main.py) — one hardcoded admin
    # account. ADMIN_PASSWORD unset means login is disabled entirely,
    # matching the old "admin stays hidden until configured" default.
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'kaushikghosh199832@gmail.com')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
    # Signs the session cookie. Set this explicitly in Railway too — left
    # unset, it's a fresh random value per process restart, which logs
    # everyone out on every redeploy.
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    # Cron interval for the unattended background scrape, in seconds.
    AUTO_RUN_INTERVAL_SECONDS = int(os.environ.get('AUTO_RUN_INTERVAL_SECONDS', 3600))
    # Apify (LinkedIn "last 24 hours" search, run 3x/day across up to 3
    # saved profiles — a separate cron from the hourly board scrape above).
    APIFY_TOKEN = os.environ.get('APIFY_TOKEN', '')
    APIFY_CRON_INTERVAL_SECONDS = int(os.environ.get('APIFY_CRON_INTERVAL_SECONDS', 8 * 3600))
    APIFY_LIMIT_PER_SEARCH = int(os.environ.get('APIFY_LIMIT_PER_SEARCH', 100))
    # How long a cached Apify search result is reused before a new search
    # is considered "the same request" and calling Apify again is skipped.
    # Deliberately shorter than APIFY_CRON_INTERVAL_SECONDS — this guards
    # against accidental duplicate calls (a double click, a redeploy mid-run),
    # not against the legitimate 3x/day cadence.
    APIFY_CACHE_TTL_SECONDS = int(os.environ.get('APIFY_CACHE_TTL_SECONDS', 3600))
    # Claude ATS re-scoring: a second, deeper pass on top of the free
    # keyword Scorer. Only the top ATS_TOP_N jobs (by keyword score) from
    # each run get sent to Claude — real per-token cost, so every job
    # scraped is never sent, only the ones that already look promising.
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
    CLAUDE_MODEL = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-5')
    ATS_TOP_N = int(os.environ.get('ATS_TOP_N', 25))
