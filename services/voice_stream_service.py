import os
import json
import time
import logging
import re

from flask import current_app
from websockets.sync.client import connect as ws_connect
from simple_websocket.errors import ConnectionClosed
from twilio.rest import Client as TwilioClient

from models import WhatsappNumero, ChatSessionContext, User, TenantProfile, MunicipioTicket, PymeTicket
from extensions import db
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from utils.db_utils import safe_flag_modified
from services.contact_service import resolve_contact, sanitize_profile_name
from services.whatsapp_receipts import render_ticket_whatsapp
from services.whatsapp_sender import send_whatsapp_message
from services.config_loader import cargar_configuracion_municipio
from services.voice_session_service import resolve_voice_chat_session_id
from services.realtime_voice_profiles import (
    build_multilingual_translation_policy,
    build_realtime_voice_instructions,
    build_realtime_voice_tools,
    infer_realtime_voice_vertical,
    resolve_realtime_model,
    resolve_realtime_voice,
)

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
# Solo para el canal de voz realtime (Twilio <-> OpenAI Realtime).
# No impacta los modelos de chat estándar del bot.
OPENAI_REALTIME_MODEL = resolve_realtime_model(app_config=os.environ)
OPENAI_REALTIME_URL = os.environ.get(
    "OPENAI_REALTIME_WS_URL",
    f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}",
)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# WhatsApp (para resumen post-llamada)


def _openai_realtime_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPENAI_API_KEY}"}


def _realtime_audio_format(value: str | dict | None, *, default: str = "g711_ulaw") -> dict:
    if isinstance(value, dict):
        return value
    normalized = str(value or default).strip().lower().replace("-", "_")
    if normalized in {"g711_ulaw", "ulaw", "pcmu", "mulaw", "audio/pcmu"}:
        return {"type": "audio/pcmu"}
    if normalized in {"g711_alaw", "alaw", "pcma", "audio/pcma"}:
        return {"type": "audio/pcma"}
    return {"type": "audio/pcm", "rate": 24000}


class VoiceStreamService:
    def __init__(self, ws, app=None):
        self.ws = ws  # Flask-Sock WebSocket (Twilio -> backend)
        self.app = app
        self.openai_ws = None

        self.stream_sid = None
        self.call_sid = None
        self.from_number = None
        self.to_number = None
        self.source_chat_session_id = None

        self.user = None
        self.owner_user = None
        self.tenant_profile = None
        self.whatsapp_sender = None

        self.user_id = None
        self.owner_user_id = None

        self.chat_session_id = None
        self.context_data_snapshot = {}

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
                    "required": ["descripcion", "ubicacion"],
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
        self.voice_vertical = "general"
        self.tools = build_realtime_voice_tools(self.voice_vertical)

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

    @staticmethod
    def _resolve_promo_image_url(config: dict | None) -> str | None:
        if not isinstance(config, dict):
            return None
        promo_image_url = config.get("promo_image_url")
        if promo_image_url:
            return promo_image_url
        promo_section = config.get("promo_section") or config.get("promo")
        if isinstance(promo_section, dict):
            return promo_section.get("image_url")
        return None

    def _resolve_municipio_config(self) -> dict:
        if self.tenant_profile and isinstance(getattr(self.tenant_profile, "configuracion", None), dict):
            return self.tenant_profile.configuracion or {}
        municipio_id = None
        if self.tenant_profile and getattr(self.tenant_profile, "municipio_id", None):
            municipio_id = self.tenant_profile.municipio_id
        elif self.owner_user and getattr(self.owner_user, "municipio_id", None):
            municipio_id = self.owner_user.municipio_id
        if municipio_id:
            config = cargar_configuracion_municipio(str(municipio_id), "config.json")
            if isinstance(config, dict):
                return config
        return {}

    def _resolve_voice_config(self) -> dict:
        if self.tenant_profile and isinstance(getattr(self.tenant_profile, "configuracion", None), dict):
            return self.tenant_profile.configuracion or {}
        return self._resolve_municipio_config()

    def _resolve_tenant_name(self) -> str:
        config = self._resolve_municipio_config()
        tenant_name = (
            config.get("nombre_municipio")
            or config.get("nombre")
            or (self.tenant_profile.nombre if self.tenant_profile else None)
            or (getattr(self.owner_user, "nombre_empresa", None) if self.owner_user else None)
            or "tu municipio"
        )
        if tenant_name.lower() in {"municipio inteligente", "municipio"}:
            return "Municipio"
        return tenant_name

    def _resolve_whatsapp_sender(self) -> str | None:
        if self.tenant_profile and getattr(self.tenant_profile, "configuracion", None):
            config = self.tenant_profile.configuracion or {}
            sender = (
                config.get("whatsapp_sender_id")
                or config.get("whatsapp_number")
                or config.get("whatsapp_sender")
            )
            if sender:
                return str(sender)

        if self.whatsapp_sender:
            return str(self.whatsapp_sender)

        return None

    def _extract_contacto_usuario(self, context_data: dict) -> dict:
        if not isinstance(context_data, dict):
            return {}

        for key in ("contexto_municipio_v2", "contexto_pyme_v2"):
            ctx = context_data.get(key) or {}
            if isinstance(ctx, dict):
                contacto = ctx.get("contacto_usuario") or {}
                if isinstance(contacto, dict) and contacto:
                    return contacto

        return {}

    def _resolve_identity_from_context(self, context_data: dict) -> dict:
        contacto = self._extract_contacto_usuario(context_data)
        profile_name = context_data.get("profile_name") if isinstance(context_data, dict) else None
        resolved_contact = context_data.get("resolved_contact") if isinstance(context_data, dict) else None
        resolved_name = None
        if isinstance(resolved_contact, dict):
            resolved_name = resolved_contact.get("nombre")

        return {
            "nombre": contacto.get("nombre") or resolved_name or sanitize_profile_name(profile_name),
            "email": contacto.get("email"),
            "telefono": contacto.get("telefono"),
            "direccion": contacto.get("direccion"),
        }

    def _resolve_greeting_name(self) -> str | None:
        identity = self._resolve_identity_from_context(self.context_data_snapshot)
        resolved_contact = (
            self.context_data_snapshot.get("resolved_contact")
            if isinstance(self.context_data_snapshot, dict)
            else {}
        )
        resolved_name = (
            resolved_contact.get("nombre") if isinstance(resolved_contact, dict) else None
        )
        candidates = [
            resolved_name,
            getattr(self.user, "name", None),
            identity.get("nombre"),
        ]
        for candidate in candidates:
            sanitized = sanitize_profile_name(candidate)
            if sanitized:
                return sanitized
        return None

    def _update_session_contexts(self, session_context: ChatSessionContext, updates: dict) -> None:
        if not session_context or not updates:
            return

        if not isinstance(session_context.context_data, dict):
            session_context.context_data = {}

        session_context.context_data.update(updates)
        safe_flag_modified(session_context, "context_data")

        source_id = (
            self.source_chat_session_id
            or session_context.context_data.get("source_chat_session_id")
        )
        if source_id and source_id != session_context.chat_session_id:
            source_context = ChatSessionContext.query.filter_by(chat_session_id=source_id).first()
            if source_context:
                if not isinstance(source_context.context_data, dict):
                    source_context.context_data = {}
                source_context.context_data.update(updates)
                safe_flag_modified(source_context, "context_data")

        db.session.commit()

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
                self.whatsapp_sender = whatsapp_mapping.numero_whatsapp
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
            empresa_id = self.owner_user_id
            self.voice_vertical = infer_realtime_voice_vertical(
                self.tenant_profile,
                tenant_tipo=getattr(self.tenant_profile, "tipo", None) if self.tenant_profile else None,
            )
            self.tools = build_realtime_voice_tools(self.voice_vertical)

            # 4) Session ID
            chat_session_id = resolve_voice_chat_session_id(
                call_sid=call_sid,
                from_number=user_phone_clean,
                to_number=bot_phone_clean,
            )

            self.chat_session_id = chat_session_id

            # 5) Load source session (WhatsApp/web) if provided or available
            source_session = None
            source_chat_session_id = self.source_chat_session_id

            if source_chat_session_id:
                source_session = ChatSessionContext.query.filter_by(
                    chat_session_id=source_chat_session_id
                ).first()

            if not source_session and empresa_id and user_phone_clean:
                whatsapp_session_id = f"whatsapp_{empresa_id}_{user_phone_clean}"
                source_session = ChatSessionContext.query.filter_by(
                    chat_session_id=whatsapp_session_id
                ).first()
                if source_session:
                    source_chat_session_id = whatsapp_session_id

            self.source_chat_session_id = source_chat_session_id

            # 6) Create/ensure session context
            session_context = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id).first()
            if not session_context:
                context_data = {}
                if source_session and isinstance(source_session.context_data, dict):
                    context_data = dict(source_session.context_data)
                if source_chat_session_id:
                    context_data["source_chat_session_id"] = source_chat_session_id

                session_context = ChatSessionContext(
                    chat_session_id=chat_session_id,
                    user_id=empresa_id,
                    context_data=context_data,
                )
                db.session.add(session_context)
                db.session.commit()
            elif source_session and isinstance(source_session.context_data, dict):
                merged_context = dict(source_session.context_data)
                if isinstance(session_context.context_data, dict):
                    merged_context.update(session_context.context_data)
                if source_chat_session_id:
                    merged_context["source_chat_session_id"] = source_chat_session_id
                session_context.context_data = merged_context
                safe_flag_modified(session_context, "context_data")
                db.session.commit()

            self.context_data_snapshot = session_context.context_data or {}
            identity = self._resolve_identity_from_context(self.context_data_snapshot)
            resolved_contact = resolve_contact(
                user_phone_clean,
                self.context_data_snapshot.get("profile_name"),
            )
            if resolved_contact:
                self.context_data_snapshot["resolved_contact"] = resolved_contact
                if isinstance(session_context.context_data, dict):
                    session_context.context_data["resolved_contact"] = resolved_contact
                    if resolved_contact.get("nombre") and not session_context.context_data.get("profile_name"):
                        session_context.context_data["profile_name"] = resolved_contact.get("nombre")
                    safe_flag_modified(session_context, "context_data")
                    db.session.commit()
            if self.user:
                generic_names = {"vecino", "vecino/a", "cliente", "usuario"}
                user_name = getattr(self.user, "name", None)
                if not user_name or user_name.lower() in generic_names:
                    ticket_name = None
                    if self.owner_user and getattr(self.owner_user, "municipio_id", None):
                        ticket_match = (
                            MunicipioTicket.query.filter_by(
                                municipio_id=self.owner_user.municipio_id,
                                telefono_vecino=user_phone_clean,
                            )
                            .order_by(MunicipioTicket.fecha.desc())
                            .first()
                        )
                        if ticket_match and getattr(ticket_match, "nombre_vecino", None):
                            ticket_name = ticket_match.nombre_vecino

                    identity_name = (identity.get("nombre") or "").strip()
                    banned = {"hola", "buenas", "eh", "mmm", "hola hola"}
                    candidate_name = ticket_name or identity_name
                    if candidate_name and candidate_name.lower() not in banned:
                        self.user.name = candidate_name
                        db.session.add(self.user)
                        db.session.commit()
                if identity.get("direccion") and not getattr(self.user, "direccion", None):
                    self.user.direccion = identity["direccion"]

            return True
        except Exception as e:
            logger.error(f"[VOICE] Error resolving context: {e}", exc_info=True)
            return False

    def _get_system_instruction(self):
        """
        Prompt de voz: corto, directo, SIN alucinación y orientado a acción.
        """
        tenant_name_for_voice = self._resolve_tenant_name()
        identity_for_voice = self._resolve_identity_from_context(self.context_data_snapshot)
        user_name_for_voice = (
            sanitize_profile_name(getattr(self.user, "name", None))
            or identity_for_voice.get("nombre")
            or None
        )
        user_addr_for_voice = (
            getattr(self.user, "direccion", None)
            or identity_for_voice.get("direccion")
            or ""
        )
        self.voice_vertical = infer_realtime_voice_vertical(
            self.tenant_profile,
            tenant_tipo=getattr(self.tenant_profile, "tipo", None) if self.tenant_profile else None,
        )
        self.tools = build_realtime_voice_tools(self.voice_vertical)
        voice_cfg = self._resolve_voice_config()
        return build_realtime_voice_instructions(
            tenant_name=tenant_name_for_voice,
            vertical=self.voice_vertical,
            user_name=user_name_for_voice,
            user_address=user_addr_for_voice,
            translation_policy=build_multilingual_translation_policy(voice_cfg, current_app.config),
        )

        tenant_name = self._resolve_tenant_name()
        tenant_tipo = "municipio"

        if self.tenant_profile:
            tenant_tipo = self.tenant_profile.tipo or tenant_tipo

        identity = self._resolve_identity_from_context(self.context_data_snapshot)
        user_name = (
            sanitize_profile_name(getattr(self.user, "name", None))
            or identity.get("nombre")
            or "Vecino"
        )
        user_addr = getattr(self.user, "direccion", None) or identity.get("direccion") or ""

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
            "Regla PRIORITARIA: Si el nombre del usuario es 'Vecino' o desconocido, TU PRIMERA PRIORIDAD es decir: 'No tengo tu nombre agendado, ¿cómo te llamas?' "
            "Si ya conocés el nombre del usuario, saludalo usando su nombre. "
            "IMPORTANTE: No confundas saludos como 'Hola', 'Buenas', 'Hola hola' con el nombre del usuario. Si dice 'Hola', preguntá el nombre. "
            "Regla CRÍTICA: NUNCA inventes tickets, números o confirmaciones. "
            "Solo confirmás ticket/pedido cuando la herramienta devuelve el número. "
            "Si el usuario da varios datos en una sola frase (categoría, ubicación, descripción), separalos y NO vuelvas a pedir lo que ya dijo. "
            "DISTINGUISH CLEARLY: 'Don Bosco 55' is a location. 'Tree fallen' is a description. Never mix them in the tool arguments. "
            "SUMMARIZE the description for the tool. Do not send the full raw transcript. Ex: 'Árbol caído en garage'. "
            "Be empathetic and human: 'Uy, qué problema', 'Entiendo', 'Lo siento', 'Ya mismo lo dejo asentado'. "
            "Regla: si falta un dato (ubicación/categoría/descr), preguntalo directo. "
            "Si falta la categoría pero hay descripción suficiente, inferila sin preguntar. "
            "Si el usuario menciona esquina/cruce, incluí ambas calles (ej: 'Don Bosco y Sarmiento'). "
            "Cuando tengas lo mínimo, ejecutá la herramienta correspondiente. "
            "IMPORTANTE: Si el usuario corrige la dirección o descripción DESPUÉS de que ya creaste el ticket, NO vuelvas a llamar a crear_reclamo. "
            "Simplemente decile que tomaste nota de la corrección. "
            "Al finalizar, confirmá el número con una frase breve, por ejemplo: "
            "'Tu reclamo quedó cargado con el número [Nro]'. "
            "Avisá que se envió el comprobante por WhatsApp. "
            "Si el usuario confirma que ya está todo listo o dice 'no', 'nada más', 'listo' o 'perfecto', "
            "saludá y ejecutá finalizar_llamada."
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
                additional_headers=_openai_realtime_headers(),
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
            self.source_chat_session_id = custom.get("chat_session_id") or custom.get("source_chat_session_id")

            logger.info(f"[VOICE] Stream started: {self.stream_sid} Call: {self.call_sid}")

            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                if self._resolve_context(self.from_number, self.to_number, self.call_sid):
                    voice_cfg = self._resolve_voice_config()
                    voice_name = resolve_realtime_voice(voice_cfg, current_app.config)
                    self.voice_vertical = infer_realtime_voice_vertical(
                        self.tenant_profile,
                        tenant_tipo=getattr(self.tenant_profile, "tipo", None) if self.tenant_profile else None,
                    )
                    self.tools = build_realtime_voice_tools(self.voice_vertical)
                    session_update = {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "model": OPENAI_REALTIME_MODEL,
                            "output_modalities": ["audio"],
                            "instructions": self._get_system_instruction(),
                            "audio": {
                                "input": {
                                    "format": _realtime_audio_format(voice_cfg.get("openai_realtime_input_audio_format") or "g711_ulaw"),
                                    "noise_reduction": {"type": voice_cfg.get("openai_realtime_noise_reduction") or "near_field"},
                                    "turn_detection": {
                                        "type": voice_cfg.get("openai_realtime_turn_detection_type") or "semantic_vad",
                                        "eagerness": voice_cfg.get("openai_realtime_semantic_vad_eagerness") or "auto",
                                        "create_response": True,
                                        "interrupt_response": True,
                                    },
                                    "transcription": {
                                        "model": voice_cfg.get("openai_realtime_transcription_model") or "gpt-4o-mini-transcribe",
                                    },
                                },
                                "output": {
                                    "format": _realtime_audio_format(voice_cfg.get("openai_realtime_output_audio_format") or "g711_ulaw"),
                                    "voice": voice_name,
                                    "speed": float(voice_cfg.get("openai_realtime_voice_speed") or 1.0),
                                },
                            },
                            "tools": self.tools,
                            "tool_choice": "auto",
                        },
                    }
                    self.openai_ws.send(json.dumps(session_update))

                    tenant_name = self._resolve_tenant_name()

                    user_name = self._resolve_greeting_name()

                    if user_name:
                        greeting_line = (
                            f"Hola {user_name}. Te saluda el asistente de {tenant_name}. ¿En qué te ayudo?"
                        )
                    else:
                        greeting_line = (
                            f"Hola. Te saluda el asistente de {tenant_name}. "
                            "Quiero agendar tu nombre, ¿cómo te llamás?"
                        )

                    greeting_text = f"Decí exactamente: \"{greeting_line}\""

                    self.openai_ws.send(
                        json.dumps(
                            {
                                "type": "response.create",
                                "response": {
                                    "output_modalities": ["audio"],
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

        if msg_type == "response.created":
            self.response_active = True
            response_payload = data.get("response") or {}
            self.response_id = (
                data.get("response_id")
                or response_payload.get("id")
                or data.get("id")
            )
            self.cancel_pending = False
            return

        if msg_type in ("response.canceled", "response.cancelled", "response.failed"):
            self.response_active = False
            self.response_id = None
            self.cancel_pending = False
            return

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
            self.response_active = True

        elif msg_type == "response.created":
            self.response_active = True

        elif msg_type == "response.created":
            self.response_active = True
        elif msg_type in ("response.canceled", "response.cancelled", "response.failed"):
            self.response_active = False

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
            if data.get("error", {}).get("code") == "response_cancel_not_active":
                self.response_active = False

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
                # COLEGIO: Caso escolar
                # ----------------------------
                if name == "crear_caso_escolar":
                    from services.education_case_service import (
                        create_school_case_alias_for_ticket,
                        school_case_alias_payload,
                    )
                    from services.education_contracts import education_case_taxonomy, fold_text, is_education_tenant

                    if not is_education_tenant(self.tenant_profile):
                        result = "Este canal no esta configurado como colegio. Te derivo con un agente para ayudarte."
                    else:
                        taxonomy = {item["key"]: item for item in education_case_taxonomy()}
                        raw_case_type = args.get("case_type") or args.get("categoria") or "secretaria"
                        case_type = fold_text(raw_case_type).replace(" ", "_")
                        if case_type not in taxonomy:
                            case_type = "secretaria"

                        descripcion = str(
                            args.get("descripcion")
                            or args.get("detalle")
                            or args.get("consulta")
                            or ""
                        ).strip()
                        if not descripcion:
                            descripcion = "Consulta escolar recibida por llamada."
                        asunto = str(
                            args.get("asunto")
                            or taxonomy.get(case_type, {}).get("label")
                            or "Consulta escolar"
                        ).strip()

                        extra = {
                            "source": "voice_realtime",
                            "case_type": case_type,
                            "alumno": args.get("alumno"),
                            "curso": args.get("curso"),
                            "fecha": args.get("fecha"),
                            "ubicacion": args.get("ubicacion"),
                            "call_sid": self.call_sid,
                            "from_number": self._normalize_phone(self.from_number),
                        }
                        phone = getattr(self.user, "telefono", None) or self._normalize_phone(self.from_number)
                        email = getattr(self.user, "email", None) if self.user else None
                        ticket_type = "pyme" if getattr(self.tenant_profile, "pyme_id", None) else "municipio"

                        if ticket_type == "pyme":
                            max_nro = db.session.query(func.max(PymeTicket.nro_ticket)).scalar() or 0
                            ticket = PymeTicket(
                                tenant_id=getattr(self.tenant_profile, "id", None),
                                nro_ticket=int(max_nro or 0) + 1,
                                pregunta=descripcion,
                                asunto=asunto,
                                categoria=f"educacion:{case_type}",
                                user_id=getattr(self.user, "id", None),
                                telefono=phone,
                                email=email,
                                direccion=args.get("ubicacion"),
                            )
                        else:
                            ticket = MunicipioTicket(
                                tenant_id=getattr(self.tenant_profile, "id", None),
                                municipio_id=getattr(self.tenant_profile, "municipio_id", None),
                                pregunta=descripcion,
                                asunto=asunto,
                                categoria=f"educacion:{case_type}",
                                user_id=getattr(self.user, "id", None),
                                direccion=args.get("ubicacion"),
                                nombre_vecino=sanitize_profile_name(getattr(self.user, "name", None)),
                                telefono_vecino=phone,
                                email_vecino=email,
                                canal_ingreso="voice",
                            )

                        if hasattr(ticket, "detalles"):
                            ticket.detalles = json.dumps(extra, ensure_ascii=False)
                        db.session.add(ticket)
                        db.session.flush()

                        alias = create_school_case_alias_for_ticket(
                            tenant_profile=self.tenant_profile,
                            ticket_type=ticket_type,
                            ticket_id=ticket.id,
                            case_type=case_type,
                            channel="voice",
                            end_user=self.user,
                            phone=phone,
                            sensitivity_level=args.get("sensitivity_level") or taxonomy.get(case_type, {}).get("sensitivity_level"),
                        )
                        if not alias:
                            db.session.commit()

                        school_case = school_case_alias_payload(alias)
                        case_number = (school_case or {}).get("school_case_id") or getattr(ticket, "nro_ticket", ticket.id)
                        result = (
                            f"Listo. Deje registrada la consulta escolar con seguimiento #{case_number}. "
                            "Te envio el resumen por WhatsApp y el colegio podra continuar desde el panel."
                        )
                        self.last_ticket_nro = str(case_number)

                        if session_context:
                            self._update_session_contexts(
                                session_context,
                                {
                                    "latest_school_case": school_case,
                                    "latest_ticket_id": ticket.id,
                                    "latest_ticket_type": ticket_type,
                                    "latest_ticket_nro": getattr(ticket, "nro_ticket", None),
                                    "receipt_sent": True,
                                },
                            )

                        whatsapp_target = phone
                        if whatsapp_target:
                            try:
                                body = (
                                    f"Resumen de llamada - {self._resolve_tenant_name()}\n\n"
                                    f"Caso escolar: #{case_number}\n"
                                    f"Area: {taxonomy.get(case_type, {}).get('label', case_type)}\n"
                                    f"Detalle: {descripcion}\n\n"
                                    "Podes responder este WhatsApp con imagen, audio, ubicacion o archivo si queres sumar informacion."
                                )
                                send_whatsapp_message(
                                    whatsapp_target,
                                    body,
                                    from_number=self._resolve_whatsapp_sender(),
                                )
                            except Exception as ex:
                                logger.warning(f"[VOICE] Could not send school case WhatsApp summary: {ex}")

                # ----------------------------
                # MUNICIPIO: Reclamo
                # ----------------------------
                elif name == "crear_reclamo":
                    from services.actions.municipio_actions import CrearReclamoActionHandler

                    ctx = {
                        "user_obj": self.owner_user,
                        "viewer_user_obj": self.user,
                        "channel": "voice",
                        "chat_db_context_data": chat_data,
                        "municipio_config_actual": self.tenant_profile.configuracion if self.tenant_profile else {},
                    }
                    ctx["contexto_municipio_v2"] = chat_data.get("contexto_municipio_v2", {})
                    ctx["resolved_contact"] = resolve_contact(
                        getattr(self.user, "telefono", None) or self.from_number,
                        self.context_data_snapshot.get("profile_name") if isinstance(self.context_data_snapshot, dict) else None,
                    )

                    nombre_raw = args.get("nombre")
                    if isinstance(nombre_raw, str):
                        # Nunca usar la transcripción de voz para nombre.
                        args.pop("nombre", None)

                    def _is_greeting_name(value: str | None) -> bool:
                        if not value:
                            return False
                        words = re.findall(r"[a-záéíóúñ]+", value.lower())
                        return bool(words) and all(word in {"hola", "buenas", "buenos"} for word in words)

                    email_raw = args.get("email")
                    if isinstance(email_raw, str) and email_raw.endswith("@whatsapp.chatboc.com"):
                        args.pop("email", None)

                    if self.user:
                        args.setdefault("telefono", getattr(self.user, "telefono", None))
                        user_name = getattr(self.user, "name", None) or getattr(self.user, "nombre", None)
                        sanitized_name = sanitize_profile_name(user_name) if user_name else None
                        if sanitized_name and not _is_greeting_name(sanitized_name):
                            args.setdefault("nombre", sanitized_name)
                        args.setdefault("email", getattr(self.user, "email", None))

                    if self.last_ticket_nro:
                        logger.info(f"[VOICE] Skipping duplicated ticket creation. Existing: {self.last_ticket_nro}")
                        result = (
                            f"Ya tenés registrado el reclamo número {self.last_ticket_nro}. "
                            "He tomado nota de los detalles adicionales."
                        )
                        # Returning early without creating a new ticket.
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
                        self.response_active = True
                        return

                    handler = CrearReclamoActionHandler(ctx)
                    res = handler.execute(args)

                    if res.get("success"):
                        data = res.get("data", {})
                        nro = data.get("nro_ticket")
                        self.last_ticket_nro = nro

                        nombre_speech = getattr(self.user, "name", None) or ""
                        nombre_speech = nombre_speech.strip()
                        if nombre_speech:
                            saludo_ticket = f"Gracias {nombre_speech}."
                        else:
                            saludo_ticket = "Gracias."

                        result = (
                            f"{saludo_ticket} Tu reclamo quedó cargado con el número {nro}. "
                            "Te acabo de enviar el resumen por WhatsApp para que tengas el comprobante. "
                            "Si tenés una foto, podés responder a ese mensaje con la imagen. ¿Necesitas algo más?"
                        )

                        if session_context:
                            updates = {
                                "latest_ticket_nro": nro,
                                "awaiting_photo_for_ticket": nro,
                                "receipt_sent": True,
                                "last_ticket_code": nro,
                                "awaiting_ticket_photo": True,
                                "awaiting_ticket_photo_until": time.time() + 600,
                            }
                            if data.get("ticket_id"):
                                updates["latest_ticket_id"] = data.get("ticket_id")
                            if data.get("consulta_pin"):
                                updates["latest_ticket_pin"] = data.get("consulta_pin")

                            base_chat_url = (
                                self.tenant_profile.configuracion.get("base_chat_url")
                                if self.tenant_profile and self.tenant_profile.configuracion
                                else None
                            )
                            if base_chat_url:
                                ticket_id_numeric = str(nro).replace("M-", "").replace("S-", "")
                                tracking_url = f"{base_chat_url.rstrip('/')}/{ticket_id_numeric}"
                                if data.get("consulta_pin"):
                                    tracking_url = f"{tracking_url}?pin={data.get('consulta_pin')}"
                                updates["latest_tracking_url"] = tracking_url

                            self._update_session_contexts(session_context, updates)

                        # ✅ Enviar resumen por WhatsApp (Rich Receipt)
                        whatsapp_target = None
                        if self.user and getattr(self.user, "telefono", None):
                            whatsapp_target = self.user.telefono
                        elif self.from_number:
                            whatsapp_target = self._normalize_phone(self.from_number)

                        if whatsapp_target:
                            try:
                                municipio_cfg = self._resolve_municipio_config()
                                base_chat_url = municipio_cfg.get("base_chat_url", "https://www.chatboc.ar/chat")
                                resolved_contact = (
                                    self.context_data_snapshot.get("resolved_contact")
                                    if isinstance(self.context_data_snapshot, dict)
                                    else {}
                                )
                                resolved_name = (
                                    resolved_contact.get("nombre")
                                    if isinstance(resolved_contact, dict)
                                    else None
                                )
                                data_payload = res.get("data", {}) if isinstance(res, dict) else {}
                                nombre_contacto = (
                                    data_payload.get("nombre_vecino")
                                    or resolved_name
                                    or sanitize_profile_name(getattr(self.user, "name", None))
                                    or ""
                                )
                                promo_image_url = (
                                    res.get("image_url") or self._resolve_promo_image_url(municipio_cfg)
                                )
                                receipt = render_ticket_whatsapp(
                                    kind="reclamo",
                                    nombre=nombre_contacto,
                                    ticket_nro=nro,
                                    categoria=args.get("categoria", "General"),
                                    descripcion=args.get("descripcion", ""),
                                    direccion=args.get("ubicacion"),
                                    dni=args.get("dni"),
                                    consulta_pin=data.get("consulta_pin"),
                                    base_chat_url=base_chat_url,
                                    promo_image_url=promo_image_url,
                                    promo_text=data_payload.get("promo_text"),
                                    contacto_especializado=data_payload.get("contacto_especializado"),
                                    info_url=municipio_cfg.get("link_web") or municipio_cfg.get("url_web"),
                                )
                                whatsapp_sender = self._resolve_whatsapp_sender()
                                send_whatsapp_message(
                                    whatsapp_target,
                                    receipt["body_text"],
                                    media_url=promo_image_url or receipt.get("media_url"),
                                    from_number=whatsapp_sender,
                                )
                                logger.info(f"[VOICE] Sent Rich Receipt to {whatsapp_target} for ticket {nro}")
                            except Exception as ex:
                                logger.error(f"[VOICE] Could not send WhatsApp summary: {ex}", exc_info=True)

                        # We do NOT enable pending_end_call here anymore.
                        # We let the AI speak the result (including the ticket number) and ask if anything else is needed.
                        # self.pending_end_call = True

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
                        "user_id": self.owner_user.id if self.owner_user else None,
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
                            f"Excelente. Tu pedido número {nro_pedido} ya está registrado. "
                            f"El total es {monto}. Te acabo de enviar el detalle completo por WhatsApp. "
                            "¿Querés agregar algo más?"
                        )

                        if session_context:
                            updates = {
                                "latest_order_id": data.get("pedido_id"),
                                "latest_order_nro": nro_pedido,
                                "receipt_sent": True,
                            }
                            self._update_session_contexts(session_context, updates)

                        whatsapp_target = None
                        if self.user and getattr(self.user, "telefono", None):
                            whatsapp_target = self.user.telefono
                        elif self.from_number:
                            whatsapp_target = self._normalize_phone(self.from_number)

                        if whatsapp_target:
                            try:
                                from services.whatsapp_receipts import render_order_whatsapp

                                receipt = render_order_whatsapp(
                                    nombre=getattr(self.user, "name", None) or "Cliente",
                                    nro_pedido=str(nro_pedido),
                                    monto_total=float(monto or 0),
                                    items_text=resumen,
                                    direccion=getattr(self.user, "direccion", None),
                                    promo_image_url=res.get("image_url") or self._resolve_promo_image_url(self.tenant_profile.configuracion if self.tenant_profile else {}),
                                )

                                whatsapp_sender = self._resolve_whatsapp_sender()
                                send_whatsapp_message(
                                    whatsapp_target,
                                    receipt["body_text"],
                                    media_url=receipt["media_url"],
                                    from_number=whatsapp_sender,
                                )
                            except Exception as ex:
                                logger.warning(f"[VOICE] Could not send WhatsApp summary for order: {ex}")

                        # We do NOT enable pending_end_call here anymore.
                        # self.pending_end_call = True

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
                            backend_url = current_app.config.get("BACKEND_URL", "").rstrip("/")
                            transfer_url = f"{backend_url}/twilio/voice/transfer?target={target_number}"

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
            self.response_active = True

        except Exception as e:
            logger.error(f"[VOICE] Tool execution failed: {e}", exc_info=True)
