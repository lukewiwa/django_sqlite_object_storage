import os
from pathlib import Path

USE_I18N = False
SECRET_KEY = "test_secret_key"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
]

DATABASES = {
    "default": {
        "ENGINE": "django_sqlite_object_storage",
        "NAME": Path("/tmp", "db.sqlite3"),
        "TEST": {"NAME": Path("/tmp", "test_db.sqlite3")},
        "SQLITE_OBJECT_STORAGE_BUCKET_NAME": os.getenv(
            "SQLITE_OBJECT_STORAGE_BUCKET_NAME", "test"
        ),
        "SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID": os.getenv(
            "SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID"
        ),
        "SQLITE_OBJECT_STORAGE_ACCESS_SECRET": os.getenv(
            "SQLITE_OBJECT_STORAGE_ACCESS_SECRET"
        ),
        "SQLITE_OBJECT_STORAGE_ENDPOINT_URL": os.getenv(
            "SQLITE_OBJECT_STORAGE_ENDPOINT_URL"
        ),
        "SQLITE_OBJECT_STORAGE_SIGNATURE_VERSION": os.getenv(
            "SQLITE_OBJECT_STORAGE_SIGNATURE_VERSION", "s3v4"
        ),
    }
}

DATABASES = {
    "default": {
        "ENGINE": "django_sqlite_object_storage",
        "NAME": Path("/tmp", "db.sqlite3"),
        "SQLITE_OBJECT_STORAGE_BUCKET_NAME": "bucektname",
        "SQLITE_OBJECT_STORAGE_ACCESS_KEY_ID": "key",
        "SQLITE_OBJECT_STORAGE_ACCESS_SECRET": "secret",
    }
}

LOGGING = {
    "version": 1,  # the dictConfig format version
    "disable_existing_loggers": False,  # retain the default loggers
    "handlers": {
        "console": {"class": "logging.StreamHandler", "level": "DEBUG"},
    },
    "loggers": {
        "django": {"level": "DEBUG", "handlers": ["console"]},
        "django_sqlite_object_storage": {"level": "DEBUG", "handlers": ["console"]},
    },
}
