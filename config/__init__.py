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


def _coalesce_version(*candidates: Optional[str], fallback: str = "dev") -> str:
    """Return the first non-empty version string from the provided candidates."""

    for candidate in candidates:
        if not candidate:
            continue

        value = str(candidate).strip()
        if value:
            return value

    return fallback


def _env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    """Return the first defined/non-empty environment variable from *names."""

    for name in names:
        if not name:
            continue

        value = os.getenv(name)
        if value is None:
            continue

        value = value.strip()
        if value == "":
            continue

        return value

    return default


def _env_flag(default: bool, *names: str) -> bool:
    """Return a boolean flag honoring multiple environment variable aliases."""

    raw_value = _env_first(*names)
    if raw_value is None:
        return default

    return raw_value.strip().lower() in {"1", "true", "t", "yes", "y"}


# Backend/Frontend version identifiers exposed through /api/version so admins can
# double check deployed revisions from the UI without forcing a cache reset.
DEFAULT_FRONTEND_VERSION = _coalesce_version(
    os.getenv("FRONTEND_VERSION"),
    os.getenv("APP_VERSION"),
    os.getenv("VITE_APP_VERSION"),
    os.getenv("NEXT_PUBLIC_APP_VERSION"),
)

DEFAULT_BACKEND_VERSION = _coalesce_version(
    os.getenv("BACKEND_VERSION"),
    os.getenv("SOURCE_VERSION"),  # Heroku style
    os.getenv("RENDER_GIT_COMMIT"),
    os.getenv("GIT_COMMIT"),
    os.getenv("GITHUB_SHA"),
    os.getenv("VERCEL_GIT_COMMIT_SHA"),
)

# --- Variables de Entorno para Despliegue ---
ENV = os.getenv("ENV", "dev")  # "dev" o "prod"


def _is_render_runtime() -> bool:
    return os.getenv("RENDER", "").strip().lower() == "true" or bool(os.getenv("RENDER_EXTERNAL_URL"))

# Render provides the public URL of the service through RENDER_EXTERNAL_URL.
# If BACKEND_URL is not explicitly set we fall back to that value so the
# frontend can discover the correct origin via /api/config.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
BACKEND_URL = os.getenv("BACKEND_URL", RENDER_EXTERNAL_URL or "http://localhost:5000")

# Public participation surveys share image (also used for WhatsApp thumbnails)
ENCUESTAS_DEFAULT_SHARE_IMAGE_PATH = (
    "https://chatboc-demo-widget-oigs.vercel.app/junin/participacion_ciudadana.png"
)
# Local/static fallback used when WhatsApp needs an asset hosted on the backend
ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_PATH = (
    os.getenv(
        "ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_PATH",
        "/static/encuestas/participacion_ciudadana.png",
    )
)
# Optional WhatsApp template to show a banner before the encuestas menu
PUBLIC_ENCUESTAS_WHATSAPP_BANNER_TEMPLATE_SID = os.getenv(
    "PUBLIC_ENCUESTAS_WHATSAPP_BANNER_TEMPLATE_SID"
)
# Caption used when falling back to a media message for the banner
PUBLIC_ENCUESTAS_WHATSAPP_BANNER_BODY = os.getenv(
    "PUBLIC_ENCUESTAS_WHATSAPP_BANNER_BODY",
    "Encuestas/Opiniones/Sondeos",
)
# Optional media URL associated with the banner template so menus reuse the
# same artwork as the Twilio pre-message.
PUBLIC_ENCUESTAS_WHATSAPP_BANNER_MEDIA_URL = os.getenv(
    "PUBLIC_ENCUESTAS_WHATSAPP_BANNER_MEDIA_URL"
)

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

    DEBUG = ENV == "dev"

    # Public URLs exposed to the frontend. Keeping them in the Flask config
    # ensures endpoints like /api/config can always read them without having
    # to import the module-level constants.
    BACKEND_URL = str(BACKEND_URL)
    PANEL_URL = str(PANEL_URL)
    WIDGET_URL = str(WIDGET_URL)

    # Version identifiers surfaced through /api/version so that the Admin UI
    # can display the active frontend/backend revisions.
    FRONTEND_VERSION = DEFAULT_FRONTEND_VERSION
    BACKEND_VERSION = DEFAULT_BACKEND_VERSION

    # Maps provider configuration
    MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
    MAPS_DEFAULT_PROVIDER = os.getenv("MAPS_DEFAULT_PROVIDER", "google")

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
    SESSION_SQLALCHEMY_TABLE = 'flask_sessions'
    RATELIMIT_STORAGE_URI = _env_first(
        "RATELIMIT_STORAGE_URI",
        "REDIS_URL",
        "UPSTASH_REDIS_URL",
        default="memory://",
    )
    # Nombre del cookie adicional que almacena el token de acceso como
    # respaldo en caso de que la sesión basada en cookies falle
    AUTH_TOKEN_COOKIE_NAME = os.getenv("AUTH_TOKEN_COOKIE_NAME", "auth_token")
    DEFER_ANON_MIGRATION_ON_LOGIN = os.getenv("DEFER_ANON_MIGRATION_ON_LOGIN", "true").strip().lower() not in {"0", "false", "no", "off"}

    # Runtime bootstrap guards: in production, schema sync and tenant init must be explicit
    # via migrations/CLI. Local dev keeps convenience defaults enabled.
    _runtime_bootstrap_default = ENV == "dev" and not _is_render_runtime()
    ENABLE_RUNTIME_SCHEMA_SYNC = _env_flag(
        _runtime_bootstrap_default,
        "ENABLE_RUNTIME_SCHEMA_SYNC",
        "FLASK_ENABLE_RUNTIME_SCHEMA_SYNC",
    )
    ENABLE_RUNTIME_TENANT_INIT = _env_flag(
        _runtime_bootstrap_default,
        "ENABLE_RUNTIME_TENANT_INIT",
        "FLASK_ENABLE_RUNTIME_TENANT_INIT",
    )
    # Demo placeholders and synthetic catalogs must be explicitly enabled.
    ENABLE_DEMO_MODE = _env_flag(
        False,
        "ENABLE_DEMO_MODE",
        "FLASK_ENABLE_DEMO_MODE",
    )
    # Cookie aislada para los tokens emitidos al widget embebido.  Evita que
    # los tokens de corta duración del widget reemplacen la sesión del panel.
    WIDGET_TOKEN_COOKIE_NAME = os.getenv("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    WIDGET_JWT_ALG = _env_first("WIDGET_JWT_ALG", default="HS256")
    WIDGET_JWT_KID = _env_first("WIDGET_JWT_KID", default="widget-hs256")
    WIDGET_JWT_SECRET = _env_first("WIDGET_JWT_SECRET", "SECRET_KEY", default=SECRET_KEY)
    WIDGET_JWT_PRIVATE_KEY = _env_first("WIDGET_JWT_PRIVATE_KEY")
    WIDGET_JWT_PUBLIC_KEY = _env_first("WIDGET_JWT_PUBLIC_KEY")

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

    WEBAUTHN_RP_ID = os.getenv("WEBAUTHN_RP_ID", "chatboc.ar")
    WEBAUTHN_RP_NAME = os.getenv("WEBAUTHN_RP_NAME", "Chatboc")
    WEBAUTHN_EXPECTED_ORIGIN = os.getenv("WEBAUTHN_EXPECTED_ORIGIN", "https://www.chatboc.ar")

    DEMO_MAX_MESSAGES_PER_SESSION = int(os.getenv("DEMO_MAX_MESSAGES_PER_SESSION", "5"))
    DEMO_WELCOME_MESSAGE = os.getenv(
        "DEMO_WELCOME_MESSAGE",
        "Hola, ya tengo tu demo lista. Escribime una consulta, adjunta una imagen, manda una ubicacion o elegi una accion disponible para probar el agente.",
    )
    DEMO_RUBROS = _load_default_demo_rubros()

    GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", None)
    GOOGLE_DOCAI_LOCATION = os.getenv("GOOGLE_DOCAI_LOCATION", "us")
    GOOGLE_DOCAI_PROCESSOR_ID = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID", None)
    GOOGLE_APPLICATION_CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", None)

    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

    # Valores por defecto orientados a Zoho; pueden sobrescribirse mediante múltiples alias
    SMTP_HOST = _env_first("SMTP_HOST", "MAIL_SERVER", "MAIL_HOST", default="smtp.zoho.com")
    SMTP_PORT = int(
        _env_first("SMTP_PORT", "MAIL_PORT", default=str(587))
    )
    SMTP_USER = _env_first(
        "SMTP_USER",
        "SMTP_USERNAME",
        "MAIL_USERNAME",
        "MAIL_USER",
        "MAIL_FROM_ADDRESS",
        default="info@chatboc.ar",
    )
    # No se proporciona contraseña por defecto para evitar uso accidental de credenciales personales
    SMTP_PASSWORD = _env_first(
        "SMTP_PASSWORD",
        "SMTP_PASS",
        "MAIL_PASSWORD",
        default="",
    )
    SMTP_USE_TLS = _env_flag(True, "SMTP_USE_TLS", "MAIL_USE_TLS", "SMTP_TLS")
    SMTP_USE_SSL = _env_flag(False, "SMTP_USE_SSL", "MAIL_USE_SSL", "SMTP_SSL")

    MAIL_FROM_ADDRESS = _env_first(
        "MAIL_FROM_ADDRESS",
        "MAIL_DEFAULT_SENDER",
        "MAIL_SENDER",
        default=SMTP_USER if SMTP_USER else "noreply@example.com",
    )
    MAIL_FROM_NAME = _env_first(
        "MAIL_FROM_NAME",
        "MAIL_SENDER_NAME",
        "MAIL_DEFAULT_NAME",
        default="Chatboc Platform",
    )

    SMTP_HOST_CAMPAIGN = _env_first(
        "SMTP_HOST_CAMPAIGN",
        "MAIL_SERVER_CAMPAIGN",
        "MAIL_HOST_CAMPAIGN",
        default=SMTP_HOST,
    )
    SMTP_PORT_CAMPAIGN = int(
        _env_first("SMTP_PORT_CAMPAIGN", "MAIL_PORT_CAMPAIGN", default=str(SMTP_PORT))
    )
    SMTP_USER_CAMPAIGN = _env_first(
        "SMTP_USER_CAMPAIGN",
        "SMTP_USERNAME_CAMPAIGN",
        "MAIL_USERNAME_CAMPAIGN",
        "MAIL_USER_CAMPAIGN",
        default=SMTP_USER,
    )
    SMTP_PASSWORD_CAMPAIGN = _env_first(
        "SMTP_PASSWORD_CAMPAIGN",
        "SMTP_PASS_CAMPAIGN",
        "MAIL_PASSWORD_CAMPAIGN",
        default=SMTP_PASSWORD,
    )
    SMTP_USE_TLS_CAMPAIGN = _env_flag(
        SMTP_USE_TLS,
        "SMTP_USE_TLS_CAMPAIGN",
        "MAIL_USE_TLS_CAMPAIGN",
    )
    SMTP_USE_SSL_CAMPAIGN = _env_flag(
        SMTP_USE_SSL,
        "SMTP_USE_SSL_CAMPAIGN",
        "MAIL_USE_SSL_CAMPAIGN",
    )
    MAIL_FROM_ADDRESS_CAMPAIGN = _env_first(
        "MAIL_FROM_ADDRESS_CAMPAIGN",
        "MAIL_SENDER_CAMPAIGN",
        "MAIL_DEFAULT_SENDER_CAMPAIGN",
        default=MAIL_FROM_ADDRESS,
    )
    MAIL_FROM_NAME_CAMPAIGN = _env_first(
        "MAIL_FROM_NAME_CAMPAIGN",
        "MAIL_SENDER_NAME_CAMPAIGN",
        "MAIL_DEFAULT_NAME_CAMPAIGN",
        default=MAIL_FROM_NAME,
    )

    ANALYTICS_ENABLED = os.getenv("ANALYTICS_ENABLED", "true").lower() in {"1", "true", "yes"}
    ANALYTICS_CACHE_TTL = int(os.getenv("ANALYTICS_CACHE_TTL", "600"))
    ANALYTICS_CACHE_MAX_ITEMS = int(os.getenv("ANALYTICS_CACHE_MAX_ITEMS", "1024"))

    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
    TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
    TWILIO_FALLBACK_VOICE = os.getenv("TWILIO_FALLBACK_VOICE", "Polly.Lupe-Neural")
    TWILIO_FALLBACK_SAY_LANGUAGE = os.getenv("TWILIO_FALLBACK_SAY_LANGUAGE", "es-US")
    TWILIO_GATHER_LANGUAGE = os.getenv("TWILIO_GATHER_LANGUAGE", "es-AR")
    TWILIO_META_APP_ID = os.getenv("TWILIO_META_APP_ID")
    TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID = os.getenv("TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID")
    TWILIO_PARTNER_SOLUTION_ID = os.getenv("TWILIO_PARTNER_SOLUTION_ID")
    TWILIO_TECH_PROVIDER_LIVE_ENABLED = os.getenv(
        "TWILIO_TECH_PROVIDER_LIVE_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    TWILIO_TECH_PROVIDER_SECRET_REF_PREFIX = os.getenv(
        "TWILIO_TECH_PROVIDER_SECRET_REF_PREFIX",
        "TWILIO_SUBACCOUNT_AUTH_TOKEN",
    )
    TWILIO_TENANT_AUTO_BOOTSTRAP_ENABLED = os.getenv(
        "TWILIO_TENANT_AUTO_BOOTSTRAP_ENABLED",
        "true",
    ).strip().lower() in {"1", "true", "yes", "on"}
    TWILIO_TENANT_AUTO_PROVISION_ENABLED = os.getenv(
        "TWILIO_TENANT_AUTO_PROVISION_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    RENDER_ENV_SYNC_ENABLED = os.getenv("RENDER_ENV_SYNC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    RENDER_ENV_SYNC_TRIGGER_DEPLOY_ENABLED = os.getenv(
        "RENDER_ENV_SYNC_TRIGGER_DEPLOY_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    RENDER_API_KEY = os.getenv("RENDER_API_KEY")
    RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID") or os.getenv("RENDER_BACKEND_SERVICE_ID")
    RENDER_BACKEND_SERVICE_ID = os.getenv("RENDER_BACKEND_SERVICE_ID")
    RENDER_ENV_GROUP_ID = os.getenv("RENDER_ENV_GROUP_ID")
    RENDER_API_BASE_URL = os.getenv("RENDER_API_BASE_URL", "https://api.render.com/v1")
    RENDER_API_TIMEOUT_SECONDS = os.getenv("RENDER_API_TIMEOUT_SECONDS", "20")
    RENDER_ENV_SYNC_CLEAR_CACHE = os.getenv("RENDER_ENV_SYNC_CLEAR_CACHE", "do_not_clear")

    APP_BASE_URL = os.getenv("APP_BASE_URL", "https://chatboc.ar")

    # If in production-like environment (not local debug/test) and env var is missing or default,
    # reinforce the domain to ensure we don't accidentally use localhost defaults elsewhere
    if ENV != "dev" and (not APP_BASE_URL or "localhost" in APP_BASE_URL):
        APP_BASE_URL = "https://chatboc.ar"

    PUBLIC_ENCUESTAS_WHATSAPP_BANNER_TEMPLATE_SID = (
        PUBLIC_ENCUESTAS_WHATSAPP_BANNER_TEMPLATE_SID
    )
    PUBLIC_ENCUESTAS_WHATSAPP_BANNER_BODY = PUBLIC_ENCUESTAS_WHATSAPP_BANNER_BODY
    PUBLIC_ENCUESTAS_WHATSAPP_BANNER_MEDIA_URL = (
        PUBLIC_ENCUESTAS_WHATSAPP_BANNER_MEDIA_URL
    )

    PUBLIC_CATALOG_DEFAULT_TENANT = os.getenv("PUBLIC_CATALOG_DEFAULT_TENANT", "municipio")

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

    _encuestas_qr_target_base_url = os.getenv("PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL")
    if _encuestas_qr_target_base_url:
        PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL = _encuestas_qr_target_base_url.rstrip("/")
    else:
        PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL = PUBLIC_ENCUESTAS_CANONICAL_BASE_URL

    _encuestas_default_share_image = os.getenv("PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL")
    if isinstance(_encuestas_default_share_image, str) and _encuestas_default_share_image.strip():
        PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL = _encuestas_default_share_image.strip()
    else:
        asset_candidate = ENCUESTAS_DEFAULT_SHARE_IMAGE_PATH
        if isinstance(asset_candidate, str) and asset_candidate.startswith(("http://", "https://")):
            PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL = asset_candidate
        else:
            base_for_assets = (
                PUBLIC_ENCUESTAS_CANONICAL_BASE_URL
                or PUBLIC_ENCUESTAS_API_BASE_URL
                or str(BACKEND_URL)
            )
            if base_for_assets and asset_candidate:
                PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL = (
                    f"{base_for_assets.rstrip('/')}"
                    f"{asset_candidate}"
                )
            else:
                PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL = None

    _encuestas_media_fallback_url = os.getenv(
        "PUBLIC_ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_URL"
    )
    if (
        isinstance(_encuestas_media_fallback_url, str)
        and _encuestas_media_fallback_url.strip()
    ):
        PUBLIC_ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_URL = (
            _encuestas_media_fallback_url.strip()
        )
    else:
        fallback_candidate = ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_PATH
        fallback_candidate = fallback_candidate.strip() if isinstance(
            fallback_candidate, str
        ) else ""
        if fallback_candidate:
            if fallback_candidate.startswith(("http://", "https://")):
                PUBLIC_ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_URL = fallback_candidate
            else:
                base_for_media = (
                    PUBLIC_ENCUESTAS_CANONICAL_BASE_URL
                    or PUBLIC_ENCUESTAS_API_BASE_URL
                    or str(BACKEND_URL)
                )
                if base_for_media:
                    if not fallback_candidate.startswith("/"):
                        fallback_candidate = "/" + fallback_candidate
                    PUBLIC_ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_URL = (
                        f"{base_for_media.rstrip('/')}"
                        f"{fallback_candidate}"
                    )
                else:
                    PUBLIC_ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_URL = None
        else:
            PUBLIC_ENCUESTAS_DEFAULT_SHARE_MEDIA_FALLBACK_URL = None

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
        "/static/welcome/juni-saludo-sticker.webp",
    )
    CHATBOC_DEMO_WELCOME_MEDIA_URL = os.getenv(
        "CHATBOC_DEMO_WELCOME_MEDIA_URL",
        "/static/welcome/chatboc-saludo-sticker.webp",
    )
    WELCOME_AUDIO_URL = os.getenv(
        "WELCOME_AUDIO_URL",
        "https://chatboc-demo-widget-oigs.vercel.app/saludo_inicial_juni.mp3",
    )
    WELCOME_STICKER_COOLDOWN_SECONDS = int(
        os.getenv("WELCOME_STICKER_COOLDOWN_SECONDS", "300")
    )

INSECURE_SECRET_MARKERS = {
    "",
    "changeme",
    "change_me",
    "secret",
    "default",
    "una-llave-secreta-muy-segura-para-desarrollo-local",
}


def validate_runtime_security(config: Any) -> list[str]:
    """Return runtime security errors for production-like environments."""

    errors: list[str] = []
    env_value = str(getattr(config, "get", lambda *_: None)("ENV", ENV) or ENV).strip().lower()
    is_production = env_value in {"prod", "production"}
    if not is_production:
        return errors

    secret_key = str(getattr(config, "get", lambda *_: None)("SECRET_KEY", "") or "").strip()
    if not secret_key or secret_key.lower() in INSECURE_SECRET_MARKERS or len(secret_key) < 24:
        errors.append("SECRET_KEY insegura para producción.")

    debug_enabled = bool(getattr(config, "get", lambda *_: None)("DEBUG", False))
    if debug_enabled:
        errors.append("DEBUG=True no está permitido en producción.")

    return errors


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SQLALCHEMY_ENGINE_OPTIONS = {'connect_args': {'timeout': 5}}
    CELERY_TASK_ALWAYS_EAGER = True
    SESSION_COOKIE_SECURE = False
    SERVER_NAME = 'localhost'
    SESSION_COOKIE_DOMAIN = None
    SESSION_TYPE = 'null'

class TestingConfig(TestConfig):
    pass
