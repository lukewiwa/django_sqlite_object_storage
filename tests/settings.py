SECRET_KEY = "test_secret_key"
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
]

# The tests build their own connections against a mocked bucket, this is only
# here to keep Django happy.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
