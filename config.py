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
