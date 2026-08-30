import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    PORT = int(os.environ.get('PORT', 5000))
    MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/job_scraper')
    # If MONGO_URI doesn't have authSource, append it for Railway MongoDB
    if MONGO_URI and '?' not in MONGO_URI:
        MONGO_URI = MONGO_URI + '?authSource=admin'
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')
    AWS_BUCKET_NAME = os.environ.get('AWS_BUCKET_NAME')
    AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
