import boto3
from botocore.exceptions import ClientError
from app.config import Config
from typing import Optional
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
        """Sets a 10-day expiration lifecycle policy on exported files only.

        This used to apply to every key in the bucket (Prefix: ''), which
        silently deleted uploaded resumes 10 days after upload — fatal for
        a "static resume" meant to persist and drive every future run.
        Exports (CSV/JSON/MD dumps) are still fine to auto-expire; resumes
        under resumes/ are left with no expiration rule, so they're kept
        until explicitly replaced.
        """
        if not self.s3: return
        lifecycle_config = {
            'Rules': [
                {
                    'ID': 'ExpireOldExportsAfter10Days',
                    'Filter': {'Prefix': 'exports/'},
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

    def download_bytes(self, object_name: str) -> Optional[bytes]:
        if not self.s3: return None
        try:
            resp = self.s3.get_object(Bucket=self.bucket_name, Key=object_name)
            return resp['Body'].read()
        except ClientError as e:
            LOG.error(f"S3 download failed: {e}")
            return None

    def presigned_url(self, object_name: str, expires_in: int = 3600) -> Optional[str]:
        if not self.s3: return None
        try:
            return self.s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket_name, 'Key': object_name},
                ExpiresIn=expires_in,
            )
        except ClientError as e:
            LOG.error(f"S3 presigned URL failed: {e}")
            return None

s3_service = S3Service()
