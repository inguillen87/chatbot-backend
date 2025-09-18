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
            "key": os.getenv("DEMO_ALMACEN_KEY", "almacen"),
            "nombre": os.getenv("DEMO_ALMACEN_NOMBRE", "Almacén Inteligente"),
            "descripcion": os.getenv(
                "DEMO_ALMACEN_DESCRIPCION",
                "Catálogo minorista y mayorista con combos semanales, control de stock y entregas a domicilio en el día.",
            ),
            "token": os.getenv("DEMO_ALMACEN_TOKEN", "demo-token-almacen"),
            "tipo_chat": os.getenv("DEMO_ALMACEN_TIPO_CHAT", "pyme"),
            "rubro_clave": os.getenv("DEMO_ALMACEN_RUBRO", "almacen"),
            "prompt_context": os.getenv(
                "DEMO_ALMACEN_PROMPT_CONTEXT",
                (
                    "ByM Almacén Digital combina góndola física con pedidos online. Ofrece combos familiares de lácteos, "
                    "bebidas y snacks, reposiciones programadas para bares y rotiserías, precios mayoristas a partir de 6 "
                    "unidades y seguimiento de stock en tiempo real. Gestiona delivery propio en radio cercano, logística con "
                    "moto para urgencias y acuerdos con Mercado Pago, MODO y transferencias. Usa un tono cercano, ágil y "
                    "orientado a resolver pedidos mixtos (retiro o envío) en el momento."
                ),
            ),
            "welcome_message": os.getenv(
                "DEMO_ALMACEN_WELCOME_MESSAGE",
                "🛒 ¡Bienvenido al demo del almacén! Contame qué productos o combos necesitas hoy.",
            ),
            "resources": [
                {
                    "title": "Lista de precios actualizada",
                    "description": "Precios minoristas y mayoristas con combos listos para delivery o retiro en tienda.",
                    "type": "link",
                    "url": "https://www.chatboc.ar/",
                    "cta_text": "Ver combos disponibles",
                }
            ],
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
            "prompt_context": os.getenv(
                "DEMO_BODEGA_PROMPT_CONTEXT",
                (
                    "Bodega Cuatro Fincas es una bodega boutique mendocina enfocada en vinos premium. "
                    "Catálogo destacado: Gran Malbec Reserva 2021 ($18.500) con notas a ciruela y "
                    "chocolate; Blend de Altura 2019 ($21.000) con Malbec, Cabernet Franc y Petit "
                    "Verdot; Torrontés Andino 2023 ($11.500) fresco y floral; Espumante Extra Brut "
                    "Tradicional ($16.800) método champenoise; Caja Degustación 6 botellas ($89.900) "
                    "con selección del enólogo; Pack Regalo Malbec + Bonarda ($34.500) con estuche. "
                    "Promos activas: 10% off en combos de 6 botellas, 15% off en compras mayores a "
                    "$120.000 y envío gratis en Gran Mendoza para pedidos desde $45.000. Horario de "
                    "atención en sala de degustación: lunes a sábado 10 a 20 hs; degustaciones "
                    "guiadas viernes y sábado 18 hs con reserva previa. Ofrece asesoramiento para "
                    "eventos, venta mayorista y armado de regalos corporativos con envío nacional."
                ),
            ),
            "welcome_message": os.getenv(
                "DEMO_BODEGA_WELCOME_MESSAGE",
                "🍷 ¡Hola! Soy el asistente de Bodega Cuatro Fincas. ¿Querés descubrir nuestros vinos?",
            ),
            "resources": [
                {
                    "title": "Catálogo Premium 2024",
                    "description": "Selección de etiquetas reserva, notas de cata y precios por botella y por caja.",
                    "type": "pdf",
                    "url": "/static/demo/bodega/catalogo-premium-2024.pdf",
                    "cta_text": "Descargar catálogo",
                },
                {
                    "title": "Lista de precios mayoristas",
                    "description": "Bonificaciones por volumen, combos de degustación y envíos a todo el país.",
                    "type": "pdf",
                    "url": "/static/demo/bodega/lista-precios-mayoristas.pdf",
                    "cta_text": "Consultar precios corporativos",
                },
                {
                    "title": "Gran Malbec Reserva 2021",
                    "description": "Ficha visual con notas de cata, maridajes sugeridos y precio promocional.",
                    "type": "image",
                    "url": "/static/demo/bodega/gran-malbec-reserva.svg",
                    "thumbnail": "/static/demo/bodega/gran-malbec-reserva.svg",
                    "cta_text": "Ver ficha del Malbec",
                },
            ],
        },
        {
            "key": os.getenv("DEMO_FERRETERIA_KEY", "ferreteria"),
            "nombre": os.getenv("DEMO_FERRETERIA_NOMBRE", "Ferretería y Corralón"),
            "descripcion": os.getenv(
                "DEMO_FERRETERIA_DESCRIPCION",
                "Materiales de construcción, herramientas eléctricas y logística a obra con presupuestos al instante.",
            ),
            "token": os.getenv("DEMO_FERRETERIA_TOKEN", "demo-token-ferreteria"),
            "tipo_chat": os.getenv("DEMO_FERRETERIA_TIPO_CHAT", "pyme"),
            "rubro_clave": os.getenv("DEMO_FERRETERIA_RUBRO", "ferreteria"),
            "prompt_context": os.getenv(
                "DEMO_FERRETERIA_PROMPT_CONTEXT",
                (
                    "Ferretería Central atiende obras chicas y medianas con stock de cementos, áridos, hierros, "
                    "herramientas eléctricas y sanitarios. Cotiza combos para refacciones, ofrece descuentos por volumen, "
                    "planifica entregas con camión grúa y seguimiento GPS de repartos. Brinda asesoramiento técnico para "
                    "elegir materiales, vende EPP, pinturas y artículos de jardinería. Usa un tono experto pero simple para "
                    "ayudar a profesionales y particulares que construyen o remodelan."
                ),
            ),
            "welcome_message": os.getenv(
                "DEMO_FERRETERIA_WELCOME_MESSAGE",
                "🔧 ¡Hola! Soy el asistente del corralón. ¿Qué materiales o herramientas necesitas cotizar?",
            ),
            "resources": [
                {
                    "title": "Lista de materiales para obra",
                    "description": "Cementos, áridos, perfiles y promociones vigentes por cantidad.",
                    "type": "link",
                    "url": "https://www.chatboc.ar/",
                    "cta_text": "Ver catálogo de obra",
                }
            ],
        },
        {
            "key": os.getenv("DEMO_LOCAL_GENERAL_KEY", "local_comercial_general"),
            "nombre": os.getenv("DEMO_LOCAL_GENERAL_NOMBRE", "Local Comercial General"),
            "descripcion": os.getenv(
                "DEMO_LOCAL_GENERAL_DESCRIPCION",
                "Mostrador omnicanal para indumentaria, deco y regalos con stock integrado y campañas de fidelización.",
            ),
            "token": os.getenv("DEMO_LOCAL_GENERAL_TOKEN", "demo-token-local"),
            "tipo_chat": os.getenv("DEMO_LOCAL_GENERAL_TIPO_CHAT", "pyme"),
            "rubro_clave": os.getenv("DEMO_LOCAL_GENERAL_RUBRO", "local_comercial"),
            "prompt_context": os.getenv(
                "DEMO_LOCAL_GENERAL_PROMPT_CONTEXT",
                (
                    "Local Comercial Demo vende indumentaria urbana, deco y regalos corporativos. Integra catálogo en tienda "
                    "física, Instagram Shopping y tienda online con pasarela de pagos. Ofrece combos de temporada, cupones de "
                    "fidelización, reservas con seña digital y retiros en sucursal en 2 horas. Gestiona cambios, envíos a todo "
                    "el país y paquetes personalizados para empresas. El bot debe destacar disponibilidad en talles, colores, "
                    "promociones bancarias y seguimiento de pedidos."
                ),
            ),
            "welcome_message": os.getenv(
                "DEMO_LOCAL_GENERAL_WELCOME_MESSAGE",
                "🛍️ ¡Bienvenido! Contame qué prenda, regalo o combo corporativo estás buscando.",
            ),
            "resources": [
                {
                    "title": "Lookbook temporada actual",
                    "description": "Colecciones destacadas con precios, talles disponibles y combos corporativos.",
                    "type": "link",
                    "url": "https://www.chatboc.ar/",
                    "cta_text": "Descubrir novedades",
                }
            ],
        },
        {
            "key": os.getenv("DEMO_MEDICO_KEY", "medico_general"),
            "nombre": os.getenv("DEMO_MEDICO_NOMBRE", "Clínica Médico General"),
            "descripcion": os.getenv(
                "DEMO_MEDICO_DESCRIPCION",
                "Turnos online, guardias coordinadas y seguimiento de pacientes para medicina general y especialidades de base.",
            ),
            "token": os.getenv("DEMO_MEDICO_TOKEN", "demo-token-medico"),
            "tipo_chat": os.getenv("DEMO_MEDICO_TIPO_CHAT", "pyme"),
            "rubro_clave": os.getenv("DEMO_MEDICO_RUBRO", "medico"),
            "prompt_context": os.getenv(
                "DEMO_MEDICO_PROMPT_CONTEXT",
                (
                    "Clínica San Dona gestiona turnos para clínica médica, pediatría, ginecología, laboratorio y nutrición. "
                    "Permite reservar guardias programadas, coordinar estudios, validar obras sociales, enviar recordatorios "
                    "por WhatsApp y compartir resultados vía portal seguro. Atiende consultas sobre coberturas, horarios, "
                    "preparación para estudios y teleconsultas. Mantiene tono empático, claro y orientado a resolver trámites "
                    "rápidos para pacientes y familias."
                ),
            ),
            "welcome_message": os.getenv(
                "DEMO_MEDICO_WELCOME_MESSAGE",
                "🩺 Hola, soy el asistente de Clínica San Dona. ¿Querés reservar un turno o consultar tu cobertura?",
            ),
            "resources": [
                {
                    "title": "Guía de especialidades y coberturas",
                    "description": "Profesionales disponibles, obras sociales aceptadas y pasos para turnos online.",
                    "type": "link",
                    "url": "https://www.chatboc.ar/",
                    "cta_text": "Ver especialidades",
                }
            ],
        },
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
            "prompt_context": os.getenv(
                "DEMO_MUNICIPIO_PROMPT_CONTEXT",
                (
                    "El Municipio de Junín en Mendoza ofrece un asistente digital para reclamos "
                    "de luminaria, higiene urbana, arbolado, tránsito y servicios públicos. También "
                    "acompaña trámites como licencias de conducir, tasas municipales, turnos online "
                    "y consultas ciudadanas. Usa un tono cálido, profesional y resalta que el bot "
                    "permite registrar reclamos con ubicación, seguir tickets existentes y derivar "
                    "a un agente humano cuando haga falta."
                ),
            ),
            "welcome_message": os.getenv(
                "DEMO_MUNICIPIO_WELCOME_MESSAGE",
                "🙌 ¡Bienvenido a la demo municipal! Contame qué trámite o reclamo querés gestionar.",
            ),
            "resources": [
                {
                    "title": "Guía de trámites express",
                    "description": "Pasos clave para turnos, reclamos con foto y seguimiento 24/7 desde el panel ciudadano.",
                    "type": "pdf",
                    "url": "/static/demo/municipio/guia-tramites-rapidos.pdf",
                    "cta_text": "Descargar guía de trámites",
                },
                {
                    "title": "Plan de iluminación inteligente 2024",
                    "description": "Proyecto LED con sensores IoT, tablero de monitoreo y prioridades por barrio.",
                    "type": "pdf",
                    "url": "/static/demo/municipio/plan-iluminacion-inteligente.pdf",
                    "cta_text": "Ver plan de inversión",
                },
                {
                    "title": "Centro de monitoreo en tiempo real",
                    "description": "Visualización de KPIs, reclamos geolocalizados y derivación inmediata a cuadrillas.",
                    "type": "image",
                    "url": "/static/demo/municipio/centro-monitoreo-smart.svg",
                    "thumbnail": "/static/demo/municipio/centro-monitoreo-smart.svg",
                    "cta_text": "Abrir dashboard de monitoreo",
                },
            ],
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
