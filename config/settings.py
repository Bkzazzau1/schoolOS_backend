"""SchoolOS backend settings.

Everything environment-specific comes from environment variables (or a local
.env file, which is never committed). See .env.example.
"""

import sys
from datetime import timedelta
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured  # noqa: F401

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(DEBUG=(bool, False), ALLOWED_HOSTS=(list, []))
environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env("DEBUG")

SECRET_KEY = env("SECRET_KEY", default="") or (
    "insecure-dev-key-do-not-use-in-production-0123456789" if DEBUG else None
)
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set when DEBUG is off.")

ALLOWED_HOSTS = env("ALLOWED_HOSTS") or (
    ["localhost", "127.0.0.1", "10.0.2.2", "testserver"] if DEBUG else []
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "apps.core",
    "apps.accounts",
    "apps.credentials",
    "apps.organizations",
    "apps.billing",
    "apps.schools",
    "apps.students",
    "apps.academics",
    "apps.timetable",
    "apps.lesson_attendance",
    "apps.domains",
    "apps.notifications",
    "apps.access",
    "apps.sync",
    "apps.owner",
    "apps.structure",
    "apps.payroll",
    "apps.concessions",
    "apps.dashboards",
    "apps.schoollife",
    "apps.staff",
    "apps.invitations",
    "apps.alumni",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.domains.middleware.SchoolHostMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")
}

AUTH_USER_MODEL = "accounts.User"

if "test" in sys.argv:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.accounts.jwt.SchoolOSJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": "30/min", "user": "600/min",
        "invite_preview": "20/min" if "test" not in sys.argv else "10000/min",
        "invite_accept": "5/min" if "test" not in sys.argv else "10000/min",
    },
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "DEFAULT_PARSER_CLASSES": ("rest_framework.parsers.JSONParser",),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SYNC_ALLOW_UNLISTED_ENTITY_TYPES = env.bool(
    "SYNC_ALLOW_UNLISTED_ENTITY_TYPES", default=DEBUG
)
SYNC_MAX_PAYLOAD_BYTES = 256 * 1024

ACCESS_BLOCK_GRACE_HOURS = env.int("ACCESS_BLOCK_GRACE_HOURS", default=48)

INVITATION_TTL_DAYS = env.int("INVITATION_TTL_DAYS", default=14)
INVITATION_LINK_SCHEME = "http" if DEBUG else "https"
INVITATION_FALLBACK_HOST = env("INVITATION_FALLBACK_HOST", default="")

_email_url = env("EMAIL_URL", default="consolemail://" if DEBUG else "")
if _email_url:
    vars().update(env.email_url_config(_email_url))
else:
    EMAIL_BACKEND = "apps.invitations.mail.NotConfiguredBackend"
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="SchoolOS <no-reply@localhost>")

# SchoolOS SaaS payment collection. Secret keys are server-only; native/web
# clients receive only provider checkout URLs/access codes created by this API.
PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", default="")
PAYSTACK_API_BASE_URL = env("PAYSTACK_API_BASE_URL", default="https://api.paystack.co")
PAYSTACK_CALLBACK_URL = env("PAYSTACK_CALLBACK_URL", default="")

PLATFORM_DOMAIN = env("PLATFORM_DOMAIN", default="")
ANDROID_APP_PACKAGE = env("ANDROID_APP_PACKAGE", default="")
ANDROID_CERT_SHA256 = env.list("ANDROID_CERT_SHA256", default=[])

if not DEBUG:
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
