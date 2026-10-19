import os
from pathlib import Path
import dj_database_url

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Clave criptográfica para firmas de sesión y tokens
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-erp-sas-central-america-core-key-vector-2026')

# Modo de depuración configurable por entorno
DEBUG = os.environ.get('DEBUG', 'True').lower() in ('true', '1', 'yes')

# Entornos y Organizaciones Sandbox Autorizados (SEC-01 & SEC-03)
ENABLE_SANDBOX = os.environ.get('ENABLE_SANDBOX', 'False').lower() in ('true', '1', 'yes')
sandbox_orgs_env = os.environ.get('SANDBOX_ORGANIZATION_SLUGS', '')
SANDBOX_ORGANIZATION_SLUGS = [s.strip() for s in sandbox_orgs_env.split(',') if s.strip()]

# Hosts autorizados configurados por entorno (SEC-02: eliminación estricta de comodín '*')
allowed_hosts_env = os.environ.get('ALLOWED_HOSTS')
if allowed_hosts_env:
    ALLOWED_HOSTS = [h.strip() for h in allowed_hosts_env.split(',') if h.strip()]
elif DEBUG:
    # En desarrollo local seguro, permitir únicamente localhost, loopback y testserver
    ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]', 'testserver']
else:
    # En producción sin ALLOWED_HOSTS explícito, lista vacía para rechazo seguro estricto
    ALLOWED_HOSTS = []

DEFAULT_CSRF_TRUSTED_ORIGINS = [
    'https://*.trycloudflare.com',
    'https://*.devtunnels.ms',
    'https://*.ngrok-free.app',
    'http://localhost:8000',
    'http://127.0.0.1:8000',
]
csrf_origins_env = os.environ.get('CSRF_TRUSTED_ORIGINS')
env_csrf_origins = [o.strip() for o in csrf_origins_env.split(',') if o.strip()] if csrf_origins_env else []
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys(DEFAULT_CSRF_TRUSTED_ORIGINS + env_csrf_origins))

# Cabecera de proxy inverso para detección correcta de protocolo HTTPS en túneles
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Application definition
INSTALLED_APPS = [
    # Django Core (Admin exclusivo para el SuperAdmin de la plataforma)
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # ERP SAS Apps
    'apps.core',
    'apps.accounts_auth',
    'apps.tenancy',
    'apps.crm',
    'apps.operations',
    'apps.sst',
    'apps.inventory',
    'apps.finance',
    'apps.billing',
    'apps.purchasing',
    'apps.repository',
    'apps.tickets',
    'apps.ai_agent',
    'apps.payroll',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',

    # Middleware Multi-Tenant & RBAC Jerárquico
    'apps.tenancy.middleware.TenancyContextMiddleware',
]

ROOT_URLCONF = 'config.urls'

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

                # Tenancy Context Processor (Inyecta permisos, rol y módulos del tenant)
                'apps.tenancy.context_processors.tenancy_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database
# Configuración bimodal: PostgreSQL en entornos Cloud mediante DATABASE_URL
# y fallback automático a SQLite para desarrollo local y ejecución de tests.
DATABASES = {
    'default': dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# Modelo de Usuario Personalizado
AUTH_USER_MODEL = 'accounts_auth.User'

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

# Regionalización y Localización para Centroamérica (Nicaragua Core)
LANGUAGE_CODE = 'es-ni'
TIME_ZONE = 'America/Managua'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Configuración de Autenticación y Redirecciones B2B
LOGIN_URL = 'accounts_auth:login'
LOGIN_REDIRECT_URL = 'tenancy:dashboard'
LOGOUT_REDIRECT_URL = 'accounts_auth:login'

# Hardening de Seguridad HTTP para Producción
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'SAMEORIGIN'
REFERRER_POLICY = 'strict-origin-when-cross-origin'

# Políticas de Cookies Seguras (SEC-05)
# En producción (DEBUG=False), las cookies de sesión y CSRF exigen transporte HTTPS.
# En desarrollo local (DEBUG=True), se permite transporte HTTP para testing local.
SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', str(not DEBUG)).lower() in ('true', '1', 'yes')
CSRF_COOKIE_SECURE = os.environ.get('CSRF_COOKIE_SECURE', str(not DEBUG)).lower() in ('true', '1', 'yes')
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # Permitir lectura en cliente para inyección en cabeceras X-CSRFToken

