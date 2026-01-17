import os
import json
import base64
import logging
from flask import current_app
from websockets.sync.client import connect as ws_connect
from models import WhatsappNumero, ChatSessionContext, User
from extensions import db
from sqlalchemy.orm import joinedload
from utils.db_utils import safe_flag_modified
from utils.whatsapp import enviar_mensaje_whatsapp_con_fallback
import hashlib

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"

class VoiceStreamService:
    def __init__(self, ws):
        self.ws = ws # Flask-Sock WebSocket (client connection from Twilio)
        self.openai_ws = None
        self.stream_sid = None
        self.call_sid = None
        self.from_number = None
        self.to_number = None
        self.user = None
        self.owner_user = None
        self.session_context = None

        # Tools definitions
        self.tools = [
            {
                "type": "function",
                "name": "crear_reclamo",
                "description": "Registra un nuevo reclamo municipal.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "categoria": {"type": "string", "description": "Categoría del reclamo (ej: Alumbrado, Limpieza)"},
                        "descripcion": {"type": "string", "description": "Qué pasó"},
                        "ubicacion": {"type": "string", "description": "Dónde ocurrió (dirección)"}
                    },
                    "required": ["categoria", "descripcion", "ubicacion"]
                }
            },
            {
                "type": "function",
                "name": "crear_pedido",
                "description": "Registra un nuevo pedido de venta.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "string", "description": "Productos y cantidades"},
                        "direccion_entrega": {"type": "string"}
                    },
                    "required": ["items"]
                }
            },
             {
                "type": "function",
                "name": "transferir_humano",
                "description": "Transfiere la llamada a un agente humano.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "motivo": {"type": "string"}
                    },
                    "required": ["motivo"]
                }
            }
        ]

    def _resolve_context(self, from_number, to_number, call_sid):
        """
        Resolves User, Tenant, and Session Context based on phone numbers.
        Similar to handle_voice_interaction but optimized for stream init.
        """
        try:
            # Clean numbers
            user_phone_clean = from_number.replace("whatsapp:", "").strip() if from_number else ""
            bot_phone_clean = to_number.replace("whatsapp:", "").strip() if to_number else ""

            # Find Tenant
            whatsapp_mapping = WhatsappNumero.query.options(
                joinedload(WhatsappNumero.user).joinedload(User.rubro)
            ).filter(WhatsappNumero.numero_whatsapp.ilike(f"%{bot_phone_clean.replace('+','').replace(' ','')}%")).first()

            if not whatsapp_mapping:
                logger.error(f"Tenant not found for bot phone: {bot_phone_clean}")
                return False

            self.owner_user = whatsapp_mapping.user

            # Find/Create End User
            from services.pymes import get_or_create_user_by_phone
            self.user = get_or_create_user_by_phone(user_phone_clean, self.owner_user)

            # Session Context
            empresa_id = self.owner_user.id
            if call_sid and len(call_sid) <= 36:
                chat_session_id = call_sid
            else:
                raw_id = f"v_{empresa_id}_{user_phone_clean}"
                chat_session_id = hashlib.md5(raw_id.encode()).hexdigest()

            self.session_context = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id).first()
            if not self.session_context:
                self.session_context = ChatSessionContext(
                    chat_session_id=chat_session_id,
                    user_id=empresa_id,
                    context_data={}
                )
                db.session.add(self.session_context)
                db.session.commit()

            return True
        except Exception as e:
            logger.error(f"Error resolving context: {e}")
            return False

    def _get_system_instruction(self):
        """
        Constructs the voice-first system prompt based on tenant type.
        """
        tenant_name = getattr(self.owner_user, "nombre_empresa", "Tu Municipio")
        user_name = getattr(self.user, "name", "Vecino")

        base_prompt = (
            f"Eres el asistente de voz de {tenant_name}. Hablas con {user_name}. "
            "Tu objetivo es resolver la consulta AUTOMÁTICAMENTE (tomar reclamo o pedido). "
            "Habla fluido, rápido, con acento argentino rioplatense (usa 'vos', 'che', 'dale'). "
            "Sé BREVE. Una pregunta a la vez. No hagas listas largas. "
            "Si piden humano, intenta resolver primero. Solo transfiere si insisten. "
            "Si necesitan mandar foto, diles que lo hagan por WhatsApp al cortar. "
        )
        return base_prompt

    def run(self):
        """
        Main loop for the bi-directional stream.
        """
        try:
            # Connect to OpenAI Realtime
            self.openai_ws = ws_connect(
                OPENAI_REALTIME_URL,
                additional_headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "OpenAI-Beta": "realtime=v1"
                }
            )
            logger.info("Connected to OpenAI Realtime API")

            # Initialize OpenAI Session
            # We delay sending session.update until we have context (from 'start' event)

            while True:
                message = self.ws.receive()
                if not message:
                    break

                data = json.loads(message)
                event_type = data.get('event')

                if event_type == 'start':
                    self.stream_sid = data['start']['streamSid']
                    self.call_sid = data['start']['callSid']
                    custom_params = data['start'].get('customParameters', {})
                    self.from_number = custom_params.get('from_number')
                    self.to_number = custom_params.get('to_number')

                    logger.info(f"Stream started: {self.stream_sid} Call: {self.call_sid}")

                    # Resolve Context
                    with current_app.app_context():
                        if self._resolve_context(self.from_number, self.to_number, self.call_sid):
                            # Configure OpenAI Session
                            session_update = {
                                "type": "session.update",
                                "session": {
                                    "modalities": ["text", "audio"],
                                    "instructions": self._get_system_instruction(),
                                    "voice": "shimmer", # or 'alloy', etc.
                                    "input_audio_format": "g711_ulaw",
                                    "output_audio_format": "g711_ulaw",
                                    "turn_detection": {
                                        "type": "server_vad",
                                        "threshold": 0.5,
                                        "prefix_padding_ms": 300,
                                        "silence_duration_ms": 500
                                    },
                                    "tools": self.tools
                                }
                            }
                            self.openai_ws.send(json.dumps(session_update))

                            # Initial greeting (triggers AI generation)
                            # Or we can just let VAD handle user saying "Hola"
                            # To force greeting:
                            self.openai_ws.send(json.dumps({
                                "type": "response.create",
                                "response": {
                                    "modalities": ["text", "audio"],
                                    "instructions": "Saluda brevemente: 'Hola, soy el asistente de [Nombre]. ¿En qué te ayudo?'"
                                }
                            }))

                elif event_type == 'media':
                    if self.openai_ws:
                        audio_payload = data['media']['payload']
                        self.openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": audio_payload
                        }))

                elif event_type == 'stop':
                    logger.info("Stream stopped by Twilio")
                    break

                # Poll OpenAI for events (non-blocking if possible, but here we are in a sync loop)
                # Since we are in a loop reading from Twilio, we need a way to read from OpenAI concurrently.
                # 'websockets.sync' is blocking. We might need threads or async.
                # HOWEVER, flask-sock is running in a greenlet (eventlet).
                # We can spawn a greenlet to read from OpenAI.

                # To keep it simple in this iteration without major refactor:
                # We need to read from OpenAI. `recv` on openai_ws is blocking.
                # If we block waiting for Twilio, we delay OpenAI audio.
                # Solution: Use eventlet to spawn a reader for OpenAI.

            self.openai_ws.close()

        except Exception as e:
            logger.error(f"Stream error: {e}", exc_info=True)
            if self.openai_ws:
                self.openai_ws.close()

    # Note: The logic above has a flaw: single-threaded blocking receive.
    # Proper implementation requires reading both sockets concurrently.
    # Since we are using eventlet, we can use `eventlet.spawn`.

    def run_concurrent(self):
        # Implementation using eventlet spawn
        import eventlet

        self.openai_ws = ws_connect(
            OPENAI_REALTIME_URL,
             additional_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            }
        )

        def listen_openai():
            try:
                for msg in self.openai_ws:
                    data = json.loads(msg)
                    self.handle_openai_message(data)
            except Exception as e:
                logger.error(f"OpenAI listener error: {e}")

        def listen_twilio():
            try:
                while True:
                    msg = self.ws.receive() # Flask-Sock receive
                    if not msg: break
                    data = json.loads(msg)
                    self.handle_twilio_message(data)
            except Exception as e:
                logger.error(f"Twilio listener error: {e}")

        # Spawn OpenAI listener
        openai_thread = eventlet.spawn(listen_openai)

        # Run Twilio listener in main thread
        listen_twilio()

        # Cleanup
        self.openai_ws.close()
        openai_thread.kill()

    # Redefine run to use concurrent
    run = run_concurrent

    def handle_twilio_message(self, data):
        event_type = data.get('event')
        if event_type == 'start':
            self.stream_sid = data['start']['streamSid']
            self.call_sid = data['start']['callSid']
            custom = data['start'].get('customParameters', {})
            self.from_number = custom.get('from_number')
            self.to_number = custom.get('to_number')

            with current_app.app_context():
                if self._resolve_context(self.from_number, self.to_number, self.call_sid):
                    # Init Session
                     session_update = {
                        "type": "session.update",
                        "session": {
                            "modalities": ["text", "audio"],
                            "instructions": self._get_system_instruction(),
                            "voice": "shimmer",
                            "input_audio_format": "g711_ulaw",
                            "output_audio_format": "g711_ulaw",
                            "turn_detection": {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300, "silence_duration_ms": 500},
                            "tools": self.tools
                        }
                    }
                     self.openai_ws.send(json.dumps(session_update))

        elif event_type == 'media':
            if self.openai_ws:
                self.openai_ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": data['media']['payload']
                }))

        elif event_type == 'clear':
            # Twilio buffer cleared (user interrupted?)
            if self.openai_ws:
                self.openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))

    def handle_openai_message(self, data):
        msg_type = data.get('type')

        if msg_type == 'response.audio.delta':
            audio_payload = data.get('delta')
            if audio_payload:
                self.ws.send(json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": audio_payload}
                }))

        elif msg_type == 'input_audio_buffer.speech_started':
            # User started speaking (Barge-in)
            # Clear Twilio buffer
            self.ws.send(json.dumps({
                "event": "clear",
                "streamSid": self.stream_sid
            }))
            # Cancel current OpenAI response generation if any
            self.openai_ws.send(json.dumps({"type": "response.cancel"}))

        elif msg_type == 'response.function_call_arguments.done':
            # Function call triggered
            call_id = data.get('call_id')
            name = data.get('name')
            args = data.get('arguments')
            self.execute_tool(call_id, name, args)

    def execute_tool(self, call_id, name, args_str):
        logger.info(f"Executing tool: {name} args: {args_str}")
        try:
            args = json.loads(args_str)
            result = "Error executing tool"

            with current_app.app_context():
                if name == "crear_reclamo":
                    from services.actions.municipio_actions import CrearReclamoActionHandler
                    # Mock context for handler
                    ctx = {
                        "user_obj": self.owner_user,
                        "viewer_user_obj": self.user,
                        "channel": "voice",
                        "chat_db_context_data": self.session_context.context_data
                    }
                    handler = CrearReclamoActionHandler(ctx)
                    res = handler.execute(args)
                    if res.get("success"):
                        data = res.get("data", {})
                        nro = data.get("nro_ticket", "N/A")
                        result = f"Reclamo creado con éxito. Número: {nro}."
                        # Save for summary
                        self.session_context.context_data["latest_ticket_nro"] = nro
                        safe_flag_modified(self.session_context, "context_data")
                        db.session.commit()
                    else:
                        result = "No se pudo crear el reclamo. Faltan datos."

                elif name == "crear_pedido":
                     # Mock logic for pyme
                     result = "Pedido creado con éxito (simulado)."

                elif name == "transferir_humano":
                     result = "Entendido, te transfiero con un agente."
                     # Logic to trigger transfer on Twilio side?
                     # We can't trigger transfer from here easily via WebSocket back to Twilio TwiML
                     # Twilio Media Stream is mostly audio.
                     # We might need to update TwiML via REST API or just say "Corta y llama al X".
                     # Or use 'mark' event to trigger logic on backend?
                     # For now, just confirm voice.

            # Send output back to OpenAI
            self.openai_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result
                }
            }))
            self.openai_ws.send(json.dumps({"type": "response.create"})) # Trigger response based on output

        except Exception as e:
            logger.error(f"Tool execution failed: {e}")
