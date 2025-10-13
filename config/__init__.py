import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Directorio base de la aplicación
basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "-3"))

_logger = logging.getLogger(__name__)


def _normalize_domain(value: object) -> Optional[str]:
    """Return a sanitized host/domain value suitable for lookups."""

    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    text = re.sub(r"^[a-zA-Z]+://", "", text)
    text = text.split("/")[0]
    text = text.split(":")[0]
    text = text.strip().lower()
    return text or None


def _parse_public_encuestas_domain_map(raw_value: Optional[str]) -> Dict[str, int]:
    """Parse mapping definitions for public survey tenant resolution."""

    mapping: Dict[str, int] = {}
    if not raw_value:
        return mapping

    try:
        loaded = json.loads(raw_value)
    except json.JSONDecodeError:
        loaded = {}
        for chunk in raw_value.split(","):
            if not chunk.strip():
                continue
            if "=" in chunk:
                domain_part, tenant_part = chunk.split("=", 1)
            elif ":" in chunk:
                domain_part, tenant_part = chunk.split(":", 1)
            else:
                _logger.warning(
                    "[config] Invalid PUBLIC_ENCUESTAS_DOMAIN_MAP entry '%s'. Use 'domain=tenant_id' format.",
                    chunk.strip(),
                )
                continue
            loaded[domain_part.strip()] = tenant_part.strip()
    else:
        if not isinstance(loaded, dict):
            _logger.warning(
                "[config] PUBLIC_ENCUESTAS_DOMAIN_MAP must be a JSON object or 'domain=tenant' list."
            )
            loaded = {}

    for domain, tenant in loaded.items():
        normalized = _normalize_domain(domain)
        if not normalized:
            continue
        try:
            tenant_id = int(tenant)
        except (TypeError, ValueError):
            _logger.warning(
                "[config] Invalid tenant id '%s' for domain '%s' in PUBLIC_ENCUESTAS_DOMAIN_MAP.",
                tenant,
                domain,
            )
            continue

        mapping[normalized] = tenant_id
        if normalized.startswith("www."):
            bare = normalized[4:]
            if bare:
                mapping.setdefault(bare, tenant_id)
        else:
            mapping.setdefault(f"www.{normalized}", tenant_id)

    return mapping

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

# --- Demo Rubros Loader ----------------------------------------------------

_DEMO_RUBRO_ENV_FIELDS = {
    "key": "KEY",
    "nombre": "NOMBRE",
    "descripcion": "DESCRIPCION",
    "token": "TOKEN",
    "tipo_chat": "TIPO_CHAT",
    "rubro_clave": "RUBRO",
    "prompt_context": "PROMPT_CONTEXT",
    "welcome_message": "WELCOME_MESSAGE",
}


def _coerce_demo_rubros_payload(payload: Any) -> List[Dict[str, Any]]:
    """Return a list of demo rubros from a JSON payload."""

    if isinstance(payload, dict):
        for key in ("demo_rubros", "rubros", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                payload = value
                break
        else:
            return []

    if not isinstance(payload, list):
        return []

    resultados: List[Dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict):
            resultados.append(item)
    return resultados


def _read_demo_rubros_file(path: str) -> List[Dict[str, Any]]:
    """Load demo rubros metadata from a JSON file."""

    file_path = Path(path)
    if not file_path.is_file():
        logging.getLogger(__name__).warning(
            "Demo rubros file '%s' not found. The demo catalog will be empty.", path
        )
        return []

    try:
        raw_content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).error(
            "Unable to read demo rubros file '%s': %s", path, exc
        )
        return []

    if not raw_content.strip():
        return []

    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        logging.getLogger(__name__).error(
            "Invalid JSON in demo rubros file '%s': %s", path, exc
        )
        return []

    return _coerce_demo_rubros_payload(payload)


def _sanitize_demo_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Clone an entry removing helper fields and copying nested values."""

    sanitized: Dict[str, Any] = {}
    for key, value in entry.items():
        if key == "env_prefix":
            continue
        if key in {"resources", "faq_preview"} and isinstance(value, list):
            sanitized[key] = [dict(item) for item in value if isinstance(item, dict)]
        else:
            sanitized[key] = value
    return sanitized


def _apply_env_overrides(entry: Dict[str, Any], env_prefix: Any) -> Dict[str, Any]:
    """Override demo metadata fields with environment variables when available."""

    prefix: str = ""
    if isinstance(env_prefix, str) and env_prefix.strip():
        prefix = env_prefix.strip()
    else:
        raw_key = entry.get("key")
        if isinstance(raw_key, str) and raw_key.strip():
            normalized = re.sub(r"[^A-Z0-9]+", "_", raw_key.upper()).strip("_")
            if normalized:
                prefix = f"DEMO_{normalized}"

    if not prefix:
        return entry

    overridden = dict(entry)
    for field, suffix in _DEMO_RUBRO_ENV_FIELDS.items():
        env_name = f"{prefix}_{suffix}"
        value = os.getenv(env_name)
        if value is None:
            continue
        if field == "token" and value == "":
            value = None
        overridden[field] = value

    return overridden


def _load_default_demo_rubros() -> List[Dict[str, Any]]:
    """Load the curated demo catalog from JSON and apply environment overrides."""

    entries: List[Dict[str, Any]] = []
    loaded_from_env = False
    json_override = os.getenv("DEMO_RUBROS_JSON")
    if json_override:
        try:
            payload = json.loads(json_override)
        except json.JSONDecodeError as exc:
            logging.getLogger(__name__).error(
                "Invalid JSON provided in DEMO_RUBROS_JSON: %s", exc
            )
        else:
            entries = _coerce_demo_rubros_payload(payload)
            loaded_from_env = True

    if not entries and not loaded_from_env:
        default_path = os.getenv("DEMO_RUBROS_FILE") or os.path.join(
            basedir, "data", "demo_rubros.json"
        )
        entries = _read_demo_rubros_file(default_path)

    normalized: List[Dict[str, Any]] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        env_prefix = raw_entry.get("env_prefix")
        sanitized = _sanitize_demo_entry(raw_entry)
        normalized.append(_apply_env_overrides(sanitized, env_prefix))

    return normalized

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
    # Cookie aislada para los tokens emitidos al widget embebido.  Evita que
    # los tokens de corta duración del widget reemplacen la sesión del panel.
    WIDGET_TOKEN_COOKIE_NAME = os.getenv("WIDGET_TOKEN_COOKIE_NAME", "widget_token")

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
        "🎯 Bienvenido al showroom interactivo de Chatboc. Elegí el rubro que querés explorar y descubrí catálogos, pedidos y herramientas inteligentes en segundos.",
    )
    DEMO_RUBROS = _load_default_demo_rubros()

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

    ANALYTICS_ENABLED = os.getenv("ANALYTICS_ENABLED", "true").lower() in {"1", "true", "yes"}
    ANALYTICS_CACHE_TTL = int(os.getenv("ANALYTICS_CACHE_TTL", "600"))
    ANALYTICS_CACHE_MAX_ITEMS = int(os.getenv("ANALYTICS_CACHE_MAX_ITEMS", "1024"))

    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
    TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5000")

    _encuestas_default = os.getenv("PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID")
    if _encuestas_default is None or _encuestas_default == "":
        PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID = 4
    else:
        normalized_default = _encuestas_default.strip().lower()
        if normalized_default in {"none", "null"}:
            PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID = None
        else:
            try:
                PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID = int(_encuestas_default)
            except ValueError:
                PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID = 4
                _logger.warning(
                    "[config] Ignoring invalid PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID value '%s'.",
                    _encuestas_default,
                )

    PUBLIC_ENCUESTAS_DOMAIN_MAP = _parse_public_encuestas_domain_map(
        os.getenv("PUBLIC_ENCUESTAS_DOMAIN_MAP")
    )

    _encuestas_base_url = os.getenv("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
    if _encuestas_base_url:
        PUBLIC_ENCUESTAS_CANONICAL_BASE_URL = _encuestas_base_url.rstrip("/")
    else:
        fallback_domain = (public_root or "").strip().lower()
        fallback_url = None

        if fallback_domain and fallback_domain not in {"localhost", "127.0.0.1"}:
            if fallback_domain.startswith("http://") or fallback_domain.startswith("https://"):
                fallback_url = fallback_domain
            else:
                normalized_domain = fallback_domain.lstrip("www.")
                if normalized_domain.count(".") == 1:
                    normalized_domain = f"www.{normalized_domain}"
                fallback_url = f"https://{normalized_domain}"

        if not fallback_url:
            fallback_url = BACKEND_URL

        PUBLIC_ENCUESTAS_CANONICAL_BASE_URL = str(fallback_url).rstrip("/")

    _encuestas_api_base_url = os.getenv("PUBLIC_ENCUESTAS_API_BASE_URL")
    if _encuestas_api_base_url:
        PUBLIC_ENCUESTAS_API_BASE_URL = _encuestas_api_base_url.rstrip("/")
    else:
        PUBLIC_ENCUESTAS_API_BASE_URL = str(BACKEND_URL).rstrip("/")

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
    WELCOME_MEDIA_URL = os.getenv(
        "WELCOME_MEDIA_URL",
        "https://chatboc-demo-widget-oigs.vercel.app/stickerJuni2.webp",
    )
    WELCOME_AUDIO_URL = os.getenv(
        "WELCOME_AUDIO_URL",
        "https://chatboc-demo-widget-oigs.vercel.app/saludo_inicial_juni.mp3",
    )
    WELCOME_STICKER_COOLDOWN_SECONDS = int(
        os.getenv("WELCOME_STICKER_COOLDOWN_SECONDS", "300")
    )

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    CELERY_TASK_ALWAYS_EAGER = True
    SESSION_COOKIE_SECURE = False
    SERVER_NAME = 'localhost'
    SESSION_COOKIE_DOMAIN = None
    SESSION_TYPE = 'null'

class TestingConfig(TestConfig):
    pass
