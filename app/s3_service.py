import boto3
from botocore.exceptions import ClientError
from app.config import Config
import logging

LOG = logging.getLogger(__name__)

class S3Service:
    def __init__(self):
        self.bucket_name = Config.AWS_BUCKET_NAME
        if not all([Config.AWS_ACCESS_KEY_ID, Config.AWS_SECRET_ACCESS_KEY, self.bucket_name]):
            LOG.warning("S3 credentials or bucket name missing. S3 operations will be disabled.")
            self.s3 = None
            return

        self.s3 = boto3.client(
            's3',
            aws_access_key_id=Config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY,
            region_name=Config.AWS_REGION
        )
        self._ensure_lifecycle_policy()

    def _ensure_lifecycle_policy(self):
        """Sets a 10-day expiration lifecycle policy on the bucket if possible."""
        if not self.s3: return
        lifecycle_config = {
            'Rules': [
                {
                    'ID': 'ExpireOldResourcesAfter10Days',
                    'Filter': {'Prefix': ''},
                    'Status': 'Enabled',
                    'Expiration': {
                        'Days': 10
                    }
                }
            ]
        }
        try:
            self.s3.put_bucket_lifecycle_configuration(
                Bucket=self.bucket_name,
                LifecycleConfiguration=lifecycle_config
            )
            LOG.info(f"Successfully set 10-day lifecycle policy on bucket: {self.bucket_name}")
        except ClientError as e:
            LOG.error(f"Failed to set bucket lifecycle policy: {e}")

    def upload_file(self, file_path: str, object_name: str = None) -> bool:
        if not self.s3: return False
        if object_name is None:
            object_name = file_path.split('/')[-1]
            
        try:
            self.s3.upload_file(file_path, self.bucket_name, object_name)
            LOG.info(f"Uploaded {file_path} to s3://{self.bucket_name}/{object_name}")
            return True
        except ClientError as e:
            LOG.error(f"S3 Upload failed: {e}")
            return False

    def upload_fileobj(self, file_obj, object_name: str) -> bool:
        if not self.s3: return False
        try:
            self.s3.upload_fileobj(file_obj, self.bucket_name, object_name)
            return True
        except ClientError as e:
            LOG.error(f"S3 Upload failed: {e}")
            return False

s3_service = S3Service()
