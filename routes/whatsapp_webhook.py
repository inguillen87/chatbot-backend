from flask import Blueprint, request, jsonify, abort # Basic Flask components
from twilio.request_validator import RequestValidator # For validating Twilio requests
from twilio.rest import Client # For sending messages via Twilio
import os # For accessing environment variables
import json # For loading the numbers mapping file

# Define the blueprint for WhatsApp webhooks
webhook_bp = Blueprint('whatsapp_webhook', __name__)

# Load environment variables for Twilio credentials
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# Load numbers mapping which links Twilio numbers to client configurations
# Use a default path if the environment variable is not set, for easier local development
DEFAULT_NUMEROS_PATH = "data/numeros_whatsapp.json"
TWILIO_NUMEROS_JSON_PATH = os.environ.get("TWILIO_NUMEROS_JSON", DEFAULT_NUMEROS_PATH)

NUMERO_TO_CLIENTE = {} # Dictionary to store the mapping
try:
    with open(TWILIO_NUMEROS_JSON_PATH, "r") as f:
        NUMERO_TO_CLIENTE = json.load(f)
except FileNotFoundError:
    # Log a warning if the mapping file is not found
    print(f"Warning: WhatsApp numbers mapping file {TWILIO_NUMEROS_JSON_PATH} not found. NUMERO_TO_CLIENTE will be empty.")
except json.JSONDecodeError:
    # Log a warning if the mapping file is not valid JSON
    print(f"Warning: WhatsApp numbers mapping file {TWILIO_NUMEROS_JSON_PATH} is not valid JSON. NUMERO_TO_CLIENTE will be empty.")


# Initialize Twilio client and request validator
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    validator = RequestValidator(TWILIO_AUTH_TOKEN) # Used to validate incoming webhook signatures
else:
    # Log a warning if Twilio credentials are not set
    print("Warning: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN environment variables not set. Twilio client and validator will not be initialized.")
    twilio_client = None
    validator = None

@webhook_bp.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """
    Handles incoming WhatsApp messages from Twilio.
    Validates the request, identifies the client, processes the message (stubbed),
    and sends a response back (stubbed).
    """
    if not validator:
        # Abort if the validator wasn't initialized (due to missing auth token)
        print("Error: Twilio RequestValidator not initialized. Ensure TWILIO_AUTH_TOKEN is set.")
        abort(500, "Twilio validator not configured")

    # 1. Validar autenticidad del webhook Twilio
    # This ensures the request is genuinely from Twilio and not a malicious actor.
    signature = request.headers.get("X-Twilio-Signature", "")
    url = request.url # The full URL of the request
    post_vars = request.form.to_dict() # POST variables from the webhook

    # Note: When testing locally with ngrok or similar, ensure `url` matches what Twilio uses for signing.
    # If behind a proxy, Flask's `request.url` might need adjustment or ProxyFix middleware.
    if not validator.validate(url, post_vars, signature):
        abort(403, "Invalid Twilio signature") # Forbidden if signature is invalid

    # 2. Obtener info básica del mensaje
    to_number_raw = post_vars.get("To", "")      # Our Twilio number (identifies the client/tenant)
    from_number_raw = post_vars.get("From", "")  # The end-user's WhatsApp number
    message_body = post_vars.get("Body", "")     # The content of the user's message

    # Clean the "whatsapp:" prefix from numbers for internal use
    to_number = to_number_raw.replace("whatsapp:", "")
    from_number = from_number_raw.replace("whatsapp:", "")

    # Log for initial debugging (replace with proper logging in production)
    print(f"Received WhatsApp message to: {to_number}, from: {from_number}, body: '{message_body}'")

    # 3. Identificar a qué cliente corresponde el número de destino
    # Looks up the `to_number` in our mapping to find client-specific config.
    cliente_info = NUMERO_TO_CLIENTE.get(to_number)
    if not cliente_info:
        print(f"Error: Cliente no encontrado para el número de destino {to_number}")
        return "Cliente no encontrado para el número de destino.", 404 # Not Found if number isn't mapped

    empresa_id = cliente_info.get("empresa_id")
    if not empresa_id:
        # This indicates a configuration error in the numeros_whatsapp.json file
        print(f"Error: empresa_id no configurada para el cliente {cliente_info.get('nombre')} (número {to_number})")
        return "Configuración de cliente incompleta (falta empresa_id).", 500 # Internal Server Error

    print(f"Mensaje para cliente: {cliente_info.get('nombre')} (ID: {empresa_id})")

    # 4. Manejo de sesión/contexto por usuario (STUBBED)
    # Creates a unique session ID for this user with this client.
    user_session_id = f"{empresa_id}_{from_number}"

    # STUBBED IMPLEMENTATION for session retrieval:
    # In a real application, this would fetch session data from Redis, a database, etc.
    def recuperar_sesion(session_id):
        print(f"STUB: Intentando recuperar sesión para {session_id}")
        return {"historial_chat": [], "estado_conversacion": "inicio"} # Example session data

    # STUBBED IMPLEMENTATION for session saving:
    # In a real application, this would save session data to Redis, a database, etc.
    def guardar_sesion(session_id, data):
        print(f"STUB: Guardando sesión para {session_id} con datos: {data}")
        pass

    session_data = recuperar_sesion(user_session_id)
    print(f"STUB: Sesión recuperada para {user_session_id}: {session_data}")

    # 5. Llamar a la lógica del bot (STUBBED)
    # This is where your core chatbot processing logic would be invoked.
    # It should take the user's message and current session state to generate a response.

    # STUBBED IMPLEMENTATION for chatbot logic:
    def responder_chatboc(empresa_id, user_number, mensaje, session):
        print(f"STUB: Llamando a responder_chatboc para empresa {empresa_id}, usuario {user_number}, mensaje '{mensaje}'")
        # Simulate a simple echo response and session update
        respuesta_bot = f"Eco de empresa {empresa_id}: '{mensaje}'. Estado de sesión: {session.get('estado_conversacion')}"
        session["historial_chat"].append({"usuario": mensaje, "bot": respuesta_bot})
        session["estado_conversacion"] = "continuando" # Example state change
        return respuesta_bot, session

    respuesta_del_bot, session_data_actualizada = responder_chatboc(
        empresa_id=empresa_id,
        user_number=from_number, # Pass the cleaned user number
        mensaje=message_body,
        session=session_data
    )
    print(f"STUB: Respuesta del bot: '{respuesta_del_bot}', Sesión actualizada: {session_data_actualizada}")

    # 6. Guardar el contexto actualizado (STUBBED)
    # Persist the updated session data after the bot has processed the message.
    guardar_sesion(user_session_id, session_data_actualizada)

    # 7. Enviar respuesta por WhatsApp
    # Uses the Twilio client to send the bot's response back to the user.
    if twilio_client:
        try:
            message = twilio_client.messages.create(
                from_=f"whatsapp:{to_number_raw.replace('whatsapp:', '')}", # Twilio requires "whatsapp:" prefix for sender
                to=f"whatsapp:{from_number_raw.replace('whatsapp:', '')}",   # Twilio requires "whatsapp:" prefix for recipient
                body=respuesta_del_bot
                # For interactive messages (buttons, lists), refer to Twilio documentation.
            )
            print(f"Mensaje de respuesta enviado a {from_number_raw}, SID: {message.sid}")
        except Exception as e:
            print(f"Error al enviar mensaje de Twilio: {e}")
            # Error handling for Twilio API failures. Depending on the error,
            # you might want to retry or log for investigation.
            # Twilio expects a 2xx response to the webhook, so even if sending fails,
            # we return "OK" here to acknowledge receipt. A more robust system
            # might have background retries for failed outbound messages.
    else:
        print("Warning: Twilio client no inicializado. No se puede enviar respuesta por WhatsApp.")

    # Return "OK" (HTTP 200) to Twilio to acknowledge receipt of the webhook.
    # If Twilio doesn't receive a 2xx response, it will retry the webhook,
    # potentially leading to duplicate message processing.
    return "OK", 200
