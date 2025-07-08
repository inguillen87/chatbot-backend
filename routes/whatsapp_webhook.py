from flask import Blueprint, request, jsonify, abort # Basic Flask components
from twilio.request_validator import RequestValidator # For validating Twilio requests
from twilio.rest import Client
import os
import json

# Define the blueprint
webhook_bp = Blueprint('whatsapp_webhook', __name__)

# Load environment variables
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# Load numbers mapping
# Use a default path if the environment variable is not set, for easier local development
DEFAULT_NUMEROS_PATH = "data/numeros_whatsapp.json"
TWILIO_NUMEROS_JSON_PATH = os.environ.get("TWILIO_NUMEROS_JSON", DEFAULT_NUMEROS_PATH)

NUMERO_TO_CLIENTE = {}
try:
    with open(TWILIO_NUMEROS_JSON_PATH, "r") as f:
        NUMERO_TO_CLIENTE = json.load(f)
except FileNotFoundError:
    print(f"Warning: File {TWILIO_NUMEROS_JSON_PATH} not found. NUMERO_TO_CLIENTE will be empty.")
except json.JSONDecodeError:
    print(f"Warning: File {TWILIO_NUMEROS_JSON_PATH} is not valid JSON. NUMERO_TO_CLIENTE will be empty.")


# Initialize Twilio client and validator
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
else:
    print("Warning: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN environment variables not set. Twilio client and validator will not be initialized.")
    twilio_client = None
    validator = None

@webhook_bp.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    if not validator:
        print("Error: Twilio RequestValidator not initialized. Ensure TWILIO_AUTH_TOKEN is set.")
        abort(500, "Twilio validator not configured")

    # 1. Validar autenticidad del webhook Twilio
    signature = request.headers.get("X-Twilio-Signature", "")
    # Asegúrate de que la URL que usas para validar es la que Twilio usa para firmar.
    # Esto puede requerir configuración si estás detrás de un proxy.
    url = request.url
    post_vars = request.form.to_dict()

    # For local testing, Flask's dev server might use http. Twilio might expect https.
    # If so, you might need to manually set the URL scheme for validation during development.
    # Example: if request.url.startswith("http://") and "YOUR_HTTPS_DOMAIN" in os.environ:
    # url = request.url.replace("http://", "https://", 1)

    if not validator.validate(url, post_vars, signature):
        abort(403, "Invalid Twilio signature")

    # 2. Obtener info básica del mensaje
    to_number_raw = post_vars.get("To", "")      # nuestro número (a qué empresa/pyme)
    from_number_raw = post_vars.get("From", "")  # número del usuario
    message_body = post_vars.get("Body", "")

    # Limpiar el prefijo "whatsapp:" si está presente
    to_number = to_number_raw.replace("whatsapp:", "")
    from_number = from_number_raw.replace("whatsapp:", "")

    # Log para debugging inicial
    print(f"Received message to: {to_number}, from: {from_number}, body: '{message_body}'")

    # 3. Identificar a qué cliente corresponde el número de destino
    cliente_info = NUMERO_TO_CLIENTE.get(to_number)
    if not cliente_info:
        print(f"Error: Cliente no encontrado para el número {to_number}")
        # Consider returning a more informative error or handling differently
        return "Cliente no encontrado para el número de destino.", 404

    empresa_id = cliente_info.get("empresa_id")
    if not empresa_id:
        print(f"Error: empresa_id no configurada para el cliente {cliente_info.get('nombre')}")
        # This indicates a configuration error in numeros_whatsapp.json
        return "Configuración de cliente incompleta.", 500

    print(f"Mensaje para cliente: {cliente_info.get('nombre')} (ID: {empresa_id})")

    # 4. Manejo de sesión/contexto por usuario (STUBBED)
    user_session_id = f"{empresa_id}_{from_number}" # from_number is already cleaned

    # Aquí recuperás la sesión/estado desde tu base de datos o memoria
    # session_data = recuperar_sesion(user_session_id) # implementalo según tu stack

    # STUBBED IMPLEMENTATION:
    def recuperar_sesion(session_id):
        print(f"STUB: Intentando recuperar sesión para {session_id}")
        # En una implementación real, aquí se consultaría Redis, DB, etc.
        # Por ahora, devolvemos un diccionario vacío o datos de ejemplo.
        return {"historial_chat": [], "estado_conversacion": "inicio"}

    def guardar_sesion(session_id, data):
        print(f"STUB: Guardando sesión para {session_id} con datos: {data}")
        # En una implementación real, aquí se guardaría en Redis, DB, etc.
        pass # No hace nada en el stub

    session_data = recuperar_sesion(user_session_id)
    print(f"STUB: Sesión recuperada para {user_session_id}: {session_data}")

    # 5. Llamar a la lógica del bot (STUBBED)
    # Esta es tu función central, igual que para el widget web.
    # respuesta, session_data = responder_chatboc(
    #     empresa_id=empresa_id,
    #     user_number=from_number, # Podrías pasar el raw from_number si necesitas el "whatsapp:" prefijo
    #     mensaje=message_body,
    #     session=session_data
    # )

    # STUBBED IMPLEMENTATION:
    def responder_chatboc(empresa_id, user_number, mensaje, session):
        print(f"STUB: Llamando a responder_chatboc para empresa {empresa_id}, usuario {user_number}, mensaje '{mensaje}'")
        # Simular una respuesta y actualización de sesión
        respuesta_bot = f"Eco de empresa {empresa_id}: '{mensaje}'. Estado de sesión: {session.get('estado_conversacion')}"
        session["historial_chat"].append({"usuario": mensaje, "bot": respuesta_bot})
        session["estado_conversacion"] = "continuando" # Ejemplo de cambio de estado
        return respuesta_bot, session

    respuesta_del_bot, session_data_actualizada = responder_chatboc(
        empresa_id=empresa_id,
        user_number=from_number,
        mensaje=message_body,
        session=session_data
    )
    print(f"STUB: Respuesta del bot: '{respuesta_del_bot}', Sesión actualizada: {session_data_actualizada}")

    # 6. Guardar el contexto actualizado (STUBBED)
    guardar_sesion(user_session_id, session_data_actualizada)

    # 7. Enviar respuesta por WhatsApp
    if twilio_client:
        try:
            message = twilio_client.messages.create(
                from_=f"whatsapp:{to_number_raw.replace('whatsapp:', '')}", # Ensure "whatsapp:" prefix for Twilio
                to=f"whatsapp:{from_number_raw.replace('whatsapp:', '')}",   # Ensure "whatsapp:" prefix for Twilio
                body=respuesta_del_bot
                # Si necesitas enviar botones, ver documentación Twilio Interactive Messages
            )
            print(f"Mensaje enviado a {from_number_raw}, SID: {message.sid}")
        except Exception as e:
            print(f"Error al enviar mensaje de Twilio: {e}")
            # Consider how to handle Twilio send errors, e.g., retry, log critical
            # For now, we'll still return 200 to Twilio to acknowledge receipt of webhook
            # but a real app might return 500 or have a retry mechanism.
    else:
        print("Warning: Twilio client no inicializado. No se puede enviar respuesta.")

    return "OK", 200
