import os
import re
from urllib.parse import urlparse

# Directorio base de la aplicación
basedir = os.path.abspath(os.path.dirname(__file__))
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "-3"))

# --- Variables de Entorno para Despliegue ---
ENV = os.getenv("ENV", "dev")  # "dev" o "prod"

# Render provides the public URL of the service through RENDER_EXTERNAL_URL.
# If BACKEND_URL is not explicitly set we fall back to that value so the
# frontend can discover the correct origin via /api/config.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
BACKEND_URL = os.getenv("BACKEND_URL", RENDER_EXTERNAL_URL or "http://localhost:5000")

PANEL_URL = os.getenv("PANEL_URL", "http://localhost:8080")
WIDGET_URL = os.getenv("WIDGET_URL", "http://localhost:8080")

parsed_backend = urlparse(BACKEND_URL)
IS_HTTPS = parsed_backend.scheme == "https"

# CORS_ALLOWED_ORIGINS can override the default allowed origins.  When unset we
# allow the panel and widget URLs.  Values are cleaned of trailing slashes and
# duplicates are removed.
cors_env = os.getenv("CORS_ALLOWED_ORIGINS")
if cors_env:
    allowed_urls = [u.strip().rstrip('/') for u in cors_env.split(',') if u.strip()]
else:
    allowed_urls = [PANEL_URL.rstrip('/'), WIDGET_URL.rstrip('/')]

# Always add the root domain(s) so that the public widget can reach the API
host = parsed_backend.hostname
if host and host != "localhost":
    parts = host.split('.')
    if len(parts) >= 2:
        root_domain = ".".join(parts[-2:])
        allowed_urls.extend([
            f"https://{root_domain}",
            f"https://www.{root_domain}",
        ])

public_root = os.getenv("PUBLIC_ROOT_DOMAIN", "chatboc.ar")
if public_root and public_root not in ("localhost", "127.0.0.1"):
    allowed_urls.extend([
        f"https://{public_root}",
        f"https://www.{public_root}",
    ])

ALLOWED_ORIGINS = list(dict.fromkeys(allowed_urls))
# Allow Vercel preview deployments (e.g. https://<project>.vercel.app)
ALLOWED_ORIGINS.append(re.compile(r"https://.*\.vercel\.app"))

# Derive cookie domain for production if not provided explicitly
cookie_domain_env = os.getenv("COOKIE_DOMAIN")
if cookie_domain_env:
    COOKIE_DOMAIN = cookie_domain_env
elif ENV != "dev" and parsed_backend.hostname:
    parts = parsed_backend.hostname.split('.')
    COOKIE_DOMAIN = f".{parts[-2]}.{parts[-1]}" if len(parts) >= 2 else None
else:
    COOKIE_DOMAIN = None

class Config:
    """
    Clase de configuración principal de la aplicación.
    Contiene todas las variables de configuración.
    """

    DEBUG = True

    # 1. LLAVE SECRETA
    SECRET_KEY = os.getenv("SECRET_KEY", "una-llave-secreta-muy-segura-para-desarrollo-local")

    # 2. CONFIGURACIÓN DE LA BASE DE DATOS
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        SQLALCHEMY_DATABASE_URI = db_url
    elif os.getenv("RENDER") == "true":
        db_path_render = "/data/database.db"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path_render}?check_same_thread=False"
    else:
        local_db_path = os.path.join(basedir, 'instance', 'database.db')
        os.makedirs(os.path.dirname(local_db_path), exist_ok=True)
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{local_db_path}?check_same_thread=False"

    # Directory for persistent data such as uploaded media.
    DATA_DIR = os.getenv("DATA_DIR", "/data")

    if SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
        SQLALCHEMY_ENGINE_OPTIONS = {'connect_args': {'timeout': 5}}
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {
            'pool_size': 10,
            'max_overflow': 20,
            'pool_pre_ping': True,
            'pool_recycle': 1800,
        }
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 3. CONFIGURACIÓN DE COOKIES DE SESIÓN (MODO DEV/PROD)
    SESSION_COOKIE_DOMAIN = (None if ENV == "dev" else COOKIE_DOMAIN)
    SESSION_COOKIE_SECURE = (ENV == "prod") or IS_HTTPS
    SESSION_COOKIE_SAMESITE = "None"

    # Flask-Login "remember me" cookie settings
    REMEMBER_COOKIE_SAMESITE = "None"
    REMEMBER_COOKIE_SECURE = (ENV == "prod") or IS_HTTPS

    SESSION_TYPE = 'sqlalchemy'
    SESSION_SQLALCHEMY_TABLE = 'sessions'
    # Nombre del cookie adicional que almacena el token de acceso como
    # respaldo en caso de que la sesión basada en cookies falle
    AUTH_TOKEN_COOKIE_NAME = os.getenv("AUTH_TOKEN_COOKIE_NAME", "auth_token")

    # 4. RESTO DE LA CONFIGURACIÓN...
    ATTENTION_BUBBLE_TEXT = os.getenv("ATTENTION_BUBBLE_TEXT", "¡Hola! ¿Necesitas ayuda?")
    ATTENTION_BUBBLE_CHOICES = [m.strip() for m in os.getenv(
        "ATTENTION_BUBBLE_CHOICES", ""
    ).split("|") if m.strip()] or None

    TIENDA_BASE_URL = os.getenv("TIENDA_BASE_URL", "")

    ANONYMOUS_MAX_MESSAGES_PER_SESSION = int(os.getenv("ANONYMOUS_MAX_MESSAGES_PER_SESSION", "10"))
    ANONYMOUS_SESSION_TIMEOUT_MINUTES = int(os.getenv("ANONYMOUS_SESSION_TIMEOUT_MINUTES", "15"))
    ANONYMOUS_MAX_TICKETS_PER_SESSION = int(os.getenv("ANONYMOUS_MAX_TICKETS_PER_SESSION", "1"))
    ALLOW_ANON_GPS = os.getenv("ALLOW_ANON_GPS", "false").lower() == "true"
    PERMISSIONS_POLICY_HEADER = os.getenv("PERMISSIONS_POLICY_HEADER", "geolocation=(self)")
    TICKETS_PER_PAGE_DEFAULT = int(os.getenv("TICKETS_PER_PAGE_DEFAULT", "50"))

    ANON_SESSION_COOKIE_NAME = os.getenv("ANON_SESSION_COOKIE_NAME", "chatboc_anon_id")
    ANON_SESSION_COOKIE_MAX_AGE = int(os.getenv("ANON_SESSION_COOKIE_MAX_AGE", str(60 * 60 * 24 * 30)))

    DEMO_MAX_MESSAGES_PER_SESSION = int(os.getenv("DEMO_MAX_MESSAGES_PER_SESSION", "5"))
    DEMO_WELCOME_MESSAGE = os.getenv(
        "DEMO_WELCOME_MESSAGE",
        "👋 ¡Bienvenido a la demo de Chatboc! Elegí la experiencia que querés probar:",
    )
    DEMO_RUBROS = [
        {
            "key": os.getenv("DEMO_MUNICIPIO_KEY", "municipio"),
            "nombre": os.getenv("DEMO_MUNICIPIO_NOMBRE", "Municipio Inteligente"),
            "descripcion": os.getenv(
                "DEMO_MUNICIPIO_DESCRIPCION",
                "Descubrí cómo un municipio gestiona reclamos, trámites y consultas en segundos.",
            ),
            "token": os.getenv("DEMO_MUNICIPIO_TOKEN"),
            "tipo_chat": os.getenv("DEMO_MUNICIPIO_TIPO_CHAT", "municipio"),
            "rubro_clave": os.getenv("DEMO_MUNICIPIO_RUBRO", "municipio"),
        },
        {
            "key": os.getenv("DEMO_BODEGA_KEY", "bodega"),
            "nombre": os.getenv("DEMO_BODEGA_NOMBRE", "Bodega Cuatro Fincas"),
            "descripcion": os.getenv(
                "DEMO_BODEGA_DESCRIPCION",
                "Probá la experiencia de compra de una pyme: catálogo de vinos, precios y pedidos en vivo.",
            ),
            "token": os.getenv("DEMO_BODEGA_TOKEN", "demo-token-bodega"),
            "tipo_chat": os.getenv("DEMO_BODEGA_TIPO_CHAT", "pyme"),
            "rubro_clave": os.getenv("DEMO_BODEGA_RUBRO", "bodega"),
        },
    ]

    GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", None)
    GOOGLE_DOCAI_LOCATION = os.getenv("GOOGLE_DOCAI_LOCATION", "us")
    GOOGLE_DOCAI_PROCESSOR_ID = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID", None)
    GOOGLE_APPLICATION_CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", None)

    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

    # Valores por defecto orientados a Zoho; pueden sobrescribirse mediante variables de entorno
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.zoho.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER = os.getenv("SMTP_USER", "info@chatboc.ar")
    # No se proporciona contraseña por defecto para evitar uso accidental de credenciales personales
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "True").lower() in ('true', '1', 't')
    SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "False").lower() in ('true', '1', 't')

    MAIL_FROM_ADDRESS = os.getenv("MAIL_FROM_ADDRESS", SMTP_USER if SMTP_USER else "noreply@example.com")
    MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Chatboc Platform")

    SMTP_HOST_CAMPAIGN = os.getenv("SMTP_HOST_CAMPAIGN", SMTP_HOST)
    SMTP_PORT_CAMPAIGN = int(os.getenv("SMTP_PORT_CAMPAIGN", SMTP_PORT))
    SMTP_USER_CAMPAIGN = os.getenv("SMTP_USER_CAMPAIGN", SMTP_USER)
    SMTP_PASSWORD_CAMPAIGN = os.getenv("SMTP_PASSWORD_CAMPAIGN", SMTP_PASSWORD)
    SMTP_USE_TLS_CAMPAIGN = os.getenv("SMTP_USE_TLS_CAMPAIGN", str(SMTP_USE_TLS)).lower() in ('true', '1', 't')
    SMTP_USE_SSL_CAMPAIGN = os.getenv("SMTP_USE_SSL_CAMPAIGN", str(SMTP_USE_SSL)).lower() in ('true', '1', 't')
    MAIL_FROM_ADDRESS_CAMPAIGN = os.getenv("MAIL_FROM_ADDRESS_CAMPAIGN", MAIL_FROM_ADDRESS)
    MAIL_FROM_NAME_CAMPAIGN = os.getenv("MAIL_FROM_NAME_CAMPAIGN", MAIL_FROM_NAME)

    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
    TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5000")

    PYME_UMBRAL_SUGERENCIA_REGISTRO = int(os.getenv("PYME_UMBRAL_SUGERENCIA_REGISTRO", "3"))
    MUNICIPIO_UMBRAL_SUGERENCIA_REGISTRO = int(os.getenv("MUNICIPIO_UMBRAL_SUGERENCIA_REGISTRO", "3"))

    # 5. PUSHER CONFIGURATION
    PUSHER_APP_ID = os.getenv("PUSHER_APP_ID")
    PUSHER_KEY = os.getenv("PUSHER_KEY")
    PUSHER_SECRET = os.getenv("PUSHER_SECRET")
    PUSHER_CLUSTER = os.getenv("PUSHER_CLUSTER")

    # WhatsApp Welcome Message Configuration
    WELCOME_TEMPLATE_SID = os.getenv("WELCOME_TEMPLATE_SID", "HXaf135ced6edd005551a456bbb2258d4a")
    WELCOME_MESSAGE_DELAY_SECONDS = int(os.getenv("WELCOME_MESSAGE_DELAY_SECONDS", "5"))

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    CELERY_TASK_ALWAYS_EAGER = True
    SESSION_COOKIE_SECURE = False
    SERVER_NAME = 'localhost'
    SESSION_COOKIE_DOMAIN = None

class TestingConfig(TestConfig):
    pass
