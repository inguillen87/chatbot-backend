import os
import json
import logging
import hashlib
import re

from flask import current_app, url_for
from websockets.sync.client import connect as ws_connect
from simple_websocket.errors import ConnectionClosed
from twilio.rest import Client as TwilioClient

from models import WhatsappNumero, ChatSessionContext, User, TenantProfile
from extensions import db
from sqlalchemy.orm import joinedload

from utils.db_utils import safe_flag_modified
from utils.whatsapp import enviar_mensaje_whatsapp_con_fallback

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# WhatsApp (para resumen post-llamada)
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")
MESSAGING_SERVICE_SID = os.environ.get("MESSAGING_SERVICE_SID")


class VoiceStreamService:
    def __init__(self, ws, app=None):
        self.ws = ws  # Flask-Sock WebSocket (Twilio -> backend)
        self.app = app
        self.openai_ws = None

        self.stream_sid = None
        self.call_sid = None
        self.from_number = None
        self.to_number = None

        self.user = None
        self.owner_user = None
        self.tenant_profile = None

        self.user_id = None
        self.owner_user_id = None

        self.chat_session_id = None

        # Flags de control
        self.pending_end_call = False
        self.last_ticket_nro = None
        self.last_order_nro = None
        self.response_active = False
        self.response_id = None
        self.cancel_pending = False

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
                    },
                    "required": ["categoria", "descripcion", "ubicacion"],
                },
            },
            {
                "type": "function",
                "name": "crear_pedido",
                "description": "Registra un nuevo pedido de venta.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "string", "description": "Productos y cantidades"},
                        "direccion_entrega": {"type": "string", "description": "Dirección de entrega (si aplica)"},
                    },
                    "required": ["items"],
                },
            },
            {
                "type": "function",
                "name": "transferir_humano",
                "description": "Transfiere la llamada a un agente humano.",
                "parameters": {
                    "type": "object",
                    "properties": {"motivo": {"type": "string"}},
                    "required": ["motivo"],
                },
            },
            {
                "type": "function",
                "name": "finalizar_llamada",
                "description": "Corta la llamada telefónica.",
                "parameters": {"type": "object", "properties": {}, "required": []},
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
                    "required": ["nombre"],
                },
            },
        ]

    # ----------------------------
    # Helpers
    # ----------------------------
    def _normalize_phone(self, n: str) -> str:
        if not n:
            return ""
        return str(n).replace("whatsapp:", "").strip()

    def _safe_end_call_twilio(self):
        """Corta la llamada usando la API de Twilio (más confiable que esperar al LLM)."""
        if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and self.call_sid):
            return
        try:
            client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.calls(self.call_sid).update(status="completed")
            logger.info(f"[VOICE] Ended call {self.call_sid}")
        except Exception as exc:
            logger.error(f"[VOICE] Failed to end call: {exc}")

    def _resolve_context(self, from_number, to_number, call_sid):
        """
        Resuelve Tenant, owner_user y user final.
        """
        try:
            user_phone_clean = self._normalize_phone(from_number)
            bot_phone_clean = self._normalize_phone(to_number)

            self.tenant_profile = None
            self.owner_user = None
            self.user = None

            # 1) Legacy: WhatsappNumero mapping (si existe)
            whatsapp_mapping = (
                WhatsappNumero.query.options(joinedload(WhatsappNumero.user).joinedload(User.rubro))
                .filter(WhatsappNumero.numero_whatsapp.ilike(f"%{bot_phone_clean.replace('+','').replace(' ','')}%"))
                .first()
            )

            if whatsapp_mapping:
                self.owner_user = whatsapp_mapping.user
                self.tenant_profile = getattr(self.owner_user, "tenant", None) or getattr(
                    self.owner_user, "tenant_profile", None
                )
            else:
                # 2) Buscar por TenantProfile.configuracion.twilio_voice_number
                candidates = TenantProfile.query.filter(TenantProfile.is_active == True).all()
                for t in candidates:
                    if t.configuracion and t.configuracion.get("twilio_voice_number") == bot_phone_clean:
                        self.tenant_profile = t
                        self.owner_user = t.municipio or t.pyme
                        break

            if not self.owner_user and not self.tenant_profile:
                logger.error(f"[VOICE] Tenant not found for bot phone: {bot_phone_clean}")
                return False

            if not self.owner_user and self.tenant_profile:
                self.owner_user = self.tenant_profile.municipio or self.tenant_profile.pyme

            # 3) User final
            from services.pymes import get_or_create_user_by_phone
            if self.owner_user:
                self.user = get_or_create_user_by_phone(user_phone_clean, self.owner_user)
            else:
                self.user = User(name="Vecino", email=f"{user_phone_clean}@voice.temp")
            self.user_id = getattr(self.user, "id", None) if self.user else None
            self.owner_user_id = getattr(self.owner_user, "id", None) if self.owner_user else None

            # 4) Session ID
            empresa_id = self.owner_user.id if self.owner_user else 0
            if call_sid and len(call_sid) <= 36:
                chat_session_id = call_sid
            else:
                raw_id = f"v_{empresa_id}_{user_phone_clean}"
                chat_session_id = hashlib.md5(raw_id.encode()).hexdigest()

            self.chat_session_id = chat_session_id

            # 5) Create/ensure session context
            session_context = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id).first()
            if not session_context:
                session_context = ChatSessionContext(
                    chat_session_id=chat_session_id,
                    user_id=empresa_id,
                    context_data={},
                )
                db.session.add(session_context)
                db.session.commit()

            return True
        except Exception as e:
            logger.error(f"[VOICE] Error resolving context: {e}", exc_info=True)
            return False

    def _get_system_instruction(self):
        """
        Prompt de voz: corto, directo, SIN alucinación y orientado a acción.
        """
        tenant_name = "Tu Municipio"
        tenant_tipo = "municipio"

        if self.tenant_profile:
            tenant_name = self.tenant_profile.nombre
            tenant_tipo = self.tenant_profile.tipo or tenant_tipo
        elif self.owner_user:
            tenant_name = getattr(self.owner_user, "nombre_empresa", "Tu Municipio")

        user_name = getattr(self.user, "name", "Vecino")
        user_addr = getattr(self.user, "direccion", "")

        known_data_str = f"Datos conocidos del usuario: Nombre: {user_name}."
        if user_addr:
            known_data_str += f" Dirección guardada: {user_addr}."

        prompt = (
            f"Sos el asistente telefónico de {tenant_name}. "
            "Hablas en español argentino neutro (usá 'vos', sin jerga). "
            "Tono: amable, empático y profesional. "
            "Respuestas MUY cortas: 1 o 2 oraciones. "
            "Objetivo: resolver rápido. "
            f"{known_data_str} "
            "Regla CRÍTICA: NUNCA inventes tickets, números o confirmaciones. "
            "Solo confirmás ticket/pedido cuando la herramienta devuelve el número. "
            "Regla: si falta un dato (ubicación/categoría/descr), preguntalo directo. "
            "Cuando tengas lo mínimo, ejecutá la herramienta correspondiente. "
            "Al finalizar, confirmá lo registrado y avisá que se envía un resumen por WhatsApp para adjuntar fotos. "
            "Si el usuario confirma que ya está todo listo o dice 'no', 'nada más', 'listo' o 'perfecto', "
            "resumí en una frase lo registrado, avisá que se envía por WhatsApp y ejecutá finalizar_llamada."
        )

        # Si podés detectar tipo tenant: municipio vs pyme
        # (si no, el modelo decide por intención)
        if tenant_tipo == "pyme":
            prompt += " Si la intención es compra, registrá un pedido. Si es consulta general, respondé breve."

        return prompt

    # ----------------------------
    # Main loop
    # ----------------------------
    def run(self):
        if not OPENAI_API_KEY:
            logger.error("[VOICE] Missing OPENAI_API_KEY. Cannot start stream.")
            return

        try:
            self.openai_ws = ws_connect(
                OPENAI_REALTIME_URL,
                additional_headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "OpenAI-Beta": "realtime=v1",
                },
            )
            logger.info("[VOICE] Connected to OpenAI Realtime API")

            import eventlet

            def listen_openai():
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
                        logger.error(f"[VOICE] OpenAI listener error: {e}", exc_info=True)

            openai_thread = eventlet.spawn(listen_openai)

            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                while True:
                    try:
                        message = self.ws.receive()
                    except ConnectionClosed:
                        logger.info("[VOICE] Twilio WebSocket connection closed.")
                        break

                    if not message:
                        break

                    data = json.loads(message)
                    self.handle_twilio_message(data)

            openai_thread.kill()
            self.openai_ws.close()

        except Exception as e:
            logger.error(f"[VOICE] Stream error: {e}", exc_info=True)
            if self.openai_ws:
                self.openai_ws.close()

    # ----------------------------
    # Twilio -> OpenAI
    # ----------------------------
    def handle_twilio_message(self, data):
        event_type = data.get("event")

        if event_type == "start":
            self.stream_sid = data["start"]["streamSid"]
            self.call_sid = data["start"]["callSid"]
            custom = data["start"].get("customParameters", {})

            # Robusto: Twilio a veces manda From/To directos, o por custom params
            self.from_number = custom.get("from_number") or data["start"].get("from") or data["start"].get("From")
            self.to_number = custom.get("to_number") or data["start"].get("to") or data["start"].get("To")

            logger.info(f"[VOICE] Stream started: {self.stream_sid} Call: {self.call_sid}")

            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                if self._resolve_context(self.from_number, self.to_number, self.call_sid):
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
                                "interrupt_response": True,
                            },
                            "tools": self.tools,
                        },
                    }
                    self.openai_ws.send(json.dumps(session_update))

                    tenant_name = "tu municipio"
                    if self.tenant_profile:
                        tenant_name = self.tenant_profile.nombre
                    elif self.owner_user:
                        tenant_name = getattr(self.owner_user, "nombre_empresa", "tu municipio")

                    greeting_text = f"Hola, soy el asistente de {tenant_name}. ¿En qué te puedo ayudar hoy?"
                    self.openai_ws.send(
                        json.dumps(
                            {
                                "type": "response.create",
                                "response": {
                                    "modalities": ["text", "audio"],
                                    "instructions": greeting_text,
                                },
                            }
                        )
                    )
        elif event_type == "media":
            if self.openai_ws:
                self.openai_ws.send(
                    json.dumps({"type": "input_audio_buffer.append", "audio": data["media"]["payload"]})
                )

        elif event_type == "clear":
            if self.openai_ws:
                self.openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))

    # ----------------------------
    # OpenAI -> Twilio
    # ----------------------------
    def handle_openai_message(self, data):
        msg_type = data.get("type")

        if msg_type == "response.audio.delta":
            audio_payload = data.get("delta")
            if audio_payload:
                self.ws.send(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": self.stream_sid,
                            "media": {"payload": audio_payload},
                        }
                    )
                )

        elif msg_type == "response.created":
            self.response_active = True
            response_payload = data.get("response") or {}
            self.response_id = (
                data.get("response_id")
                or response_payload.get("id")
                or data.get("id")
            )
            self.cancel_pending = False
        elif msg_type in ("response.canceled", "response.cancelled", "response.failed"):
            self.response_active = False
            self.response_id = None
            self.cancel_pending = False

        elif msg_type == "input_audio_buffer.speech_started":
            # Interrupción real-time
            self.ws.send(json.dumps({"event": "clear", "streamSid": self.stream_sid}))
            if self.response_active and not self.cancel_pending:
                cancel_payload = {"type": "response.cancel"}
                if self.response_id:
                    cancel_payload["response_id"] = self.response_id
                self.openai_ws.send(json.dumps(cancel_payload))
                self.response_active = False
                self.cancel_pending = True

        elif msg_type == "response.function_call_arguments.done":
            call_id = data.get("call_id")
            name = data.get("name")
            args = data.get("arguments")
            self.execute_tool(call_id, name, args)

        # ✅ IMPORTANTÍSIMO: cuando el modelo termina de hablar, si ya registramos ticket/pedido -> cortamos la llamada
        elif msg_type in ("response.done", "response.completed"):
            self.response_active = False
            self.response_id = None
            self.cancel_pending = False
            if self.pending_end_call:
                self.pending_end_call = False
                self._safe_end_call_twilio()

        elif msg_type == "error":
            error_info = data.get("error", {})
            if error_info.get("code") == "response_cancel_not_active":
                logger.warning(f"[VOICE] OpenAI warning: {data}")
                self.response_active = False
                self.response_id = None
                self.cancel_pending = False
                return
            logger.error(f"[VOICE] OpenAI error: {data}")

    # ----------------------------
    # Tools executor
    # ----------------------------
    def execute_tool(self, call_id, name, args_str):
        logger.info(f"[VOICE] Executing tool: {name} args: {args_str}")
        try:
            args = json.loads(args_str) if args_str else {}
            result = "No se pudo procesar la acción."

            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                if self.owner_user_id:
                    self.owner_user = db.session.get(User, self.owner_user_id)
                if self.user_id:
                    self.user = db.session.get(User, self.user_id)

                session_context = (
                    ChatSessionContext.query.filter_by(chat_session_id=self.chat_session_id).first()
                    if self.chat_session_id
                    else None
                )
                chat_data = session_context.context_data if session_context else {}

                # ----------------------------
                # MUNICIPIO: Reclamo
                # ----------------------------
                if name == "crear_reclamo":
                    from services.actions.municipio_actions import CrearReclamoActionHandler

                    ctx = {
                        "user_obj": self.owner_user,
                        "viewer_user_obj": self.user,
                        "channel": "voice",
                        "chat_db_context_data": chat_data,
                    }

                    nombre_raw = args.get("nombre")
                    if isinstance(nombre_raw, str):
                        words = re.findall(r"[a-záéíóúñ]+", nombre_raw.lower())
                        if words and all(word in {"hola", "buenas", "buenos"} for word in words):
                            args.pop("nombre", None)

                    email_raw = args.get("email")
                    if isinstance(email_raw, str) and email_raw.endswith("@whatsapp.chatboc.com"):
                        args.pop("email", None)

                    if self.user:
                        args.setdefault("telefono", getattr(self.user, "telefono", None))
                        args.setdefault("nombre", getattr(self.user, "name", None) or getattr(self.user, "nombre", None))
                        args.setdefault("email", getattr(self.user, "email", None))

                    handler = CrearReclamoActionHandler(ctx)
                    res = handler.execute(args)

                    if res.get("success"):
                        data = res.get("data", {})
                        nro = data.get("nro_ticket")
                        self.last_ticket_nro = nro

                        result = (
                            f"Listo. Registré tu reclamo. Tu número de ticket es {nro}. "
                            "Te envío un resumen por WhatsApp para que puedas adjuntar una foto si querés. "
                            "Gracias, hasta luego."
                        )

                        if session_context:
                            session_context.context_data["latest_ticket_nro"] = nro
                            session_context.context_data["awaiting_photo_for_ticket"] = nro
                            if data.get("ticket_id"):
                                session_context.context_data["latest_ticket_id"] = data.get("ticket_id")

                            safe_flag_modified(session_context, "context_data")
                            db.session.commit()

                        # ✅ Enviar resumen por WhatsApp (bien armado)
                        whatsapp_target = None
                        if self.user and getattr(self.user, "telefono", None):
                            whatsapp_target = self.user.telefono
                        elif self.from_number:
                            whatsapp_target = self._normalize_phone(self.from_number)
                        if whatsapp_target:
                            try:
                                msg_body = (
                                    f"✅ *Reclamo registrado*\n"
                                    f"📌 N°: *{nro}*\n"
                                    f"🧾 Categoría: {args.get('categoria', 'General')}\n"
                                    f"📝 {args.get('descripcion', '')}\n"
                                    f"📍 {args.get('ubicacion', '')}\n\n"
                                    f"📷 *Si tenés una foto, respondé a este mensaje con la imagen.*"
                                )

                                enviar_mensaje_whatsapp_con_fallback(
                                    whatsapp_target,
                                    msg_body,
                                    image_url=None,
                                    from_number=TWILIO_WHATSAPP_NUMBER,
                                    messaging_service_sid=MESSAGING_SERVICE_SID,
                                )
                            except Exception as ex:
                                logger.warning(f"[VOICE] Could not send WhatsApp summary: {ex}")

                        # ✅ Marca que hay que cortar cuando termine de hablar
                        self.pending_end_call = True

                    else:
                        # Esto hace que el modelo pregunte lo que falta (sin inventar)
                        result = (
                            res.get("message_body")
                            or res.get("message_to_user")
                            or "No pude registrar el reclamo. ¿Me repetís la dirección y qué pasó?"
                        )

                # ----------------------------
                # PYME: Pedido
                # ----------------------------
                elif name == "crear_pedido":
                    from services.actions.pyme_order_actions import CrearPedidoAction

                    ctx = {
                        "user_obj": self.owner_user,
                        "viewer_user_obj": self.user,
                        "channel": "voice",
                        "cliente_id": self.user.id if self.user else None,
                        "chat_db_context_data": chat_data,
                    }

                    nombre_raw = args.get("nombre_usuario_detectado")
                    if isinstance(nombre_raw, str):
                        words = re.findall(r"[a-záéíóúñ]+", nombre_raw.lower())
                        if words and all(word in {"hola", "buenas", "buenos"} for word in words):
                            args.pop("nombre_usuario_detectado", None)

                    email_raw = args.get("email_detectado")
                    if isinstance(email_raw, str) and email_raw.endswith("@whatsapp.chatboc.com"):
                        args.pop("email_detectado", None)

                    if self.user:
                        args.setdefault("telefono_detectado", getattr(self.user, "telefono", None))
                        args.setdefault(
                            "nombre_usuario_detectado",
                            getattr(self.user, "name", None) or getattr(self.user, "nombre", None),
                        )
                        args.setdefault("email_detectado", getattr(self.user, "email", None))
                        args.setdefault("direccion_entrega", getattr(self.user, "direccion", None))

                    handler = CrearPedidoAction(ctx)
                    res = handler.execute(args)

                    if res.get("success"):
                        data = res.get("data", {})
                        nro_pedido = data.get("nro_pedido")
                        monto = data.get("monto_total", 0)
                        resumen = data.get("order_summary_text", "")

                        self.last_order_nro = nro_pedido

                        result = (
                            f"Perfecto. Registré tu pedido número {nro_pedido}. "
                            f"El total es {monto}. Te mando el resumen por WhatsApp. Gracias, hasta luego."
                        )

                        if session_context:
                            session_context.context_data["latest_order_id"] = data.get("pedido_id")
                            session_context.context_data["latest_order_nro"] = nro_pedido
                            safe_flag_modified(session_context, "context_data")
                            db.session.commit()

                        whatsapp_target = None
                        if self.user and getattr(self.user, "telefono", None):
                            whatsapp_target = self.user.telefono
                        elif self.from_number:
                            whatsapp_target = self._normalize_phone(self.from_number)
                        if whatsapp_target:
                            try:
                                msg_body = (
                                    f"✅ *Pedido registrado*\n"
                                    f"🆔 N°: *{nro_pedido}*\n"
                                    f"📦 *Resumen:*\n{resumen}\n\n"
                                    f"💰 *Total: {monto:,.2f}*\n"
                                )

                                enviar_mensaje_whatsapp_con_fallback(
                                    whatsapp_target,
                                    msg_body,
                                    image_url=None,
                                    from_number=TWILIO_WHATSAPP_NUMBER,
                                    messaging_service_sid=MESSAGING_SERVICE_SID,
                                )
                            except Exception as ex:
                                logger.warning(f"[VOICE] Could not send WhatsApp summary for order: {ex}")

                        self.pending_end_call = True

                    else:
                        result = (
                            res.get("message_body")
                            or res.get("message_to_user")
                            or "No pude registrar el pedido. ¿Qué productos querés y cuántos?"
                        )

                # ----------------------------
                # Consultar producto
                # ----------------------------
                elif name == "consultar_producto":
                    from services.actions.pyme_order_actions import ConsultarProductoAction

                    ctx = {
                        "user_obj": self.owner_user,
                        "viewer_user_obj": self.user,
                        "channel": "voice",
                        "chat_db_context_data": chat_data,
                    }

                    handler = ConsultarProductoAction(ctx)
                    action_args = {"nombre_producto_mencionado": args.get("nombre")}
                    res = handler.execute(action_args)

                    result = res.get("message_to_user") or "No encontré información sobre ese producto."

                # ----------------------------
                # Transferir humano
                # ----------------------------
                elif name == "transferir_humano":
                    motivo = args.get("motivo", "General")
                    target_number = None

                    if self.tenant_profile and self.tenant_profile.configuracion:
                        target_number = self.tenant_profile.configuracion.get("human_handoff_number")

                    if not target_number:
                        target_number = "+5492610000000"

                    result = "Perfecto. Te transfiero con un agente."

                    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and self.call_sid:
                        try:
                            client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                            transfer_url = url_for("voice.voice_transfer", target=target_number, _external=True)
                            client.calls(self.call_sid).update(method="POST", url=transfer_url)
                            logger.info(f"[VOICE] Transferred call {self.call_sid} to {target_number}")
                        except Exception as exc:
                            logger.error(f"[VOICE] Failed to transfer call: {exc}")
                            result = "No pude transferir la llamada. Probemos de nuevo en un minuto."

                # ----------------------------
                # Finalizar llamada
                # ----------------------------
                elif name == "finalizar_llamada":
                    result = "Perfecto. Gracias, hasta luego."
                    self.pending_end_call = True

            # Return tool output to OpenAI
            self.openai_ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": result,
                        },
                    }
                )
            )
            self.openai_ws.send(json.dumps({"type": "response.create"}))

        except Exception as e:
            logger.error(f"[VOICE] Tool execution failed: {e}", exc_info=True)
