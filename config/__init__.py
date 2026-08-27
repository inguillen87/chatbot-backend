import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

from utils.runtime_environment import (
    is_production_runtime,
    is_render_runtime,
    is_vercel_runtime,
    resolved_runtime_environment,
)

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


def _resolve_backend_version(
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve the deployed backend revision before manual fallbacks.

    ``CHATBOC_DEPLOYMENT_REVISION`` is an immutable, deployment-scoped value
    supplied by release automation. It is required for CLI/container releases,
    where Vercel may expose the Git integration SHA instead of the local
    worktree revision that produced the image. Platform-provided Git revisions
    remain the next-best source, while ``BACKEND_VERSION`` is only a manual
    fallback because copied project variables can become stale.
    """

    runtime_env = os.environ if environ is None else environ
    platform_revisions: List[Optional[str]] = []
    if is_vercel_runtime(runtime_env):
        platform_revisions.append(runtime_env.get("VERCEL_GIT_COMMIT_SHA"))
    if is_render_runtime(runtime_env):
        platform_revisions.append(runtime_env.get("RENDER_GIT_COMMIT"))

    return _coalesce_version(
        runtime_env.get("CHATBOC_DEPLOYMENT_REVISION"),
        *platform_revisions,
        runtime_env.get("BACKEND_VERSION"),
        runtime_env.get("SOURCE_VERSION"),  # Heroku style
        runtime_env.get("GIT_COMMIT"),
        runtime_env.get("GITHUB_SHA"),
        runtime_env.get("VERCEL_GIT_COMMIT_SHA"),
        runtime_env.get("RENDER_GIT_COMMIT"),
    )


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


def _env_strict_opt_in(name: str) -> bool:
    """Parse an explicit rollout opt-in; missing or invalid values stay off."""

    raw_value = os.getenv(name)
    if raw_value is None:
        return False
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    _logger.error("[config] Invalid %s; feature remains disabled fail-closed.", name)
    return False


def _bounded_timeout_seconds(
    value: object,
    *,
    default: float,
    minimum: float = 0.1,
    maximum: float = 10.0,
) -> float:
    """Parse an operator timeout without allowing unbounded pool waits."""

    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(timeout):
        return default
    return min(max(timeout, minimum), maximum)


def _bounded_pool_count(
    value: object,
    *,
    default: int,
    minimum: int = 0,
    maximum: int = 50,
) -> int:
    """Parse a connection-pool count without allowing runaway autoscaling."""

    try:
        count = int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(count, minimum), maximum)


def build_database_engine_options(
    database_uri: object,
    *,
    connect_timeout_seconds: object = 2.0,
    pool_timeout_seconds: object = 2.0,
    pool_size: object = 10,
    max_overflow: object = 20,
) -> dict[str, Any]:
    """Build bounded SQLAlchemy options for SQLite or network databases."""

    uri = str(database_uri or "")
    if uri.startswith("sqlite"):
        return {"connect_args": {"timeout": 5}}

    connect_timeout = _bounded_timeout_seconds(
        connect_timeout_seconds,
        default=2.0,
        minimum=1.0,
    )
    pool_timeout = _bounded_timeout_seconds(
        pool_timeout_seconds,
        default=2.0,
    )
    bounded_pool_size = _bounded_pool_count(
        pool_size,
        default=10,
        minimum=1,
    )
    bounded_max_overflow = _bounded_pool_count(
        max_overflow,
        default=20,
    )
    return {
        "pool_size": bounded_pool_size,
        "max_overflow": bounded_max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        # DBAPI/libpq requires an integer connect_timeout. Round upward so an
        # operator value such as 1.5 seconds never becomes zero or unbounded.
        "connect_args": {"connect_timeout": int(math.ceil(connect_timeout))},
        "pool_timeout": pool_timeout,
    }


def resolve_database_uri(
    *,
    environ: Optional[Dict[str, str]] = None,
    base_dir: Optional[str] = None,
) -> str:
    """Resolve the database URI and fail closed on stateless Vercel runtimes."""

    runtime_env = os.environ if environ is None else environ
    configured = str(runtime_env.get("DATABASE_URL") or "").strip()
    if configured:
        return configured
    if is_vercel_runtime(runtime_env):
        raise RuntimeError(
            "DATABASE_URL es obligatoria en Vercel; SQLite efimero no es un almacenamiento valido."
        )
    if is_render_runtime(runtime_env):
        return "sqlite:////data/database.db?check_same_thread=False"

    resolved_base = base_dir or basedir
    local_db_path = os.path.join(resolved_base, "instance", "database.db")
    os.makedirs(os.path.dirname(local_db_path), exist_ok=True)
    return f"sqlite:///{local_db_path}?check_same_thread=False"


def _env_fail_closed_hold(default: bool, name: str) -> bool:
    """Parse a deletion hold; invalid values preserve data."""

    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    _logger.error("[config] Invalid %s; legal hold enabled fail-closed.", name)
    return True


# Backend/Frontend version identifiers exposed through /api/version so admins can
# double check deployed revisions from the UI without forcing a cache reset.
DEFAULT_FRONTEND_VERSION = _coalesce_version(
    os.getenv("FRONTEND_VERSION"),
    os.getenv("APP_VERSION"),
    os.getenv("VITE_APP_VERSION"),
    os.getenv("NEXT_PUBLIC_APP_VERSION"),
)

DEFAULT_BACKEND_VERSION = _resolve_backend_version()

# --- Variables de Entorno para Despliegue ---
# Resolve all production signals together.  A missing/stale ``ENV=dev`` must
# never enable development behavior on Render or when FLASK_ENV is production.
_CONFIGURED_ENV = os.getenv("ENV")
ENV = resolved_runtime_environment(config_env=_CONFIGURED_ENV)


def _is_render_runtime() -> bool:
    return is_render_runtime()


def _is_vercel_runtime() -> bool:
    return is_vercel_runtime()


IS_PRODUCTION_RUNTIME = is_production_runtime(config_env=_CONFIGURED_ENV)

# Render provides the public URL of the service through RENDER_EXTERNAL_URL.
# If BACKEND_URL is not explicitly set we fall back to that value so the
# frontend can discover the correct origin via /api/config.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
_VERCEL_HOST = (
    os.getenv("VERCEL_PROJECT_PRODUCTION_URL")
    or os.getenv("VERCEL_BRANCH_URL")
    or os.getenv("VERCEL_URL")
)
VERCEL_EXTERNAL_URL = f"https://{_VERCEL_HOST.strip()}" if _VERCEL_HOST else None
BACKEND_URL = os.getenv(
    "BACKEND_URL",
    RENDER_EXTERNAL_URL or VERCEL_EXTERNAL_URL or "http://localhost:5000",
)

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

def _normalize_cors_origin(value: object) -> Optional[str]:
    """Return an exact HTTP(S) origin suitable for credentialed CORS."""

    raw = str(value or "").strip().rstrip("/")
    if not raw or raw == "*":
        return None

    parsed = urlparse(raw)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return None

    try:
        parsed.port
    except ValueError:
        return None

    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _credential_site_root(backend_host: str) -> str:
    configured_root = _normalize_domain(os.getenv("PUBLIC_ROOT_DOMAIN"))
    if configured_root and (
        backend_host == configured_root
        or backend_host.endswith(f".{configured_root}")
    ):
        return configured_root

    labels = backend_host.split(".")
    if len(labels) >= 3 and labels[0] in {"api", "app", "admin", "backend"}:
        return ".".join(labels[1:])
    return backend_host


def is_same_site_credential_origin(
    value: object,
    *,
    backend_url: object = None,
    public_root_domain: object = None,
) -> bool:
    """Return whether an exact origin can receive first-party auth cookies."""

    normalized = _normalize_cors_origin(value)
    parsed_origin = urlparse(normalized) if normalized else None
    parsed_api = urlparse(str(backend_url or BACKEND_URL))
    if not parsed_origin or not parsed_origin.hostname or not parsed_api.hostname:
        return False
    if parsed_api.scheme.lower() == "https" and parsed_origin.scheme.lower() != "https":
        return False

    site_roots = {_credential_site_root(parsed_api.hostname.lower())}
    configured_public_root = _normalize_domain(
        public_root_domain or os.getenv("PUBLIC_ROOT_DOMAIN")
    )
    if configured_public_root:
        # Render exposes an internal ``*.onrender.com`` URL even when the
        # service is reached through api.chatboc.ar.  The explicitly configured
        # public root remains first-party and must survive production filtering.
        site_roots.add(configured_public_root)
    origin_host = parsed_origin.hostname.lower()
    return any(
        origin_host == site_root or origin_host.endswith(f".{site_root}")
        for site_root in site_roots
    )


def _append_exact_origin(target: List[object], value: object) -> None:
    normalized = _normalize_cors_origin(value)
    if normalized and normalized not in target:
        target.append(normalized)


# This list is exclusively for browser requests that may include cookies or
# Authorization. Public widget endpoints are handled separately in app.py and
# never receive Access-Control-Allow-Credentials.
cors_env = os.getenv("CORS_ALLOWED_ORIGINS")
configured_origins = (
    [entry.strip() for entry in cors_env.split(",") if entry.strip()]
    if cors_env
    else [PANEL_URL, WIDGET_URL]
)
allowed_urls: List[object] = []
for configured_origin in configured_origins:
    _append_exact_origin(allowed_urls, configured_origin)

LOCAL_DEV_ORIGIN_PATTERN = re.compile(
    r"^http://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$"
)
CORS_ALLOW_LOCAL_DEV = (
    _env_flag(False, "CORS_ALLOW_LOCAL_DEV") and not IS_PRODUCTION_RUNTIME
)
if CORS_ALLOW_LOCAL_DEV:
    allowed_urls.append(LOCAL_DEV_ORIGIN_PATTERN)

# Always add the root domain(s) so that the public widget can reach the API
host = parsed_backend.hostname
if host and host != "localhost":
    parts = host.split('.')
    if len(parts) >= 2:
        root_domain = ".".join(parts[-2:])
        _append_exact_origin(allowed_urls, f"https://{root_domain}")
        _append_exact_origin(allowed_urls, f"https://www.{root_domain}")

PUBLIC_ROOT_DOMAIN = os.getenv("PUBLIC_ROOT_DOMAIN", "chatboc.ar")
if PUBLIC_ROOT_DOMAIN and PUBLIC_ROOT_DOMAIN not in ("localhost", "127.0.0.1"):
    _append_exact_origin(allowed_urls, f"https://{PUBLIC_ROOT_DOMAIN}")
    _append_exact_origin(allowed_urls, f"https://www.{PUBLIC_ROOT_DOMAIN}")

if IS_PRODUCTION_RUNTIME:
    allowed_urls = [
        origin
        for origin in allowed_urls
        if isinstance(origin, str)
        and is_same_site_credential_origin(
            origin,
            backend_url=BACKEND_URL,
            public_root_domain=PUBLIC_ROOT_DOMAIN,
        )
    ]

CREDENTIALS_ALLOWED_ORIGINS = list(allowed_urls)
# Backwards-compatible export. It no longer contains wildcard Vercel previews.
ALLOWED_ORIGINS = CREDENTIALS_ALLOWED_ORIGINS


def _resolve_socket_cors_origins() -> List[str]:
    """Build an exact allowlist for Socket.IO browser handshakes.

    Socket.IO may be hosted on a different Vercel project than the frontend.
    Those cross-project origins must be configured explicitly and are kept
    separate from the credentialed HTTP CORS policy. Wildcards, paths and
    insecure production origins fail closed.
    """

    resolved = [origin for origin in CREDENTIALS_ALLOWED_ORIGINS if isinstance(origin, str)]
    configured = str(os.getenv("SOCKET_CORS_ALLOWED_ORIGINS") or "")
    for candidate in configured.split(","):
        normalized = _normalize_cors_origin(candidate)
        if not normalized:
            continue
        parsed_origin = urlparse(normalized)
        if "*" in str(parsed_origin.hostname or ""):
            continue
        if IS_PRODUCTION_RUNTIME and parsed_origin.scheme.lower() != "https":
            continue
        if normalized not in resolved:
            resolved.append(normalized)
    return resolved


SOCKET_CORS_ALLOWED_ORIGINS = _resolve_socket_cors_origins()

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

# Keep cookies host-only unless an operator deliberately configures a shared
# parent domain. Deriving this from RENDER_EXTERNAL_URL can produce
# ``.onrender.com`` when the public API uses a custom domain, causing browsers
# to reject the cookie and widening its scope unnecessarily.
cookie_domain_env = os.getenv("COOKIE_DOMAIN")
if cookie_domain_env:
    COOKIE_DOMAIN = cookie_domain_env
else:
    COOKIE_DOMAIN = None

class Config:
    """
    Clase de configuración principal de la aplicación.
    Contiene todas las variables de configuración.
    """

    ENV = ENV
    DEBUG = ENV == "dev"
    CORS_ALLOW_LOCAL_DEV = CORS_ALLOW_LOCAL_DEV
    CORS_CREDENTIALS_ALLOWED_ORIGINS = tuple(CREDENTIALS_ALLOWED_ORIGINS)
    PUBLIC_ROOT_DOMAIN = PUBLIC_ROOT_DOMAIN

    # Public URLs exposed to the frontend. Keeping them in the Flask config
    # ensures endpoints like /api/config can always read them without having
    # to import the module-level constants.
    BACKEND_URL = str(BACKEND_URL)
    PUBLIC_API_BASE_URL = str(os.getenv("PUBLIC_API_BASE_URL") or BACKEND_URL)
    PANEL_URL = str(PANEL_URL)
    WIDGET_URL = str(WIDGET_URL)

    # Version identifiers surfaced through /api/version so that the Admin UI
    # can display the active frontend/backend revisions.
    FRONTEND_VERSION = DEFAULT_FRONTEND_VERSION
    BACKEND_VERSION = DEFAULT_BACKEND_VERSION

    # Maps provider configuration
    MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
    MAPS_DEFAULT_PROVIDER = os.getenv("MAPS_DEFAULT_PROVIDER", "google")

    # LLM provider configuration. Gemini is server-side only; do not expose this
    # key through VITE_/NEXT_PUBLIC_ variables.
    GEMINI_API_KEY = _env_first("GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY", default="")
    GEMINI_CHAT_MODEL = _env_first(
        "GEMINI_CHAT_MODEL",
        "GEMINI_MODEL",
        default="gemini-2.5-flash",
    )
    OLLAMA_ENABLED = _env_flag(False, "OLLAMA_ENABLED", "LLM_OLLAMA_ENABLED")
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", os.getenv("OLLAMA_OPENAI_BASE_URL", "http://localhost:11434/v1"))
    OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", os.getenv("OLLAMA_MODEL", "glm-5.2:cloud"))
    OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "45"))
    HUGGINGFACE_API_TOKEN = _env_first("HUGGINGFACE_API_TOKEN", "HF_TOKEN", default="")
    HUGGINGFACE_ENABLED = _env_flag(False, "HUGGINGFACE_ENABLED", "HF_ENABLED")
    HUGGINGFACE_PROVIDER = os.getenv("HUGGINGFACE_PROVIDER", "auto")
    HUGGINGFACE_EMBEDDINGS_ENABLED = _env_flag(False, "HUGGINGFACE_EMBEDDINGS_ENABLED", "HF_EMBEDDINGS_ENABLED")
    HUGGINGFACE_EMBEDDING_MODEL = os.getenv("HUGGINGFACE_EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
    HUGGINGFACE_ZERO_SHOT_ENABLED = _env_flag(False, "HUGGINGFACE_ZERO_SHOT_ENABLED", "HF_ZERO_SHOT_ENABLED")
    HUGGINGFACE_ZERO_SHOT_MODEL = os.getenv("HUGGINGFACE_ZERO_SHOT_MODEL", "joeddav/xlm-roberta-large-xnli")
    HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE = float(os.getenv("HUGGINGFACE_RECLAMO_CATEGORY_MIN_SCORE", "0.72"))
    HUGGINGFACE_RECLAMO_PRIORITY_MIN_SCORE = float(os.getenv("HUGGINGFACE_RECLAMO_PRIORITY_MIN_SCORE", "0.66"))
    HUGGINGFACE_RECLAMO_SIGNAL_MIN_SCORE = float(os.getenv("HUGGINGFACE_RECLAMO_SIGNAL_MIN_SCORE", "0.62"))
    HUGGINGFACE_SENTIMENT_MIN_SCORE = float(os.getenv("HUGGINGFACE_SENTIMENT_MIN_SCORE", "0.56"))
    HUGGINGFACE_PYME_INTENT_MIN_SCORE = float(os.getenv("HUGGINGFACE_PYME_INTENT_MIN_SCORE", "0.62"))
    VISION_HUGGINGFACE_ENABLED = _env_flag(False, "VISION_HUGGINGFACE_ENABLED", "HUGGINGFACE_VISION_ENABLED")
    HUGGINGFACE_IMAGE_CLASSIFICATION_MODEL = os.getenv("HUGGINGFACE_IMAGE_CLASSIFICATION_MODEL", "google/vit-base-patch16-224")
    HUGGINGFACE_OBJECT_DETECTION_MODEL = os.getenv("HUGGINGFACE_OBJECT_DETECTION_MODEL", "facebook/detr-resnet-50")
    INSTALL_OPEN_SOURCE_AI_EXTRAS = _env_flag(False, "INSTALL_OPEN_SOURCE_AI_EXTRAS")
    DOCLING_ENABLED = _env_flag(False, "DOCLING_ENABLED", "OPEN_SOURCE_DOCUMENT_AI_ENABLED")
    DOCLING_MAX_FILE_MB = float(os.getenv("DOCLING_MAX_FILE_MB", "15"))

    # 1. LLAVE SECRETA
    SECRET_KEY = os.getenv("SECRET_KEY", "una-llave-secreta-muy-segura-para-desarrollo-local")
    # Vercel sends this value as ``Authorization: Bearer ...`` to scheduled
    # endpoints. An empty value must never authorize an internal invocation.
    CRON_SECRET = os.getenv("CRON_SECRET", "")
    # Historical marketplace OAuth/webhook routes trusted request-supplied
    # tenant selectors.  Keep their migration marker false by default on every
    # runtime (including Vercel and Render).  The routes remain fail-closed even
    # if this is mistakenly enabled until signed, expiring, one-time OAuth
    # state and tenant-bound provider credentials are implemented.
    LEGACY_INTEGRATIONS_TRANSPORT_ENABLED = _env_flag(
        False,
        "LEGACY_INTEGRATIONS_TRANSPORT_ENABLED",
    )
    # A Vercel Production deployment can become the active cron target before
    # DNS or database cutover. Keep reconciliation inert until the operator
    # explicitly confirms that the deployment owns the production workload.
    VERCEL_OUTBOX_CRON_ENABLED = _env_flag(
        False,
        "VERCEL_OUTBOX_CRON_ENABLED",
    )
    # The scheduled drain is intentionally smaller than the platform request
    # limit.  Batches are effect-level leased/fenced and the coordinator holds
    # a PostgreSQL transaction advisory lock so duplicate cron deliveries do
    # not run overlapping consumers.
    # Keep raw values here: a typo must make only the gated cron unavailable,
    # not crash the web application while importing Config.
    VERCEL_OUTBOX_CRON_TIME_BUDGET_SECONDS = os.getenv(
        "VERCEL_OUTBOX_CRON_TIME_BUDGET_SECONDS",
        "45",
    )
    VERCEL_OUTBOX_CRON_MAX_CYCLES = os.getenv(
        "VERCEL_OUTBOX_CRON_MAX_CYCLES",
        "4",
    )
    VERCEL_OUTBOX_CRON_WHATSAPP_INBOUND_BATCH_SIZE = os.getenv(
        "VERCEL_OUTBOX_CRON_WHATSAPP_INBOUND_BATCH_SIZE",
        "1",
    )
    VERCEL_OUTBOX_CRON_WHATSAPP_OUTBOUND_BATCH_SIZE = os.getenv(
        "VERCEL_OUTBOX_CRON_WHATSAPP_OUTBOUND_BATCH_SIZE",
        "2",
    )
    VERCEL_OUTBOX_CRON_DOMAIN_EFFECT_BATCH_SIZE = os.getenv(
        "VERCEL_OUTBOX_CRON_DOMAIN_EFFECT_BATCH_SIZE",
        "10",
    )
    VERCEL_OUTBOX_CRON_SURVEY_EFFECT_BATCH_SIZE = os.getenv(
        "VERCEL_OUTBOX_CRON_SURVEY_EFFECT_BATCH_SIZE",
        "25",
    )
    # Destructive retention jobs need an independent production cutover.  A
    # scheduled deployment must remain inert until the operator explicitly
    # transfers ownership of maintenance work away from Render.
    VERCEL_MAINTENANCE_CRONS_ENABLED = _env_flag(
        False,
        "VERCEL_MAINTENANCE_CRONS_ENABLED",
    )
    # Paid weekly AI reports are fenced independently from both outbox and
    # destructive maintenance ownership.
    VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED = _env_flag(
        False,
        "VERCEL_WEEKLY_ANALYTICS_CRON_ENABLED",
    )
    WEEKLY_ANALYTICS_RESERVATION_REDIS_URL = os.getenv(
        "WEEKLY_ANALYTICS_RESERVATION_REDIS_URL",
        "",
    ).strip()
    WEEKLY_ANALYTICS_MAX_TENANTS_PER_RUN = os.getenv(
        "WEEKLY_ANALYTICS_MAX_TENANTS_PER_RUN",
        "5",
    )
    WEEKLY_ANALYTICS_MAX_BATCHES_PER_DRAIN = os.getenv(
        "WEEKLY_ANALYTICS_MAX_BATCHES_PER_DRAIN",
        "12",
    )
    WEEKLY_ANALYTICS_RESERVATION_TTL_SECONDS = os.getenv(
        "WEEKLY_ANALYTICS_RESERVATION_TTL_SECONDS",
        "900",
    )
    # Dedicated/versioned HMAC boundary for TenantTicket intake receipts.  It
    # intentionally has no SECRET_KEY fallback: creation and tracking fail
    # closed when it is absent or shorter than 32 UTF-8 bytes.
    TENANT_CLAIM_RECEIPT_SECRET_V1 = os.getenv("TENANT_CLAIM_RECEIPT_SECRET_V1", "")
    # Dedicated rotation boundary for one-time WhatsApp Flow correlation tokens.
    # Native Flows stay disabled when this value is missing or too short.
    WHATSAPP_FLOW_TOKEN_KEY_V1 = os.getenv("WHATSAPP_FLOW_TOKEN_KEY_V1", "")
    WHATSAPP_FLOW_TOKEN_TTL_SECONDS = int(
        os.getenv("WHATSAPP_FLOW_TOKEN_TTL_SECONDS", str(48 * 60 * 60))
    )
    META_FLOW_DATA_EXCHANGE_MAX_PAYLOAD_BYTES = int(
        os.getenv("META_FLOW_DATA_EXCHANGE_MAX_PAYLOAD_BYTES", str(64 * 1024))
    )
    META_GRAPH_ACCESS_TOKEN = os.getenv("META_GRAPH_ACCESS_TOKEN", "")
    META_GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
    META_GRAPH_API_BASE_URL = os.getenv(
        "META_GRAPH_API_BASE_URL",
        "https://graph.facebook.com",
    )
    META_GRAPH_API_TIMEOUT_SECONDS = os.getenv(
        "META_GRAPH_API_TIMEOUT_SECONDS",
        "20",
    )

    # 2. CONFIGURACIÓN DE LA BASE DE DATOS
    SQLALCHEMY_DATABASE_URI = resolve_database_uri()

    # Directory for persistent data such as uploaded media.
    DATA_DIR = os.getenv("DATA_DIR", "/data")

    DATABASE_CONNECT_TIMEOUT_SECONDS = _bounded_timeout_seconds(
        os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "2"),
        default=2.0,
        minimum=1.0,
    )
    DATABASE_POOL_TIMEOUT_SECONDS = _bounded_timeout_seconds(
        os.getenv("DATABASE_POOL_TIMEOUT_SECONDS", "2"),
        default=2.0,
    )
    DATABASE_POOL_SIZE = _bounded_pool_count(
        os.getenv("DATABASE_POOL_SIZE", "10"),
        default=10,
        minimum=1,
    )
    DATABASE_MAX_OVERFLOW = _bounded_pool_count(
        os.getenv("DATABASE_MAX_OVERFLOW", "20"),
        default=20,
    )
    SQLALCHEMY_ENGINE_OPTIONS = build_database_engine_options(
        SQLALCHEMY_DATABASE_URI,
        connect_timeout_seconds=DATABASE_CONNECT_TIMEOUT_SECONDS,
        pool_timeout_seconds=DATABASE_POOL_TIMEOUT_SECONDS,
        pool_size=DATABASE_POOL_SIZE,
        max_overflow=DATABASE_MAX_OVERFLOW,
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MUNICIPIO_CHAT_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS = _bounded_timeout_seconds(
        os.getenv("MUNICIPIO_CHAT_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS", "20"),
        default=20.0,
        minimum=0.1,
        maximum=60.0,
    )
    MUNICIPIO_CHAT_IDEMPOTENCY_RESPONSE_RETENTION_DAYS = int(
        _bounded_timeout_seconds(
            os.getenv("MUNICIPIO_CHAT_IDEMPOTENCY_RESPONSE_RETENTION_DAYS", "30"),
            default=30.0,
            minimum=1.0,
            maximum=90.0,
        )
    )
    MUNICIPIO_CHAT_IDEMPOTENCY_RETENTION_BATCH_SIZE = int(
        _bounded_timeout_seconds(
            os.getenv("MUNICIPIO_CHAT_IDEMPOTENCY_RETENTION_BATCH_SIZE", "100"),
            default=100.0,
            minimum=1.0,
            maximum=500.0,
        )
    )
    MUNICIPIO_CHAT_IDEMPOTENCY_RETENTION_SWEEP_SECONDS = int(
        _bounded_timeout_seconds(
            os.getenv("MUNICIPIO_CHAT_IDEMPOTENCY_RETENTION_SWEEP_SECONDS", "300"),
            default=300.0,
            minimum=60.0,
            maximum=3600.0,
        )
    )

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
    # Public readiness probes stay bounded and never serialize dependency
    # details. Runtime parsing clamps probe timeouts and cache TTL to 0.1-5 s.
    READINESS_DATABASE_TIMEOUT_SECONDS = os.getenv(
        "READINESS_DATABASE_TIMEOUT_SECONDS",
        "1.5",
    )
    READINESS_REDIS_TIMEOUT_SECONDS = os.getenv(
        "READINESS_REDIS_TIMEOUT_SECONDS",
        "1.0",
    )
    READINESS_CACHE_TTL_SECONDS = os.getenv(
        "READINESS_CACHE_TTL_SECONDS",
        "1.0",
    )
    # The legacy full demo catalog is intentionally backward compatible but
    # expensive to materialize and transfer.  Current first-party clients use
    # response_profile=selector, so keep a per-client budget plus a wider
    # deployment circuit breaker for callers that still need the full contract.
    DEMO_CATALOG_FULL_RATE_LIMIT = _env_first(
        "DEMO_CATALOG_FULL_RATE_LIMIT",
        default="12 per minute",
    )
    DEMO_CATALOG_FULL_GLOBAL_RATE_LIMIT = _env_first(
        "DEMO_CATALOG_FULL_GLOBAL_RATE_LIMIT",
        default="48 per minute",
    )
    # Operational queue reads can fan out across three legacy ticket stores.
    # Rate capacity is shared by tenant+actor; row inspection remains bounded
    # even when a portable SQL pushdown is unavailable (for example SLA JSON).
    CRM_OPERATIONAL_QUEUE_RATE_LIMIT = _env_first(
        "CRM_OPERATIONAL_QUEUE_RATE_LIMIT",
        default="120 per 60 seconds",
    )
    CRM_OPERATIONAL_QUEUE_MAX_SCANNED_ROWS = _env_first(
        "CRM_OPERATIONAL_QUEUE_MAX_SCANNED_ROWS",
        default="5000",
    )
    # Nombre del cookie adicional que almacena el token de acceso como
    # respaldo en caso de que la sesión basada en cookies falle
    AUTH_TOKEN_COOKIE_NAME = os.getenv("AUTH_TOKEN_COOKIE_NAME", "auth_token")
    # Legacy widget/profile clients may still place an auth token in a JSON
    # body.  Keep that compatibility bounded: authentication must never
    # materialize an arbitrarily large request before a route-level feature or
    # tenant gate can reject it.  New clients should always use headers.
    AUTH_TOKEN_JSON_BODY_MAX_BYTES = int(
        os.getenv("AUTH_TOKEN_JSON_BODY_MAX_BYTES", str(64 * 1024))
    )
    # Global contact continuity may inspect JSON only on explicitly supported
    # public/conversational routes, and never beyond this bounded size.
    CONTACT_IDENTITY_JSON_BODY_MAX_BYTES = int(
        os.getenv("CONTACT_IDENTITY_JSON_BODY_MAX_BYTES", str(64 * 1024))
    )
    DEFER_ANON_MIGRATION_ON_LOGIN = os.getenv("DEFER_ANON_MIGRATION_ON_LOGIN", "true").strip().lower() not in {"0", "false", "no", "off"}

    # Runtime bootstrap guards: in production, schema sync and tenant init must be explicit
    # via migrations/CLI. Local dev keeps convenience defaults enabled.
    _runtime_bootstrap_default = (
        ENV == "dev"
        and not _is_render_runtime()
        and not _is_vercel_runtime()
    )
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
    # Synthetic survey responses are destructive QA tooling and require a
    # separate, explicit opt-in. General demo content must not enable them.
    ALLOW_SURVEY_DEMO_SEEDING = _env_flag(
        False,
        "ALLOW_SURVEY_DEMO_SEEDING",
    )
    # Production synthetic seeding is a separate, tenant-scoped canary.  The
    # legacy flag above remains limited to safe QA/test runtimes.
    ENABLE_SURVEY_SYNTHETIC_SEEDING_V1 = _env_strict_opt_in(
        "ENABLE_SURVEY_SYNTHETIC_SEEDING_V1"
    )
    # Stores only isolated, non-municipal demo interactions. Runtime guards in
    # the participation service additionally require Vercel Preview + Neon and
    # reject both Production and Render even if this flag is misconfigured.
    ENABLE_PREVIEW_DURABLE_DEMO_SURVEY_VOTES_V1 = _env_strict_opt_in(
        "ENABLE_PREVIEW_DURABLE_DEMO_SURVEY_VOTES_V1"
    )
    PREVIEW_DURABLE_DEMO_NEON_BRANCH_ID = os.getenv(
        "PREVIEW_DURABLE_DEMO_NEON_BRANCH_ID",
        "",
    ).strip()
    PREVIEW_DURABLE_DEMO_SURVEY_MAX_INTERACTIONS = os.getenv(
        "PREVIEW_DURABLE_DEMO_SURVEY_MAX_INTERACTIONS",
        "50",
    )
    SURVEY_SYNTHETIC_SEED_TENANT_IDS = os.getenv(
        "SURVEY_SYNTHETIC_SEED_TENANT_IDS",
        "",
    )
    # Staged jurisdiction rollout. ``observe`` preserves legacy reads/writes;
    # enforcement is restricted to an explicit tenant-id canary allowlist.
    SURVEY_JURISDICTION_GATE_MODE = os.getenv(
        "SURVEY_JURISDICTION_GATE_MODE",
        "observe",
    ).strip().lower()
    SURVEY_JURISDICTION_GATE_TENANT_IDS = os.getenv(
        "SURVEY_JURISDICTION_GATE_TENANT_IDS",
        "",
    )
    # Governance-sensitive interview/admission APIs require a deliberate
    # deployment opt-in after migrations, tenant policy and staging evidence.
    ENABLE_ASSESSMENT_INTERVIEWS_V1 = _env_strict_opt_in(
        "ENABLE_ASSESSMENT_INTERVIEWS_V1"
    )
    # Managed assignment adds operator writes on top of the read-only inbox and
    # therefore requires a second, deliberate rollout gate.
    ENABLE_INTERVIEW_ASSIGNMENTS_V1 = _env_strict_opt_in(
        "ENABLE_INTERVIEW_ASSIGNMENTS_V1"
    )
    # PSTN Media Streams remain unavailable until the consent/lifecycle
    # migration and tenant policy have been explicitly enabled.
    ENABLE_VOICE_CONSENT_LIFECYCLE_V1 = _env_strict_opt_in(
        "ENABLE_VOICE_CONSENT_LIFECYCLE_V1"
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
    # Socket.IO pub/sub is intentionally configured independently from Celery.
    # A background process must never assume that a broker used for task
    # wakeups is also the transport consumed by every web Socket.IO process.
    CHATBOC_PROCESS_ROLE = os.getenv("CHATBOC_PROCESS_ROLE", "").strip().lower()
    SOCKETIO_MESSAGE_QUEUE_URL = _env_first(
        "SOCKETIO_MESSAGE_QUEUE_URL",
        "SOCKETIO_REDIS_URL",
        default="",
    )
    SOCKETIO_MESSAGE_QUEUE_CHANNEL = os.getenv(
        "SOCKETIO_MESSAGE_QUEUE_CHANNEL",
        "chatboc-realtime-v1",
    ).strip()
    SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS = float(
        os.getenv("SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS", "2")
    )

    # Valores por defecto orientados a Zoho; pueden sobrescribirse mediante múltiples alias
    SMTP_HOST = _env_first(
        "SMTP_HOST",
        "MAIL_SERVER",
        "MAIL_HOST",
        "ZOHO_SMTP_HOST",
        default="smtp.zoho.com",
    )
    SMTP_PORT = int(
        _env_first("SMTP_PORT", "MAIL_PORT", "ZOHO_SMTP_PORT", default=str(587))
    )
    SMTP_USER = _env_first(
        "SMTP_USER",
        "SMTP_USERNAME",
        "MAIL_USERNAME",
        "MAIL_USER",
        "MAIL_FROM_ADDRESS",
        "ZOHO_SMTP_USER",
        default="info@chatboc.ar",
    )
    # No se proporciona contraseña por defecto para evitar uso accidental de credenciales personales
    SMTP_PASSWORD = _env_first(
        "SMTP_PASSWORD",
        "SMTP_PASS",
        "MAIL_PASSWORD",
        "ZOHO_SMTP_PASSWORD",
        default="",
    )
    SMTP_USE_TLS = _env_flag(True, "SMTP_USE_TLS", "MAIL_USE_TLS", "SMTP_TLS")
    SMTP_USE_SSL = _env_flag(False, "SMTP_USE_SSL", "MAIL_USE_SSL", "SMTP_SSL")
    SMTP_REQUIRE_AUTH = _env_flag(True, "SMTP_REQUIRE_AUTH")
    EMAIL_NOTIFICATIONS_ENABLED = _env_flag(
        False,
        "EMAIL_NOTIFICATIONS_ENABLED",
        "ENABLE_EMAIL_NOTIFICATIONS",
    )

    MAIL_FROM_ADDRESS = _env_first(
        "MAIL_FROM_ADDRESS",
        "MAIL_DEFAULT_SENDER",
        "MAIL_SENDER",
        "AUTH_EMAIL_FROM",
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
    # Dedicated PSTN number verified for Voice. Never fall back to a WhatsApp
    # sender when initiating callbacks.
    TWILIO_VOICE_PHONE_NUMBER = os.getenv("TWILIO_VOICE_PHONE_NUMBER")
    TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
    # Durable inbound processing is opt-in until the database migration and a
    # dedicated worker are live.  ``queue`` acknowledges the signed webhook
    # after the turn is persisted; ``legacy`` keeps the synchronous path.
    WHATSAPP_INBOUND_DURABILITY_MODE = os.getenv(
        "WHATSAPP_INBOUND_DURABILITY_MODE",
        "legacy",
    ).strip().lower()
    # ``queue`` is a tenant canary, never a global switch. Tenants outside this
    # explicit allowlist remain on the synchronous legacy path.
    WHATSAPP_INBOUND_QUEUE_TENANT_IDS = os.getenv(
        "WHATSAPP_INBOUND_QUEUE_TENANT_IDS",
        "",
    ).strip()
    WHATSAPP_INBOUND_HASH_SECRET = os.getenv("WHATSAPP_INBOUND_HASH_SECRET")
    # Canonical, tenant-scoped channel identity rollout. ``legacy`` is the
    # rollback default; deployment manifests opt into ``shadow`` explicitly
    # and durable queue/voice production modes require ``enforce`` below.
    CHANNEL_SESSION_IDENTITY_MODE = os.getenv(
        "CHANNEL_SESSION_IDENTITY_MODE",
        "legacy",
    ).strip().lower()
    CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1 = os.getenv(
        "CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1"
    )
    CHANNEL_SESSION_IDENTITY_VERSION_V1 = os.getenv(
        "CHANNEL_SESSION_IDENTITY_VERSION_V1",
        "v1",
    ).strip().lower()
    # Normalized non-WhatsApp/non-voice adapters remain fail-closed until a
    # dedicated ProviderConnection is active and every request carries the
    # per-connection HMAC derived from this root secret.
    OMNICHANNEL_SIGNED_INBOUND_MODE = os.getenv(
        "OMNICHANNEL_SIGNED_INBOUND_MODE",
        "disabled",
    ).strip().lower()
    OMNICHANNEL_INBOUND_HMAC_SECRET_V1 = os.getenv(
        "OMNICHANNEL_INBOUND_HMAC_SECRET_V1"
    )
    OMNICHANNEL_INBOUND_MAX_PAYLOAD_BYTES = int(
        os.getenv("OMNICHANNEL_INBOUND_MAX_PAYLOAD_BYTES", "65536")
    )
    OMNICHANNEL_INBOUND_MAX_CLOCK_SKEW_SECONDS = int(
        os.getenv("OMNICHANNEL_INBOUND_MAX_CLOCK_SKEW_SECONDS", "300")
    )
    WHATSAPP_INBOUND_MAX_PAYLOAD_BYTES = int(
        os.getenv("WHATSAPP_INBOUND_MAX_PAYLOAD_BYTES", "65536")
    )
    WHATSAPP_INBOUND_LEASE_SECONDS = int(
        os.getenv("WHATSAPP_INBOUND_LEASE_SECONDS", "180")
    )
    # Durable outbound Notification transport. It is fail-closed and canary
    # scoped; setting the boolean without an explicit tenant list still sends
    # nothing.
    WHATSAPP_NOTIFICATION_TRANSPORT_ENABLED = _env_flag(
        False,
        "WHATSAPP_NOTIFICATION_TRANSPORT_ENABLED",
    )
    WHATSAPP_NOTIFICATION_TRANSPORT_TENANT_IDS = os.getenv(
        "WHATSAPP_NOTIFICATION_TRANSPORT_TENANT_IDS",
        "",
    ).strip()
    NOTIFICATION_DISPATCH_LEASE_SECONDS = int(
        os.getenv("NOTIFICATION_DISPATCH_LEASE_SECONDS", "180")
    )
    WHATSAPP_INBOUND_MAX_ATTEMPTS = int(
        os.getenv("WHATSAPP_INBOUND_MAX_ATTEMPTS", "8")
    )
    WHATSAPP_INBOUND_WORKER_BATCH_SIZE = int(
        os.getenv("WHATSAPP_INBOUND_WORKER_BATCH_SIZE", "8")
    )
    WHATSAPP_INBOUND_WORKER_POLL_SECONDS = float(
        os.getenv("WHATSAPP_INBOUND_WORKER_POLL_SECONDS", "0.5")
    )
    # The database poller is authoritative. Celery may only be used as an
    # optional low-latency wakeup after the durable commit.
    WHATSAPP_INBOUND_CELERY_WAKEUP_ENABLED = os.getenv(
        "WHATSAPP_INBOUND_CELERY_WAKEUP_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    # A Render worker may be provisioned before queue cutover, but only this
    # explicit flag permits its zero-I/O legacy standby path.
    WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED = _env_strict_opt_in(
        "WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED"
    )
    # Retention is independently scoped so removing a queue canary cannot
    # orphan that tenant's already-persisted payloads after rollback.
    WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS = os.getenv(
        "WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS",
        "",
    ).strip()
    WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED = _env_strict_opt_in(
        "WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED"
    )
    WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS = int(
        os.getenv("WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS", "72")
    )
    WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE = int(
        os.getenv("WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE", "200")
    )
    WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD = _env_fail_closed_hold(
        True,
        "WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD",
    )
    # Domain mutations (tickets, comments and orders) must stage every external
    # effect in the same transaction before this mode can replace legacy direct
    # sends. Rollout is tenant-canary only until staging evidence proves the
    # worker, provider credentials and operational reconciliation path.
    DOMAIN_EFFECT_OUTBOX_MODE = os.getenv(
        "DOMAIN_EFFECT_OUTBOX_MODE",
        "legacy",
    ).strip().lower()
    DOMAIN_EFFECT_OUTBOX_SECRET = os.getenv("DOMAIN_EFFECT_OUTBOX_SECRET")
    DOMAIN_EFFECT_OUTBOX_TENANT_IDS = os.getenv(
        "DOMAIN_EFFECT_OUTBOX_TENANT_IDS",
        "",
    ).strip()
    DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES = int(
        os.getenv("DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES", "4096")
    )
    DOMAIN_EFFECT_OUTBOX_LEASE_SECONDS = int(
        os.getenv("DOMAIN_EFFECT_OUTBOX_LEASE_SECONDS", "180")
    )
    DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS = int(
        os.getenv("DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS", "8")
    )
    DOMAIN_EFFECT_OUTBOX_WORKER_BATCH_SIZE = int(
        os.getenv("DOMAIN_EFFECT_OUTBOX_WORKER_BATCH_SIZE", "20")
    )
    DOMAIN_EFFECT_OUTBOX_WORKER_POLL_SECONDS = float(
        os.getenv("DOMAIN_EFFECT_OUTBOX_WORKER_POLL_SECONDS", "0.5")
    )
    DOMAIN_EFFECT_OUTBOX_CELERY_WAKEUP_ENABLED = _env_flag(
        False,
        "DOMAIN_EFFECT_OUTBOX_CELERY_WAKEUP_ENABLED",
    )
    # The survey-response outbox is always database-authoritative. These
    # settings bound the dedicated multi-tenant poller; they do not merge it
    # with the generic ticket/order effect table.
    SURVEY_RESPONSE_EFFECT_LEASE_SECONDS = int(
        os.getenv("SURVEY_RESPONSE_EFFECT_LEASE_SECONDS", "120")
    )
    SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE = int(
        os.getenv("SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE", "50")
    )
    SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE = int(
        os.getenv("SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE", "50")
    )
    SURVEY_RESPONSE_EFFECT_WORKER_POLL_SECONDS = float(
        os.getenv("SURVEY_RESPONSE_EFFECT_WORKER_POLL_SECONDS", "0.5")
    )
    # Stable, dedicated key for source-anonymous uniqueness fingerprints.
    # The version suffix is part of the storage contract; do not silently
    # rotate this value for an already-published survey generation.
    SURVEY_IDENTITY_HMAC_SECRET_V1 = os.getenv(
        "SURVEY_IDENTITY_HMAC_SECRET_V1",
        "",
    )
    # Controlled institutional/manual eligibility is a separate canary.  A
    # global opt-in never enables every tenant: the explicit tenant allowlist
    # and a dedicated secret are both required by the runtime gate.
    ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1 = _env_strict_opt_in(
        "ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1"
    )
    SURVEY_ELIGIBILITY_GRANT_TENANT_IDS = os.getenv(
        "SURVEY_ELIGIBILITY_GRANT_TENANT_IDS",
        "",
    )
    SURVEY_ELIGIBILITY_SECRET_V1 = os.getenv(
        "SURVEY_ELIGIBILITY_SECRET_V1",
        "",
    )
    # Workflow Studio durable writes remain a reviewed control-plane canary.
    # Runtime consumption is intentionally a separate, currently disabled gate.
    ENABLE_WHATSAPP_WORKFLOW_STUDIO_DURABLE_V1 = _env_strict_opt_in(
        "ENABLE_WHATSAPP_WORKFLOW_STUDIO_DURABLE_V1"
    )
    WHATSAPP_WORKFLOW_STUDIO_DURABLE_TENANT_IDS = os.getenv(
        "WHATSAPP_WORKFLOW_STUDIO_DURABLE_TENANT_IDS",
        "",
    )
    # The current SIGEM adapter is a placeholder, not a production transport.
    # Keep this false until a tenant-bound authenticated integration is added.
    SIGEM_LIVE_ENABLED = _env_flag(False, "SIGEM_LIVE_ENABLED")
    WHATSAPP_MEDIA_DOWNLOAD_TIMEOUT_SECONDS = float(
        os.getenv("WHATSAPP_MEDIA_DOWNLOAD_TIMEOUT_SECONDS", "12")
    )
    WHATSAPP_MEDIA_MAX_BYTES = int(
        os.getenv("WHATSAPP_MEDIA_MAX_BYTES", str(25 * 1024 * 1024))
    )
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
        fallback_domain = (PUBLIC_ROOT_DOMAIN or "").strip().lower()
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
    ENABLE_PYME_WHATSAPP_CHAT = _env_flag(
        True,
        "ENABLE_PYME_WHATSAPP_CHAT",
    )

    # 5. PUSHER CONFIGURATION
    PUSHER_APP_ID = os.getenv("PUSHER_APP_ID")
    PUSHER_KEY = os.getenv("PUSHER_KEY")
    PUSHER_SECRET = os.getenv("PUSHER_SECRET")
    PUSHER_CLUSTER = os.getenv("PUSHER_CLUSTER")

    # WhatsApp Welcome Message Configuration
    # Fail closed when no reviewed welcome template is configured.  The old
    # hard-coded ContentSid referenced a media template that Twilio accepted
    # asynchronously and then rejected with 63019, so a deployment with an
    # empty registry kept attempting a known-bad template.  The webhook first
    # resolves an approved tenant/manifest template and only uses this value as
    # an explicit deployment override.
    WELCOME_TEMPLATE_SID = os.getenv("WELCOME_TEMPLATE_SID")
    # Emergency-only escape hatch. Production stays fail-closed by default;
    # the tenant registry/manifest is the normal approval authority.
    WELCOME_TEMPLATE_OVERRIDE_ENABLED = os.getenv(
        "WELCOME_TEMPLATE_OVERRIDE_ENABLED",
        "false",
    ).lower() in ("true", "1", "t", "yes")
    # A checked-in approval snapshot is only a short-lived fallback when the
    # tenant registry is unavailable.  Old snapshots fail closed instead of
    # being treated as permanent proof of provider deliverability.
    TWILIO_TEMPLATE_MANIFEST_MAX_AGE_HOURS = float(
        os.getenv("TWILIO_TEMPLATE_MANIFEST_MAX_AGE_HOURS", "168")
    )
    WELCOME_MESSAGE_DELAY_SECONDS = int(os.getenv("WELCOME_MESSAGE_DELAY_SECONDS", "5"))
    WELCOME_MEDIA_URL = os.getenv(
        "WELCOME_MEDIA_URL",
        "/static/welcome/juni-saludo-sticker.webp",
    )
    CHATBOC_DEMO_WELCOME_MEDIA_URL = os.getenv(
        "CHATBOC_DEMO_WELCOME_MEDIA_URL",
        "/static/welcome/chatboc-saludo-sticker.webp",
    )
    # Static menu audios should be generated once and reused from
    # /static/audio_cache for accessibility and Twilio media reliability.
    WELCOME_AUDIO_URL = os.getenv("WELCOME_AUDIO_URL")
    WHATSAPP_MENU_AUDIO_ENABLED = os.getenv("WHATSAPP_MENU_AUDIO_ENABLED", "true")
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

    render_web_runtime = (
        str(os.getenv("RENDER") or "").strip().lower() == "true"
        and str(os.getenv("RENDER_SERVICE_TYPE") or "").strip().lower() == "web"
    )
    rate_limit_storage_uri = str(
        getattr(config, "get", lambda *_: None)(
            "RATELIMIT_STORAGE_URI",
            "",
        )
        or ""
    ).strip().lower()
    if render_web_runtime and not rate_limit_storage_uri.startswith(
        ("redis://", "rediss://")
    ):
        errors.append(
            "RATELIMIT_STORAGE_URI debe usar Redis compartido en el servicio web de Render."
        )

    if getattr(config, "get", lambda *_: None)(
        "VERCEL_OUTBOX_CRON_ENABLED",
        False,
    ) is True:
        vercel_outbox_bounds = (
            ("VERCEL_OUTBOX_CRON_TIME_BUDGET_SECONDS", 5.0, 55.0, float, 45.0),
            ("VERCEL_OUTBOX_CRON_MAX_CYCLES", 1, 20, int, 4),
            (
                "VERCEL_OUTBOX_CRON_WHATSAPP_INBOUND_BATCH_SIZE",
                1,
                10,
                int,
                1,
            ),
            (
                "VERCEL_OUTBOX_CRON_WHATSAPP_OUTBOUND_BATCH_SIZE",
                1,
                10,
                int,
                2,
            ),
            ("VERCEL_OUTBOX_CRON_DOMAIN_EFFECT_BATCH_SIZE", 1, 50, int, 10),
            ("VERCEL_OUTBOX_CRON_SURVEY_EFFECT_BATCH_SIZE", 1, 100, int, 25),
        )
        for key, minimum, maximum, cast, default_value in vercel_outbox_bounds:
            raw_value = getattr(config, "get", lambda *_: None)(key, default_value)
            try:
                if isinstance(raw_value, bool):
                    raise ValueError(key)
                if cast is int:
                    if isinstance(raw_value, int):
                        value = raw_value
                    elif (
                        isinstance(raw_value, str)
                        and raw_value.strip().lstrip("+-").isdigit()
                    ):
                        value = int(raw_value.strip())
                    else:
                        raise ValueError(key)
                else:
                    value = cast(raw_value)
            except (TypeError, ValueError, OverflowError):
                errors.append(f"{key} invalido para reconciliacion Vercel.")
                continue
            if (
                isinstance(value, float)
                and not math.isfinite(value)
            ) or value < minimum or value > maximum:
                errors.append(f"{key} fuera de rango para reconciliacion Vercel.")

    raw_demo_seed_flag = getattr(config, "get", lambda *_: None)(
        "ALLOW_SURVEY_DEMO_SEEDING",
        False,
    )
    demo_seed_enabled = raw_demo_seed_flag is True or str(
        raw_demo_seed_flag or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if demo_seed_enabled:
        errors.append(
            "ALLOW_SURVEY_DEMO_SEEDING no puede habilitarse en produccion."
        )
    legacy_bootstrap_enabled = str(
        os.getenv("ENCUESTAS_BOOTSTRAP_SAMPLE", "") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if legacy_bootstrap_enabled:
        errors.append(
            "ENCUESTAS_BOOTSTRAP_SAMPLE no puede habilitarse en produccion."
        )

    synthetic_seed_enabled = getattr(config, "get", lambda *_: None)(
        "ENABLE_SURVEY_SYNTHETIC_SEEDING_V1",
        False,
    )
    raw_synthetic_seed_tenants = str(
        getattr(config, "get", lambda *_: None)(
            "SURVEY_SYNTHETIC_SEED_TENANT_IDS",
            "",
        )
        or ""
    ).strip()
    synthetic_seed_tenants: set[int] = set()
    synthetic_seed_allowlist_invalid = False
    if raw_synthetic_seed_tenants:
        for raw_tenant_id in raw_synthetic_seed_tenants.split(","):
            normalized_tenant_id = raw_tenant_id.strip()
            try:
                tenant_id = int(normalized_tenant_id)
            except (TypeError, ValueError):
                synthetic_seed_allowlist_invalid = True
                break
            if tenant_id <= 0 or str(tenant_id) != normalized_tenant_id:
                synthetic_seed_allowlist_invalid = True
                break
            synthetic_seed_tenants.add(tenant_id)
    if synthetic_seed_allowlist_invalid:
        errors.append(
            "SURVEY_SYNTHETIC_SEED_TENANT_IDS contiene un tenant invalido."
        )
    if synthetic_seed_enabled is True and (
        not synthetic_seed_tenants or synthetic_seed_allowlist_invalid
    ):
        errors.append(
            "ENABLE_SURVEY_SYNTHETIC_SEEDING_V1 requiere tenants canarios explicitos."
        )

    jurisdiction_mode = str(
        getattr(config, "get", lambda *_: None)(
            "SURVEY_JURISDICTION_GATE_MODE",
            "observe",
        )
        or "observe"
    ).strip().lower()
    jurisdiction_modes = {"observe", "enforce_publish", "enforce_visibility"}
    if jurisdiction_mode not in jurisdiction_modes:
        errors.append("SURVEY_JURISDICTION_GATE_MODE es invalido.")
    raw_jurisdiction_tenants = str(
        getattr(config, "get", lambda *_: None)(
            "SURVEY_JURISDICTION_GATE_TENANT_IDS",
            "",
        )
        or ""
    ).strip()
    jurisdiction_tenants: set[int] = set()
    jurisdiction_allowlist_invalid = False
    if raw_jurisdiction_tenants:
        for raw_tenant_id in raw_jurisdiction_tenants.split(","):
            normalized_tenant_id = raw_tenant_id.strip()
            if not re.fullmatch(r"[1-9][0-9]*", normalized_tenant_id):
                jurisdiction_allowlist_invalid = True
                break
            jurisdiction_tenants.add(int(normalized_tenant_id))
    if jurisdiction_allowlist_invalid:
        errors.append(
            "SURVEY_JURISDICTION_GATE_TENANT_IDS contiene un tenant invalido."
        )
    if jurisdiction_mode in {"enforce_publish", "enforce_visibility"} and (
        not jurisdiction_tenants or jurisdiction_allowlist_invalid
    ):
        errors.append(
            "El enforcement de jurisdiccion requiere tenants canarios explicitos."
        )

    secret_key = str(getattr(config, "get", lambda *_: None)("SECRET_KEY", "") or "").strip()
    if not secret_key or secret_key.lower() in INSECURE_SECRET_MARKERS or len(secret_key) < 24:
        errors.append("SECRET_KEY insegura para producción.")

    debug_enabled = bool(getattr(config, "get", lambda *_: None)("DEBUG", False))
    if debug_enabled:
        errors.append("DEBUG=True no está permitido en producción.")

    survey_identity_secret = str(
        getattr(config, "get", lambda *_: None)(
            "SURVEY_IDENTITY_HMAC_SECRET_V1",
            "",
        )
        or ""
    )
    if survey_identity_secret and len(survey_identity_secret.encode("utf-8")) < 32:
        errors.append(
            "SURVEY_IDENTITY_HMAC_SECRET_V1 debe tener al menos 32 bytes cuando se configura."
        )

    survey_eligibility_enabled = getattr(config, "get", lambda *_: None)(
        "ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1",
        False,
    )
    survey_eligibility_secret = str(
        getattr(config, "get", lambda *_: None)(
            "SURVEY_ELIGIBILITY_SECRET_V1",
            "",
        )
        or ""
    )
    if (
        survey_eligibility_secret
        and len(survey_eligibility_secret.encode("utf-8")) < 32
    ):
        errors.append(
            "SURVEY_ELIGIBILITY_SECRET_V1 debe tener al menos 32 bytes cuando se configura."
        )
    raw_survey_eligibility_tenants = str(
        getattr(config, "get", lambda *_: None)(
            "SURVEY_ELIGIBILITY_GRANT_TENANT_IDS",
            "",
        )
        or ""
    ).strip()
    survey_eligibility_tenants: set[int] = set()
    survey_eligibility_allowlist_invalid = False
    if raw_survey_eligibility_tenants:
        for raw_tenant_id in raw_survey_eligibility_tenants.split(","):
            normalized_tenant_id = raw_tenant_id.strip()
            try:
                tenant_id = int(normalized_tenant_id)
            except (TypeError, ValueError):
                survey_eligibility_allowlist_invalid = True
                break
            if tenant_id <= 0 or str(tenant_id) != normalized_tenant_id:
                survey_eligibility_allowlist_invalid = True
                break
            survey_eligibility_tenants.add(tenant_id)
    if survey_eligibility_allowlist_invalid:
        errors.append(
            "SURVEY_ELIGIBILITY_GRANT_TENANT_IDS contiene un tenant invalido."
        )
    if survey_eligibility_enabled is True:
        if len(survey_eligibility_secret.encode("utf-8")) < 32:
            errors.append(
                "ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1 requiere SURVEY_ELIGIBILITY_SECRET_V1."
            )
        if not survey_eligibility_tenants or survey_eligibility_allowlist_invalid:
            errors.append(
                "ENABLE_SURVEY_ELIGIBILITY_GRANTS_V1 requiere tenants canarios explicitos."
            )

    workflow_studio_durable_enabled = getattr(config, "get", lambda *_: None)(
        "ENABLE_WHATSAPP_WORKFLOW_STUDIO_DURABLE_V1",
        False,
    )
    raw_workflow_studio_tenants = str(
        getattr(config, "get", lambda *_: None)(
            "WHATSAPP_WORKFLOW_STUDIO_DURABLE_TENANT_IDS",
            "",
        )
        or ""
    ).strip()
    workflow_studio_tenants: set[int] = set()
    workflow_studio_allowlist_invalid = False
    if raw_workflow_studio_tenants:
        for raw_tenant_id in raw_workflow_studio_tenants.split(","):
            normalized_tenant_id = raw_tenant_id.strip()
            try:
                tenant_id = int(normalized_tenant_id)
            except (TypeError, ValueError):
                workflow_studio_allowlist_invalid = True
                break
            if tenant_id <= 0 or str(tenant_id) != normalized_tenant_id:
                workflow_studio_allowlist_invalid = True
                break
            workflow_studio_tenants.add(tenant_id)
    if workflow_studio_allowlist_invalid:
        errors.append(
            "WHATSAPP_WORKFLOW_STUDIO_DURABLE_TENANT_IDS contiene un tenant invalido."
        )
    if workflow_studio_durable_enabled is True and (
        not workflow_studio_tenants or workflow_studio_allowlist_invalid
    ):
        errors.append(
            "ENABLE_WHATSAPP_WORKFLOW_STUDIO_DURABLE_V1 requiere tenants canarios explicitos."
        )

    sigem_live_enabled = getattr(config, "get", lambda *_: None)(
        "SIGEM_LIVE_ENABLED",
        False,
    )
    if sigem_live_enabled is True:
        errors.append(
            "SIGEM_LIVE_ENABLED no puede habilitarse hasta instalar un transporte autenticado."
        )
    elif sigem_live_enabled is not False and sigem_live_enabled is not None:
        errors.append("SIGEM_LIVE_ENABLED inválido.")

    notification_transport_enabled = getattr(config, "get", lambda *_: None)(
        "WHATSAPP_NOTIFICATION_TRANSPORT_ENABLED",
        False,
    )
    if notification_transport_enabled is True:
        raw_tenants = str(
            getattr(config, "get", lambda *_: None)(
                "WHATSAPP_NOTIFICATION_TRANSPORT_TENANT_IDS",
                "",
            )
            or ""
        )
        tenant_tokens = [
            token.strip() for token in raw_tenants.split(",") if token.strip()
        ]
        if not tenant_tokens:
            errors.append(
                "WHATSAPP_NOTIFICATION_TRANSPORT_TENANT_IDS requiere tenants canarios explicitos."
            )
        for token in tenant_tokens:
            try:
                if int(token) <= 0:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                errors.append(
                    "WHATSAPP_NOTIFICATION_TRANSPORT_TENANT_IDS contiene un tenant invalido."
                )
                break
        try:
            notification_lease = int(
                getattr(config, "get", lambda *_: None)(
                    "NOTIFICATION_DISPATCH_LEASE_SECONDS",
                    180,
                )
            )
        except (TypeError, ValueError, OverflowError):
            errors.append("NOTIFICATION_DISPATCH_LEASE_SECONDS invalido.")
        else:
            if notification_lease < 30 or notification_lease > 3600:
                errors.append("NOTIFICATION_DISPATCH_LEASE_SECONDS fuera de rango.")
    elif notification_transport_enabled is not False and notification_transport_enabled is not None:
        errors.append("WHATSAPP_NOTIFICATION_TRANSPORT_ENABLED invalido.")

    durability_mode = str(
        getattr(config, "get", lambda *_: None)(
            "WHATSAPP_INBOUND_DURABILITY_MODE",
            "legacy",
        )
        or "legacy"
    ).strip().lower()
    if durability_mode not in {"legacy", "queue"}:
        errors.append("WHATSAPP_INBOUND_DURABILITY_MODE inválido.")
    inbound_queue_tenant_tokens = [
        token.strip()
        for token in str(
            getattr(config, "get", lambda *_: None)(
                "WHATSAPP_INBOUND_QUEUE_TENANT_IDS",
                "",
            )
            or ""
        ).split(",")
        if token.strip()
    ]
    if durability_mode == "queue" and not inbound_queue_tenant_tokens:
        errors.append(
            "WHATSAPP_INBOUND_QUEUE_TENANT_IDS requiere tenants canarios explicitos en modo queue."
        )
    for token in inbound_queue_tenant_tokens:
        try:
            if int(token) <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            errors.append(
                "WHATSAPP_INBOUND_QUEUE_TENANT_IDS contiene un tenant invalido."
            )
            break

    session_identity_mode = str(
        getattr(config, "get", lambda *_: None)(
            "CHANNEL_SESSION_IDENTITY_MODE",
            "legacy",
        )
        or "legacy"
    ).strip().lower()
    if session_identity_mode not in {"legacy", "shadow", "enforce"}:
        errors.append("CHANNEL_SESSION_IDENTITY_MODE invalido.")
    session_identity_version = str(
        getattr(config, "get", lambda *_: None)(
            "CHANNEL_SESSION_IDENTITY_VERSION_V1",
            "v1",
        )
        or ""
    ).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,31}", session_identity_version):
        errors.append("CHANNEL_SESSION_IDENTITY_VERSION_V1 invalida.")
    dedicated_identity_secret = str(
        getattr(config, "get", lambda *_: None)(
            "CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1",
            "",
        )
        or ""
    )
    if session_identity_mode == "enforce" and len(
        dedicated_identity_secret.encode("utf-8")
    ) < 32:
        errors.append(
            "CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1 debe tener al menos 32 bytes en modo enforce."
        )
    if session_identity_mode == "shadow":
        shadow_fallbacks = (
            dedicated_identity_secret,
            str(
                getattr(config, "get", lambda *_: None)(
                    "WHATSAPP_INBOUND_HASH_SECRET",
                    "",
                )
                or ""
            ),
            str(
                getattr(config, "get", lambda *_: None)("SECRET_KEY", "")
                or ""
            ),
        )
        if not any(len(value.encode("utf-8")) >= 32 for value in shadow_fallbacks):
            errors.append(
                "CHANNEL_SESSION_IDENTITY_MODE=shadow requiere material HMAC durable de al menos 32 bytes."
            )
    if durability_mode == "queue" and session_identity_mode != "enforce":
        errors.append(
            "WHATSAPP_INBOUND_DURABILITY_MODE=queue requiere CHANNEL_SESSION_IDENTITY_MODE=enforce."
        )
    omnichannel_mode = str(
        getattr(config, "get", lambda *_: None)(
            "OMNICHANNEL_SIGNED_INBOUND_MODE",
            "disabled",
        )
        or "disabled"
    ).strip().lower()
    if omnichannel_mode not in {"disabled", "enforce"}:
        errors.append("OMNICHANNEL_SIGNED_INBOUND_MODE invalido.")
    if omnichannel_mode == "enforce":
        omnichannel_secret = str(
            getattr(config, "get", lambda *_: None)(
                "OMNICHANNEL_INBOUND_HMAC_SECRET_V1",
                "",
            )
            or ""
        )
        if len(omnichannel_secret.encode("utf-8")) < 32:
            errors.append(
                "OMNICHANNEL_INBOUND_HMAC_SECRET_V1 debe tener al menos 32 bytes en modo enforce."
            )
        omnichannel_bounds = (
            ("OMNICHANNEL_INBOUND_MAX_PAYLOAD_BYTES", 1024, 262144, int, 65536),
            (
                "OMNICHANNEL_INBOUND_MAX_CLOCK_SKEW_SECONDS",
                30,
                900,
                int,
                300,
            ),
        )
        for key, minimum, maximum, cast, default_value in omnichannel_bounds:
            raw_value = getattr(config, "get", lambda *_: None)(key, default_value)
            try:
                value = cast(raw_value)
            except (TypeError, ValueError, OverflowError):
                errors.append(f"{key} invalido para modo enforce.")
                continue
            if value < minimum or value > maximum:
                errors.append(f"{key} fuera de rango para modo enforce.")
    voice_lifecycle_enabled = getattr(config, "get", lambda *_: None)(
        "ENABLE_VOICE_CONSENT_LIFECYCLE_V1",
        False,
    )
    testing_runtime = bool(
        getattr(config, "get", lambda *_: None)("TESTING", False)
    )
    if (
        voice_lifecycle_enabled is True
        and not testing_runtime
        and session_identity_mode != "enforce"
    ):
        errors.append(
            "ENABLE_VOICE_CONSENT_LIFECYCLE_V1 requiere CHANNEL_SESSION_IDENTITY_MODE=enforce en produccion."
        )

    if durability_mode == "queue":
        stream_secret = str(
            getattr(config, "get", lambda *_: None)(
                "WHATSAPP_INBOUND_HASH_SECRET",
                "",
            )
            or ""
        )
        if len(stream_secret.encode("utf-8")) < 32:
            errors.append(
                "WHATSAPP_INBOUND_HASH_SECRET debe tener al menos 32 bytes en modo queue."
            )
        queue_bounds = (
            ("WHATSAPP_INBOUND_MAX_PAYLOAD_BYTES", 1024, 65536, int),
            ("WHATSAPP_INBOUND_LEASE_SECONDS", 30, 3600, int),
            ("WHATSAPP_INBOUND_MAX_ATTEMPTS", 1, 32, int),
            ("WHATSAPP_INBOUND_WORKER_BATCH_SIZE", 1, 100, int),
            ("WHATSAPP_INBOUND_WORKER_POLL_SECONDS", 0.05, 60.0, float),
            ("WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS", 0, 720, int),
            ("WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE", 1, 500, int),
        )
        queue_defaults = {
            "WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS": 72,
            "WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE": 200,
        }
        for key, minimum, maximum, cast in queue_bounds:
            raw_value = getattr(config, "get", lambda *_: None)(
                key,
                queue_defaults.get(key),
            )
            try:
                value = cast(raw_value)
            except (TypeError, ValueError, OverflowError):
                errors.append(f"{key} invalido para modo queue.")
                continue
            if value < minimum or value > maximum:
                errors.append(f"{key} fuera de rango para modo queue.")
        legal_hold = getattr(config, "get", lambda *_: None)(
            "WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD",
            False,
        )
        if not isinstance(legal_hold, bool):
            errors.append("WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD invalido para modo queue.")

    domain_effect_mode = str(
        getattr(config, "get", lambda *_: None)(
            "DOMAIN_EFFECT_OUTBOX_MODE",
            "legacy",
        )
        or "legacy"
    ).strip().lower()
    if domain_effect_mode not in {"legacy", "queue"}:
        errors.append("DOMAIN_EFFECT_OUTBOX_MODE inválido.")
    if domain_effect_mode == "queue":
        domain_effect_secret = str(
            getattr(config, "get", lambda *_: None)(
                "DOMAIN_EFFECT_OUTBOX_SECRET",
                "",
            )
            or ""
        )
        if len(domain_effect_secret.encode("utf-8")) < 32:
            errors.append(
                "DOMAIN_EFFECT_OUTBOX_SECRET debe tener al menos 32 bytes en modo queue."
            )

        tenant_scope = str(
            getattr(config, "get", lambda *_: None)(
                "DOMAIN_EFFECT_OUTBOX_TENANT_IDS",
                "",
            )
            or ""
        ).strip()
        tenant_tokens = [token.strip() for token in tenant_scope.split(",") if token.strip()]
        if not tenant_tokens:
            errors.append(
                "DOMAIN_EFFECT_OUTBOX_TENANT_IDS requiere una lista canaria explícita en modo queue."
            )
        else:
            for token in tenant_tokens:
                try:
                    tenant_id = int(token)
                except (TypeError, ValueError):
                    errors.append("DOMAIN_EFFECT_OUTBOX_TENANT_IDS contiene un tenant inválido.")
                    break
                if tenant_id <= 0:
                    errors.append("DOMAIN_EFFECT_OUTBOX_TENANT_IDS contiene un tenant inválido.")
                    break

        domain_effect_bounds = (
            ("DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES", 256, 8192, int, 4096),
            ("DOMAIN_EFFECT_OUTBOX_LEASE_SECONDS", 30, 3600, int, 180),
            ("DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS", 1, 32, int, 8),
            ("DOMAIN_EFFECT_OUTBOX_WORKER_BATCH_SIZE", 1, 100, int, 20),
            ("DOMAIN_EFFECT_OUTBOX_WORKER_POLL_SECONDS", 0.05, 60.0, float, 0.5),
        )
        for key, minimum, maximum, cast, default_value in domain_effect_bounds:
            raw_value = getattr(config, "get", lambda *_: None)(key, default_value)
            try:
                value = cast(raw_value)
            except (TypeError, ValueError, OverflowError):
                errors.append(f"{key} invalido para modo queue.")
                continue
            if value < minimum or value > maximum:
                errors.append(f"{key} fuera de rango para modo queue.")

    survey_effect_worker_bounds = (
        ("SURVEY_RESPONSE_EFFECT_LEASE_SECONDS", 30, 3600, int, 120),
        ("SURVEY_RESPONSE_EFFECT_WORKER_BATCH_SIZE", 1, 500, int, 50),
        (
            "SURVEY_RESPONSE_EFFECT_WORKER_MAX_TENANTS_PER_CYCLE",
            1,
            500,
            int,
            50,
        ),
        (
            "SURVEY_RESPONSE_EFFECT_WORKER_POLL_SECONDS",
            0.05,
            60.0,
            float,
            0.5,
        ),
    )
    for key, minimum, maximum, cast, default_value in survey_effect_worker_bounds:
        raw_value = getattr(config, "get", lambda *_: None)(key, default_value)
        try:
            value = cast(raw_value)
        except (TypeError, ValueError, OverflowError):
            errors.append(f"{key} invalido para worker de encuestas.")
            continue
        if value < minimum or value > maximum:
            errors.append(f"{key} fuera de rango para worker de encuestas.")

    process_role = str(
        getattr(config, "get", lambda *_: None)("CHATBOC_PROCESS_ROLE", "")
        or ""
    ).strip().lower()
    durable_worker_standby = getattr(config, "get", lambda *_: None)(
        "WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED",
        False,
    )
    if not isinstance(durable_worker_standby, bool):
        errors.append("WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED invalido.")
    if (
        process_role == "whatsapp-durable-worker"
        and durability_mode != "queue"
        and durable_worker_standby is not True
    ):
        errors.append(
            "CHATBOC_PROCESS_ROLE=whatsapp-durable-worker requiere "
            "WHATSAPP_INBOUND_DURABILITY_MODE=queue o standby explicito."
        )

    payload_scrub_enabled = getattr(config, "get", lambda *_: None)(
        "WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED",
        False,
    )
    payload_legal_hold = getattr(config, "get", lambda *_: None)(
        "WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD",
        True,
    )
    if not isinstance(payload_scrub_enabled, bool):
        errors.append("WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED invalido.")
    if not isinstance(payload_legal_hold, bool):
        errors.append("WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD invalido.")
    scrub_tenant_tokens = [
        token.strip()
        for token in str(
            getattr(config, "get", lambda *_: None)(
                "WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS",
                "",
            )
            or ""
        ).split(",")
        if token.strip()
    ]
    for token in scrub_tenant_tokens:
        try:
            if int(token) <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            errors.append(
                "WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS contiene un tenant invalido."
            )
            break
    if payload_scrub_enabled is True and payload_legal_hold is False:
        if not scrub_tenant_tokens:
            errors.append(
                "WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS requiere un scope historico explicito."
            )
        retention_bounds = (
            ("WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS", 0, 720, int, 72),
            ("WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE", 1, 500, int, 200),
        )
        for key, minimum, maximum, cast, default_value in retention_bounds:
            raw_value = getattr(config, "get", lambda *_: None)(key, default_value)
            try:
                value = cast(raw_value)
            except (TypeError, ValueError, OverflowError):
                errors.append(f"{key} invalido para retencion WhatsApp.")
                continue
            if value < minimum or value > maximum:
                errors.append(f"{key} fuera de rango para retencion WhatsApp.")
    socket_queue_url = str(
        getattr(config, "get", lambda *_: None)(
            "SOCKETIO_MESSAGE_QUEUE_URL",
            "",
        )
        or ""
    ).strip()
    if process_role == "survey-effect-worker" and not socket_queue_url:
        errors.append(
            "SOCKETIO_MESSAGE_QUEUE_URL es obligatoria para survey-effect-worker."
        )
    if socket_queue_url:
        parsed_socket_queue = urlparse(socket_queue_url)
        if parsed_socket_queue.scheme.lower() not in {"redis", "rediss"}:
            errors.append(
                "SOCKETIO_MESSAGE_QUEUE_URL debe usar redis:// o rediss://."
            )
        if not parsed_socket_queue.hostname:
            errors.append("SOCKETIO_MESSAGE_QUEUE_URL no contiene un host válido.")

    socket_queue_channel = str(
        getattr(config, "get", lambda *_: None)(
            "SOCKETIO_MESSAGE_QUEUE_CHANNEL",
            "chatboc-realtime-v1",
        )
        or ""
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", socket_queue_channel):
        errors.append("SOCKETIO_MESSAGE_QUEUE_CHANNEL inválido.")

    raw_socket_timeout = getattr(config, "get", lambda *_: None)(
        "SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS",
        2,
    )
    try:
        socket_timeout = float(raw_socket_timeout)
    except (TypeError, ValueError, OverflowError):
        errors.append(
            "SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS inválido."
        )
    else:
        if socket_timeout < 0.1 or socket_timeout > 10:
            errors.append(
                "SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS fuera de rango."
            )

    return errors


class TestConfig(Config):
    ENV = 'testing'
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    SQLALCHEMY_ENGINE_OPTIONS = {'connect_args': {'timeout': 5}}
    CELERY_TASK_ALWAYS_EAGER = True
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    SKIP_INIT_TENANTS = True
    SESSION_COOKIE_SECURE = False
    SERVER_NAME = 'localhost'
    SESSION_COOKIE_DOMAIN = None
    SESSION_TYPE = 'null'
    CORS_ALLOW_LOCAL_DEV = True
    ALLOW_SURVEY_DEMO_SEEDING = True
    ENABLE_SURVEY_SYNTHETIC_SEEDING_V1 = False
    ENABLE_PREVIEW_DURABLE_DEMO_SURVEY_VOTES_V1 = False
    PREVIEW_DURABLE_DEMO_NEON_BRANCH_ID = ""
    PREVIEW_DURABLE_DEMO_SURVEY_MAX_INTERACTIONS = "50"
    SURVEY_SYNTHETIC_SEED_TENANT_IDS = ""
    SURVEY_JURISDICTION_GATE_MODE = "observe"
    SURVEY_JURISDICTION_GATE_TENANT_IDS = ""

class TestingConfig(TestConfig):
    pass
