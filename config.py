# En config.py
import os

# Directorio base de la aplicación
basedir = os.path.abspath(os.path.dirname(__file__))
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "-3"))

class Config:
    """
    Clase de configuración principal de la aplicación.
    Contiene todas las variables de configuración.
    """

    DEBUG = True  # <--- AGREGÁ ESTA LÍNEA

    # 1. LLAVE SECRETA: Crucial para la seguridad de la sesión.
    SECRET_KEY = os.getenv("SECRET_KEY", "una-llave-secreta-muy-segura-para-desarrollo-local")

    # 2. CONFIGURACIÓN DE LA BASE DE DATOS:
    # Se añade '?check_same_thread=False' a las URIs de SQLite para prevenir
    # el error 500 en entornos de servidor multi-hilo como Render.
    if os.getenv("RENDER") == "true":
        # --- MODIFICADO: Configuración para producción en Render ---
        db_path_render = "/data/database.db"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path_render}?check_same_thread=False"
    else:
        # --- MODIFICADO: Configuración para desarrollo local ---
        local_db_path = os.path.join(basedir, 'instance', 'database.db')
        # Asegurarse de que el directorio 'instance' exista
        os.makedirs(os.path.dirname(local_db_path), exist_ok=True)
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{local_db_path}?check_same_thread=False"

    # Opciones adicionales para el engine de SQLAlchemy
    SQLALCHEMY_ENGINE_OPTIONS = {
        'connect_args': {'timeout': 15}  # Aumentar timeout de SQLite a 15 segundos
    }

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 3. CONFIGURACIÓN DE COOKIES DE SESIÓN:
    # Para que funcionen en un entorno con dominios separados (Vercel + Render).
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = 'None'
    # Ajustado para funcionar en subdominios, si es necesario. Si no, se puede quitar.
    # SESSION_COOKIE_DOMAIN = '.chatboc.ar' # Descomentar si tienes problemas entre www y api.

    # 4. CONFIGURACIÓN PARA SESIONES EN EL LADO DEL SERVIDOR (Flask-Session)
    # Le decimos a Flask-Session que guarde la "memoria" en nuestra base de datos.
    SESSION_TYPE = 'sqlalchemy'
    SESSION_SQLALCHEMY_TABLE = 'sessions'

    # Texto opcional para el globito de atención del widget
    ATTENTION_BUBBLE_TEXT = os.getenv(
        "ATTENTION_BUBBLE_TEXT", "¡Hola! ¿Necesitas ayuda?"
    )
    # Lista opcional de mensajes para rotar en el globito de atención
    ATTENTION_BUBBLE_CHOICES = [m.strip() for m in os.getenv(
        "ATTENTION_BUBBLE_CHOICES", ""
    ).split("|") if m.strip()] or None

    # Base URL del frontend para generar links de productos
    TIENDA_BASE_URL = os.getenv("TIENDA_BASE_URL", "")

    # --- LÍMITES PARA USUARIOS ANÓNIMOS ---
    # Número máximo de mensajes que un usuario anónimo puede enviar/recibir por sesión.
    # Una pregunta del usuario y su respuesta del bot cuentan como 1 o 2 interacciones (a definir en la lógica).
    # Por ahora, consideraremos cada pregunta del usuario como una "interacción".
    ANONYMOUS_MAX_MESSAGES_PER_SESSION = int(os.getenv("ANONYMOUS_MAX_MESSAGES_PER_SESSION", "10"))

    # Tiempo en minutos tras el cual una sesión anónima sin actividad se considera expirada.
    # Esto reiniciará el conteo de mensajes para ese anon_id si vuelve a interactuar.
    ANONYMOUS_SESSION_TIMEOUT_MINUTES = int(os.getenv("ANONYMOUS_SESSION_TIMEOUT_MINUTES", "15"))

    # Número máximo de tickets (reclamos, pedidos, etc.) que un usuario anónimo puede crear por sesión.
    ANONYMOUS_MAX_TICKETS_PER_SESSION = int(os.getenv("ANONYMOUS_MAX_TICKETS_PER_SESSION", "1"))

    # Cantidad de tickets que se muestran por página en el panel
    TICKETS_PER_PAGE_DEFAULT = int(os.getenv("TICKETS_PER_PAGE_DEFAULT", "50"))

    # --- CONFIGURACIÓN DE GOOGLE CLOUD AI SERVICES ---
    GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", None)
    # Para Document AI
    GOOGLE_DOCAI_LOCATION = os.getenv("GOOGLE_DOCAI_LOCATION", "us") # Default a 'us' si no se especifica
    GOOGLE_DOCAI_PROCESSOR_ID = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID", None) # ID del procesador de Document AI
    # GOOGLE_DOCAI_FORM_PARSER_PROCESSOR_ID = os.getenv("GOOGLE_DOCAI_FORM_PARSER_PROCESSOR_ID", None) # Específico para Form Parser
    # GOOGLE_DOCAI_OCR_PROCESSOR_ID = os.getenv("GOOGLE_DOCAI_OCR_PROCESSOR_ID", None) # Específico para OCR

    # Para Vision AI (actualmente usa credenciales de entorno, pero podría tener configs específicas si es necesario)
    # GOOGLE_VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", None) # No se usa si se usan ADC

    # Path al archivo JSON de credenciales de cuenta de servicio de Google Cloud
    # Si está seteado GOOGLE_APPLICATION_CREDENTIALS en el entorno, las librerías lo usan automáticamente.
    # Esta variable es más para referencia o si se necesita cargar manualmente en algún punto.
    GOOGLE_APPLICATION_CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", None)

    # --- CONFIGURACIÓN DE CELERY ---
    # URL del broker (ej. Redis, RabbitMQ). Default a Redis local para desarrollo.
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    # URL del backend de resultados (ej. Redis, base de datos). Default a Redis local.
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
    # Opcional: un diccionario con más configuraciones de Celery que se pasará a celery_app.conf.update()
    # CELERY_CONFIG = {
    #     "task_serializer": "json",
    #     "result_serializer": "json",
    #     "accept_content": ["json"],
    #     "timezone": "America/Argentina/Buenos_Aires",
    #     "enable_utc": True,
    #     "worker_concurrency": 2, # Ejemplo: limitar concurrencia
    #     # Para autodiscover de tareas si no se hace explícitamente en init_celery
    #     # "imports": ("services.analisis_archivo_service",) 
    # }

    # --- CONFIGURACIÓN DE EMAIL (SMTP) ---
    SMTP_HOST = os.getenv("SMTP_HOST")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587)) # Default a 587 para TLS
    SMTP_USER = os.getenv("SMTP_USER")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
    SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "True").lower() in ('true', '1', 't') # Convertir a booleano
    SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "False").lower() in ('true', '1', 't') # Convertir a booleano

    MAIL_FROM_ADDRESS = os.getenv("MAIL_FROM_ADDRESS", SMTP_USER if SMTP_USER else "noreply@example.com")
    MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Chatboc Platform")

    # Opcional: Configuraciones específicas para campañas (fallback a las generales si no se definen)
    SMTP_HOST_CAMPAIGN = os.getenv("SMTP_HOST_CAMPAIGN", SMTP_HOST)
    SMTP_PORT_CAMPAIGN = int(os.getenv("SMTP_PORT_CAMPAIGN", SMTP_PORT))
    SMTP_USER_CAMPAIGN = os.getenv("SMTP_USER_CAMPAIGN", SMTP_USER)
    SMTP_PASSWORD_CAMPAIGN = os.getenv("SMTP_PASSWORD_CAMPAIGN", SMTP_PASSWORD)
    SMTP_USE_TLS_CAMPAIGN = os.getenv("SMTP_USE_TLS_CAMPAIGN", str(SMTP_USE_TLS)).lower() in ('true', '1', 't')
    SMTP_USE_SSL_CAMPAIGN = os.getenv("SMTP_USE_SSL_CAMPAIGN", str(SMTP_USE_SSL)).lower() in ('true', '1', 't')
    MAIL_FROM_ADDRESS_CAMPAIGN = os.getenv("MAIL_FROM_ADDRESS_CAMPAIGN", MAIL_FROM_ADDRESS)
    MAIL_FROM_NAME_CAMPAIGN = os.getenv("MAIL_FROM_NAME_CAMPAIGN", MAIL_FROM_NAME)

    # --- CONFIGURACIÓN DE TWILIO (para SMS/WhatsApp) ---
    # email_service.py actualmente usa os.getenv() para estas, pero las centralizamos aquí
    # para que la app Flask las conozca y puedan ser usadas por otros módulos si es necesario.
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER") # Para SMS
    TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER") # Para WhatsApp (ej. "whatsapp:+14155238886")

    # --- CONFIGURACIÓN DE APP BASE URL (para generar links en emails/notificaciones) ---
    # Usado en el paso de descarga de catálogo, también útil para links en campañas.
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5000") # Default para desarrollo

    # --- UMBRALES PARA SUGERENCIA DE REGISTRO PROACTIVA ---
    # Después de cuántas interacciones de un usuario anónimo en una sesión de chat se sugiere registrarse.
    # Establecer a 0 o None para desactivar esta sugerencia basada en conteo.
    PYME_UMBRAL_SUGERENCIA_REGISTRO = int(os.getenv("PYME_UMBRAL_SUGERENCIA_REGISTRO", "3"))
    MUNICIPIO_UMBRAL_SUGERENCIA_REGISTRO = int(os.getenv("MUNICIPIO_UMBRAL_SUGERENCIA_REGISTRO", "3"))
