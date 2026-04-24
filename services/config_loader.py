import json
import os
import logging

logger = logging.getLogger(__name__)

# Determine the base directory for municipal data. Deployments on services like
# Render provide a persistent volume mounted at `/data`. We want to always store
# mutable configuration (like `agenda_cultural.json`) there so it survives
# redeploys. Allow overriding via the `DATA_DIR` environment variable, and fall
# back to the repository's `data` directory only if writing to `/data` is not
# possible (e.g. during local development without permissions).

_default_data_path = os.environ.get("DATA_DIR", "/data")

def _ensure_base_directory(base_path: str, *subdirs: str) -> str:
    """Ensure the desired directory tree exists, returning the usable base path."""

    try:
        os.makedirs(os.path.join(base_path, *subdirs), exist_ok=True)
        return base_path
    except OSError:
        return ""

# Prefer the external persistent volume when available; otherwise fall back to
# the repository bundled data directory for local development and automated
# tests.
base_path_candidate = _ensure_base_directory(_default_data_path, "municipios")
if not base_path_candidate:
    repo_data_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    _default_data_path = repo_data_path
    _ensure_base_directory(_default_data_path, "municipios")
else:
    _default_data_path = base_path_candidate

# Ensure PYME directories mirror the municipio layout so configuration files
# can be dropped in the same persistent volume.
_ensure_base_directory(_default_data_path, "pyme", "rubros")
# Ensure tenant-specific directories exist
_ensure_base_directory(_default_data_path, "tenants")

BASE_DATA_PATH = _default_data_path
BASE_CONFIG_PATH = os.path.join(BASE_DATA_PATH, "municipios")
BASE_PYME_CONFIG_PATH = os.path.join(BASE_DATA_PATH, "pyme", "rubros")
BASE_TENANT_CONFIG_PATH = os.path.join(BASE_DATA_PATH, "tenants")

_config_cache = {}
_mtime_cache = {}

_pyme_config_cache = {}
_pyme_mtime_cache = {}


def cargar_configuracion_municipio(municipio_id: str, archivo: str) -> dict:
    """Carga un archivo de configuración JSON para el municipio indicado.

    `municipio_id` puede recibirse como ``int`` o ``str``. Para evitar errores
    al construir la ruta del archivo, se fuerza su conversión a cadena.

    La información se recarga automáticamente si el archivo es modificado.
    """
    municipio_id = str(municipio_id)
    clave = (municipio_id, archivo)
    ruta = os.path.join(BASE_CONFIG_PATH, municipio_id, archivo)

    # If the file is not present in the mounted `/data` volume, fall back to the
    # repository's bundled `data/` directory. This covers test environments and
    # fresh deployments where the persistent volume has not yet been populated.
    repo_ruta = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "data", "municipios", municipio_id, archivo)
    if not os.path.exists(ruta):
        if os.path.exists(repo_ruta):
            ruta = repo_ruta
        elif municipio_id != "default":
            # Fallback to the shared "default" configuration when a municipality
            # specific file is missing. This prevents noisy errors in logs and
            # keeps behaviour consistent for municipalities that have not yet
            # provided their own overrides.
            return cargar_configuracion_municipio("default", archivo)

    try:
        mtime = os.path.getmtime(ruta)
    except OSError as e:
        # Si el archivo no existe, evitar loguear como error en cada acceso.
        # Para configuraciones "default" inexistentes, registrar un warning
        # más amigable y continuar con un dict vacío.
        if isinstance(e, FileNotFoundError):
            logger.warning(f"[CONFIG] Archivo de configuración no encontrado en {ruta}. Usando valores por defecto.")
        else:
            logger.error(f"[CONFIG] No se pudo acceder a {ruta}: {e}")
        _config_cache[clave] = {}
        _mtime_cache[clave] = None
        return {}

    if clave in _config_cache and _mtime_cache.get(clave) == mtime:
        return _config_cache[clave]

    try:
        with open(ruta, "r", encoding="utf-8") as f:
            datos = json.load(f)
        _config_cache[clave] = datos
        _mtime_cache[clave] = mtime
        return datos
    except Exception as e:
        logger.error(f"[CONFIG] No se pudo cargar {ruta}: {e}")
        _config_cache[clave] = {}
        _mtime_cache[clave] = mtime
        return {}


def cargar_configuracion_pyme(rubro_slug: str, archivo: str, tenant_slug: str | None = None) -> dict:
    """Carga un archivo de configuración JSON.

    Prioridad:
    1. Tenant específico (`data/tenants/<tenant_slug>`)
    2. Rubro específico (`data/pyme/rubros/<rubro_slug>`)
    3. Default (`data/pyme/rubros/default`)
    """

    if not rubro_slug:
        rubro_slug = "default"

    rubro_slug = str(rubro_slug).strip().lower()

    # Construct a cache key that includes tenant_slug to differentiate
    cache_key_prefix = f"tenant_{tenant_slug}" if tenant_slug else f"rubro_{rubro_slug}"
    clave = (cache_key_prefix, archivo)

    rutas_candidatas = []

    # 1. Check tenant-specific path if provided
    if tenant_slug:
        tenant_slug = str(tenant_slug).strip().lower()
        rutas_candidatas.append(os.path.join(BASE_TENANT_CONFIG_PATH, tenant_slug, archivo))
        # Also check repo fallback for tenant
        rutas_candidatas.append(os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", "tenants", tenant_slug, archivo
        ))

    # 2. Check rubro-specific path
    # If a tenant_slug is present, we check `data/pyme/rubros/{rubro_slug}/{tenant_slug}/{archivo}`
    if tenant_slug:
        rutas_candidatas.append(os.path.join(BASE_PYME_CONFIG_PATH, rubro_slug, tenant_slug, archivo))
        rutas_candidatas.append(os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", "pyme", "rubros", rubro_slug, tenant_slug, archivo
        ))

    rutas_candidatas.append(os.path.join(BASE_PYME_CONFIG_PATH, rubro_slug, archivo))
    rutas_candidatas.append(os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "pyme", "rubros", rubro_slug, archivo
    ))

    ruta_elegida = None
    for ruta in rutas_candidatas:
        if os.path.exists(ruta):
            ruta_elegida = ruta
            break

    # 3. Fallback to default if not found
    if not ruta_elegida and rubro_slug != "default":
        # Recursive call without tenant_slug to force fallback logic
        return cargar_configuracion_pyme("default", archivo, tenant_slug=None)

    if not ruta_elegida:
        logger.warning(
            f"[CONFIG] Archivo PYME '{archivo}' no encontrado para rubro '{rubro_slug}' (tenant: {tenant_slug})."
        )
        _pyme_config_cache[clave] = {}
        _pyme_mtime_cache[clave] = None
        return {}

    try:
        mtime = os.path.getmtime(ruta_elegida)
    except OSError as e:
        logger.error(f"[CONFIG] No se pudo acceder a {ruta_elegida}: {e}")
        _pyme_config_cache[clave] = {}
        _pyme_mtime_cache[clave] = None
        return {}

    if clave in _pyme_config_cache and _pyme_mtime_cache.get(clave) == mtime:
        return _pyme_config_cache[clave]

    try:
        with open(ruta_elegida, "r", encoding="utf-8") as f:
            datos = json.load(f)
        _pyme_config_cache[clave] = datos
        _pyme_mtime_cache[clave] = mtime
        return datos
    except Exception as e:
        logger.error(f"[CONFIG] No se pudo cargar {ruta_elegida}: {e}")
        _pyme_config_cache[clave] = {}
        _pyme_mtime_cache[clave] = mtime
        return {}

    try:
        mtime = os.path.getmtime(ruta)
    except OSError as e:
        if isinstance(e, FileNotFoundError):
            logger.warning(
                f"[CONFIG] Archivo de configuración PYME no encontrado en {ruta}. Usando valores por defecto."
            )
        else:
            logger.error(f"[CONFIG] No se pudo acceder a {ruta}: {e}")
        _pyme_config_cache[clave] = {}
        _pyme_mtime_cache[clave] = None
        return {}

    if clave in _pyme_config_cache and _pyme_mtime_cache.get(clave) == mtime:
        return _pyme_config_cache[clave]

    try:
        with open(ruta, "r", encoding="utf-8") as f:
            datos = json.load(f)
        _pyme_config_cache[clave] = datos
        _pyme_mtime_cache[clave] = mtime
        return datos
    except Exception as e:
        logger.error(f"[CONFIG] No se pudo cargar {ruta}: {e}")
        _pyme_config_cache[clave] = {}
        _pyme_mtime_cache[clave] = mtime
        return {}
