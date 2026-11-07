"""
Django settings for MagicWand project.
This is the main Django project that integrates all MagicWand functionality.
"""

import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit
from decouple import config, Csv


def _load_env_from_secret_manager() -> None:
    """Populate environment variables from Google Secret Manager when running in GCP."""

    settings_name = os.getenv("SETTINGS_NAME")
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")

    if not settings_name or not project_id:
        return

    marker = f"{project_id}:{settings_name}"
    if os.environ.get("MAGICWAND_SECRET_LOADED") == marker:
        return

    try:
        from google.cloud import secretmanager  # type: ignore
    except Exception:
        # Library missing locally; assume env vars provided through other means.
        return

    version = os.getenv("SETTINGS_VERSION", "latest")
    resource_name = f"projects/{project_id}/secrets/{settings_name}/versions/{version}"

    try:
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(name=resource_name)
    except Exception as exc:  # pragma: no cover - defensive against GCP runtime failures
        print(f"⚠️  Failed to load Secret Manager settings '{resource_name}': {exc}")
        return

    payload = response.payload.data.decode("utf-8")
    allow_overwrite = os.getenv("ALLOW_SECRET_OVERWRITE", "false").lower() in {"1", "true", "yes"}

    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if allow_overwrite or key not in os.environ:
            os.environ[key] = value

    os.environ["MAGICWAND_SECRET_LOADED"] = marker


_load_env_from_secret_manager()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-me-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config('DEBUG', default=False, cast=bool)

# Emergency lockout switch. The requested env key is lower-case
# `contract_enabled`; the upper-case variant is accepted for deploy systems that
# normalize env names.
CONTRACT_ENABLED = config(
    "contract_enabled",
    default=config("CONTRACT_ENABLED", default=False, cast=bool),
    cast=bool,
)

# Internal service authentication. The warmup endpoint reuses the existing
# service auth token unless a dedicated deploy warmup token is provided.
AUTH_BYPASS_TOKEN = config("AUTH_BYPASS_TOKEN", default="")
AUTH_BYPASS_HEADER = config("AUTH_BYPASS_HEADER", default="X-Service-Auth")
INTERNAL_WARMUP_TOKEN = config("INTERNAL_WARMUP_TOKEN", default=AUTH_BYPASS_TOKEN)
INTERNAL_WARMUP_HEADER = config("INTERNAL_WARMUP_HEADER", default=AUTH_BYPASS_HEADER)

# Allowed hosts
ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='.run.app,.appspot.com,magicalwand.in,www.magicalwand.in,localhost,127.0.0.1',
    cast=Csv()
)

if DEBUG and config('MAGICWAND_DEV_ALLOW_ALL_HOSTS', default=False, cast=bool):
    ALLOWED_HOSTS = ['*']

# Comma-separated emails that should always have admin access (auto-promoted on login).
_super_admin_emails = config('SUPER_ADMIN_EMAILS', default='', cast=Csv())
SUPER_ADMIN_EMAILS = {
    (email or '').strip().lower()
    for email in (_super_admin_emails or [])
    if (email or '').strip()
}

# CSRF trusted origins
_DEFAULT_CSRF = 'https://magicalwand.in,https://www.magicalwand.in'
CSRF_TRUSTED_ORIGINS = list(config('CSRF_TRUSTED_ORIGINS', default=_DEFAULT_CSRF, cast=Csv()))

# Ensure Django knows the original request scheme/host when behind Cloud Run's proxy
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Application definition
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
]

THIRD_PARTY_APPS = [
    'corsheaders',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
]

LOCAL_APPS = [
    'dashboard',  # Gmail dashboard functionality
    'enquiries',  # Enquiry management shell
    'marketing',  # Lost enquiries (Rejected / No response) and win-back
    'compounding',  # Compounding production plan board
    'discussion',  # Discussion forum
    'docscanner',  # Phone camera document scanning + AI extraction
    'push_notifications',  # Native Android push notification tokens
    'gasket_counter',  # Native Android camera gasket counting
    'mrp',  # Native MRP layer for factory planning, integrations, and AI
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'magicwand.middleware.CanonicalHostMiddleware',
    'magicwand.middleware.ContractExpiredMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'magicwand.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'magicwand.context_processors.service_access',
            ],
        },
    },
]

WSGI_APPLICATION = 'magicwand.wsgi.application'

# Disable APPEND_SLASH to prevent redirect loops when served behind reverse proxies
APPEND_SLASH = False

# Session configuration for localhost development
SESSION_COOKIE_DOMAIN = None  # Allow cookies on localhost
SESSION_COOKIE_SECURE = False  # Allow cookies over HTTP in development
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 1209600  # 2 weeks
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_NAME = 'magicwand_sessionid'  # Custom name for clarity

# CSRF settings for localhost development
CSRF_COOKIE_DOMAIN = None
CSRF_COOKIE_SECURE = False
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = 'Lax'

# Service portals (optional)
CAD_PORTAL_URL = config('CAD_PORTAL_URL', default='http://localhost:8000/cad/')
ENQUIRY_PORTAL_URL = config('ENQUIRY_PORTAL_URL', default='http://localhost:8405/docs')
ACCOUNTS_PORTAL_URL = config('ACCOUNTS_PORTAL_URL', default='')
ACCOUNTS_PROXY_TIMEOUT = config('ACCOUNTS_PROXY_TIMEOUT', default=120, cast=int)
IOT_PORTAL_URL = config('IOT_PORTAL_URL', default='')
IOT_PROXY_TIMEOUT = config('IOT_PROXY_TIMEOUT', default=20, cast=int)
IOT_SERVICE_TOKEN = config('IOT_SERVICE_TOKEN', default='')
IOT_MACHINE_ID_DEFAULT = config('IOT_MACHINE_ID_DEFAULT', default='mixing-mill-2')
IOT_MACHINE_ID_SOURCE = config('IOT_MACHINE_ID_SOURCE', default='extrusion-mill-1')
IOT_DASHBOARD_TIME_ZONE = config('IOT_DASHBOARD_TIME_ZONE', default='Asia/Kolkata')
GASKET_COUNTER_TOKEN = config('GASKET_COUNTER_TOKEN', default=IOT_SERVICE_TOKEN)
KANBAN_PORTAL_URL = config('KANBAN_PORTAL_URL', default='')
SETTINGS_PORTAL_URL = config('SETTINGS_PORTAL_URL', default='')

DISCUSSION_UPLOADS_LOCAL_DIR = config('DISCUSSION_UPLOADS_LOCAL_DIR', default='discussion-uploads')
DISCUSSION_UPLOADS_BUCKET = config('DISCUSSION_UPLOADS_BUCKET', default='')
DISCUSSION_UPLOADS_PREFIX = config('DISCUSSION_UPLOADS_PREFIX', default='discussion/uploads')

DESKERA_MRP_BASE_URL = config('DESKERA_MRP_BASE_URL', default='')
DESKERA_MRP_API_KEY = config('DESKERA_MRP_API_KEY', default='')
DESKERA_MRP_TENANT = config('DESKERA_MRP_TENANT', default='')
DESKERA_MRP_KANBAN_PATH = config('DESKERA_MRP_KANBAN_PATH', default='/mrp/api/v1/kanban-board')
DESKERA_MRP_BOARD_ID = config('DESKERA_MRP_BOARD_ID', default='')
DESKERA_MRP_TIMEOUT = config('DESKERA_MRP_TIMEOUT', default=15, cast=int)
DESKERA_MRP_UI_TIMEOUT = config('DESKERA_MRP_UI_TIMEOUT', default=15, cast=int)
DESKERA_PORTAL_BASE_URL = config('DESKERA_PORTAL_BASE_URL', default=DESKERA_MRP_BASE_URL)
DESKERA_WORKORDER_PATH = config('DESKERA_WORKORDER_PATH', default='/v1/mrp/workorder/search')
DESKERA_WORKSTATION_PATH = config('DESKERA_WORKSTATION_PATH', default='/v1/mrp/work-stations/search')
DESKERA_JOBCARD_PATH = config('DESKERA_JOBCARD_PATH', default='/v1/mrp/jobcard/search')
DESKERA_WORKORDER_COMPOUND_CODE_FIELDS = config('DESKERA_WORKORDER_COMPOUND_CODE_FIELDS', default='')

# Deskera pagination defaults (used by MagicWand list views + chat endpoints).
DESKERA_WORKORDER_PAGE_SIZE = config('DESKERA_WORKORDER_PAGE_SIZE', default=100, cast=int)
DESKERA_WORKORDER_MAX_PAGES = config('DESKERA_WORKORDER_MAX_PAGES', default=10, cast=int)
DESKERA_WORKSTATION_PAGE_SIZE = config('DESKERA_WORKSTATION_PAGE_SIZE', default=100, cast=int)
DESKERA_WORKSTATION_MAX_PAGES = config('DESKERA_WORKSTATION_MAX_PAGES', default=10, cast=int)
DESKERA_JOBCARD_PAGE_SIZE = config('DESKERA_JOBCARD_PAGE_SIZE', default=100, cast=int)
DESKERA_JOBCARD_MAX_PAGES = config('DESKERA_JOBCARD_MAX_PAGES', default=10, cast=int)

# Safety cap for pagination loops when "fetch all" is enabled.
DESKERA_PAGINATION_HARD_MAX_PAGES = config('DESKERA_PAGINATION_HARD_MAX_PAGES', default=250, cast=int)

# UI + copilot tuning
DESKERA_WORKORDER_UI_PAGE_SIZE = config('DESKERA_WORKORDER_UI_PAGE_SIZE', default=50, cast=int)
DESKERA_WORKORDER_SNAPSHOT_TTL_SECONDS = config('DESKERA_WORKORDER_SNAPSHOT_TTL_SECONDS', default=900, cast=int)
DESKERA_JOBCARD_UI_PAGE_SIZE = config('DESKERA_JOBCARD_UI_PAGE_SIZE', default=50, cast=int)
DESKERA_JOBCARD_SNAPSHOT_TTL_SECONDS = config('DESKERA_JOBCARD_SNAPSHOT_TTL_SECONDS', default=900, cast=int)
DESKERA_PERSISTENT_SNAPSHOT_CACHE_ENABLED = config('DESKERA_PERSISTENT_SNAPSHOT_CACHE_ENABLED', default=True, cast=bool)
DESKERA_PERSISTENT_SNAPSHOT_RETENTION_SECONDS = config('DESKERA_PERSISTENT_SNAPSHOT_RETENTION_SECONDS', default=86400, cast=int)
DESKERA_LAST_GOOD_SNAPSHOT_RETENTION_SECONDS = config(
    'DESKERA_LAST_GOOD_SNAPSHOT_RETENTION_SECONDS',
    default=2592000,
    cast=int,
)

# Gemini AI
GEMINI_API_KEY = config('GEMINI_API_KEY', default='')
GEMINI_MODEL = config('GEMINI_MODEL', default='gemini-2.5-flash')
AI_REPLY_ALLOW_STUB = config('AI_REPLY_ALLOW_STUB', default=DEBUG, cast=bool)
AI_REPLY_STUB_SIGNATURE = config('AI_REPLY_STUB_SIGNATURE', default="Thanks,\nMagicWand Team")

# Enquiry follow-up emails
FOLLOW_UP_CONTACT_PHONE = config('FOLLOW_UP_CONTACT_PHONE', default='')  # shown under the signature
FOLLOW_UP_MAX_COUNT = config('FOLLOW_UP_MAX_COUNT', default=4, cast=int)  # stop after this many sent
FOLLOW_UP_DEFAULT_GAP_DAYS = config('FOLLOW_UP_DEFAULT_GAP_DAYS', default=2, cast=int)

# Stage 2: automatic follow-ups, triggered by n8n through /enquiries/api/auto-follow-ups/...
FOLLOW_UP_AUTO_ENABLED = config('FOLLOW_UP_AUTO_ENABLED', default=False, cast=bool)  # master on/off switch
FOLLOW_UP_AUTO_DRY_RUN = config('FOLLOW_UP_AUTO_DRY_RUN', default=True, cast=bool)  # log only, send nothing
FOLLOW_UP_AUTO_SENDER = config('FOLLOW_UP_AUTO_SENDER', default='')  # username whose Gmail sends
# Days to wait after follow-up #1, #2, #3 before the next one (no more after FOLLOW_UP_MAX_COUNT).
FOLLOW_UP_AUTO_GAP_DAYS = [
    int(x) for x in config('FOLLOW_UP_AUTO_GAP_DAYS', default='4,7,7').split(',') if x.strip()
]
FOLLOW_UP_ACK_WITHIN_DAYS = config('FOLLOW_UP_ACK_WITHIN_DAYS', default=2, cast=int)  # acknowledge enquiries this recent (0 = off)
FOLLOW_UP_CLOSE_AFTER_DAYS = config('FOLLOW_UP_CLOSE_AFTER_DAYS', default=7, cast=int)  # close this long after the last follow-up (0 = off)
# Marketing portal: days to wait after an enquiry is lost before contacting the customer again.
MARKETING_WAIT_DAYS = config('MARKETING_WAIT_DAYS', default=30, cast=int)
# Marketing campaign emails. Test mode is on by default: nothing is sent until this is set to False.
MARKETING_CAMPAIGN_DRY_RUN = config('MARKETING_CAMPAIGN_DRY_RUN', default=True, cast=bool)
MARKETING_CAMPAIGN_MAX_RECIPIENTS = config('MARKETING_CAMPAIGN_MAX_RECIPIENTS', default=50, cast=int)
# Do not email the same address again within this many days of an earlier campaign email.
MARKETING_CAMPAIGN_GAP_DAYS = config('MARKETING_CAMPAIGN_GAP_DAYS', default=30, cast=int)
N8N_API_TOKEN = config('N8N_API_TOKEN', default='')  # shared secret sent by n8n in X-N8N-Token

# Material Test Certificate (MTC) generation (DOCX templates)
MTC_TEMPLATES_BUCKET = config('MTC_TEMPLATES_BUCKET', default='')
MTC_TEMPLATES_PREFIX = config('MTC_TEMPLATES_PREFIX', default='')
MTC_TEMPLATE_INDEX_OBJECT = config('MTC_TEMPLATE_INDEX_OBJECT', default='')
MTC_DEFAULT_TEMPLATE_OBJECT = config('MTC_DEFAULT_TEMPLATE_OBJECT', default='')
MTC_TEMPLATE_CACHE_TTL_SECONDS = config('MTC_TEMPLATE_CACHE_TTL_SECONDS', default=3600, cast=int)
MTC_TEMPLATES_LOCAL_DIR = config('MTC_TEMPLATES_LOCAL_DIR', default='')

CAD_API_ROOT = config('CAD_API_ROOT', default='http://localhost:8300')
CAD_API_TIMEOUT = config('CAD_API_TIMEOUT', default=20, cast=int)
ENQUIRY_API_ROOT = config('ENQUIRY_API_ROOT', default='http://localhost:8405')
ENQUIRY_API_TIMEOUT = config('ENQUIRY_API_TIMEOUT', default=20, cast=int)
ENQUIRY_SERVICE_TOKEN = config('ENQUIRY_SERVICE_TOKEN', default='')
ENQUIRY_SERVICE_AUTH_HEADER = config('ENQUIRY_SERVICE_AUTH_HEADER', default='X-Service-Auth')

# Frontend configuration
MAGICWAND_FRONTEND_URL = config('MAGICWAND_FRONTEND_URL', default='http://localhost:8000/')
if not MAGICWAND_FRONTEND_URL.endswith('/'):
    MAGICWAND_FRONTEND_URL = f"{MAGICWAND_FRONTEND_URL}/"

_FRONTEND_PARTS = urlsplit(MAGICWAND_FRONTEND_URL)
MAGICWAND_FRONTEND_ORIGIN = f"{_FRONTEND_PARTS.scheme}://{_FRONTEND_PARTS.netloc}"

if DEBUG and _FRONTEND_PARTS.hostname in {'localhost', '127.0.0.1'}:
    local_origins = {
        'http://localhost:8000',
        'http://127.0.0.1:8000',
        'http://localhost:3000',
        'http://127.0.0.1:3000',
    }
    for origin in local_origins:
        if origin not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(origin)

# CORS settings for frontend-backend communication
CORS_ALLOWED_ORIGINS = [MAGICWAND_FRONTEND_ORIGIN]

if _FRONTEND_PARTS.hostname in {'localhost', '127.0.0.1'}:
    fallback_host = '127.0.0.1' if _FRONTEND_PARTS.hostname == 'localhost' else 'localhost'
    fallback_origin = f"{_FRONTEND_PARTS.scheme}://{fallback_host}:{_FRONTEND_PARTS.port or 3000}"
    if fallback_origin not in CORS_ALLOWED_ORIGINS:
        CORS_ALLOWED_ORIGINS.append(fallback_origin)

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
]

# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Design library storage (optional GCS backing for production).
DESIGN_LIBRARY_GCS_BUCKET = os.getenv("DESIGN_LIBRARY_GCS_BUCKET", "").strip()
DESIGN_LIBRARY_GCS_PREFIX = os.getenv("DESIGN_LIBRARY_GCS_PREFIX", "design-library").strip().strip("/")

# Document scanner upload storage. In production, an empty bucket setting falls
# back to the App Engine default bucket.
DOCSCANNER_UPLOADS_BUCKET = os.getenv("DOCSCANNER_UPLOADS_BUCKET", "").strip()
DOCSCANNER_UPLOADS_PREFIX = os.getenv("DOCSCANNER_UPLOADS_PREFIX", "document-scans").strip().strip("/")

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# WhiteNoise configuration for static files
DEPLOY_ENV = os.getenv("DEPLOY_ENV", "").lower()
if DEBUG or DEPLOY_ENV == "local":
    STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
else:
    STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Google OAuth2 Configuration
# Accept legacy GOOGLE_OAUTH_* names for backwards compatibility.
GOOGLE_OAUTH2_CLIENT_ID = config('GOOGLE_OAUTH2_CLIENT_ID', default=config('GOOGLE_OAUTH_CLIENT_ID', default=''))
GOOGLE_OAUTH2_CLIENT_SECRET = config('GOOGLE_OAUTH2_CLIENT_SECRET', default=config('GOOGLE_OAUTH_CLIENT_SECRET', default=''))

# Gmail API configuration
GMAIL_API_SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
]
GMAIL_API_SERVICE_NAME = 'gmail'
GMAIL_API_VERSION = 'v1'
DASHBOARD_MAX_EMAIL_RESULTS = config('DASHBOARD_MAX_EMAIL_RESULTS', default=2000, cast=int)
GMAIL_INCLUDE_READ_DEFAULT = config('GMAIL_INCLUDE_READ_DEFAULT', default=False, cast=bool)
GMAIL_ONLY_PRIMARY_DEFAULT = config('GMAIL_ONLY_PRIMARY_DEFAULT', default=True, cast=bool)
DASHBOARD_INITIAL_BATCH = config('DASHBOARD_INITIAL_BATCH', default=50, cast=int)
GMAIL_PUBSUB_TOPIC = config('GMAIL_PUBSUB_TOPIC', default='')
GMAIL_PUBSUB_VERIFICATION_TOKEN = config('GMAIL_PUBSUB_VERIFICATION_TOKEN', default='')

# Django Allauth Configuration
AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

SITE_ID = 1

ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_AUTHENTICATION_METHOD = 'email'
ACCOUNT_UNIQUE_EMAIL = True

# Social account settings
SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': [
            'profile',
            'email',
            'https://www.googleapis.com/auth/gmail.readonly',
            'https://www.googleapis.com/auth/gmail.send',
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive.file',
        ],
        'AUTH_PARAMS': {
            'access_type': 'offline',
            'prompt': 'consent',  # Force consent screen to always get refresh token
        },
        'OAUTH_PKCE_ENABLED': True,
        'FETCH_USERINFO': True,
    }
}

# Allow login on GET requests for OAuth
SOCIALACCOUNT_LOGIN_ON_GET = True

# Store OAuth tokens
SOCIALACCOUNT_STORE_TOKENS = True

# Auto signup and email verification
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_VERIFICATION = 'none'
SOCIALACCOUNT_EMAIL_REQUIRED = True
SOCIALACCOUNT_QUERY_EMAIL = True

# Login/Logout URLs
LOGIN_URL = '/accounts/google/login/'
LOGIN_REDIRECT_URL = '/api/gmail/magicwand/complete/'  # Redirect to our endpoint first
LOGOUT_REDIRECT_URL = f"{MAGICWAND_FRONTEND_ORIGIN}/?logout=1"

# Custom adapters for OAuth flow and token storage
ACCOUNT_ADAPTER = 'dashboard.adapters.NoNextRedirectAdapter'
SOCIALACCOUNT_ADAPTER = 'dashboard.adapters.CustomSocialAccountAdapter'

# Logging

def _resolve_log_dir() -> Optional[Path]:
    """Pick the first writable log directory, falling back to /tmp."""

    candidate_dirs = []

    # Allow deployments to override the log location explicitly.
    env_dir = config('DJANGO_LOG_DIR', default='')
    if env_dir:
        candidate_dirs.append(Path(env_dir))

    candidate_dirs.extend(
        [
            BASE_DIR / 'logs',
            Path('/tmp/magicwand/logs'),
            Path('/tmp/magicwand-logs'),
        ]
    )

    for path in candidate_dirs:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        return path

    return None


LOG_DIR = _resolve_log_dir()

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'dashboard': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'allauth': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'allauth.socialaccount': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'allauth.account': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}

if LOG_DIR is not None:
    file_handler_name = 'file'
    LOGGING['handlers'][file_handler_name] = {
        'level': 'DEBUG',
        'class': 'logging.FileHandler',
        'filename': LOG_DIR / 'django.log',
        'formatter': 'verbose',
    }

    LOGGING['root']['handlers'].append(file_handler_name)

    for logger_config in LOGGING['loggers'].values():
        logger_config['handlers'].append(file_handler_name)

# Encryption key for token storage
ENCRYPTION_KEY = config('ENCRYPTION_KEY', default='your-encryption-key-here')

# Firebase Cloud Messaging. Set FIREBASE_PROJECT_ID and either
# FIREBASE_SERVICE_ACCOUNT_JSON or FIREBASE_SERVICE_ACCOUNT_FILE to enable sends.
FIREBASE_PROJECT_ID = config('FIREBASE_PROJECT_ID', default='')
FIREBASE_SERVICE_ACCOUNT_JSON = config('FIREBASE_SERVICE_ACCOUNT_JSON', default='')
FIREBASE_SERVICE_ACCOUNT_FILE = config('FIREBASE_SERVICE_ACCOUNT_FILE', default='')

# Frontend URL for redirects is defined above with MAGICWAND_FRONTEND_URL/MAGICWAND_FRONTEND_ORIGIN
