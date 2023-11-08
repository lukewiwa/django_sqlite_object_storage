import base64
import binascii
import logging
from functools import cached_property
from hashlib import md5
from http import HTTPStatus
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from django.db.backends.sqlite3 import base
from django.utils.asyncio import async_unsafe

logger = logging.getLogger(__name__)


class DatabaseWrapper(base.DatabaseWrapper):
    """
    Wraps the normal Django SQLite DB engine in one that shuttles the SQLite database
    back and forth from an S3 bucket.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.remote_db_md5 = ""

    @property
    def database_local_path(self):
        return Path(self.settings_dict["NAME"])

    @property
    def database_remote_path(self):
        return self.database_local_path.name

    @property
    def local_db_md5(self):
        return md5(self.database_local_path.read_bytes()).hexdigest()

    @cached_property
    def bucket_name(self):
        return self.settings_dict["SQLITE_OBJECT_STORAGE_BUCKET_NAME"]

    def get_object_storage_client(self):
        aws_access_key_id = self.settings_dict.get(
            "SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID"
        )
        aws_secret_access_key = self.settings_dict.get(
            "SQLITE_OBJECT_STORAGE_ACCESS_SECRET"
        )
        endpoint_url = self.settings_dict.get(
            "SQLITE_OBJECT_STORAGE_ENDPOINT_URL", None
        )
        signature_version = self.settings_dict.get(
            "SQLITE_OBJECT_STORAGE_SIGNATURE_VERSION", "s3v4"
        )

        session = boto3.Session(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )
        return session.client(
            "s3",
            config=Config(signature_version=signature_version),
            endpoint_url=endpoint_url,
        )

    @async_unsafe
    def connect(self) -> None:
        super().connect()
        logger.info(
            "[DB Connect] File %s Initialising object storage", self.database_local_path
        )

        # check if the existing local db matches the one in S3
        try:
            response = self.get_object_storage_client().get_object(
                Bucket=self.bucket_name,
                Key=self.database_remote_path,
                IfNoneMatch=self.local_db_md5,
            )
            body = response["Body"].read()
            self.database_local_path.write_bytes(body)
            self.remote_db_md5 = self.local_db_md5
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == str(HTTPStatus.NOT_MODIFIED.value):
                self.remote_db_md5 = self.local_db_md5
                logger.info(
                    "[DB Connect] File %s Unchanged Local md5:%s, Remote md5:%s",
                    self.database_local_path,
                    self.local_db_md5,
                    self.remote_db_md5,
                )
            elif error_code == "NoSuchKey":
                logger.warning(
                    "[DB Connect] Database doesn't exist in object storage: %s", e
                )
            else:
                raise e
        finally:
            logger.info(
                "[DB Connect] Local database is ready at %s. md5:%s",
                self.database_local_path,
                self.local_db_md5,
            )

    def close(self, *args, **kwargs):
        """
        Engine closed, copy file to object storage if it has changed
        """
        super().close(*args, **kwargs)
        if self.remote_db_md5 == self.local_db_md5:
            logger.debug(
                "[DB Close] File %s Unchanged Local md5:%s, Remote md5:%s, not saving to object storage",
                self.database_local_path,
                self.local_db_md5,
                self.remote_db_md5,
            )
            return
        logger.debug(
            "[DB Close] File Changed %s Local md5:%s, Remote md5:%s. Pushing to object storage.",
            self.database_local_path,
            self.local_db_md5,
            self.remote_db_md5,
        )
        file_bytes = self.database_local_path.read_bytes()
        content_md5 = base64.b64encode(binascii.unhexlify(self.local_db_md5)).decode(
            "utf-8"
        )
        self.get_object_storage_client().put_object(
            Bucket=self.bucket_name,
            Key=str(self.database_remote_path),
            Body=file_bytes,
            ContentMD5=content_md5,
        )
        self.remote_db_md5 = self.local_db_md5
        logger.debug(
            "[DB Close] File Uploaded %s Local md5:%s, Remote md5:%s. Saved to object storage.",
            self.database_local_path,
            self.local_db_md5,
            self.remote_db_md5,
        )
