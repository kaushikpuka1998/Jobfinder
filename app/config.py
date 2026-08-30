import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    PORT = int(os.environ.get('PORT', 5000))
    MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/job_scraper')
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')
    AWS_BUCKET_NAME = os.environ.get('AWS_BUCKET_NAME')
    AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
    # Unset by default: the log/progress panel stays hidden from every
    # visitor until this is set and someone opens /?admin=<token> once.
    ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '')
    # Cron interval for the unattended background scrape, in seconds.
    AUTO_RUN_INTERVAL_SECONDS = int(os.environ.get('AUTO_RUN_INTERVAL_SECONDS', 3600))
