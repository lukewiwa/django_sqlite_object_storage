#!/usr/bin/env python
import argparse
import os
import sys

import boto3
import django
from botocore.client import Config
from django.conf import settings
from django.test.utils import get_runner


SQLITE_OBJECT_STORAGE_BUCKET_NAME = os.getenv(
    "SQLITE_OBJECT_STORAGE_BUCKET_NAME", "test"
)
SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID = os.getenv("SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID")
SQLITE_OBJECT_STORAGE_ACCESS_SECRET = os.getenv("SQLITE_OBJECT_STORAGE_ACCESS_SECRET")
SQLITE_OBJECT_STORAGE_ENDPOINT_URL = os.getenv("SQLITE_OBJECT_STORAGE_ENDPOINT_URL")
SQLITE_OBJECT_STORAGE_SIGNATURE_VERSION = os.getenv(
    "SQLITE_OBJECT_STORAGE_SIGNATURE_VERSION", "s3v4"
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--create_test_bucket", type=bool, required=False)
    args = parser.parse_args()
    if args.create_test_bucket:
        session = boto3.Session(
            aws_access_key_id=SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID,
            aws_secret_access_key=SQLITE_OBJECT_STORAGE_ACCESS_SECRET,
        )
        client = session.client(
            "s3",
            config=Config(signature_version=SQLITE_OBJECT_STORAGE_SIGNATURE_VERSION),
            endpoint_url=SQLITE_OBJECT_STORAGE_ENDPOINT_URL,
        )

        client.create_bucket(Bucket=SQLITE_OBJECT_STORAGE_BUCKET_NAME)
        sys.exit()

    os.environ["DJANGO_SETTINGS_MODULE"] = "tests.settings"
    django.setup()

    TestRunner = get_runner(settings)
    test_runner = TestRunner()
    failures = test_runner.run_tests(["tests"])
    sys.exit(bool(failures))
