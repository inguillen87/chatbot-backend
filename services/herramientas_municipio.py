import json
import logging
import os
import re
import unicodedata
import requests
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
import services.google_maps_service as google_maps_service
from services.config_loader import cargar_configuracion_municipio
from services.location_service import geocode_address
from services.tts_orchestrator import generar_audio
from models import MunicipioTicket, MunicipioPost
from database import db
from services.openai_bridge import client as openai_client
from services.openai_maps_service import geocodificar_inversa_llm, geocodificar_texto_llm
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
    logger.info(f"Sugiriendo categorías (NO-LLM) para: '{texto_usuario[:50]}...'")
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

    logger.info(f"Categorías sugeridas (NO-LLM) para '{texto_usuario[:50]}...': {sugeridas}")
    return sugeridas # Devuelve hasta 3, o menos si no hay suficientes matches.
   
logger = logging.getLogger(__name__)
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
        "localidad": localidad,
        "provincia": provincia,
        "codigo_postal": codigo_postal,
        "otros_detalles": ", ".join(otros_detalles) if otros_detalles else None,
    }

    if not resultado["calle"] or not resultado["localidad"]:
        return None

    logger.info(
        "[ParseDireccion][Fallback] Dirección parseada sin LLM para '%s': %s",
        texto_direccion,
        resultado,
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

    # Fallback heurístico: acepta direcciones que contengan texto y un número
    return bool(re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ ]+\s+\d+", texto))


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
    - "localidad"
    - "provincia"
    - "codigo_postal" (opcional)
    - "otros_detalles" (cualquier información adicional relevante que no encaje en los otros campos)

    Responde únicamente con el objeto JSON. Si no puedes extraer una calle o una localidad, devuelve un JSON vacío.
    """
    try:
        if not openai_client:
            raise ConnectionError("OpenAI client is not initialized.")

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Sos un experto en normalización de direcciones argentinas. Tu única función es devolver un objeto JSON con los datos de la dirección.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        respuesta_llm = response.choices[0].message.content
        parsed_data = json.loads(respuesta_llm)
        if not isinstance(parsed_data, dict) or not parsed_data.get("calle") or not parsed_data.get("localidad"):
            logger.warning(
                f"LLM no pudo extraer datos clave de la dirección: '{texto_direccion}'. Respuesta: {respuesta_llm}"
            )
        else:
            logger.info(f"Dirección parseada con LLM para '{texto_direccion}': {parsed_data}")
            return parsed_data
    except (json.JSONDecodeError, Exception) as e:
        logger.error(
            f"Error al parsear dirección con LLM (OpenAI): {e}. Respuesta cruda: '{locals().get('respuesta_llm', 'N/A')}'"
        )

    # Fallback determinístico si el LLM no entrega datos útiles
    parsed_fallback = _parse_direccion_basica(texto_direccion, municipio_config)
    if parsed_fallback:
        return parsed_fallback

    logger.warning(
        f"[ParseDireccion] No se pudo extraer dirección de forma automática para '{texto_direccion}'."
    )
    return None

# --- HERRAMIENTA 1: CONSULTA DE RECOLECCIÓN ---
def consultar_recoleccion_por_direccion(direccion: str, context: dict | None = None) -> str:
    """
    Herramienta profesional que usa la API de Google Maps para geocodificar una dirección
    y luego determina el horario de recolección.
    """
    logger.info(f"[HERRAMIENTA GEO] Buscando horario para: '{direccion}'")

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
    logger.info(f"Geocoding recoleccion with components: {components_str}")

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
            logger.warning(f"[HERRAMIENTA GEO] La API de Google no pudo geocodificar la dirección: {direccion}")
            return "No pude verificar esa dirección. ¿Puedes ser un poco más específico, incluyendo la ciudad?"

        location = data['results'][0]['geometry']['location']
        lat, lng = location['lat'], location['lng']
        logger.info(f"[HERRAMIENTA GEO] Coordenadas para '{direccion}': Lat={lat}, Lng={lng}")

        if -34.595 <= lat <= -34.580 and -60.955 <= lng <= -60.935:
            return f"Detecté que la dirección '{direccion}' está en la **zona céntrica**. Allí, la recolección es de **Lunes a Sábado por la noche (a partir de las 22:00 hs)**."
        elif -34.580 <= lat <= -34.570 and -60.935 <= lng <= -60.920:
            return f"Para la zona de **Villa Belgrano**, la recolección es los días **Martes, Jueves y Sábado por la mañana (a partir de las 08:00 hs)**."
        else:
            return "Según la ubicación, te corresponde el servicio de recolección zonal. Los días son **Lunes, Miércoles y Viernes por la noche (a partir de las 21:00 hs)**. Te recomiendo confirmarlo en la web del municipio."
    except requests.exceptions.RequestException as e:
        logger.error(f"[HERRAMIENTA GEO] Error de conexión con la API de Google: {e}")
        return "Tuve un problema de comunicación con el servicio de mapas. Por favor, intenta de nuevo en unos momentos."


# --- HERRAMIENTA 2: CATEGORIZACIÓN DE RECLAMOS ---

# En herramientas_municipio.py, reemplaza tu diccionario

KEYWORD_TO_CATEGORY_MAP = {
    # Luminaria
    "luminaria": "Luminaria", "luz": "Luminaria", "poste": "Luminaria", "farol": "Luminaria", "farola": "Luminaria", "iluminacion": "Luminaria", "foco": "Luminaria", "lampara": "Luminaria", "poste caido": "Luminaria", "poste caído": "Luminaria", "sin luz": "Luminaria", "poste sin luz": "Luminaria", "farol apagado": "Luminaria",
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
# Se aprovechan los reclamos ya cargados para ampliar el diccionario de
# keywords con términos reales usados por los vecinos. Esto permite que el
# sistema sea más proactivo y evite llamadas innecesarias al LLM.

_DYNAMIC_KEYWORD_CACHE: dict[str, str] = {}
_CACHE_LAST_LOAD: float = 0.0
_CACHE_TTL_SECONDS = 60 * 15  # 15 minutos


def _cargar_keywords_desde_db() -> None:
    """Refresca el cache de palabras clave consultando los tickets previos."""
    from time import time
    global _DYNAMIC_KEYWORD_CACHE, _CACHE_LAST_LOAD
    try:
        rows = (
            MunicipioTicket.query.with_entities(
                MunicipioTicket.categoria, MunicipioTicket.detalles
            )
            .order_by(MunicipioTicket.id.desc())
            .limit(200)
            .all()
        )
        dynamic: dict[str, str] = {}
        for categoria, descripcion in rows:
            if not categoria or not descripcion:
                continue
            canon = categoria.strip().title()
            if normalizar_texto(canon) == "luminaria":
                canon = "Luminarias"
            for token in normalizar_texto(descripcion).split():
                if token and token not in KEYWORD_TO_CATEGORY_MAP:
                    dynamic[token] = canon
        _DYNAMIC_KEYWORD_CACHE = dynamic
        _CACHE_LAST_LOAD = time()
        logger.info("Cache de keywords cargado con %d términos", len(dynamic))
    except Exception as exc:  # pragma: no cover - fallbacks no interrumpen ejecución
        logger.warning("No se pudo cargar cache de keywords: %s", exc)


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
from services.scraper_avanzado import extraer_noticias
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

    try:
        eventos_query = (
            MunicipioPost.query.filter(
                MunicipioPost.municipio_id == MUNICIPIO_ID,
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

    respuesta += "\n\n---\n"
    respuesta += "Seguinos en nuestras redes para más eventos y noticias:\n"
    respuesta += "Facebook: https://www.facebook.com/JuninMunicipio\n"
    respuesta += "Instagram: https://www.instagram.com/munijuninmdz"

    return respuesta

def consultar_noticias_municipio() -> str:
    """
    Consulta las últimas noticias y eventos del municipio desde la base de datos y las formatea para el usuario.
    """
    logger.info("[HERRAMIENTA NOTICIAS] Consultando noticias y eventos desde la base de datos.")

    municipio_id = MUNICIPIO_ID
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

    logger.info(f"[HERRAMIENTA POI] Buscando puntos de interés para: rubro='{rubro}', localidad='{localidad}', opennow={opennow}")

    if not Maps_API_KEY:
        logger.error("[HERRAMIENTA POI] Clave de API de Google Maps (Maps_API_KEY) no configurada en el entorno.")
        return "Error de configuración: El servicio de mapas no está disponible en este momento."

    geocode_url = f"https://maps.googleapis.com/maps/api/geocode/json?address={requests.utils.quote(localidad)}&key={Maps_API_KEY}&language=es"

    try:
        response = requests.get(geocode_url)
        response.raise_for_status()
        data = response.json()

        if not data or data.get('status') != 'OK' or not data.get('results'):
            logger.warning(f"[HERRAMIENTA POI] La API de Google no pudo geocodificar la localidad: {localidad}")
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
        logger.error(f"Error de conexión con Google API para POI ({rubro}, {localidad}): {e}")
        return "Tuve un problema de comunicación con el servicio de mapas. Por favor, intenta de nuevo en unos momentos."
    except Exception as e:
        logger.error(f"Error inesperado en búsqueda de POI para {rubro}, {localidad}: {e}", exc_info=True)
        return "Ocurrió un error inesperado al buscar los puntos de interés."

def log_uso_herramienta(nombre, usuario, parametros, resultado):
    logger.info(f"[USO_HERRAMIENTA] {nombre} | Usuario: {usuario} | Parámetros: {parametros} | Resultado: {resultado[:100]}")

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

    logger.warning("[GEO] No se pudo resolver la dirección '%s' sin Google.", direccion)
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
        logger.error(f"OpenAI inverse geocoding failed: {e}", exc_info=True)

    # --- Fallback determinístico usando geolocalizadores tradicionales ---
    fallback_structured = _reverse_geocode_with_geopy(lat, lon)
    if fallback_structured:
        return fallback_structured

    # --- Último recurso: Google Geocoding ---
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
                    f"Google API no devolvió dirección formateada ni componentes suficientes para {lat},{lon}."
                )
                return None
        else:
            logger.warning(
                f"Google API no pudo obtener dirección para {lat},{lon}. Status: {data.get('status')}, Error: {data.get('error_message', 'N/A')}"
            )
            return None

    except requests.exceptions.RequestException as e:
        logger.error(
            f"Error de conexión con Google API para reverse geocoding ({lat},{lon}): {e}"
        )
        return None
    except Exception as e:
        logger.error(
            f"Error inesperado en reverse geocoding para {lat},{lon}: {e}",
            exc_info=True,
        )
        return None


def _reverse_geocode_with_geopy(lat: float, lon: float, municipio_config: dict | None = None) -> dict | None:
    """Utiliza geopy (Google, MapTiler o Nominatim) para obtener una dirección estructurada."""

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
                "[GeoFallback] Dirección obtenida vía geopy para (%s,%s): %s",
                lat,
                lon,
                resultado,
            )
            return resultado

        except (GeocoderTimedOut, GeocoderServiceError) as e:
            logger.warning(
                "[GeoFallback] Error en geolocalizador %s: %s",
                getattr(geolocator, "__class__", type(geolocator)).__name__,
                e,
            )
        except Exception as e:  # pragma: no cover - defensive logging
            logger.error(
                "[GeoFallback] Error inesperado en geolocalizador %s: %s",
                getattr(geolocator, "__class__", type(geolocator)).__name__,
                e,
                exc_info=True,
            )

    logger.warning(
        "[GeoFallback] No se pudo determinar la dirección para las coordenadas (%s,%s) con geopy.",
        lat,
        lon,
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