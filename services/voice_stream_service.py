import os
import json
import base64
import logging
from flask import current_app, url_for
from websockets.sync.client import connect as ws_connect
from models import WhatsappNumero, ChatSessionContext, User, TenantProfile
from extensions import db
from sqlalchemy.orm import joinedload
from utils.db_utils import safe_flag_modified
from utils.whatsapp import enviar_mensaje_whatsapp_con_fallback
import hashlib
from twilio.rest import Client as TwilioClient
from simple_websocket.errors import ConnectionClosed

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

class VoiceStreamService:
    def __init__(self, ws, app=None):
        self.ws = ws # Flask-Sock WebSocket (client connection from Twilio)
        self.app = app # Flask App Instance (for context in threads)
        self.openai_ws = None
        self.stream_sid = None
        self.call_sid = None
        self.from_number = None
        self.to_number = None
        self.user = None
        self.owner_user = None
        self.tenant_profile = None
        self.chat_session_id = None
        self.session_context = None # Deprecated: use self.chat_session_id and query fresh
        self.response_active = False # Flag to track active OpenAI response

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
                        "ubicacion": {"type": "string", "description": "Dónde ocurrió (dirección)"},
                        "telefono": {"type": "string", "description": "Teléfono de contacto (opcional si ya se conoce)"},
                        "nombre": {"type": "string", "description": "Nombre del vecino (opcional si ya se conoce)"}
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
            },
            {
                "type": "function",
                "name": "finalizar_llamada",
                "description": "Corta la llamada telefónica.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "type": "function",
                "name": "consultar_producto",
                "description": "Busca un producto en el catálogo por nombre y devuelve precio y stock.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "nombre": {"type": "string", "description": "Nombre o descripción del producto a buscar"}
                    },
                    "required": ["nombre"]
                }
            }
        ]

    def _resolve_context(self, from_number, to_number, call_sid):
        """
        Resolves User, Tenant, and Session Context based on phone numbers.
        Prioritizes looking up by twilio_voice_number in TenantProfile config.
        """
        try:
            # Clean numbers
            user_phone_clean = from_number.replace("whatsapp:", "").strip() if from_number else ""
            bot_phone_clean = to_number.replace("whatsapp:", "").strip() if to_number else ""

            # 1. Try to find Tenant via Config (JSON) - Robust method for Voice
            # We look for a TenantProfile where configuracion['twilio_voice_number'] matches bot_phone_clean
            # Since configuracion is JSON/JSONB, this query depends on DB type (Postgres vs Sqlite).
            # For compatibility and speed, we might try a direct query if possible, or fallback.

            # In Postgres: TenantProfile.configuracion['twilio_voice_number'].astext == bot_phone_clean
            # In Sqlite: JSON_EXTRACT(...)

            # We will try a Python-side filtering if the DB is small, or a specific query.
            # Given we can't easily change models/queries safely without knowing DB engine details in this context,
            # we'll try a hybrid approach or rely on the previous method as fallback.

            # ATTEMPT 1: JSON Search (SQLAlchemy hybrid/JSON support)
            # This is "risky" without knowing if it's Postgres or SQLite for sure in all envs,
            # but models.py defines JSONType = JSONB().with_variant(SQLITE_JSON, "sqlite")

            self.tenant_profile = None

            # Try finding by config first (if we have many tenants this is inefficient in python, but ok for now)
            # Efficient way:
            # tenant = TenantProfile.query.filter(TenantProfile.configuracion['twilio_voice_number'].astext == bot_phone_clean).first()
            # But 'astext' is PG specific.

            # Let's try to iterate or use a safer filter if mapped.
            # Fallback for now: Fetch all active tenants and check in python (cacheable in future)
            # OR use the WhatsappNumero fallback if that fails.

            # Strategy: Try WhatsappNumero first (legacy), then search Tenants if fails.

            whatsapp_mapping = WhatsappNumero.query.options(
                joinedload(WhatsappNumero.user).joinedload(User.rubro)
            ).filter(WhatsappNumero.numero_whatsapp.ilike(f"%{bot_phone_clean.replace('+','').replace(' ','')}%")).first()

            if whatsapp_mapping:
                self.owner_user = whatsapp_mapping.user
                self.tenant_profile = getattr(self.owner_user, "tenant", None) or getattr(self.owner_user, "tenant_profile", None)
            else:
                # Fallback: Scan tenants for voice number config
                # Note: This is a scan. acceptable for low volume.
                candidates = TenantProfile.query.filter(TenantProfile.is_active == True).all()
                for t in candidates:
                    if t.configuracion and t.configuracion.get("twilio_voice_number") == bot_phone_clean:
                        self.tenant_profile = t
                        self.owner_user = t.municipio or t.pyme # heuristic
                        break

            if not self.owner_user and not self.tenant_profile:
                logger.error(f"Tenant not found for bot phone: {bot_phone_clean}")
                return False

            # Ensure we have an owner_user for logic that depends on it
            if not self.owner_user and self.tenant_profile:
                 self.owner_user = self.tenant_profile.municipio or self.tenant_profile.pyme

            # Find/Create End User
            from services.pymes import get_or_create_user_by_phone
            # If we don't have owner_user, we can't easily create end user linked to them.
            # But we should have one by now.
            if self.owner_user:
                 self.user = get_or_create_user_by_phone(user_phone_clean, self.owner_user)
            else:
                 # Minimal user context
                 self.user = User(name="Vecino", email=f"{user_phone_clean}@voice.temp")

            # Session Context
            empresa_id = self.owner_user.id if self.owner_user else 0
            if call_sid and len(call_sid) <= 36:
                chat_session_id = call_sid
            else:
                raw_id = f"v_{empresa_id}_{user_phone_clean}"
                chat_session_id = hashlib.md5(raw_id.encode()).hexdigest()

            self.chat_session_id = chat_session_id

            # Ensure session exists
            session_context = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id).first()
            if not session_context:
                session_context = ChatSessionContext(
                    chat_session_id=chat_session_id,
                    user_id=empresa_id,
                    context_data={}
                )
                db.session.add(session_context)
                db.session.commit()

            return True
        except Exception as e:
            logger.error(f"Error resolving context: {e}")
            return False

    def _get_system_instruction(self):
        """
        Constructs the voice-first system prompt based on tenant type.
        """
        tenant_name = "Tu Municipio"
        if self.tenant_profile:
             tenant_name = self.tenant_profile.nombre
        elif self.owner_user:
             tenant_name = getattr(self.owner_user, "nombre_empresa", "Tu Municipio")

        user_name = getattr(self.user, "name", "Vecino")
        user_phone = getattr(self.user, "telefono", "")
        user_addr = getattr(self.user, "direccion", "")

        known_data_str = f"Datos conocidos del usuario: Nombre: {user_name}."
        if user_phone:
            known_data_str += f" Teléfono: {user_phone}."
        if user_addr:
            known_data_str += f" Dirección: {user_addr}."

        base_prompt = (
            f"Sos el asistente telefónico de {tenant_name}. "
            "Tu objetivo es resolver rápido y con claridad. "
            "Español argentino neutro: usá 'vos' pero sin jerga (no digas 'joya', 'che', 'dale'). "
            "Tono: amable, empático y profesional. "
            "Respuestas cortas: máximo 1 o 2 oraciones. "
            "Hacé preguntas directas y específicas. "
            f"{known_data_str} "
            "Si ya tenés estos datos, NO los preguntes de nuevo, solo confirmalos brevemente si es necesario para el trámite (ej: '¿Confirmás que es para la dirección X?'). "
            "Cuando tengas los datos mínimos, ejecutá la acción inmediatamente. "
            "Si el usuario confirma que ya está todo listo, o dice 'no', 'nada más', 'listo', 'perfecto', "
            "o muestra intención de terminar, FINALIZÁ la llamada usando la herramienta finalizar_llamada. "
            "Antes de cortar, repetí un resumen muy corto del reclamo/pedido y avisá que se envía por WhatsApp."
        )
        return base_prompt

    def run(self):
        """
        Main loop for the bi-directional stream.
        """
        if not OPENAI_API_KEY:
             logger.error("Missing OPENAI_API_KEY. Cannot start stream.")
             return

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

            # We use a simple strategy:
            # 1. Main thread reads from Twilio (Flask-Sock) and writes to OpenAI.
            # 2. A greenlet (Eventlet) reads from OpenAI and writes to Twilio.

            import eventlet

            def listen_openai():
                # Use self.app if available, else fallback to current_app (which fails in threads)
                app_ctx = self.app.app_context() if self.app else current_app.app_context()
                with app_ctx:
                    try:
                        while True:
                            msg = self.openai_ws.recv()
                            if not msg:
                                break
                            data = json.loads(msg)
                            self.handle_openai_message(data)
                    except Exception as e:
                        logger.error(f"OpenAI listener error: {e}")

            # Spawn OpenAI listener
            openai_thread = eventlet.spawn(listen_openai)

            # Main Loop (Twilio Listener)
            # Use self.app if available, else fallback to current_app (which fails in threads)
            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                while True:
                    try:
                        message = self.ws.receive()
                    except ConnectionClosed:
                        logger.info("Twilio WebSocket connection closed.")
                        break

                    if not message:
                        break

                    data = json.loads(message)
                    self.handle_twilio_message(data)

            # Cleanup
            openai_thread.kill()
            self.openai_ws.close()

        except Exception as e:
            logger.error(f"Stream error: {e}", exc_info=True)
            if self.openai_ws:
                self.openai_ws.close()

    def handle_twilio_message(self, data):
        event_type = data.get('event')

        if event_type == 'start':
            self.stream_sid = data['start']['streamSid']
            self.call_sid = data['start']['callSid']
            custom = data['start'].get('customParameters', {})
            self.from_number = custom.get('from_number')
            self.to_number = custom.get('to_number')

            logger.info(f"Stream started: {self.stream_sid} Call: {self.call_sid}")

            # Use self.app if available, else fallback to current_app (which fails in threads)
            app_ctx = self.app.app_context() if self.app else current_app.app_context()

            with app_ctx:
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
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.5,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 500,
                                "create_response": True,
                                "interrupt_response": True
                            },
                            "tools": self.tools
                        }
                    }
                     self.openai_ws.send(json.dumps(session_update))

                     # Initial greeting trigger
                     # Construct dynamic greeting
                     tenant_name = "tu municipio"
                     if self.tenant_profile:
                         tenant_name = self.tenant_profile.nombre
                     elif self.owner_user:
                         tenant_name = getattr(self.owner_user, "nombre_empresa", "tu municipio")

                     greeting_text = f"Hola, soy el asistente de {tenant_name}. Te llamo para ayudarte. ¿En qué puedo ser útil?"

                     self.openai_ws.send(json.dumps({
                        "type": "response.create",
                        "response": {
                            "modalities": ["text", "audio"],
                            "instructions": f"Saludá presentándote: '{greeting_text}'"
                        }
                    }))

        elif event_type == 'media':
            if self.openai_ws:
                self.openai_ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": data['media']['payload']
                }))

        elif event_type == 'clear':
            if self.openai_ws:
                self.openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))

    def handle_openai_message(self, data):
        msg_type = data.get('type')

        if msg_type == 'response.created':
            self.response_active = True

        elif msg_type == 'response.done':
            self.response_active = False

        elif msg_type == 'response.audio.delta':
            audio_payload = data.get('delta')
            if audio_payload:
                self.ws.send(json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": audio_payload}
                }))

        elif msg_type == 'input_audio_buffer.speech_started':
            self.ws.send(json.dumps({
                "event": "clear",
                "streamSid": self.stream_sid
            }))
            # Only cancel if there is an active response
            if self.response_active:
                self.openai_ws.send(json.dumps({"type": "response.cancel"}))

        elif msg_type == 'response.function_call_arguments.done':
            call_id = data.get('call_id')
            name = data.get('name')
            args = data.get('arguments')
            self.execute_tool(call_id, name, args)

    def execute_tool(self, call_id, name, args_str):
        logger.info(f"Executing tool: {name} args: {args_str}")
        try:
            args = json.loads(args_str)
            result = "Error executing tool"

            # Use self.app if available, else fallback to current_app (which fails in threads)
            app_ctx = self.app.app_context() if self.app else current_app.app_context()

            with app_ctx:
                # Reload context to avoid Detached Instance error
                session_context = ChatSessionContext.query.filter_by(chat_session_id=self.chat_session_id).first() if self.chat_session_id else None
                chat_data = session_context.context_data if session_context else {}

                if name == "crear_reclamo":
                    # AUTO-COMPLETE MISSING DATA
                    if self.user:
                         if not args.get("telefono") and self.user.telefono:
                             args["telefono"] = self.user.telefono
                         if not args.get("nombre") and self.user.name and self.user.name != "Vecino":
                             args["nombre"] = self.user.name
                         if not args.get("email") and self.user.email and "voice.temp" not in self.user.email:
                             args["email"] = self.user.email

                    from services.actions.municipio_actions import CrearReclamoActionHandler
                    ctx = {
                        "user_obj": self.owner_user,
                        "viewer_user_obj": self.user,
                        "channel": "voice",
                        "chat_db_context_data": chat_data
                    }
                    handler = CrearReclamoActionHandler(ctx)
                    res = handler.execute(args)
                    if res.get("success"):
                        data = res.get("data", {})
                        nro = data.get("nro_ticket", "N/A")
                        result = f"Reclamo creado con éxito. Número: {nro}."
                        if session_context:
                            session_context.context_data["latest_ticket_nro"] = nro
                            # Save awaiting photo flag to bridge with WhatsApp Webhook
                            session_context.context_data["awaiting_photo_for_ticket"] = nro
                            if data.get("ticket_id"):
                                session_context.context_data["latest_ticket_id"] = data.get("ticket_id")

                            safe_flag_modified(session_context, "context_data")
                            db.session.commit()

                        # Send WhatsApp Summary IMMEDIATELLY
                        if self.user and self.user.telefono:
                             try:
                                 messaging_service_sid = os.environ.get("MESSAGING_SERVICE_SID")
                                 from_ = messaging_service_sid if messaging_service_sid else os.environ.get("TWILIO_PHONE_NUMBER")
                                 if from_:
                                     msg_body = (
                                         f"✅ *Reclamo registrado*\n"
                                         f"📌 N°: *{nro}*\n"
                                         f"🧾 Categoría: {args.get('categoria', 'General')}\n"
                                         f"📝 {args.get('descripcion', '')}\n"
                                         f"📍 {args.get('ubicacion', '')}\n\n"
                                         f"📷 *Si tenés una foto, respondé a este mensaje con la imagen.*"
                                     )
                                     enviar_mensaje_whatsapp_con_fallback(
                                         self.user.telefono,
                                         msg_body,
                                         None, # image_url
                                         from_number=from_
                                     )
                             except Exception as ex:
                                 logger.warning(f"Could not send WhatsApp summary: {ex}")

                    else:
                        # Return the error message to the LLM so it can ask for missing info
                        result = res.get("message_body") or res.get("message_to_user") or "No se pudo crear el reclamo. Faltan datos."

                elif name == "crear_pedido":
                    from services.actions.pyme_order_actions import CrearPedidoAction
                    ctx = {
                        "user_obj": self.owner_user,
                        "viewer_user_obj": self.user,
                        "channel": "voice",
                        "cliente_id": self.user.id if self.user else None,
                        "chat_db_context_data": chat_data
                    }
                    handler = CrearPedidoAction(ctx)
                    res = handler.execute(args)

                    if res.get("success"):
                        data = res.get("data", {})
                        nro_pedido = data.get("nro_pedido", "N/A")
                        monto = data.get("monto_total", 0)
                        resumen = data.get("order_summary_text", "")

                        result = f"Pedido creado con éxito. Número: {nro_pedido}. Total: ${monto}."

                        if session_context:
                            session_context.context_data["latest_order_id"] = data.get("pedido_id")
                            session_context.context_data["latest_order_nro"] = nro_pedido
                            safe_flag_modified(session_context, "context_data")
                            db.session.commit()

                        # Send WhatsApp Summary IMMEDIATELY for Orders
                        if self.user and self.user.telefono:
                             try:
                                 messaging_service_sid = os.environ.get("MESSAGING_SERVICE_SID")
                                 from_ = messaging_service_sid if messaging_service_sid else os.environ.get("TWILIO_PHONE_NUMBER")
                                 if from_:
                                     msg_body = (
                                         f"✅ *Pedido registrado*\n"
                                         f"🆔 N°: *{nro_pedido}*\n"
                                         f"📦 *Resumen:*\n{resumen}\n\n"
                                         f"💰 *Total: ${monto:,.2f}*\n"
                                     )
                                     enviar_mensaje_whatsapp_con_fallback(
                                         self.user.telefono,
                                         msg_body,
                                         None, # image_url
                                         from_number=from_
                                     )
                             except Exception as ex:
                                 logger.warning(f"Could not send WhatsApp summary for order: {ex}")
                    else:
                        result = res.get("message_body") or res.get("message_to_user") or "No se pudo crear el pedido. Faltan datos."

                elif name == "consultar_producto":
                    from services.actions.pyme_order_actions import ConsultarProductoAction
                    ctx = {
                        "user_obj": self.owner_user,
                        "viewer_user_obj": self.user,
                        "channel": "voice",
                        "chat_db_context_data": chat_data
                    }
                    handler = ConsultarProductoAction(ctx)
                    # Map 'nombre' param to 'nombre_producto_mencionado' which the action expects
                    action_args = {"nombre_producto_mencionado": args.get("nombre")}
                    res = handler.execute(action_args)

                    if res.get("success"):
                        # The action returns a nice formatted message in 'message_to_user'
                        result = res.get("message_to_user")
                    else:
                        result = res.get("message_to_user") or "No encontré información sobre ese producto."

                elif name == "transferir_humano":
                     motivo = args.get("motivo", "General")
                     target_number = None

                     # Determine target number
                     if self.tenant_profile and self.tenant_profile.configuracion:
                         target_number = self.tenant_profile.configuracion.get("human_handoff_number")

                     if not target_number:
                         # Fallback hardcoded or generic
                         target_number = "+5492610000000" # Placeholder

                     result = "Transfiriendo con un agente..."

                     # Perform the transfer using Twilio API
                     if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and self.call_sid:
                         try:
                             client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                             # Generate the TwiML URL for transfer
                             # We need the full external URL
                             transfer_url = url_for('voice.voice_transfer', target=target_number, _external=True)

                             client.calls(self.call_sid).update(method="POST", url=transfer_url)
                             logger.info(f"Transferred call {self.call_sid} to {target_number}")
                         except Exception as exc:
                             logger.error(f"Failed to transfer call: {exc}")
                             result = "No pude transferir la llamada. Intente más tarde."

                elif name == "finalizar_llamada":
                    result = "Cortando llamada..."
                    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and self.call_sid:
                         try:
                             client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                             client.calls(self.call_sid).update(status="completed")
                             logger.info(f"Ended call {self.call_sid}")
                         except Exception as exc:
                             logger.error(f"Failed to end call: {exc}")

            # Send output back to OpenAI
            self.openai_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result
                }
            }))
            self.openai_ws.send(json.dumps({"type": "response.create"}))

        except Exception as e:
            logger.error(f"Tool execution failed: {e}")
