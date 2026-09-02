"""
Django settings for config project.
"""

from pathlib import Path

from decouple import config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('DJANGO_SECRET_KEY')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config('DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=lambda v: [s.strip() for s in v.split(',')])

# Full scheme+host origins Django will accept POSTs from (e.g. file upload forms).
# Separate from ALLOWED_HOSTS, which only checks the Host header. Needed for tunnels
# like ngrok where the public origin differs from localhost. Supports '*.' wildcard
# subdomains per Django's own CSRF_TRUSTED_ORIGINS syntax.
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='',
    cast=lambda v: [s.strip() for s in v.split(',') if s.strip()],
)

TMDB_API_KEY = config('TMDB_API_KEY', default='')

# Both ngrok (current temp-testing tunnel) and Render (the planned real deployment)
# terminate HTTPS themselves and forward plain HTTP to this app, setting
# X-Forwarded-Proto to say so. Without this, request.is_secure() (and anything built
# from it, like build_absolute_uri() on the dashboard's share link) reports http://
# even though the visitor is genuinely on https://. Safe to trust unconditionally
# here since this app is never reached directly -- only through one of those two
# proxies, which are the only things that can set this header in practice.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'core',
    'accounts',
    'imports',
    'tmdb',
    'stats',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        # SQLite only allows one writer at a time. The default ~5s busy timeout is too
        # short for a long enrichment run (many small writes) happening alongside the
        # dev server's own per-request writes (e.g. session middleware) -- raise it so
        # SQLite waits for the lock to clear instead of raising "database is locked".
        'OPTIONS': {'timeout': 30},
    }
}


# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

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
# https://docs.djangoproject.com/en/6.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.1/howto/static-files/

STATIC_URL = 'static/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Director's Cut stays fully anonymous; only Double Feature's entry points
# (imports.CompareUploadView / CompareJoinView) require login -- see LoginRequiredMixin
# on those views. LOGIN_URL is where that mixin sends an anonymous visitor.
LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'core:landing'
LOGOUT_REDIRECT_URL = 'core:landing'


# --- App-specific settings ---

# Max accepted upload size for a Letterboxd export zip, in bytes.
MAX_UPLOAD_SIZE = 25 * 1024 * 1024  # 25 MB

# Max accepted *uncompressed* total size of a zip's contents (zip-bomb guard).
MAX_UNCOMPRESSED_ZIP_SIZE = 100 * 1024 * 1024  # 100 MB

# Max number of entries a valid Letterboxd export zip could plausibly contain.
MAX_ZIP_ENTRY_COUNT = 2000

# Cap on new TMDB lookups performed synchronously during a single upload request.
TMDB_ENRICHMENT_CAP = 150

# Keep Django's own upload-parsing ceiling in line with our own cap (with headroom
# for multipart overhead) so oversized files are rejected before they're fully buffered.
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE + (1 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE + (1 * 1024 * 1024)
