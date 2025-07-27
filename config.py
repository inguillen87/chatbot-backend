import os

# Directorio base de la aplicación
basedir = os.path.abspath(os.path.dirname(__file__))
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "-3"))

class Config:
    """
    Clase de configuración principal de la aplicación.
    Contiene todas las variables de configuración.
    """

    DEBUG = True

    # 1. LLAVE SECRETA
    SECRET_KEY = os.getenv("SECRET_KEY", "una-llave-secreta-muy-segura-para-desarrollo-local")

    # 2. CONFIGURACIÓN DE LA BASE DE DATOS
    if os.getenv("RENDER") == "true":
        db_path_render = "/data/database.db"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path_render}?check_same_thread=False"
    else:
        local_db_path = os.path.join(basedir, 'instance', 'database.db')
        os.makedirs(os.path.dirname(local_db_path), exist_ok=True)
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{local_db_path}?check_same_thread=False"

    SQLALCHEMY_ENGINE_OPTIONS = {
        'connect_args': {'timeout': 15}
    }
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 3. CONFIGURACIÓN DE COOKIES DE SESIÓN (SIN VUELTAS)
    SESSION_COOKIE_DOMAIN = '.chatboc.ar'         # <- SIEMPRE este dominio
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = 'None'
    SESSION_TYPE = 'sqlalchemy'
    SESSION_SQLALCHEMY_TABLE = 'sessions'

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

    GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", None)
    GOOGLE_DOCAI_LOCATION = os.getenv("GOOGLE_DOCAI_LOCATION", "us")
    GOOGLE_DOCAI_PROCESSOR_ID = os.getenv("GOOGLE_DOCAI_PROCESSOR_ID", None)
    GOOGLE_APPLICATION_CREDENTIALS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", None)

    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER = os.getenv("SMTP_USER", "tucorreo@gmail.com")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "tucontraseña")
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

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    CELERY_TASK_ALWAYS_EAGER = True
    SESSION_COOKIE_SECURE = False
    SERVER_NAME = 'localhost'

class TestingConfig(TestConfig):
    pass
