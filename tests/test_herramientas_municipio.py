import pytest
from unittest.mock import patch
from services.herramientas_municipio import validar_y_formatear_direccion, consultar_noticias_municipio
from models import MunicipioTicket, db
from sqlalchemy import text
from app import create_app
from config import TestConfig
from datetime import datetime

@pytest.fixture
def app_context():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        db.session.execute(text("PRAGMA foreign_keys=OFF"))
        yield
        db.session.remove()
        db.drop_all()

@patch('services.herramientas_municipio.AddressResolver.resolve')
def test_validar_y_formatear_direccion_exitosa(mock_resolve, monkeypatch):
    monkeypatch.setenv('GOOGLE_MAPS_API_KEY', 'TEST')
    mock_resolve.return_value = {
        "formatted": "Av. Siempreviva 742, Junín, Mendoza, AR",
        "lat": -33.0,
        "lon": -68.5,
        "calle": "Av. Siempreviva",
        "numero": "742",
        "entre_calles": [],
        "barrio": None,
        "localidad": "Junín",
        "provincia": "Mendoza",
        "pais": "AR",
        "precision": "point",
        "validez": True,
    }
    resultado = validar_y_formatear_direccion("Av. Siempreviva 742")
    assert resultado == {
        "formatted_address": "Av. Siempreviva 742, Junín, Mendoza, AR",
        "lat": -33.0,
        "lng": -68.5,
        "calle": "Av. Siempreviva",
        "numero": "742",
        "entre_calles": [],
        "barrio": None,
        "localidad": "Junín",
        "provincia": "Mendoza",
        "pais": "AR",
        "precision": "point",
        "validez": True,
        "maps_link": "https://www.google.com/maps?q=-33.0,-68.5",
        "static_map_url": "https://maps.googleapis.com/maps/api/staticmap?center=-33.0,-68.5&zoom=18&size=800x500&markers=color:red|-33.0,-68.5&key=TEST",
    }

@patch('services.herramientas_municipio.AddressResolver.resolve', return_value=None)
def test_validar_y_formatear_direccion_invalida(mock_resolve):
    resultado = validar_y_formatear_direccion("una dirección inválida")
    assert resultado is None


@patch('services.herramientas_municipio.reverse_geocode')
def test_validar_y_formatear_direccion_maps_link(mock_reverse, monkeypatch):
    monkeypatch.setenv('GOOGLE_MAPS_API_KEY', 'TEST')
    mock_reverse.return_value = {
        'calle': 'Sarmiento',
        'numero': '100',
        'barrio': None,
        'localidad': 'Junín',
        'provincia': 'Mendoza',
        'display': 'Sarmiento 100, Junín, Mendoza, AR'
    }
    config = {"ciudad": "Junín", "provincia": "Mendoza", "pais": "AR", "bounds": (-68.6, -33.1, -68.4, -32.9)}
    url = "https://www.google.com/maps?q=-33.0,-68.5"
    resultado = validar_y_formatear_direccion(url, config)
    assert resultado["lat"] == -33.0 and resultado["lng"] == -68.5
    assert "google.com/maps" in resultado["maps_link"]


@patch('services.herramientas_municipio.AddressResolver.resolve')
@patch('services.herramientas_municipio.cargar_configuracion_municipio')
def test_validar_y_formatear_direccion_con_id(mock_cfg, mock_resolve):
    mock_cfg.return_value = {"ciudad": "Junín", "provincia": "Mendoza", "pais": "AR"}
    mock_resolve.return_value = {
        "formatted": "Calle Falsa 123, Junín, Mendoza, AR",
        "lat": 1,
        "lon": 2,
        "validez": True,
    }
    resultado = validar_y_formatear_direccion("Calle Falsa 123", "junin")
    assert mock_cfg.called
    assert resultado["formatted_address"] == "Calle Falsa 123, Junín, Mendoza, AR"

def test_consultar_noticias_municipio_exitosa(app_context):
    """
    Consulta la agenda de eventos culturales desde un archivo JSON local,
    filtrando por tipo de post 'evento' y por fecha.
    """
    logger.info(f"[HERRAMIENTA EVENTOS] Consultando agenda de eventos para fecha: '{fecha}'")

    try:
        agenda_path = os.path.join(BASE_CONFIG_PATH, MUNICIPIO_ID, 'agenda_cultural.json')
        with open(agenda_path, 'r', encoding='utf-8') as f:
            agenda_data = json.load(f).get('eventos', [])
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"No se pudo cargar o parsear el archivo de agenda cultural: {e}")
        return "Lo siento, no pude acceder a la agenda cultural en este momento. Por favor, intenta más tarde."

    # 1. Filtrar solo los que son 'eventos'
    eventos_culturales = [post for post in agenda_data if post.get('tipo_post') == 'evento']

    # 2. Filtrar por fecha
    fecha_norm = normalizar_texto(fecha)
    target_date_str = None

    if fecha_norm == "hoy":
        target_date_str = datetime.now().strftime('%Y-%m-%d')
    elif fecha_norm == "manana":
        target_date_str = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    else:
        return f"No entiendo la fecha '{fecha}'. Por favor, intentá con 'hoy' o 'mañana'."

    # Filtrar por fecha de inicio del evento
    eventos_encontrados = [
        evento for evento in eventos_culturales
        if evento.get('fecha_evento_inicio') and evento.get('fecha_evento_inicio').startswith(target_date_str)
    ]

    if not eventos_encontrados:
        return f"No encontré eventos culturales programados para '{fecha_norm}'. Puedes consultar la agenda completa en la web del municipio."

    # 3. Formatear la respuesta
    lista_eventos_str = []
    for evento in eventos_encontrados:
        titulo = evento.get('titulo', 'Sin título')
        subtitulo = evento.get('subtitulo')
        descripcion = evento.get('descripcion', 'Sin descripción')

        evento_str = f"*{titulo}*"
        if subtitulo:
            evento_str += f"\n_{subtitulo}_"
        evento_str += f"\n{descripcion}"

        lista_eventos_str.append(evento_str)

    respuesta = f"Para '{fecha_norm}', la agenda cultural es:\n\n" + "\n\n---\n\n".join(lista_eventos_str)

    # Add social media links
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

    # Asumimos un municipio_id por defecto. En una implementación real, esto debería ser dinámico.
    municipio_id = 1

    try:
        news_and_events = db.session.query(MunicipioTicket).filter(
            MunicipioTicket.municipio_id == municipio_id,
            MunicipioTicket.categoria.in_(['Noticia', 'Evento'])
        ).order_by(MunicipioTicket.fecha.desc()).limit(3).all()

        if not news_and_events:
            return "No se encontraron noticias o eventos recientes en la base de datos."

        mensaje = "Aquí están las últimas noticias y eventos de Junín Mendoza:\n\n"
        for i, item in enumerate(news_and_events, 1):
            mensaje += f"📰 *{item.asunto}* ({item.categoria})\n"
            # Podríamos agregar un link si tuviéramos una vista de detalle
            # mensaje += f"   {item.link}\n\n"
            mensaje += f"   {item.detalles}\n\n"

        return mensaje.strip()

    except Exception as e:
        logger.error(f"[HERRAMIENTA NOTICIAS] Error al consultar la base de datos: {e}", exc_info=True)
        return (
            "No pude obtener las últimas noticias en este momento debido a un error interno. "
            "Puedes consultarlas directamente en el sitio web: https://www.juninmendoza.gov.ar/noticias/"
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


def _extract_coords_from_maps(url: str) -> tuple[float, float] | None:
    """Extrae coordenadas de un enlace de Google Maps.

    Soporta patrones comunes como ``@lat,lon`` o ``q=lat,lon``. Si el enlace
    proviene de ``maps.app.goo.gl`` se sigue la redirección para obtener la URL
    final.
    """

    if not url or "maps" not in url:
        return None

    try:
        if "maps.app.goo.gl" in url:
            # Expand short links
            resp = requests.get(url, allow_redirects=True, timeout=5)
            url = resp.url
        patterns = [
            r"@(-?\d+\.\d+),(-?\d+\.\d+)",
            r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)",
            r"[?&](?:ll|saddr|daddr|destination)=(-?\d+\.\d+),(-?\d+\.\d+)",
            r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                return float(m.group(1)), float(m.group(2))
    except Exception as e:
        logger.error(f"[GEO] Error al parsear enlace de Google Maps '{url}': {e}")
    return None


def _resolve_municipio_config(cfg: dict | str | None) -> dict:
    """Return a full municipio config, loading from ID if needed."""
    if isinstance(cfg, dict) and cfg:
        return {**_DEFAULT_GEO_CONFIG, **cfg}
    if isinstance(cfg, str) and cfg:
        loaded = cargar_configuracion_municipio(cfg, "config.json")
        if loaded:
            return {**_DEFAULT_GEO_CONFIG, **loaded}
    return CONFIG_MUNICIPIO


def validar_y_formatear_direccion(
    direccion: str, municipio_config: dict | str | None = None
) -> dict | None:
    """Valida y formatea una dirección utilizando ``AddressResolver``.

    La resolución se restringe al ámbito provisto en ``municipio_config`` para
    devolver campos canónicos como ``calle``, ``numero``, ``entre_calles`` y
    coordenadas, además del ``formatted_address`` para compatibilidad.
    """
    from services.herramientas_municipio import TOOL_REGISTRY
    assert "generar_respuesta_audio" in TOOL_REGISTRY
    tool_info = TOOL_REGISTRY["generar_respuesta_audio"]
    assert "funcion" in tool_info
    assert "descripcion" in tool_info
    assert "parametros" in tool_info
    assert "text" in tool_info["parametros"]

@patch('services.herramientas_municipio.AddressResolver.resolve', side_effect=Exception("boom"))
def test_validar_y_formatear_direccion_error_api(mock_resolve):
    resultado = validar_y_formatear_direccion("Av. Siempreviva 742")
    assert resultado is None
