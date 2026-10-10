import json
import logging
import os
import re
import unicodedata
import requests
from flask import current_app, has_app_context
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
import services.google_maps_service as google_maps_service
from services.config_loader import cargar_configuracion_municipio
from services import location_service
from utils.municipio_utils import get_numeric_municipio_id
from services.tts_orchestrator import generar_audio
from models import MunicipioPost
from database import db
from services.openai_bridge import client as openai_client
from services.openai_maps_service import geocodificar_inversa_llm, geocodificar_texto_llm
from services.llm_provider_network_policy import llm_provider_network_allowed
from services.openai_model_defaults import (
    DEFAULT_OPENAI_TERRA_MODEL,
    chat_completion_compatibility_options,
    resolve_openai_model,
)
from services.estacionamiento_utils import aproximar_coordenadas_por_texto
from pathlib import Path
from zoneinfo import ZoneInfo

# ... (el resto de tus herramientas y diccionarios)

# --- NUEVA HERRAMIENTA DE SUGERENCIA DE CATEGORÍAS ---

def crear_prompt_sugerir_categorias(texto_usuario: str, categorias_disponibles: list[str]) -> str:
    """Crea un prompt específico para que el LLM sugiera categorías relevantes."""
    
    lista_categorias_str = "\n".join(f"- {cat}" for cat in categorias_disponibles)
    
    prompt = f"""
Tu tarea es actuar como un experto clasificador de reclamos municipales.
Dado un RECLAMO DE USUARIO y una LISTA DE CATEGORÍAS, tu única función es seleccionar las 3 categorías más relevantes de la lista que mejor correspondan al reclamo del usuario.

LISTA DE CATEGORÍAS DISPONIBLES:
{lista_categorias_str}

RECLAMO DE USUARIO: "{texto_usuario}"

INSTRUCCIONES:
- Analiza el reclamo del usuario y compáralo con la lista de categorías.
- Devuelve SÓLO un objeto JSON que contenga una única clave "sugerencias" con una lista de hasta 3 nombres de categorías extraídos EXACTAMENTE de la lista proporcionada.
- Si ninguna categoría parece relevante, devuelve una lista vacía.

Ejemplo 1:
- Reclamo: "la esquina de mi casa está a oscuras y el asfalto es un desastre"
- Respuesta: {{"sugerencias": ["Luminaria", "Arreglo de calle"]}}

Ejemplo 2:
- Reclamo: "quiero saber el teléfono del intendente"
- Respuesta: {{"sugerencias": []}}

Tu respuesta:
"""
    return prompt

def sugerir_categorias_relevantes(texto_usuario: str) -> list[str]:
    """
    Usa el LLM para obtener una lista de categorías sugeridas basadas en el texto del usuario.
    """
    todas_las_categorias = sorted(list(set(KEYWORD_TO_CATEGORY_MAP.values()))) # Still useful for keyword matching
    # LLM call removed. Category suggestion is now expected from the main LLM call.
    # This function now performs basic keyword matching as a fallback or primary if called directly.
    logger.info(
        "Suggesting categories without LLM input_chars=%s",
        len(str(texto_usuario or "")),
    )
    sugeridas = []
    if not texto_usuario:
        return sugeridas

    _ensure_keyword_cache_actualizado()
    texto_norm = normalizar_texto(texto_usuario)
    from services.categorias_municipio import CATEGORIAS_RECLAMO
    # Contar ocurrencias de keywords para cada categoría
    conteo_categorias = {cat: 0 for cat in CATEGORIAS_RECLAMO} # Use the defined list
    palabras_usuario = set(texto_norm.split())

    for keyword, category_target in KEYWORD_TO_CATEGORY_MAP.items():
        # Usar una keyword normalizada para la comparación si es necesario,
        # aunque KEYWORD_TO_CATEGORY_MAP ya tiene claves en minúscula y sin acentos (asumido).
        if keyword in palabras_usuario:
            conteo_categorias[category_target] = conteo_categorias.get(category_target, 0) + 1
            if keyword in texto_norm: # Dar más peso si es una frase
                 conteo_categorias[category_target] = conteo_categorias.get(category_target, 0) + 2


    # Ordenar por conteo descendente
    categorias_ordenadas = sorted(conteo_categorias.items(), key=lambda item: item[1], reverse=True)

    for cat, count in categorias_ordenadas:
        if count > 0 and len(sugeridas) < 3:
            if cat not in sugeridas: # Evitar duplicados si diferentes keywords apuntan a la misma categoría
                 sugeridas.append(cat)
        if len(sugeridas) >= 3:
            break

    if not sugeridas and texto_usuario:
        # Si después del keyword matching no hay nada, pero había texto, sugerir "Otro Motivo"
        # Asegurarse que "Otro Motivo" sea una de las CATEGORIAS_RECLAMO válidas.
        if "Otro Motivo" in CATEGORIAS_RECLAMO: # Check against the defined list
            sugeridas.append("Otro Motivo")

    logger.info(
        "Categories suggested without LLM input_chars=%s suggestion_count=%s",
        len(str(texto_usuario or "")),
        len(sugeridas),
    )
    return sugeridas # Devuelve hasta 3, o menos si no hay suficientes matches.
   
logger = logging.getLogger(__name__)

_SAFE_GEO_RESULT_FIELDS = frozenset(
    {
        "barrio",
        "calle",
        "codigo_postal",
        "departamento",
        "distrito",
        "formatted_address",
        "localidad",
        "lote",
        "manzana",
        "numero",
        "otros_detalles",
        "piso",
        "provincia",
    }
)
_SAFE_TOOL_PARAMETER_FIELDS = frozenset(
    {
        "direccion",
        "fecha",
        "lat",
        "latitude",
        "localidad",
        "lon",
        "longitude",
        "opennow",
        "radio",
        "radius",
        "rubro",
        "tipo_lugar",
        "tipo_negocio",
        "ubicacion",
    }
)


def _safe_geo_present_fields(value) -> list[str]:
    """Return known structural field names without serialising geo values."""

    if not isinstance(value, dict):
        return []
    return sorted(
        field
        for field in _SAFE_GEO_RESULT_FIELDS
        if field in value and value.get(field) not in (None, "")
    )


def _env_flag_enabled(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _openai_network_allowed_for_address_parsing() -> bool:
    """Disable real OpenAI calls in tests unless explicitly opted in."""

    app_testing = bool(has_app_context() and current_app.config.get("TESTING"))
    testing = app_testing or _env_flag_enabled("TESTING")
    return not testing or _env_flag_enabled("OPENAI_ALLOW_NETWORK_IN_TESTS")


def geocode_address(*args, **kwargs):
    """Patch-friendly proxy used by legacy callers and isolated tests."""

    return location_service.geocode_address(*args, **kwargs)
Maps_API_KEY = os.environ.get("Maps_API_KEY")
MUNICIPIO_ID = os.environ.get("MUNICIPIO_ID", "default")
CONFIG_MUNICIPIO = cargar_configuracion_municipio(MUNICIPIO_ID, "config.json")
ARG_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


# --- Utilidades internas para parsing sin LLM ---
def _extract_level_info(segmento: str) -> tuple[None | str, None | str, str | None]:
    """Extrae datos de piso/departamento de un segmento de texto."""

    if not segmento:
        return None, None, None

    restante = segmento
    piso = None
    departamento = None

    piso_match = re.search(r"(?:piso|p\.?|nivel)\s*([0-9a-zA-Z]+)", segmento, flags=re.IGNORECASE)
    if piso_match:
        piso = piso_match.group(1).strip().upper()
        restante = re.sub(r"(?:piso|p\.?|nivel)\s*[0-9a-zA-Z]+", "", restante, flags=re.IGNORECASE)

    depto_match = re.search(r"(?:departamento|dpto|depto)\s*([0-9a-zA-Z]+)", restante, flags=re.IGNORECASE)
    if depto_match:
        departamento = depto_match.group(1).strip().upper()
        restante = re.sub(r"(?:departamento|dpto|depto)\s*[0-9a-zA-Z]+", "", restante, flags=re.IGNORECASE)

    restante = restante.strip(",;.- ")
    return piso, departamento, restante or None


def _parse_direccion_basica(texto_direccion: str, municipio_config: dict | None = None) -> dict | None:
    """Fallback determinístico para extraer componentes de una dirección."""

    if not texto_direccion:
        return None

    municipio_config = municipio_config or {}
    default_localidad = municipio_config.get('ciudad_default') or municipio_config.get('ciudad')
    default_provincia = municipio_config.get('provincia_default') or municipio_config.get('provincia')

    partes = [p.strip() for p in re.split(r",|\n|;", texto_direccion) if p.strip()]
    if not partes:
        return None

    calle = None
    numero = None
    piso = None
    departamento = None
    barrio = None
    distrito = None
    manzana = None
    lote = None
    localidad = None
    provincia = None
    codigo_postal = None
    otros_detalles: list[str] = []

    primera = partes[0]
    match = re.match(r"^(?P<calle>.+?)\s+(?P<numero>\d+[0-9A-Za-z/-]*)\b(?:\s+(?P<resto>.*))?", primera)
    if match:
        calle = match.group('calle').strip(", ")
        numero = match.group('numero').strip()
        resto = match.group('resto')
        if resto:
            p_tmp, d_tmp, sobrante = _extract_level_info(resto)
            piso = piso or p_tmp
            departamento = departamento or d_tmp
            if sobrante:
                otros_detalles.append(sobrante)
    else:
        calle = primera.strip()
        lower_primera = calle.lower()
        if any(token in lower_primera for token in ("barrio", "b°", "bº")):
            barrio = re.sub(r"^(barrio|b°|bº)\s+", "", calle, flags=re.IGNORECASE).strip() or barrio
        if any(token in lower_primera for token in ("distrito", "zona", "localidad", "ciudad")):
            distrito = re.sub(r"^(distrito|zona|localidad|ciudad)\s+", "", calle, flags=re.IGNORECASE).strip() or distrito
            localidad = localidad or distrito

    for segmento in partes[1:]:
        if not segmento:
            continue

        p_tmp, d_tmp, sobrante = _extract_level_info(segmento)
        if p_tmp and not piso:
            piso = p_tmp
        if d_tmp and not departamento:
            departamento = d_tmp
        if sobrante:
            segmento = sobrante

        lower = segmento.lower()
        if not barrio and any(token in lower for token in ('barrio', 'b°', 'bº')):
            barrio = re.sub(r"^(barrio|b°|bº)\s+", "", segmento, flags=re.IGNORECASE).strip()
            continue
        if any(token in lower for token in ("distrito", "zona", "localidad", "ciudad")) and not localidad:
            distrito = re.sub(r"^(distrito|zona|localidad|ciudad)\s+", "", segmento, flags=re.IGNORECASE).strip()
            localidad = distrito or localidad
            continue
        manzana_match = re.search(r"\b(?:manzana|mz)\s*([0-9a-zA-Z-]+)", segmento, flags=re.IGNORECASE)
        if manzana_match and not manzana:
            manzana = manzana_match.group(1).strip()
        lote_match = re.search(r"\blote\s*([0-9a-zA-Z-]+)", segmento, flags=re.IGNORECASE)
        if lote_match and not lote:
            lote = lote_match.group(1).strip()
        if re.search(r"\b(manzana|mz|lote)\b", lower):
            otros_detalles.append(segmento)
            continue
        if not codigo_postal and re.fullmatch(r"\d{4}", segmento):
            codigo_postal = segmento
            continue
        if not localidad:
            localidad = segmento
            continue
        if not provincia:
            provincia = segmento
            continue
        otros_detalles.append(segmento)

    localidad = localidad or default_localidad
    provincia = provincia or default_provincia

    resultado = {
        "calle": calle,
        "numero": numero,
        "piso": piso,
        "departamento": departamento,
        "barrio": barrio,
        "distrito": distrito,
        "manzana": manzana,
        "lote": lote,
        "localidad": localidad,
        "provincia": provincia,
        "codigo_postal": codigo_postal,
        "otros_detalles": ", ".join(otros_detalles) if otros_detalles else None,
    }

    if not resultado["calle"] or not resultado["localidad"]:
        return None

    logger.info(
        "[ParseDireccion][Fallback] Address parsed without LLM input_chars=%s "
        "result_fields=%s",
        len(str(texto_direccion or "")),
        _safe_geo_present_fields(resultado),
    )
    return resultado


# --- NUEVA FUNCIÓN DE NORMALIZACIÓN ---
def normalizar_texto(texto: str) -> str:
    """Normaliza un texto eliminando acentos y puntuación sin modificar palabras."""

    if not texto:
        return ""

    texto = texto.lower().strip()

    # Quitar diacríticos (acentos)
    texto = ''.join(
        c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c)
    )

    # Mantener solo caracteres alfanuméricos y espacios
    texto = re.sub(r"[^a-z0-9\s]", "", texto)

    # Normalizar espacios múltiples
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto

# --- VALIDACIÓN DE DIRECCIONES ---
def direccion_es_valida(texto: str) -> bool:
    """Verifica si una dirección es válida.

    Intenta usar el servicio de geocodificación; si no está disponible
    (por ejemplo, falta la API key) o no encuentra resultados, se aplica
    una validación heurística simple para evitar repetir pedidos de
    dirección al usuario.
    """
    if not texto:
        return False

    geocode_result = geocode_address(texto)
    if geocode_result is not None:
        return True

    texto_normalizado = normalizar_texto(texto)
    if not texto_normalizado:
        return False

    invalid_phrases = {"sin numero", "sin nro", "no tengo numero", "no se el numero", "hola", "menu", "cancelar", "confirmar"}
    if texto_normalizado in invalid_phrases or any(p in texto_normalizado for p in {"sin numero", "sin nro", "no tengo numero"}):
        return False

    if re.search(r"\d+", texto):
        return True

    # Si la dirección tiene al menos 2 palabras (ej: "Sarmiento, Junín" o "Av. San Martín"), es válida.
    if len(texto_normalizado.split()) >= 2:
        return True

    if re.search(r"\b(esquina|interseccion|intersección|entre)\b", texto_normalizado):
        return True

    if re.search(r"\b[a-z]{3,}\s+(y|e)\s+[a-z]{3,}\b", texto_normalizado):
        return True

    if re.search(r"\b(km|ruta|autopista|rotonda|puente)\b", texto_normalizado):
        return True

    if re.search(r"\b(plaza|parque|monumento|terminal|hospital|escuela|cementerio)\b", texto_normalizado):
        return True

    if re.search(r"\b(barrio|distrito|manzana|mz|lote)\b", texto_normalizado):
        return True

    return len(texto_normalizado) >= 3


def parse_direccion_completa(texto_direccion: str, municipio_config: dict = None) -> dict | None:
    """
    Usa un LLM para extraer componentes estructurados de una dirección.
    Args:
        texto_direccion: La dirección proporcionada por el usuario.
        municipio_config: Configuración del municipio actual (puede contener ciudad/provincia por defecto).
    Returns:
        Un diccionario con los campos de la dirección o None si falla la extracción.
    """
    if not texto_direccion:
        return None

    if municipio_config is None:
        # This case should be less frequent if context always provides one (even the global one)
        logger.warning("[ParseDireccion] municipio_config no fue proporcionado, usando un diccionario vacío como fallback para defaults.")
        municipio_config = {}

    # Prioritize '_default' suffixed keys, then direct keys, then hardcoded N/A
    default_localidad = municipio_config.get('ciudad_default', municipio_config.get('ciudad', 'Localidad Desconocida'))
    default_provincia = municipio_config.get('provincia_default', municipio_config.get('provincia', 'Provincia Desconocida'))

    prompt = f"""
    Tu tarea es extraer de forma precisa los componentes de una dirección argentina en un objeto JSON.

    Dirección de entrada: "{texto_direccion}"

    Considera estos valores por defecto si no están presentes en la dirección:
    - Localidad: {default_localidad}
    - Provincia: {default_provincia}

    Extrae los siguientes campos:
    - "calle"
    - "numero"
    - "piso" (opcional)
    - "departamento" (opcional)
    - "barrio" (opcional)
    - "distrito" (opcional)
    - "manzana" (opcional)
    - "lote" (opcional)
    - "localidad"
    - "provincia"
    - "codigo_postal" (opcional)
    - "otros_detalles" (cualquier información adicional relevante: esquina/intersección, "entre calles", plaza, monumento, referencias)

    Responde únicamente con el objeto JSON. Si no puedes extraer una calle o una localidad, devuelve un JSON vacío.
    """
    if not _openai_network_allowed_for_address_parsing():
        logger.info(
            "[ParseDireccion] OpenAI normalization skipped reason=test_network_disabled "
            "input_chars=%s",
            len(texto_direccion),
        )
    else:
        try:
            if not openai_client:
                raise ConnectionError("OpenAI client is not initialized.")

            model = resolve_openai_model(
                "OPENAI_ADDRESS_NORMALIZATION_MODEL",
                DEFAULT_OPENAI_TERRA_MODEL,
            )
            request_kwargs = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Sos un experto en normalización de direcciones argentinas. Tu única función es devolver un objeto JSON con los datos de la dirección.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            }
            request_kwargs.update(chat_completion_compatibility_options(model))
            response = openai_client.chat.completions.create(**request_kwargs)
            respuesta_llm = response.choices[0].message.content
            parsed_data = json.loads(respuesta_llm)
            if not isinstance(parsed_data, dict) or not parsed_data.get("calle") or not parsed_data.get("localidad"):
                logger.warning(
                    "[ParseDireccion] LLM response missing required fields model=%s input_chars=%s",
                    model,
                    len(texto_direccion),
                )
            else:
                logger.info(
                    "[ParseDireccion] Address normalized model=%s input_chars=%s "
                    "result_fields=%s",
                    model,
                    len(texto_direccion),
                    _safe_geo_present_fields(parsed_data),
                )
                return parsed_data
        except Exception as exc:
            logger.error(
                "[ParseDireccion] OpenAI normalization failed error_type=%s input_chars=%s",
                type(exc).__name__,
                len(texto_direccion),
            )

    # Fallback determinístico si el LLM no entrega datos útiles
    parsed_fallback = _parse_direccion_basica(texto_direccion, municipio_config)
    if parsed_fallback:
        return parsed_fallback

    logger.warning(
        "[ParseDireccion] Address could not be normalized input_chars=%s",
        len(texto_direccion),
    )
    return None

# --- HERRAMIENTA 1: CONSULTA DE RECOLECCIÓN ---
def consultar_recoleccion_por_direccion(direccion: str, context: dict | None = None) -> str:
    """
    Herramienta profesional que usa la API de Google Maps para geocodificar una dirección
    y luego determina el horario de recolección.
    """
    logger.info(
        "[HERRAMIENTA GEO] Looking up collection schedule input_chars=%s",
        len(str(direccion or "")),
    )

    if not Maps_API_KEY:
        logger.error("[HERRAMIENTA GEO] Clave de API de Google Maps (Maps_API_KEY) no configurada en el entorno.")
        return "Error de configuración: El servicio de mapas no está disponible en este momento. No se pudo validar la dirección geográficamente, pero puedes continuar con el reclamo si la dirección es correcta."

    municipio_config = context.get("municipio_config_actual") if context else CONFIG_MUNICIPIO
    if not municipio_config:
        municipio_config = CONFIG_MUNICIPIO

    ciudad = municipio_config.get("ciudad", "")
    provincia = municipio_config.get("provincia", "")

    component_parts = ["country:AR"]
    if provincia and provincia != 'N/A':
        component_parts.append(f"administrative_area:{provincia.replace(' ', '')}")
    if ciudad and ciudad != 'N/A':
        component_parts.append(f"locality:{ciudad.replace(' ', '')}")

    components_str = "|".join(component_parts)
    logger.info(
        "Geocoding recoleccion component_count=%s has_country=%s "
        "has_admin_area=%s has_locality=%s",
        len(component_parts),
        True,
        bool(provincia),
        bool(ciudad),
    )

    params = {
        'address': direccion,
        'key': Maps_API_KEY,
        'language': 'es',
        'components': components_str
    }
    geocode_url = "https://maps.googleapis.com/maps/api/geocode/json"

    try:
        response = requests.get(geocode_url, params=params)
        response.raise_for_status()
        data = response.json()

        if not data or data['status'] != 'OK' or not data.get('results'):
            logger.warning(
                "[HERRAMIENTA GEO] Provider could not geocode address "
                "input_chars=%s has_results=%s",
                len(str(direccion or "")),
                bool(data and data.get("results")),
            )
            return "No pude verificar esa dirección. ¿Puedes ser un poco más específico, incluyendo la ciudad?"

        location = data['results'][0]['geometry']['location']
        lat, lng = location['lat'], location['lng']
        logger.info(
            "[HERRAMIENTA GEO] Geocode succeeded input_chars=%s has_coordinates=%s",
            len(str(direccion or "")),
            lat is not None and lng is not None,
        )

        if -34.595 <= lat <= -34.580 and -60.955 <= lng <= -60.935:
            return f"Detecté que la dirección '{direccion}' está en la **zona céntrica**. Allí, la recolección es de **Lunes a Sábado por la noche (a partir de las 22:00 hs)**."
        elif -34.580 <= lat <= -34.570 and -60.935 <= lng <= -60.920:
            return f"Para la zona de **Villa Belgrano**, la recolección es los días **Martes, Jueves y Sábado por la mañana (a partir de las 08:00 hs)**."
        else:
            return "Según la ubicación, te corresponde el servicio de recolección zonal. Los días son **Lunes, Miércoles y Viernes por la noche (a partir de las 21:00 hs)**. Te recomiendo confirmarlo en la web del municipio."
    except requests.exceptions.RequestException as e:
        logger.error(
            "[HERRAMIENTA GEO] Provider request failed error_type=%s input_chars=%s",
            type(e).__name__,
            len(str(direccion or "")),
        )
        return "Tuve un problema de comunicación con el servicio de mapas. Por favor, intenta de nuevo en unos momentos."


# --- HERRAMIENTA 2: CATEGORIZACIÓN DE RECLAMOS ---

# En herramientas_municipio.py, reemplaza tu diccionario

KEYWORD_TO_CATEGORY_MAP = {
    # Luminaria
    "luminaria": "Luminaria", "alumbrado": "Luminaria", "luz": "Luminaria", "poste": "Luminaria", "farol": "Luminaria", "farola": "Luminaria", "iluminacion": "Luminaria", "foco": "Luminaria", "lampara": "Luminaria", "poste caido": "Luminaria", "poste caído": "Luminaria", "sin luz": "Luminaria", "poste sin luz": "Luminaria", "farol apagado": "Luminaria",
    # Arbol Caido
    "arbol": "Arbol Caido", "arbol caido": "Arbol Caido", "rama": "Arbol Caido", "ramas": "Arbol Caido", "gajo": "Arbol Caido", "tronco": "Arbol Caido", "poda": "Arbol Caido", "podar": "Arbol Caido", "medianera": "Arbol Caido", "arbol del vecino": "Arbol Caido", "raiz": "Arbol Caido",
    # Limpieza
    "limpieza": "Limpieza", "basura": "Limpieza", "mugre": "Limpieza", "escombros": "Limpieza", "pasto": "Limpieza", "yuyos": "Limpieza", "maleza": "Limpieza", "desmalezado": "Limpieza", "baldio": "Limpieza", "pasto alto": "Limpieza", "basural": "Limpieza",
    # Arreglo de calle
    "bache": "Arreglo de calle", "calle": "Arreglo de calle", "asfalto": "Arreglo de calle", "vereda": "Arreglo de calle", "pozo": "Arreglo de calle", "pavimento": "Arreglo de calle", "calzada": "Arreglo de calle", "hueco": "Arreglo de calle", "vereda rota": "Arreglo de calle", "vereda levantada": "Arreglo de calle", "calle en mal estado": "Arreglo de calle",
    # Falta de agua, rotura de caño
    "agua": "Falta de agua, rotura de caño", "caño": "Falta de agua, rotura de caño", "cano": "Falta de agua, rotura de caño", "perdida": "Falta de agua, rotura de caño", "fuga": "Falta de agua, rotura de caño", "rotura": "Falta de agua, rotura de caño", "tuberia": "Falta de agua, rotura de caño", "canilla": "Falta de agua, rotura de caño", "canilla rota": "Falta de agua, rotura de caño", "sin servicio de agua": "Falta de agua, rotura de caño",
    # Rotura de semaforo
    "semaforo": "Rotura de semaforo", "semáforo": "Rotura de semaforo", "luz roja": "Rotura de semaforo", "luz verde": "Rotura de semaforo", "semaforo fuera de servicio": "Rotura de semaforo",
    # Fumigacion
    "fumigacion": "Fumigacion", "fumigar": "Fumigacion", "bichos": "Fumigacion", "plaga": "Fumigacion", "mosquitos": "Fumigacion", "insectos": "Fumigacion", "ratas": "Fumigacion", "cucarachas": "Fumigacion",
    # Riego de Calle
    "riego": "Riego de Calle", "regar": "Riego de Calle", "camion de agua": "Riego de Calle", "camion cisterna": "Riego de Calle",
    # Castracion de mascota
    "castracion": "Castracion de mascota", "castración": "Castracion de mascota", "castrar": "Castracion de mascota", "mascota": "Castracion de mascota", "perro": "Castracion de mascota", "gato": "Castracion de mascota", "esterilizacion": "Castracion de mascota", "esterilización": "Castracion de mascota",
    # Inspeccion de comercio
    "inspeccion": "Inspeccion de comercio", "inspección": "Inspeccion de comercio", "comercio": "Inspeccion de comercio", "negocio": "Inspeccion de comercio", "habilitacion": "Inspeccion de comercio",
    # Tramites de Obras Privadas
    "obra": "Tramites de Obras Privadas", "construccion": "Tramites de Obras Privadas", "construcción": "Tramites de Obras Privadas", "plano": "Tramites de Obras Privadas", "obra nueva": "Tramites de Obras Privadas", "habilitacion de obra": "Tramites de Obras Privadas",
    # Incendio
    "incendio": "Incendio", "fuego": "Incendio", "humo": "Incendio", "quema": "Incendio", "llamas": "Incendio", "quema de basura": "Incendio", "fuego en pastizal": "Incendio",
}

# --- CARGA DINÁMICA DE PALABRAS CLAVE DESDE LA BASE DE DATOS ---
# Legacy hooks are retained for compatibility, but ticket text must never be
# promoted into a process-global dictionary. That leaked one tenant's language
# and classifications into every other government served by the worker.

_DYNAMIC_KEYWORD_CACHE: dict[str, str] = {}
_CACHE_LAST_LOAD: float = 0.0
_CACHE_TTL_SECONDS = 60 * 15  # 15 minutos


def _cargar_keywords_desde_db() -> None:
    """Clear the retired cross-tenant learner without reading citizen tickets."""
    from time import time
    global _DYNAMIC_KEYWORD_CACHE, _CACHE_LAST_LOAD
    _DYNAMIC_KEYWORD_CACHE = {}
    _CACHE_LAST_LOAD = time()
    logger.debug("Aprendizaje global desde tickets deshabilitado por aislamiento tenant")


def _ensure_keyword_cache_actualizado() -> None:
    from time import time
    if time() - _CACHE_LAST_LOAD > _CACHE_TTL_SECONDS:
        _cargar_keywords_desde_db()
    if _DYNAMIC_KEYWORD_CACHE:
        KEYWORD_TO_CATEGORY_MAP.update(_DYNAMIC_KEYWORD_CACHE)


def recargar_cache_keywords_para_tests() -> None:
    """Forza recarga del cache (util para pruebas)."""
    _cargar_keywords_desde_db()


def ensure_keyword_cache() -> None:
    """Exposed helper to refresh cache when needed."""
    _ensure_keyword_cache_actualizado()


def categorizar_reclamo_por_palabra_clave(texto_usuario: str) -> str:
    _ensure_keyword_cache_actualizado()
    texto_normalizado = normalizar_texto(texto_usuario)

    # 1. Intentá match EXACTO con la lista de categorías ya normalizadas
    categorias_normalizadas = {normalizar_texto(cat): cat for cat in set(KEYWORD_TO_CATEGORY_MAP.values())}
    if texto_normalizado in categorias_normalizadas:
        return categorias_normalizadas[texto_normalizado]

    # 2. Si no, buscá por keywords
    for keyword, category in KEYWORD_TO_CATEGORY_MAP.items():
        if keyword in texto_normalizado:
            return category

    return "Otros"


from services.google_search import google_search
from datetime import datetime, timedelta, timezone
from utils.time_utils import get_local_now

# --- HERRAMIENTA DINÁMICA: AGENDA DE EVENTOS DESDE ARCHIVO ---

def consultar_eventos_culturales(fecha: str) -> str:
    """Consulta eventos culturales almacenados en la base de datos."""

    logger.info(f"[HERRAMIENTA EVENTOS] Consultando agenda de eventos para fecha: '%s'", fecha)

    fecha_norm = normalizar_texto(fecha)
    now_local = get_local_now().astimezone(ARG_TZ)
    if fecha_norm == "hoy":
        target_date = now_local.date()
    elif fecha_norm == "manana":
        target_date = (now_local + timedelta(days=1)).date()
    else:
        return f"No entiendo la fecha '{fecha}'. Por favor, intentá con 'hoy' o 'mañana'."

    db_municipio_id = get_numeric_municipio_id(MUNICIPIO_ID)
    eventos_db = []
    if db_municipio_id is not None:
        try:
            eventos_query = (
                MunicipioPost.query.filter(
                    MunicipioPost.municipio_id == db_municipio_id,
                    MunicipioPost.tipo_post == "evento",
                )
                .order_by(
                    MunicipioPost.fecha_evento_inicio.asc(),
                    MunicipioPost.fecha_publicacion.desc(),
                )
                .limit(100)
            )
            eventos_db = eventos_query.all()
        except Exception:
            logger.exception("No se pudo consultar la agenda cultural desde la base de datos")
            return (
                "Lo siento, no pude acceder a la agenda cultural en este momento. "
                "Por favor, intenta más tarde."
            )

    eventos_encontrados: list[MunicipioPost] = []
    proximos_eventos: list[MunicipioPost] = []

    for evento in eventos_db:
        inicio = evento.fecha_evento_inicio
        if inicio is None:
            proximos_eventos.append(evento)
            continue
        inicio_local = inicio if inicio.tzinfo else inicio.replace(tzinfo=timezone.utc)
        inicio_local = inicio_local.astimezone(ARG_TZ)
        if inicio_local.date() == target_date:
            eventos_encontrados.append(evento)
        elif inicio_local.date() > target_date:
            proximos_eventos.append(evento)

    if not eventos_encontrados:
        if proximos_eventos:
            eventos_encontrados = proximos_eventos[:3]
        else:
            return (
                f"No encontré eventos culturales programados para '{fecha_norm}'. "
                "Puedes consultar la agenda completa en la web del municipio."
            )

    lista_eventos_str: list[str] = []
    for evento in eventos_encontrados:
        data = evento.to_dict()
        titulo = data.get("titulo") or "Sin título"
        subtitulo = data.get("subtitulo")
        descripcion = data.get("descripcion") or "Sin descripción"

        evento_str = f"*{titulo}*"
        if subtitulo:
            evento_str += f"\n_{subtitulo}_"
        evento_str += f"\n{descripcion}"

        if evento.ubicacion:
            evento_str += f"\n📍 {evento.ubicacion}"
        if data.get("enlace"):
            evento_str += f"\n🔗 {data['enlace']}"

        lista_eventos_str.append(evento_str)

    respuesta = (
        f"Para '{fecha_norm}', la agenda cultural es:\n\n" + "\n\n---\n\n".join(lista_eventos_str)
    )

    social_links = []
    if isinstance(CONFIG_MUNICIPIO, dict):
        social_links = CONFIG_MUNICIPIO.get("social_links", []) or []

    social_lines: list[str] = []
    for link in social_links:
        name = link.get("name")
        url = link.get("url")
        if not name or not url:
            continue
        social_lines.append(f"{name}: {url}")

    if social_lines:
        respuesta += "\n\n---\n"
        respuesta += "Seguinos en nuestras redes para más eventos y noticias:\n"
        respuesta += "\n".join(social_lines)

    return respuesta

def consultar_noticias_municipio() -> str:
    """
    Consulta las últimas noticias y eventos del municipio desde la base de datos y las formatea para el usuario.
    """
    logger.info("[HERRAMIENTA NOTICIAS] Consultando noticias y eventos desde la base de datos.")

    municipio_id = get_numeric_municipio_id(MUNICIPIO_ID)
    if municipio_id is None:
        return "No se encontraron noticias o eventos recientes en la base de datos."
    municipio_nombre = CONFIG_MUNICIPIO.get("nombre", "el municipio") if isinstance(CONFIG_MUNICIPIO, dict) else "el municipio"

    try:
        posts = (
            MunicipioPost.query.filter(MunicipioPost.municipio_id == municipio_id)
            .filter(MunicipioPost.tipo_post != "evento")
            .order_by(MunicipioPost.fecha_publicacion.desc())
            .limit(5)
            .all()
        )

        if not posts:
            return "No se encontraron noticias o eventos recientes en la base de datos."

        mensaje = f"Aquí están las últimas novedades de {municipio_nombre}:\n\n"
        for item in posts:
            etiqueta = "Evento" if item.tipo_post == "informacion" else item.tipo_post.capitalize()
            mensaje += f"📰 *{item.titulo}* ({etiqueta})\n"
            if item.subtitulo:
                mensaje += f"   _{item.subtitulo}_\n"
            if item.descripcion:
                mensaje += f"   {item.descripcion}\n"
            if item.enlace:
                mensaje += f"   🔗 {item.enlace}\n"
            mensaje += "\n"

        return mensaje.strip()

    except Exception:
        logger.exception("[HERRAMIENTA NOTICIAS] Error al consultar la base de datos")
        return (
            "No pude obtener las últimas noticias en este momento debido a un error interno. "
            "Puedes consultarlas directamente en el sitio web oficial."
        )


def buscar_puntos_de_interes(
    rubro: str = None,
    tipo_lugar: str = None,
    localidad: str = None,
    opennow: bool = False,
    context: dict = None,
    ubicacion: str = None,
    tipo_negocio: str = None,
) -> str:
    """
    Busca puntos de interés cercanos a la ubicación del usuario.
    """
    # Aceptar sinónimos de parámetros usados por el LLM
    if not rubro:
        if tipo_lugar:
            rubro = tipo_lugar
        elif tipo_negocio:
            rubro = tipo_negocio

    if not localidad and ubicacion:
        localidad = ubicacion

    if context and context.get('last_search'):
        if not rubro:
            rubro = context['last_search'].get('rubro')
        if not localidad:
            localidad = context['last_search'].get('localidad')

    if not localidad:
        return "No tengo la localidad para buscar. Por favor, decime dónde querés buscar."

    if not rubro:
        return "Por favor, decime qué tipo de lugar o comercio estás buscando (por ejemplo, 'farmacia', 'ferretería', etc.)."

    rubro_original = rubro
    rubro_normalizado = normalizar_texto(rubro)
    if "farmacia" in rubro_normalizado:
        rubro = "farmacia"
    if any(token in rubro_normalizado for token in ("de turno", "24", "24hs", "24 horas", "guardia")):
        opennow = True

    logger.info(
        "[HERRAMIENTA POI] Searching points of interest keyword_chars=%s "
        "location_chars=%s opennow=%s",
        len(str(rubro or "")),
        len(str(localidad or "")),
        bool(opennow),
    )

    if not Maps_API_KEY:
        logger.error("[HERRAMIENTA POI] Clave de API de Google Maps (Maps_API_KEY) no configurada en el entorno.")
        return "Error de configuración: El servicio de mapas no está disponible en este momento."

    geocode_url = f"https://maps.googleapis.com/maps/api/geocode/json?address={requests.utils.quote(localidad)}&key={Maps_API_KEY}&language=es"

    try:
        response = requests.get(geocode_url)
        response.raise_for_status()
        data = response.json()

        if not data or data.get('status') != 'OK' or not data.get('results'):
            logger.warning(
                "[HERRAMIENTA POI] Provider could not geocode locality "
                "location_chars=%s has_results=%s",
                len(str(localidad or "")),
                bool(data and data.get("results")),
            )
            return f"No pude encontrar la localidad '{localidad}'. ¿Puedes ser más específico?"

        location = data['results'][0]['geometry']['location']
        lat, lng = location['lat'], location['lng']

        keyword_param = str(rubro) if rubro else ""
        places_url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={lat},{lng}&radius=5000&keyword={requests.utils.quote(keyword_param)}&key={Maps_API_KEY}&language=es"
        if opennow:
            places_url += "&opennow=true"

        response = requests.get(places_url)
        response.raise_for_status()
        data = response.json()

        if data and data.get('status') == 'OK' and data.get('results'):
            if context:
                context['last_search'] = {
                    'rubro': rubro,
                    'localidad': localidad,
                    'results': data['results']
                }

            page = context.get('last_search_page', 0) if context else 0
            start = page * 3
            end = start + 3
            results_to_show = data['results'][start:end]

            if not results_to_show:
                return "No hay más resultados para mostrar."

            mensaje = f"Encontré estos lugares para '{rubro}' cerca de tu ubicación:\n"
            for i, item in enumerate(results_to_show, 1):
                nombre = item.get('name')
                direccion = item.get('vicinity')
                place_id = item.get('place_id')
                maps_link = f"https://www.google.com/maps/place/?q=place_id:{place_id}"

                details_url = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&fields=name,formatted_phone_number&key={Maps_API_KEY}&language=es"
                details_response = requests.get(details_url)
                details_data = details_response.json()
                telefono = details_data.get('result', {}).get('formatted_phone_number', 'No disponible')

                whatsapp_link = ""
                if telefono != 'No disponible':
                    telefono_numerico = ''.join(filter(str.isdigit, telefono))
                    if telefono_numerico:
                        whatsapp_link = f"https://wa.me/{telefono_numerico}"

                mensaje += f"{i}. {nombre}\n   Dirección: {direccion}\n   Tel: {telefono}\n"
                if whatsapp_link:
                    mensaje += f"   WhatsApp: {whatsapp_link}\n"
                mensaje += f"   Ver en mapa: {maps_link}\n"

            if len(data['results']) > end:
                mensaje += "\nSi querés ver más resultados, respondé 'más'."
                if context:
                    context['last_search_page'] = page + 1
            elif context:
                context.pop('last_search_page', None)


            return mensaje
        else:
            if opennow:
                return f"No encontré resultados para '{rubro}' abiertos en este momento en '{localidad}'. ¿Querés que te muestre todos igualmente?"
            else:
                return f"No encontré resultados para '{rubro}' en '{localidad}'."

    except requests.exceptions.RequestException as e:
        logger.error(
            "Provider request failed for POI error_type=%s keyword_chars=%s "
            "location_chars=%s",
            type(e).__name__,
            len(str(rubro or "")),
            len(str(localidad or "")),
        )
        return "Tuve un problema de comunicación con el servicio de mapas. Por favor, intenta de nuevo en unos momentos."
    except Exception as e:
        logger.error(
            "Unexpected POI search failure error_type=%s keyword_chars=%s "
            "location_chars=%s",
            type(e).__name__,
            len(str(rubro or "")),
            len(str(localidad or "")),
        )
        return "Ocurrió un error inesperado al buscar los puntos de interés."

def log_uso_herramienta(nombre, usuario, parametros, resultado):
    raw_parameter_fields = list(parametros.keys()) if isinstance(parametros, dict) else []
    parameter_fields = sorted(
        str(key) for key in raw_parameter_fields if str(key) in _SAFE_TOOL_PARAMETER_FIELDS
    )
    logger.info(
        "[USO_HERRAMIENTA] tool_name_chars=%s has_user=%s parameter_fields=%s "
        "other_parameter_count=%s result_chars=%s",
        len(str(nombre or "")),
        usuario is not None,
        parameter_fields,
        max(0, len(raw_parameter_fields) - len(parameter_fields)),
        len(str(resultado or "")),
    )

_CAMARAS_CACHE: list[dict] | None = None


def _load_camaras_referencia() -> list[dict]:
    global _CAMARAS_CACHE
    if _CAMARAS_CACHE is None:
        cam_path = Path(__file__).resolve().parents[1] / "data" / "estacionamiento" / "camaras.json"
        try:
            with open(cam_path, "r", encoding="utf-8") as fh:
                _CAMARAS_CACHE = json.load(fh)
        except Exception:
            _CAMARAS_CACHE = []
    return _CAMARAS_CACHE


def validar_y_formatear_direccion(direccion: str, municipio_config: dict | None = None) -> dict | None:
    """Valida y formatea una dirección sin depender de Google Maps."""

    direccion = (direccion or "").strip()
    if not direccion:
        return None

    municipio_config = municipio_config or {}
    localidad = (
        municipio_config.get("ciudad")
        or municipio_config.get("ciudad_default")
        or municipio_config.get("localidad")
    )
    provincia = (
        municipio_config.get("provincia")
        or municipio_config.get("provincia_default")
    )

    camaras = _load_camaras_referencia()

    heuristica = aproximar_coordenadas_por_texto(direccion, camaras)
    if heuristica:
        lat = heuristica.get("lat")
        lon = heuristica.get("lon")
        if lat is not None and lon is not None:
            formatted = direccion
            if localidad and localidad.lower() not in formatted.lower():
                formatted = f"{formatted}, {localidad}"
            if provincia and provincia.lower() not in formatted.lower():
                formatted = f"{formatted}, {provincia}"
            return {
                "formatted_address": formatted,
                "lat": float(lat),
                "lng": float(lon),
                "confidence": heuristica.get("confidence"),
                "source": "parking_heuristic",
            }

    llm_result = geocodificar_texto_llm(
        direccion,
        puntos_referencia=camaras,
        localidad_predeterminada=localidad,
    )
    if llm_result:
        try:
            lat = float(llm_result.get("lat"))
            lon = float(llm_result.get("lon"))
        except (TypeError, ValueError):
            lat = lon = None
        if lat is not None and lon is not None:
            formatted = (
                llm_result.get("normalized_query")
                or llm_result.get("matched_reference")
                or direccion
            )
            if localidad and localidad.lower() not in formatted.lower():
                formatted = f"{formatted}, {localidad}"
            if provincia and provincia.lower() not in formatted.lower():
                formatted = f"{formatted}, {provincia}"
            return {
                "formatted_address": formatted,
                "lat": lat,
                "lng": lon,
                "confidence": llm_result.get("confidence"),
                "source": "openai_llm",
            }

    logger.warning(
        "[GEO] Address could not be resolved without Google input_chars=%s",
        len(str(direccion or "")),
    )
    return None

def obtener_direccion_de_coordenadas(lat: float, lon: float) -> dict | None:
    """Obtiene una dirección formateada y sus componentes a partir de coordenadas.

    Orden de resolución:
    1. OpenAI (geocodificación inversa vía LLM).
    2. Fallback determinístico usando geopy (Google, MapTiler o Nominatim).
    3. Google Geocoding API como último recurso.
    """

    # --- Intento con OpenAI ---
    try:
        openai_result = geocodificar_inversa_llm(lat, lon)
        formatted = openai_result.get("formatted_address") if openai_result else None
        if formatted:
            parsed = parse_direccion_completa(formatted)
            if parsed:
                parsed["formatted_address"] = formatted
                return parsed
    except Exception as e:
        logger.error(
            "OpenAI inverse geocoding failed error_type=%s has_coordinates=%s",
            type(e).__name__,
            lat is not None and lon is not None,
        )

    # --- Fallback determinístico usando geolocalizadores tradicionales ---
    fallback_structured = _reverse_geocode_with_geopy(lat, lon)
    if fallback_structured:
        return fallback_structured

    # --- Último recurso: Google Geocoding ---
    if not llm_provider_network_allowed("geocoding"):
        logger.info("Reverse geocoding provider skipped reason=test_network_disabled")
        return None
    if not Maps_API_KEY:
        logger.error("[HERRAMIENTA GEO] Clave de API de Google Maps (Maps_API_KEY) no configurada en el entorno.")
        return None

    reverse_geocode_url = (
        f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lon}&key={Maps_API_KEY}&language=es"
    )

    try:
        response = requests.get(reverse_geocode_url)
        response.raise_for_status()
        data = response.json()

        if data and data.get("status") == "OK" and data.get("results"):
            best_result = data["results"][0]
            formatted_address = best_result.get("formatted_address")

            calle = numero = localidad = provincia = cp = barrio = ""
            for component in best_result.get("address_components", []):
                types = component.get("types", [])
                if "street_number" in types:
                    numero = component["long_name"]
                if "route" in types:
                    calle = component["long_name"]
                if "locality" in types or "postal_town" in types:
                    localidad = component["long_name"]
                if "administrative_area_level_1" in types:
                    provincia = component["long_name"]
                if "postal_code" in types:
                    cp = component["long_name"]
                if "neighborhood" in types:
                    barrio = component["long_name"]

            if formatted_address:
                return {
                    "formatted_address": formatted_address,
                    "calle": calle or None,
                    "numero": numero or None,
                    "localidad": localidad or None,
                    "provincia": provincia or None,
                    "codigo_postal": cp or None,
                    "barrio": barrio or None,
                }
            elif calle and localidad:
                parts = [calle, numero, localidad]
                if provincia and localidad != provincia:
                    parts.append(provincia)
                return {
                    "formatted_address": ", ".join(filter(None, parts)),
                    "calle": calle,
                    "numero": numero,
                    "localidad": localidad,
                    "provincia": provincia,
                    "codigo_postal": cp,
                    "barrio": barrio,
                }
            else:
                logger.warning(
                    "Google API returned insufficient address data has_coordinates=%s "
                    "result_fields=%s",
                    lat is not None and lon is not None,
                    _safe_geo_present_fields(
                        {
                            "calle": calle,
                            "numero": numero,
                            "localidad": localidad,
                            "provincia": provincia,
                            "codigo_postal": cp,
                            "barrio": barrio,
                        }
                    ),
                )
                return None
        else:
            logger.warning(
                "Google API could not reverse geocode has_coordinates=%s "
                "has_results=%s has_provider_error=%s",
                lat is not None and lon is not None,
                bool(isinstance(data, dict) and data.get("results")),
                bool(isinstance(data, dict) and data.get("error_message")),
            )
            return None

    except requests.exceptions.RequestException as e:
        logger.error(
            "Provider request failed for reverse geocoding error_type=%s "
            "has_coordinates=%s",
            type(e).__name__,
            lat is not None and lon is not None,
        )
        return None
    except Exception as e:
        logger.error(
            "Unexpected reverse geocoding failure error_type=%s has_coordinates=%s",
            type(e).__name__,
            lat is not None and lon is not None,
        )
        return None


def _reverse_geocode_with_geopy(lat: float, lon: float, municipio_config: dict | None = None) -> dict | None:
    """Utiliza geopy (Google, MapTiler o Nominatim) para obtener una dirección estructurada."""

    if not llm_provider_network_allowed("geocoding"):
        logger.info("Geopy fallback skipped reason=test_network_disabled")
        return None

    municipio_config = municipio_config or CONFIG_MUNICIPIO or {}
    try:
        geolocators = google_maps_service._get_geolocators()
    except AttributeError:  # pragma: no cover - defensive guard
        geolocators = []

    for geolocator in geolocators:
        try:
            location = geolocator.reverse((lat, lon), exactly_one=True, language="es")
            if not location:
                continue

            formatted = getattr(location, "address", None)
            raw_address = getattr(location, "raw", {}).get("address", {}) if hasattr(location, "raw") else {}

            calle = (
                raw_address.get("road")
                or raw_address.get("pedestrian")
                or raw_address.get("path")
                or raw_address.get("residential")
                or raw_address.get("cycleway")
            )
            numero = raw_address.get("house_number")
            barrio = (
                raw_address.get("neighbourhood")
                or raw_address.get("suburb")
                or raw_address.get("quarter")
            )
            localidad = (
                raw_address.get("city")
                or raw_address.get("town")
                or raw_address.get("village")
                or raw_address.get("municipality")
                or raw_address.get("city_district")
                or raw_address.get("county")
            )
            provincia = (
                raw_address.get("state")
                or raw_address.get("region")
                or raw_address.get("province")
                or raw_address.get("state_district")
            )
            codigo_postal = raw_address.get("postcode")

            resultado = {
                "formatted_address": formatted or f"{lat}, {lon}",
                "calle": calle or None,
                "numero": numero or None,
                "barrio": barrio or None,
                "localidad": localidad
                or municipio_config.get("ciudad")
                or municipio_config.get("ciudad_default"),
                "provincia": provincia
                or municipio_config.get("provincia")
                or municipio_config.get("provincia_default"),
                "codigo_postal": codigo_postal or None,
                "otros_detalles": None,
            }

            logger.info(
                "[GeoFallback] Address resolved via geopy has_coordinates=%s "
                "result_fields=%s",
                lat is not None and lon is not None,
                _safe_geo_present_fields(resultado),
            )
            return resultado

        except (GeocoderTimedOut, GeocoderServiceError) as e:
            logger.warning(
                "[GeoFallback] Geolocator failure provider=%s error_type=%s",
                getattr(geolocator, "__class__", type(geolocator)).__name__,
                type(e).__name__,
            )
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[GeoFallback] Unexpected geolocator failure provider=%s error_type=%s",
                getattr(geolocator, "__class__", type(geolocator)).__name__,
                type(e).__name__,
            )

    logger.warning(
        "[GeoFallback] Address could not be resolved with geopy "
        "has_coordinates=%s provider_count=%s",
        lat is not None and lon is not None,
        len(geolocators),
    )
    return None
# --- ACTUALIZA TU TOOL_REGISTRY ASÍ ---

TOOL_REGISTRY = {
    "consultar_recoleccion_por_direccion": {
        "funcion": consultar_recoleccion_por_direccion,
        "descripcion": "Se usa para obtener los horarios y días de recolección de basura para una dirección específica.",
        "parametros": {
            "direccion": {
                "type": "string",
                "description": f"La dirección completa del lugar. Ejemplo: '{CONFIG_MUNICIPIO.get('ejemplo_direccion', 'Av. Siempreviva 123')}'."
            }
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "consultar_eventos_culturales": {
        "funcion": consultar_eventos_culturales,
        "descripcion": "Consulta la agenda de eventos culturales, recitales o actividades municipales para una fecha específica, como 'hoy', 'mañana' o 'el sábado'.",
        "parametros": {
            "fecha": {"type": "string", "description": "La fecha de la consulta. Puede ser una palabra como 'hoy', 'mañana', 'este fin de semana', o una fecha específica como '15 de junio'."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "consultar_noticias": {
        "funcion": consultar_noticias_municipio,
        "descripcion": "Consulta las 3 noticias más recientes del sitio web del municipio. No necesita parámetros.",
        "parametros": {},
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_puntos_de_interes": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Busca puntos de interés cercanos a la ubicación del usuario. Los puntos de interés pueden ser: veterinarias, farmacias, hospitales, etc.",
        "parametros": {
            "rubro": {"type": "string", "description": "El tipo de punto de interés a buscar. Por ejemplo: 'veterinaria', 'farmacia', 'hospital', etc."},
            "localidad": {"type": "string", "description": "La localidad donde se encuentra el usuario."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_poi": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Busca puntos de interés cercanos a la ubicación del usuario. Los puntos de interés pueden ser: veterinarias, farmacias, hospitales, etc.",
        "parametros": {
            "rubro": {"type": "string", "description": "El tipo de punto de interés a buscar. Por ejemplo: 'veterinaria', 'farmacia', 'hospital', etc."},
            "localidad": {"type": "string", "description": "La localidad donde se encuentra el usuario."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_comercios_por_rubro_y_localidad": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Busca comercios o servicios por rubro y localidad. Es una alias de 'buscar_puntos_de_interes'.",
        "parametros": {
            "rubro": {"type": "string", "description": "El rubro del comercio a buscar. Por ejemplo: 'veterinaria', 'ferretería', 'restaurante'."},
            "localidad": {"type": "string", "description": "La localidad donde buscar el comercio."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_comercios": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Busca comercios o servicios por rubro y localidad. Es una alias de 'buscar_puntos_de_interes'.",
        "parametros": {
            "rubro": {"type": "string", "description": "El rubro del comercio a buscar. Por ejemplo: 'veterinaria', 'ferretería', 'restaurante'."},
            "localidad": {"type": "string", "description": "La localidad donde buscar el comercio."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_lugares_cercanos": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Alias de 'buscar_puntos_de_interes'. Busca lugares de interés o comercios cercanos a una ubicación dada.",
        "parametros": {
            "rubro": {"type": "string", "description": "El tipo de lugar o comercio a buscar."},
            "localidad": {"type": "string", "description": "La ubicación o localidad para centrar la búsqueda."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "buscar_negocios_cercanos": {
        "funcion": buscar_puntos_de_interes,
        "descripcion": "Alias de 'buscar_puntos_de_interes'. Busca negocios cercanos por rubro y ubicación.",
        "parametros": {
            "rubro": {"type": "string", "description": "El rubro o tipo de negocio a buscar."},
            "localidad": {"type": "string", "description": "La ubicación o localidad de referencia."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "generar_respuesta_audio": {
        "funcion": generar_audio,
        "descripcion": "Convierte un texto a voz y devuelve la URL de un archivo de audio.",
        "parametros": {
            "text": {
                "type": "string",
                "description": "El texto que se convertirá a voz."
            }
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    },
    "google_search": {
        "funcion": google_search,
        "descripcion": "Busca en Google cuando ninguna otra herramienta es apropiada. Utilízala para consultas generales, buscar información específica o encontrar puntos de interés no cubiertos por otras herramientas.",
        "parametros": {
            "query": {"type": "string", "description": "La consulta de búsqueda precisa para Google."}
        },
        "roles_permitidos": ["usuario", "empleado", "admin_municipio"]
    }
}
