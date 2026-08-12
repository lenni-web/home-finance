import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "unsafe-development-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [x.strip() for x in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",")]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ledger",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"

if os.environ.get("USE_SQLITE") == "1":
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "test.sqlite3"}}
else:
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "ausgaben"),
        "USER": os.environ.get("POSTGRES_USER", "ausgaben"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "ausgaben"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }}

LANGUAGE_CODE = "de-de"
TIME_ZONE = "Europe/Berlin"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = os.environ.get("DJANGO_SECURE_COOKIES", "0") == "1"
CSRF_COOKIE_SECURE = os.environ.get("DJANGO_SECURE_COOKIES", "0") == "1"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# A statement review submits several fields per transaction. Django's default
# of 1,000 is already exceeded by roughly 125 statement rows.
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5_000

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "0") == "1"
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BEAT_SCHEDULE = {
    "poll-email-import": {
        "task": "ledger.poll_email_import",
        "schedule": 60.0,
    },
}
EMAIL_CREDENTIAL_KEY = os.environ.get("EMAIL_CREDENTIAL_KEY", SECRET_KEY)
