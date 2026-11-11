from __future__ import annotations

import base64
import hashlib
import logging
import os
import tempfile
import time
from functools import cached_property
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.db.backends.sqlite3 import base

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024

# A cached hash is only trusted if the file was last modified at least this long
# before the hash was taken. Filesystem timestamps can be coarse, so a write
# landing in the same tick as the hash (with the same size) would otherwise be
# missed. Same trick git uses for "racily clean" index entries.
RACY_WINDOW_NS = 2_000_000_000

# Files SQLite keeps alongside the database. If the main file is replaced with a
# fresh copy from object storage these belong to the old file and must go, or
# SQLite will happily replay them over the new one.
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


def _md5_stream(stream: BinaryIO, sink: BinaryIO | None = None) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    while chunk := stream.read(CHUNK_SIZE):
        digest.update(chunk)
        if sink is not None:
            sink.write(chunk)
    return digest.hexdigest()


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class DatabaseWrapper(base.DatabaseWrapper):
    """
    Wraps the normal Django SQLite DB engine in one that shuttles the SQLite
    database back and forth from an S3 compatible bucket.

    The database is pulled (if it changed remotely) just before SQLite opens the
    file, and pushed (if it changed locally) just after SQLite closes it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # md5 of the local file at the point we last knew it matched the remote.
        self._synced_md5: str | None = None
        # (stat key, time hashed, md5) so we don't rehash an unchanged file.
        self._md5_cache: tuple[tuple[int, int, int, int], int, str] | None = None

    @property
    def database_local_path(self) -> Path:
        return Path(self.settings_dict["NAME"])

    @property
    def database_remote_path(self) -> str:
        return self.database_local_path.name

    @cached_property
    def bucket_name(self) -> str:
        return self.settings_dict["SQLITE_OBJECT_STORAGE_BUCKET_NAME"]

    @property
    def object_storage_enabled(self) -> bool:
        return not self.is_in_memory_db()  # pyright: ignore[reportAttributeAccessIssue]

    @cached_property
    def object_storage_client(self):
        settings = self.settings_dict
        session = boto3.Session(
            aws_access_key_id=settings.get("SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID"),
            aws_secret_access_key=settings.get("SQLITE_OBJECT_STORAGE_ACCESS_SECRET"),
            region_name=settings.get("SQLITE_OBJECT_STORAGE_REGION_NAME"),
        )
        return session.client(
            "s3",
            config=Config(
                signature_version=settings.get(
                    "SQLITE_OBJECT_STORAGE_SIGNATURE_VERSION", "s3v4"
                )
            ),
            endpoint_url=settings.get("SQLITE_OBJECT_STORAGE_ENDPOINT_URL"),
        )

    def get_object_storage_client(self):
        # Kept for backwards compatibility.
        return self.object_storage_client

    def _remember_md5(self, md5: str, hashed_at_ns: int) -> None:
        st = self.database_local_path.stat()
        self._md5_cache = (_stat_key(st), hashed_at_ns, md5)

    def local_md5(self) -> str | None:
        """md5 of the local database file, or None if it doesn't exist."""
        path = self.database_local_path
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        if self._md5_cache is not None:
            key, hashed_at_ns, md5 = self._md5_cache
            if key == _stat_key(st) and hashed_at_ns - st.st_mtime_ns > RACY_WINDOW_NS:
                return md5
        hashed_at_ns = time.time_ns()
        with path.open("rb") as f:
            md5 = _md5_stream(f)
        self._remember_md5(md5, hashed_at_ns)
        return md5

    # Kept for backwards compatibility.
    @property
    def local_db_md5(self) -> str | None:
        return self.local_md5()

    def get_new_connection(self, conn_params):
        if self.object_storage_enabled:
            self.pull_from_object_storage()
        return super().get_new_connection(conn_params)

    def _close(self):
        super()._close()  # pyright: ignore[reportAttributeAccessIssue]
        if self.object_storage_enabled:
            self.push_to_object_storage()

    def pull_from_object_storage(self) -> None:
        """Replace the local database with the remote one if they differ."""
        path = self.database_local_path
        local_md5 = self.local_md5()
        kwargs = {"Bucket": self.bucket_name, "Key": self.database_remote_path}
        if local_md5 is not None:
            # ETag for a single part, non-KMS upload is the quoted md5 of the body.
            kwargs["IfNoneMatch"] = f'"{local_md5}"'

        try:
            response = self.object_storage_client.get_object(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("304", "NotModified"):
                logger.info("[DB Connect] %s unchanged (md5:%s)", path, local_md5)
                self._synced_md5 = local_md5
                return
            if code == "NoSuchKey":
                logger.warning(
                    "[DB Connect] %s doesn't exist in object storage yet",
                    self.database_remote_path,
                )
                # Remote is empty: whatever is local needs pushing on close.
                self._synced_md5 = None
                return
            raise

        # Stream into a temp file next to the target, then atomically swap it in
        # so a failed download never leaves a truncated database behind.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            hashed_at_ns = time.time_ns()
            with os.fdopen(fd, "wb") as tmp, response["Body"] as body:
                md5 = _md5_stream(body, sink=tmp)
            for suffix in SIDECAR_SUFFIXES:
                Path(f"{path}{suffix}").unlink(missing_ok=True)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

        self._remember_md5(md5, hashed_at_ns)
        self._synced_md5 = md5
        logger.info("[DB Connect] Downloaded %s (md5:%s)", path, md5)

    def push_to_object_storage(self) -> None:
        """Upload the local database if it changed since the last sync."""
        path = self.database_local_path
        local_md5 = self.local_md5()
        if local_md5 is None:
            return
        if local_md5 == self._synced_md5:
            logger.debug(
                "[DB Close] %s unchanged (md5:%s), not uploading", path, local_md5
            )
            return

        logger.debug("[DB Close] %s changed (md5:%s), uploading", path, local_md5)
        content_md5 = base64.b64encode(bytes.fromhex(local_md5)).decode()
        with path.open("rb") as f:
            self.object_storage_client.put_object(
                Bucket=self.bucket_name,
                Key=self.database_remote_path,
                Body=f,
                ContentMD5=content_md5,
            )
        self._synced_md5 = local_md5
        logger.info("[DB Close] Uploaded %s (md5:%s)", path, local_md5)
