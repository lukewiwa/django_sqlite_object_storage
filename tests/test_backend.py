import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import boto3
from django.db.utils import ConnectionHandler
from moto import mock_aws

BUCKET = "test-bucket"


class ObjectStorageBackendTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(
            os.environ,
            {
                "AWS_ACCESS_KEY_ID": "testing",
                "AWS_SECRET_ACCESS_KEY": "testing",
                "AWS_DEFAULT_REGION": "us-east-1",
            },
        )
        env.start()
        self.addCleanup(env.stop)

        aws = mock_aws()
        aws.start()
        self.addCleanup(aws.stop)

        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=BUCKET)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def make_connection(self, directory="a", name="db.sqlite3"):
        path = self.tmp / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = ConnectionHandler(
            {
                "default": {
                    "ENGINE": "django_sqlite_object_storage",
                    "NAME": path,
                    "SQLITE_OBJECT_STORAGE_BUCKET_NAME": BUCKET,
                    "SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID": "testing",
                    "SQLITE_OBJECT_STORAGE_ACCESS_SECRET": "testing",
                    "SQLITE_OBJECT_STORAGE_REGION_NAME": "us-east-1",
                }
            }
        )
        conn = handler["default"]
        self.addCleanup(conn.close)
        return conn

    def spy(self, conn, method):
        client = conn.object_storage_client
        patcher = mock.patch.object(client, method, wraps=getattr(client, method))
        self.addCleanup(patcher.stop)
        return patcher.start()

    def remote_body(self, key="db.sqlite3"):
        return self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    def write(self, conn, value):
        with conn.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
            cursor.execute("INSERT INTO t VALUES (%s)", [value])

    def read(self, conn):
        with conn.cursor() as cursor:
            cursor.execute("SELECT v FROM t ORDER BY v")
            return [row[0] for row in cursor.fetchall()]

    def test_new_database_is_uploaded_on_close(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        conn.close()
        self.assertEqual(self.remote_body(), conn.database_local_path.read_bytes())

    def test_round_trip_between_hosts(self):
        a = self.make_connection("a")
        self.write(a, "from a")
        a.close()

        b = self.make_connection("b")
        self.assertEqual(self.read(b), ["from a"])
        self.write(b, "from b")
        b.close()

        a.connect()
        self.assertEqual(self.read(a), ["from a", "from b"])

    def test_unchanged_database_is_not_uploaded(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        conn.close()

        put = self.spy(conn, "put_object")
        self.read(conn)
        conn.close()
        put.assert_not_called()

    def test_unchanged_database_is_not_downloaded(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        conn.close()

        with mock.patch("django_sqlite_object_storage.base.os.replace") as replace:
            conn.connect()
        replace.assert_not_called()

    def test_close_without_connect_does_not_upload(self):
        conn = self.make_connection()
        conn.database_local_path.write_bytes(b"stale junk")
        put = self.spy(conn, "put_object")
        conn.close()
        put.assert_not_called()

    def test_remote_change_replaces_local_and_drops_stale_journal(self):
        a = self.make_connection("a")
        self.write(a, "one")
        a.close()

        b = self.make_connection("b")
        self.write(b, "two")
        b.close()

        journal = Path(f"{a.database_local_path}-journal")
        journal.write_bytes(b"left over from a crash")
        a.connect()
        self.assertFalse(journal.exists())
        self.assertEqual(self.read(a), ["one", "two"])

    def test_failed_download_leaves_local_file_intact(self):
        a = self.make_connection("a")
        self.write(a, "one")
        a.close()
        b = self.make_connection("b")
        self.write(b, "two")
        b.close()

        before = a.database_local_path.read_bytes()
        with (
            mock.patch(
                "django_sqlite_object_storage.base._md5_stream",
                side_effect=OSError("network went away"),
            ),
            self.assertRaises(OSError),
        ):
            a.connect()
        self.assertEqual(a.database_local_path.read_bytes(), before)
        self.assertEqual(
            [p.name for p in a.database_local_path.parent.iterdir()], ["db.sqlite3"]
        )

    def test_hash_is_cached_for_untouched_file(self):
        conn = self.make_connection()
        self.write(conn, "hello")
        conn.close()
        # Backdate the file so it's outside the racy window.
        os.utime(conn.database_local_path, ns=(0, 0))
        first = conn.local_md5()
        with mock.patch("django_sqlite_object_storage.base._md5_stream") as md5:
            self.assertEqual(conn.local_md5(), first)
        md5.assert_not_called()

    def test_in_memory_database_never_touches_storage(self):
        conn = self.make_connection()
        conn.settings_dict["NAME"] = ":memory:"
        get = self.spy(conn, "get_object")
        self.write(conn, "hello")
        get.assert_not_called()
