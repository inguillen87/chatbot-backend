import os
import json
import time
import logging
import hashlib
import re
import math
import unicodedata
from datetime import datetime, timezone

from flask import current_app
from websockets.sync.client import connect as ws_connect
from simple_websocket.errors import ConnectionClosed
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse

from models import (
    AnalyticsEventV2,
    WhatsappNumero,
    ChatSessionContext,
    User,
    TenantProfile,
    TenantTicket,
    MunicipioTicket,
    PymeTicket,
    PymePedido,
    ProviderSender,
    RealtimeToolCallReceipt,
)
from extensions import db
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from utils.db_utils import safe_flag_modified
from services.contact_service import resolve_contact, sanitize_profile_name
from services.whatsapp_receipts import render_ticket_whatsapp
from services.whatsapp_sender import send_whatsapp_message
from services.config_loader import cargar_configuracion_municipio
from services.tenant_ticket_scope import (
    normalize_municipio_ticket_write_scope,
    resolve_unique_tenant_for_owner,
    scoped_municipio_ticket_query,
)
from services.voice_session_service import resolve_voice_chat_session_id
from services.voice_stream_envelope import (
    VoiceStreamEnvelopeError,
    consume_voice_stream_envelope_once,
    verify_voice_stream_envelope,
)
from services.voice_consent_lifecycle import (
    VoiceConsentLifecycleError,
    claim_voice_stream_authorization,
    mark_voice_lifecycle_failed,
    resolve_voice_consent_policy,
    voice_phone_candidates,
    voice_consent_lifecycle_enabled,
)
from services.channel_session_identity import (
    channel_session_identity_enabled,
    resolve_channel_session_identity,
)
from services.realtime_voice_profiles import (
    build_multilingual_translation_policy,
    build_realtime_voice_instructions,
    build_realtime_voice_tools,
    infer_realtime_voice_vertical,
    resolve_realtime_input_transcription_model,
    resolve_realtime_model,
    resolve_realtime_voice,
)

logger = logging.getLogger(__name__)

# Compatibility names remain importable, but credentials and model selection
# are resolved at call time after dotenv/Flask configuration is initialized.
OPENAI_API_KEY = None
TWILIO_ACCOUNT_SID = None
TWILIO_AUTH_TOKEN = None
CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER = "+18564858589"
CHATBOC_DEMO_TENANT_SLUG = os.environ.get("CHATBOC_DEMO_TENANT_SLUG") or "chatboc-demo"
CHATBOC_DEMO_OWNER_EMAIL = os.environ.get("CHATBOC_DEMO_OWNER_EMAIL") or "marcelo@chatboc.ar"
REALTIME_TOOL_RECEIPTS_KEY = "realtime_tool_call_receipts_v1"
REALTIME_TOOL_RECEIPT_LIMIT = 32
REALTIME_TOOL_OUTPUT_MAX_CHARS = 4096

# WhatsApp (para resumen post-llamada)

GRAN_MENDOZA_POINTS = {
    "mendoza": (-32.8895, -68.8458),
    "ciudad": (-32.8895, -68.8458),
    "capital": (-32.8895, -68.8458),
    "godoy cruz": (-32.9286, -68.8404),
    "guaymallen": (-32.8833, -68.7333),
    "maipu": (-32.9833, -68.7833),
    "lujan": (-33.0396, -68.8797),
    "lujan de cuyo": (-33.0396, -68.8797),
    "las heras": (-32.8521, -68.8284),
}


def _openai_realtime_headers(api_key: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key or os.environ.get('OPENAI_API_KEY') or ''}"}


def _openai_realtime_url() -> str:
    try:
        app_config = current_app.config
    except RuntimeError:
        app_config = os.environ
    model = resolve_realtime_model(app_config=app_config)
    return (
        app_config.get("OPENAI_REALTIME_WS_URL")
        or os.environ.get("OPENAI_REALTIME_WS_URL")
        or f"wss://api.openai.com/v1/realtime?model={model}"
    )


def _runtime_config_value(name: str) -> str | None:
    try:
        configured = current_app.config.get(name)
    except RuntimeError:
        configured = None
    value = configured or os.environ.get(name)
    return str(value).strip() if value not in (None, "") else None


def _safe_reference(value: object) -> str:
    raw = str(value or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12] if raw else "missing"


def _realtime_tool_error_output(
    code: str,
    *,
    retryable: bool = False,
) -> str:
    """Build the JSON string required by a Realtime function-call output."""

    messages = {
        "invalid_arguments": "Los argumentos de la herramienta no son validos.",
        "tool_not_available": "La herramienta no esta disponible en esta sesion.",
        "tool_call_conflict": "La llamada ya fue procesada con otros argumentos.",
        "tool_call_reserved": "La llamada ya esta reservada y su resultado aun no fue confirmado.",
        "tool_scope_unavailable": "No se pudo validar el alcance seguro de esta llamada.",
        "tool_execution_unknown": "No se pudo confirmar el resultado y la operacion no se repetira automaticamente.",
        "tool_execution_failed": "La herramienta no pudo completar la operacion de forma segura.",
    }
    safe_code = code if code in messages else "tool_execution_failed"
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": safe_code,
                "message": messages[safe_code],
                "retryable": bool(retryable),
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _realtime_tool_arguments_hash(tool_name: str, arguments: dict) -> str:
    canonical = json.dumps(
        {"tool": str(tool_name or ""), "arguments": arguments},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_voice_e164(value: object) -> str | None:
    raw = str(value or "").replace("whatsapp:", "").strip()
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        return None
    return f"+{digits}"


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
        self.max_call_seconds = None
        self.demo_hub = None
        self.requested_tenant_slug = None
        self.requested_vertical = None
        self.requested_intent = None
        self._preflight_context_resolved = False
        self._start_event_processed = False
        self._consent_authorized = False
        self._consent_lifecycle_tenant_id = None

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
        self._tool_call_receipts: dict[str, dict] = {}

        self.voice_vertical = "general"
        self.tools = build_realtime_voice_tools(self.voice_vertical)

    # ----------------------------
    # Helpers
    # ----------------------------
    def _normalize_phone(self, n: str) -> str:
        if not n:
            return ""
        return str(n).replace("whatsapp:", "").strip()

    def _configured_chatboc_demo_numbers(self) -> set[str]:
        raw_numbers = os.environ.get("CHATBOC_DEMO_WHATSAPP_NUMBERS") or ""
        candidates = [part for part in raw_numbers.replace(";", ",").split(",") if part.strip()]
        candidates.append(CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER)
        return {self._normalize_phone(candidate) for candidate in candidates if self._normalize_phone(candidate)}

    def _is_chatboc_demo_call(self) -> bool:
        configured = self._configured_chatboc_demo_numbers()
        return (
            str(self.demo_hub or "").strip().lower() == "chatboc"
            or self._normalize_phone(self.from_number) in configured
            or self._normalize_phone(self.to_number) in configured
        )

    @staticmethod
    def _normalize_requested_vertical(value: str | None) -> str | None:
        text = str(value or "").strip().lower()
        if not text:
            return None
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        if any(token in text for token in ("gobierno", "municip", "muni", "juni", "reclamo")):
            return "municipio"
        if any(token in text for token in ("educ", "coleg", "escuela", "school")):
            return "colegio"
        if any(token in text for token in ("empresa", "pyme", "bodega", "pedido", "shop", "commerce")):
            return "pyme"
        if text in {"municipio", "pyme", "colegio", "general"}:
            return text
        return None

    def _resolve_requested_vertical(self) -> str | None:
        return self._normalize_requested_vertical(self.requested_vertical)

    def _voice_stream_runtime_config(self):
        if self.app is not None:
            return self.app.config
        try:
            return current_app.config
        except RuntimeError:
            return os.environ

    def _voice_stream_config_int(
        self,
        name: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        config = self._voice_stream_runtime_config()
        raw_value = config.get(name) or os.environ.get(name)
        try:
            value = int(raw_value) if raw_value not in (None, "") else default
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, value))

    def _reject_twilio_preflight(self, reason: str) -> None:
        logger.warning("[VOICE] Stream preflight rejected reason=%s", reason)
        try:
            self.ws.close()
        except Exception:
            pass

    def _authorize_durable_voice_consent(self, envelope: dict[str, str]) -> bool:
        """Reload the per-call grant; the signed envelope is not consent."""

        if not voice_consent_lifecycle_enabled(self._voice_stream_runtime_config()):
            self._reject_twilio_preflight("voice_consent_feature_disabled")
            return False
        tenant_slug = str(envelope.get("tenant_slug") or "").strip()
        if not tenant_slug:
            self._reject_twilio_preflight("consent_tenant_missing")
            return False
        tenant = TenantProfile.query.filter_by(slug=tenant_slug).first()
        if tenant is None or not getattr(tenant, "is_active", True):
            self._reject_twilio_preflight("consent_tenant_unknown")
            return False
        try:
            policy = resolve_voice_consent_policy(tenant)
            if policy.ai_processing != "explicit_per_call":
                raise VoiceConsentLifecycleError("policy_disabled")
            claim_voice_stream_authorization(
                tenant_id=tenant.id,
                call_sid=envelope.get("call_sid"),
                policy_version=policy.version,
            )
        except VoiceConsentLifecycleError as exc:
            self._reject_twilio_preflight(exc.code)
            return False
        except Exception as exc:
            db.session.rollback()
            logger.error(
                "[VOICE] Consent persistence unavailable error_type=%s",
                type(exc).__name__,
            )
            self._reject_twilio_preflight("consent_persistence_unavailable")
            return False
        self._consent_lifecycle_tenant_id = int(tenant.id)
        self._consent_authorized = True
        return True

    def _mark_authorized_lifecycle_failed(self, reason_code: str) -> None:
        if not self._consent_lifecycle_tenant_id or not self.call_sid:
            return
        try:
            mark_voice_lifecycle_failed(
                tenant_id=self._consent_lifecycle_tenant_id,
                call_sid=self.call_sid,
                reason_code=reason_code,
            )
        except Exception as exc:
            db.session.rollback()
            logger.error(
                "[VOICE] Lifecycle failure audit unavailable error_type=%s",
                type(exc).__name__,
            )

    def _await_validated_twilio_start(self):
        """Authenticate Twilio's start event before any OpenAI connection."""

        max_events = self._voice_stream_config_int(
            "VOICE_STREAM_PREFLIGHT_MAX_EVENTS",
            default=3,
            minimum=1,
            maximum=5,
        )
        timeout_seconds = self._voice_stream_config_int(
            "VOICE_STREAM_PREFLIGHT_TIMEOUT_SECONDS",
            default=5,
            minimum=1,
            maximum=15,
        )
        max_message_bytes = self._voice_stream_config_int(
            "VOICE_STREAM_PREFLIGHT_MAX_MESSAGE_BYTES",
            default=32768,
            minimum=1024,
            maximum=131072,
        )

        for _ in range(max_events):
            try:
                message = self.ws.receive(timeout=timeout_seconds)
            except (ConnectionClosed, TimeoutError):
                self._reject_twilio_preflight("start_timeout_or_closed")
                return None
            except Exception as exc:
                logger.warning(
                    "[VOICE] Stream preflight receive failed error_type=%s",
                    type(exc).__name__,
                )
                self._reject_twilio_preflight("start_receive_failed")
                return None

            if message in (None, "", b""):
                self._reject_twilio_preflight("start_missing")
                return None
            if not isinstance(message, (str, bytes)) or len(message) > max_message_bytes:
                self._reject_twilio_preflight("invalid_start_message")
                return None
            try:
                data = json.loads(message)
            except (TypeError, ValueError, json.JSONDecodeError):
                self._reject_twilio_preflight("invalid_start_json")
                return None
            if not isinstance(data, dict):
                self._reject_twilio_preflight("invalid_start_payload")
                return None

            event_type = str(data.get("event") or "").strip().lower()
            if event_type == "connected":
                continue
            if event_type != "start":
                self._reject_twilio_preflight("unexpected_event_before_start")
                return None

            start = data.get("start")
            custom = start.get("customParameters") if isinstance(start, dict) else None
            try:
                envelope = verify_voice_stream_envelope(
                    custom,
                    start=start,
                    config=self._voice_stream_runtime_config(),
                )
            except VoiceStreamEnvelopeError as exc:
                self._reject_twilio_preflight(exc.code)
                return None

            demo_number = any(
                self._normalize_phone(number) in self._configured_chatboc_demo_numbers()
                for number in (envelope["from_number"], envelope["to_number"])
            )
            if (envelope["demo"] == "1") != demo_number:
                self._reject_twilio_preflight("inconsistent_demo_scope")
                return None
            demo_hub = str((custom or {}).get("demo_hub") or "").strip().lower()
            if demo_hub and (envelope["demo"] != "1" or demo_hub != "chatboc"):
                self._reject_twilio_preflight("inconsistent_demo_hub")
                return None
            try:
                consume_voice_stream_envelope_once(
                    envelope,
                    config=self._voice_stream_runtime_config(),
                )
            except VoiceStreamEnvelopeError as exc:
                self._reject_twilio_preflight(exc.code)
                return None
            return data, envelope

        self._reject_twilio_preflight("start_event_limit_exceeded")
        return None

    def _apply_verified_start(self, data: dict, envelope: dict[str, str]) -> None:
        start = data["start"]
        self.stream_sid = start["streamSid"]
        self.call_sid = envelope["call_sid"]
        self.from_number = envelope["from_number"]
        self.to_number = envelope["to_number"]
        self.source_chat_session_id = envelope["chat_session_id"] or None
        self.demo_hub = "chatboc" if envelope["demo"] == "1" else None
        self.requested_tenant_slug = envelope["tenant_slug"] or None
        self.requested_vertical = envelope["vertical"] or None
        self.requested_intent = envelope["intent"] or None
        self.max_call_seconds = (
            int(envelope["max_call_seconds"])
            if envelope["max_call_seconds"]
            else None
        )

    def _configured_demo_tenant_slugs(self) -> set[str]:
        config = self._voice_stream_runtime_config()
        raw = (
            config.get("CHATBOC_DEMO_ALLOWED_TENANT_SLUGS")
            or os.environ.get("CHATBOC_DEMO_ALLOWED_TENANT_SLUGS")
            or ""
        )
        configured = {
            part.strip()
            for part in str(raw).replace(";", ",").split(",")
            if part.strip()
        }
        configured.update({CHATBOC_DEMO_TENANT_SLUG, "chatboc-platform"})
        return configured

    def _is_safe_demo_tenant_override(self, requested_profile: TenantProfile | None) -> bool:
        if not requested_profile or str(self.demo_hub or "").lower() != "chatboc":
            return False
        has_demo_number = any(
            self._normalize_phone(number) in self._configured_chatboc_demo_numbers()
            for number in (self.from_number, self.to_number)
        )
        return bool(
            has_demo_number
            and str(getattr(requested_profile, "slug", "") or "")
            in self._configured_demo_tenant_slugs()
        )

    def _resolve_max_call_seconds(self, custom: dict | None = None) -> int | None:
        custom = custom if isinstance(custom, dict) else {}
        raw_value = custom.get("max_call_seconds")
        if not raw_value and self._is_chatboc_demo_call():
            raw_value = os.environ.get("CHATBOC_DEMO_VOICE_MAX_SECONDS") or "60"
        if not raw_value:
            return None
        try:
            return max(15, int(raw_value))
        except (TypeError, ValueError):
            return 60

    def _safe_end_call_twilio(self):
        """Corta la llamada usando la API de Twilio (más confiable que esperar al LLM)."""
        account_sid = _runtime_config_value("TWILIO_ACCOUNT_SID")
        auth_token = _runtime_config_value("TWILIO_AUTH_TOKEN")
        if not (account_sid and auth_token and self.call_sid):
            return
        try:
            client = TwilioClient(account_sid, auth_token)
            client.calls(self.call_sid).update(status="completed")
            logger.info("[VOICE] Ended call call_ref=%s", _safe_reference(self.call_sid))
        except Exception as exc:
            logger.error("[VOICE] Failed to end call error_type=%s", type(exc).__name__)

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

    def _provider_sender_for_voice_number(self, bot_phone_clean: str | None) -> ProviderSender | None:
        if not bot_phone_clean:
            return None
        clean_phone = self._normalize_phone(bot_phone_clean)
        compact_phone = clean_phone.replace("+", "").replace(" ", "")
        candidates = {
            candidate
            for candidate in (
                clean_phone,
                compact_phone,
                f"whatsapp:{clean_phone}" if clean_phone else None,
                f"whatsapp:{compact_phone}" if compact_phone else None,
            )
            if candidate
        }
        base_query = ProviderSender.query.options(joinedload(ProviderSender.tenant)).filter(
            ProviderSender.channel.in_(("whatsapp", "voice"))
        )
        for value in candidates:
            sender = (
                base_query.filter(ProviderSender.phone_number == value)
                .order_by(ProviderSender.id.desc())
                .first()
            )
            if sender:
                return sender
            sender = (
                base_query.filter(ProviderSender.sender_id == value)
                .order_by(ProviderSender.id.desc())
                .first()
            )
            if sender:
                return sender
        return None

    def _owner_for_tenant(self, tenant: TenantProfile | None) -> User | None:
        if not tenant:
            return None
        return tenant.pyme or tenant.municipio

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
            source_context = ChatSessionContext.query.filter_by(
                chat_session_id=source_id,
                tenant_id=getattr(self.tenant_profile, "id", None),
            ).first()
            if source_context:
                if not isinstance(source_context.context_data, dict):
                    source_context.context_data = {}
                source_context.context_data.update(updates)
                safe_flag_modified(source_context, "context_data")

        db.session.commit()

    def _load_current_chat_session(self) -> ChatSessionContext | None:
        if not self.chat_session_id:
            return None
        query = ChatSessionContext.query.filter_by(
            chat_session_id=self.chat_session_id
        )
        tenant_id = self._consent_lifecycle_tenant_id or getattr(
            self.tenant_profile,
            "id",
            None,
        )
        if tenant_id:
            query = query.filter_by(tenant_id=tenant_id)
        return query.first()

    def _latest_context_value(self, *keys: str):
        stack = [self.context_data_snapshot] if isinstance(self.context_data_snapshot, dict) else []
        seen = set()
        while stack:
            item = stack.pop(0)
            if not isinstance(item, dict):
                continue
            item_id = id(item)
            if item_id in seen:
                continue
            seen.add(item_id)
            for key in keys:
                value = item.get(key)
                if value not in (None, ""):
                    return value
            for value in item.values():
                if isinstance(value, dict):
                    stack.append(value)
        return None

    @staticmethod
    def _voice_compact_text(text: str | None, *, max_chars: int = 360) -> str:
        cleaned = re.sub(r"https?://\S+", "", str(text or ""))
        cleaned = re.sub(r"[*_`#>\[\]()]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max_chars - 1].rstrip() + "..."

    @staticmethod
    def _normalize_ticket_number(value) -> str | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        text = re.sub(r"^(ticket|reclamo|caso)\s*#?\s*", "", text, flags=re.IGNORECASE)
        text = text.replace("M-", "").replace("S-", "").replace("#", "").strip()
        return text or None

    def _find_municipio_ticket_for_voice(self, nro_ticket=None, pin=None):
        context_nro = self.last_ticket_nro or self._latest_context_value("latest_ticket_nro", "last_ticket_code")
        context_pin = self._latest_context_value("latest_ticket_pin", "consulta_pin")
        explicit_nro = nro_ticket not in (None, "")
        nro = self._normalize_ticket_number(nro_ticket or context_nro)
        pin_value = str(pin or context_pin or "").strip()
        if not nro:
            return None, "missing_ticket"
        if explicit_nro and not pin_value and str(context_nro or "") != str(nro_ticket or ""):
            return None, "missing_pin"

        candidates = list(dict.fromkeys([nro, str(nro), f"M-{nro}", f"S-{nro}"]))
        query = scoped_municipio_ticket_query(self.tenant_profile).filter(
            MunicipioTicket.nro_ticket.in_(candidates)
        )
        if pin_value:
            query = query.filter(MunicipioTicket.consulta_pin == pin_value)
        ticket = query.order_by(MunicipioTicket.fecha.desc()).first()
        if ticket:
            return ticket, None
        if not pin_value:
            return None, "missing_pin"
        return None, "not_found"

    def _find_pyme_order_for_voice(self, nro_pedido=None):
        nro = str(nro_pedido or self.last_order_nro or self._latest_context_value("latest_order_nro") or "").strip()
        if not nro:
            return None, "missing_order"
        query = PymePedido.query.filter(PymePedido.nro_pedido == nro)
        tenant_id = getattr(self.tenant_profile, "id", None)
        pyme_id = getattr(self.tenant_profile, "pyme_id", None) or getattr(self.owner_user, "id", None)
        if tenant_id:
            query = query.filter(PymePedido.tenant_id == tenant_id)
        elif pyme_id:
            query = query.filter(PymePedido.pyme_id == pyme_id)
        pedido = query.order_by(PymePedido.fecha.desc()).first()
        return (pedido, None) if pedido else (None, "not_found")

    def _find_school_case_for_voice(self, school_case_id=None):
        latest_case = self._latest_context_value("latest_school_case")
        if isinstance(latest_case, dict):
            latest_id = latest_case.get("school_case_id") or latest_case.get("id")
        else:
            latest_id = None
        raw_id = school_case_id or latest_id
        if raw_id in (None, ""):
            return None, None, "missing_case"
        try:
            alias_id = int(str(raw_id).replace("#", "").strip())
        except (TypeError, ValueError):
            return None, None, "invalid_case"

        from models_education import SchoolCaseAlias

        tenant_id = getattr(self.tenant_profile, "id", None)
        if not tenant_id:
            return None, None, "not_found"
        alias = SchoolCaseAlias.query.filter_by(
            id=alias_id,
            tenant_id=tenant_id,
        ).first()
        if not alias:
            return None, None, "not_found"
        if alias.ticket_type == "pyme":
            ticket = PymeTicket.query.filter_by(
                id=alias.ticket_id,
                tenant_id=tenant_id,
            ).first()
        elif alias.ticket_type == "municipio":
            ticket = scoped_municipio_ticket_query(self.tenant_profile).filter(
                MunicipioTicket.id == alias.ticket_id
            ).first()
        else:
            ticket = None
        if ticket is None:
            return None, None, "not_found"
        return alias, ticket, None

    @staticmethod
    def _coerce_float(value) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        radius_km = 6371.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lng2 - lng1)
        a = (
            math.sin(delta_phi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
        )
        return radius_km * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

    def _geo_point_from_text_or_coords(self, text=None, lat=None, lng=None) -> tuple[float, float] | None:
        lat_value = self._coerce_float(lat)
        lng_value = self._coerce_float(lng)
        if lat_value is not None and lng_value is not None:
            return lat_value, lng_value

        normalized = str(text or "").strip().lower()
        if not normalized:
            return None
        normalized = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
        for key, point in GRAN_MENDOZA_POINTS.items():
            if key in normalized:
                return point
        return None

    def _tenant_origin_point(self, cfg: dict | None) -> tuple[float, float]:
        cfg = cfg if isinstance(cfg, dict) else {}
        lat = (
            cfg.get("shipping_origin_lat")
            or cfg.get("latitud")
            or cfg.get("lat")
            or cfg.get("latitude")
        )
        lng = (
            cfg.get("shipping_origin_lng")
            or cfg.get("longitud")
            or cfg.get("lng")
            or cfg.get("longitude")
        )
        point = self._geo_point_from_text_or_coords(cfg.get("direccion") or cfg.get("address"), lat, lng)
        return point or GRAN_MENDOZA_POINTS["mendoza"]

    def _estimate_delivery_quote(self, args: dict, session_context: ChatSessionContext | None) -> str:
        cfg = self._resolve_voice_config()
        origin_text = args.get("origen") or cfg.get("shipping_origin_label") or cfg.get("direccion")
        origin = self._geo_point_from_text_or_coords(origin_text) or self._tenant_origin_point(cfg)
        destination = self._geo_point_from_text_or_coords(
            args.get("destino") or args.get("direccion_entrega"),
            args.get("lat_destino") or args.get("lat"),
            args.get("lng_destino") or args.get("lng") or args.get("lon"),
        )
        if not destination:
            return "Necesito una zona, direccion o ubicacion de Gran Mendoza para cotizar el envio."

        km = round(self._haversine_km(origin[0], origin[1], destination[0], destination[1]), 1)
        base_ars = int(cfg.get("delivery_base_ars") or cfg.get("shipping_base_ars") or 1300)
        per_km_ars = int(cfg.get("delivery_km_ars") or cfg.get("shipping_km_ars") or 320)
        minimum_ars = int(cfg.get("delivery_minimum_ars") or 1500)
        estimate = max(minimum_ars, int(round(base_ars + (km * per_km_ars), -2)))
        monto_pedido = self._coerce_float(args.get("monto_pedido"))
        free_threshold = self._coerce_float(cfg.get("free_shipping_threshold_ars"))
        free_shipping = bool(free_threshold and monto_pedido and monto_pedido >= free_threshold)
        if free_shipping:
            estimate = 0
        eta_min = max(20, int(18 + km * 3))
        eta_max = eta_min + 12

        quote = {
            "source": "voice_realtime",
            "origin": origin_text or "sede",
            "destination": args.get("destino") or args.get("direccion_entrega"),
            "distance_km": km,
            "estimated_cost_ars": estimate,
            "eta_minutes_min": eta_min,
            "eta_minutes_max": eta_max,
            "free_shipping": free_shipping,
        }
        if session_context:
            self._update_session_contexts(session_context, {"latest_delivery_quote": quote})

        if self.tenant_profile:
            db.session.add(
                AnalyticsEventV2(
                    tenant_id=self.tenant_profile.id,
                    tenant_type=self.tenant_profile.tipo,
                    user_id=getattr(self.user, "id", None),
                    channel="voice_realtime",
                    event_name="delivery_quote_created",
                    session_id=self.chat_session_id,
                    metadata_payload=quote,
                )
            )
            db.session.commit()

        cost_text = "bonificado" if free_shipping else f"{estimate} pesos aprox"
        return (
            f"El envio esta estimado en {km} kilometros. "
            f"Costo: {cost_text}. Demora aproximada: {eta_min} a {eta_max} minutos. "
            "Lo tomo como estimacion hasta confirmar el pedido."
        )

    def _resolve_commercial_lead_tenant(self) -> TenantProfile | None:
        platform_tenant = TenantProfile.query.filter_by(slug="chatboc-platform").first()
        if platform_tenant:
            return platform_tenant
        if self.tenant_profile:
            return self.tenant_profile
        return TenantProfile.query.filter(TenantProfile.is_active == True).order_by(TenantProfile.id.asc()).first()

    def _capture_commercial_lead(self, args: dict, session_context: ChatSessionContext | None) -> str:
        tenant = self._resolve_commercial_lead_tenant()
        if not tenant:
            return "No pude registrar el lead porque no hay un tenant operativo configurado."

        nombre = sanitize_profile_name(args.get("nombre")) or sanitize_profile_name(getattr(self.user, "name", None))
        telefono = str(args.get("telefono") or getattr(self.user, "telefono", None) or self._normalize_phone(self.from_number) or "").strip()
        email = str(args.get("email") or getattr(self.user, "email", None) or "").strip().lower()
        necesidad = str(args.get("necesidad") or args.get("mensaje") or "").strip()
        if not nombre:
            return "Necesito el nombre de la persona para dejar el contacto comercial."
        if not (telefono or email):
            return "Necesito un telefono o email para guardar el lead comercial."
        if not necesidad:
            return "Necesito saber que quiere automatizar para guardar bien el lead."

        details = {
            "source": "voice_realtime",
            "lead_profile": {
                "nombre": nombre,
                "telefono": telefono,
                "email": email,
                "organizacion": args.get("organizacion"),
                "rubro": args.get("rubro"),
                "necesidad": necesidad,
                "source_tenant_slug": getattr(self.tenant_profile, "slug", None),
                "call_sid": self.call_sid,
                "from_number": self._normalize_phone(self.from_number),
            },
            "lead_stage": "nuevo",
        }
        ticket = TenantTicket(
            tenant_id=tenant.id,
            user_id=getattr(self.user, "id", None),
            categoria="lead_capture",
            descripcion=necesidad,
            estado="nuevo",
            origen="voice",
            datos_extra=details,
        )
        db.session.add(ticket)
        db.session.flush()
        db.session.add(
            AnalyticsEventV2(
                tenant_id=tenant.id,
                tenant_type=tenant.tipo,
                user_id=getattr(self.user, "id", None),
                channel="voice_realtime",
                event_name="voice_commercial_lead_created",
                session_id=self.chat_session_id,
                metadata_payload={"lead_ticket_id": ticket.id, **details},
                entity_ref=f"tenant_ticket:{ticket.id}",
            )
        )
        if session_context:
            self._update_session_contexts(
                session_context,
                {
                    "latest_commercial_lead_ticket_id": ticket.id,
                    "latest_commercial_lead": details["lead_profile"],
                },
            )
        else:
            db.session.commit()

        return (
            f"Listo {nombre}. Deje tu solicitud registrada para el equipo comercial con el codigo {ticket.id}. "
            "Te van a contactar por WhatsApp o email para armar la propuesta."
        )

    def _register_school_payment_intent(self, args: dict, session_context: ChatSessionContext | None) -> str:
        from services.education_contracts import is_education_tenant

        if not is_education_tenant(self.tenant_profile):
            return "Este canal no esta configurado como colegio. Te derivo con una persona para pagos."
        if not self.tenant_profile:
            return "No pude identificar el colegio para registrar la intencion de pago."

        concepto = str(args.get("concepto") or "").strip()
        if not concepto:
            return "Necesito saber que concepto queres pagar: cuota, matricula, comedor, transporte u otro."
        phone = str(args.get("telefono") or getattr(self.user, "telefono", None) or self._normalize_phone(self.from_number) or "").strip()
        details = {
            "source": "voice_realtime",
            "payment_intent": {
                "concepto": concepto,
                "monto": args.get("monto"),
                "alumno": args.get("alumno"),
                "curso": args.get("curso"),
                "nombre_pagador": args.get("nombre_pagador") or sanitize_profile_name(getattr(self.user, "name", None)),
                "telefono": phone,
                "email": args.get("email") or getattr(self.user, "email", None),
                "call_sid": self.call_sid,
            },
            "payment_status": "intent_registered",
        }
        description = f"Intencion de pago escolar: {concepto}"
        if args.get("alumno"):
            description += f" - Alumno: {args.get('alumno')}"
        ticket = TenantTicket(
            tenant_id=self.tenant_profile.id,
            user_id=getattr(self.user, "id", None),
            categoria="school_payment_intent",
            descripcion=description,
            estado="nuevo",
            origen="voice",
            datos_extra=details,
        )
        db.session.add(ticket)
        db.session.flush()
        db.session.add(
            AnalyticsEventV2(
                tenant_id=self.tenant_profile.id,
                tenant_type=self.tenant_profile.tipo,
                user_id=getattr(self.user, "id", None),
                channel="voice_realtime",
                event_name="school_payment_intent_created",
                session_id=self.chat_session_id,
                metadata_payload={"ticket_id": ticket.id, **details},
                entity_ref=f"tenant_ticket:{ticket.id}",
            )
        )

        payment_url = None
        if isinstance(self.tenant_profile.configuracion, dict):
            payment_url = (
                self.tenant_profile.configuracion.get("school_payment_checkout_url")
                or self.tenant_profile.configuracion.get("payment_link_url")
            )
        if session_context:
            updates = {
                "latest_school_payment_intent_id": ticket.id,
                "latest_school_payment_intent": details["payment_intent"],
            }
            if payment_url:
                updates["latest_school_payment_url"] = payment_url
            self._update_session_contexts(session_context, updates)
        else:
            db.session.commit()

        if payment_url and phone:
            try:
                send_whatsapp_message(
                    phone,
                    (
                        f"Registramos tu intencion de pago por {concepto}. "
                        f"Link de pago del colegio: {payment_url}"
                    ),
                    from_number=self._resolve_whatsapp_sender(),
                )
            except Exception as ex:
                logger.warning(
                    "[VOICE] Could not send school payment link error_type=%s",
                    type(ex).__name__,
                )

        if payment_url:
            return (
                f"Deje registrada la intencion de pago #{ticket.id} por {concepto}. "
                "Te envio el link de pago por WhatsApp. El pago queda confirmado solo cuando impacte el comprobante."
            )
        return (
            f"Deje registrada la intencion de pago #{ticket.id} por {concepto}. "
            "El colegio va a continuar el cobro desde el panel."
        )

    def _register_operational_request(self, args: dict, session_context: ChatSessionContext | None) -> str:
        tenant = self.tenant_profile or self._resolve_commercial_lead_tenant()
        if not tenant:
            return "No pude registrar la solicitud porque no hay un tenant operativo configurado."

        request_type = str(args.get("tipo_solicitud") or "consulta").strip().lower()
        request_type = re.sub(r"[^a-z0-9_ -]", "", request_type).replace(" ", "_") or "consulta"
        allowed_types = {
            "consulta",
            "reclamo",
            "sugerencia",
            "certificado",
            "boleta_pago",
            "pago_a_revisar",
            "tramite",
            "turno",
            "pedido",
            "otro",
        }
        if request_type not in allowed_types:
            request_type = "otro"

        descripcion = str(args.get("descripcion") or args.get("detalle") or "").strip()
        if not descripcion:
            return "Necesito una descripcion breve para registrar la solicitud."

        categoria = str(args.get("categoria") or request_type).strip()[:80]
        ubicacion = str(args.get("ubicacion") or "").strip()
        point = self._geo_point_from_text_or_coords(ubicacion)
        nombre = sanitize_profile_name(args.get("nombre")) or sanitize_profile_name(getattr(self.user, "name", None))
        telefono = str(args.get("telefono") or getattr(self.user, "telefono", None) or self._normalize_phone(self.from_number) or "").strip()
        email = str(args.get("email") or getattr(self.user, "email", None) or "").strip().lower()
        details = {
            "source": "voice_realtime",
            "request_type": request_type,
            "asunto": args.get("asunto"),
            "categoria": categoria,
            "ubicacion": ubicacion,
            "identificador": args.get("identificador"),
            "contacto": {
                "nombre": nombre,
                "telefono": telefono,
                "email": email,
            },
            "tenant_slug": getattr(tenant, "slug", None),
            "tenant_tipo": getattr(tenant, "tipo", None),
            "call_sid": self.call_sid,
            "from_number": self._normalize_phone(self.from_number),
        }
        ticket = TenantTicket(
            tenant_id=tenant.id,
            user_id=getattr(self.user, "id", None),
            categoria=f"voice:{categoria}"[:80],
            descripcion=descripcion,
            estado="nuevo",
            origen="voice",
            latitud=point[0] if point else None,
            longitud=point[1] if point else None,
            datos_extra=details,
        )
        db.session.add(ticket)
        db.session.flush()
        db.session.add(
            AnalyticsEventV2(
                tenant_id=tenant.id,
                tenant_type=tenant.tipo,
                user_id=getattr(self.user, "id", None),
                channel="voice_realtime",
                event_name="operational_request_created",
                session_id=self.chat_session_id,
                metadata_payload={"ticket_id": ticket.id, **details},
                lat=point[0] if point else None,
                lng=point[1] if point else None,
                entity_ref=f"tenant_ticket:{ticket.id}",
            )
        )

        updates = {
            "latest_operational_request_id": ticket.id,
            "latest_operational_request": {
                "id": ticket.id,
                "type": request_type,
                "category": categoria,
                "description": descripcion,
                "address": ubicacion,
                "status": "nuevo",
            },
        }
        if session_context:
            self._update_session_contexts(session_context, updates)
        else:
            db.session.commit()

        whatsapp_target = telefono
        if whatsapp_target:
            try:
                send_whatsapp_message(
                    whatsapp_target,
                    (
                        f"Registramos tu solicitud #{ticket.id} en {getattr(tenant, 'nombre', 'Chatboc')}.\n"
                        f"Tipo: {request_type.replace('_', ' ')}\n"
                        f"Detalle: {descripcion}\n\n"
                        "Podes responder este WhatsApp con imagen, audio, ubicacion o archivo si queres sumar informacion."
                    ),
                    from_number=self._resolve_whatsapp_sender(),
                )
            except Exception as ex:
                logger.warning(
                    "[VOICE] Could not send operational summary error_type=%s",
                    type(ex).__name__,
                )

        return (
            f"Listo. Registre la solicitud con seguimiento numero {ticket.id}. "
            "Queda disponible para el equipo en el panel."
        )

    @staticmethod
    def _tool_receipt_key(call_id: object) -> str:
        value = str(call_id or "").strip()
        return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""

    def _tool_effect_idempotency_key(self, call_id: object) -> str:
        tenant_id = getattr(self.tenant_profile, "id", None) or "unscoped"
        session_id = self.chat_session_id or self.call_sid or "missing"
        seed = f"voice-realtime\0{tenant_id}\0{session_id}\0{str(call_id or '')}"
        return f"voice:{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"

    def _find_tool_receipt(
        self,
        session_context: ChatSessionContext | None,
        *,
        call_id: object,
        tool_name: str,
        arguments_hash: str,
    ) -> tuple[str, str | None]:
        receipt_key = self._tool_receipt_key(call_id)
        if not receipt_key:
            return "missing", None
        receipt = self._tool_call_receipts.get(receipt_key)
        if receipt is None and session_context and isinstance(session_context.context_data, dict):
            bucket = session_context.context_data.get(REALTIME_TOOL_RECEIPTS_KEY)
            if isinstance(bucket, dict) and isinstance(bucket.get(receipt_key), dict):
                receipt = dict(bucket[receipt_key])
                self._tool_call_receipts[receipt_key] = receipt
        if not isinstance(receipt, dict):
            return "missing", None
        if (
            receipt.get("tool_name") != tool_name
            or receipt.get("arguments_hash") != arguments_hash
        ):
            return "conflict", None
        output = receipt.get("output")
        if not isinstance(output, str):
            return "conflict", None
        return "replay", output

    def _store_tool_receipt(
        self,
        session_context: ChatSessionContext | None,
        *,
        call_id: object,
        tool_name: str,
        arguments_hash: str,
        output: str,
        status: str,
        commit: bool = True,
    ) -> str:
        receipt_key = self._tool_receipt_key(call_id)
        safe_output = str(output or "")[:REALTIME_TOOL_OUTPUT_MAX_CHARS]
        if not receipt_key:
            return safe_output
        receipt = {
            "tool_name": str(tool_name or "unknown")[:80],
            "arguments_hash": arguments_hash,
            "output": safe_output,
            "status": "error" if status == "error" else "completed",
            "completed_at": int(time.time()),
        }
        self._tool_call_receipts[receipt_key] = receipt
        while len(self._tool_call_receipts) > REALTIME_TOOL_RECEIPT_LIMIT:
            self._tool_call_receipts.pop(next(iter(self._tool_call_receipts)))

        if not session_context:
            return safe_output
        if not isinstance(session_context.context_data, dict):
            session_context.context_data = {}
        bucket = session_context.context_data.get(REALTIME_TOOL_RECEIPTS_KEY)
        bucket = dict(bucket) if isinstance(bucket, dict) else {}
        bucket[receipt_key] = receipt
        while len(bucket) > REALTIME_TOOL_RECEIPT_LIMIT:
            bucket.pop(next(iter(bucket)))
        session_context.context_data[REALTIME_TOOL_RECEIPTS_KEY] = bucket
        safe_flag_modified(session_context, "context_data")
        db.session.add(session_context)
        if commit:
            db.session.commit()
        return safe_output

    def _store_tool_receipt_for_session(
        self,
        *,
        call_id: object,
        tool_name: str,
        arguments_hash: str,
        output: str,
        status: str,
    ) -> str:
        """Best-effort persistence when execution has already left its DB context."""

        safe_output = self._store_tool_receipt(
            None,
            call_id=call_id,
            tool_name=tool_name,
            arguments_hash=arguments_hash,
            output=output,
            status=status,
        )
        if not self.chat_session_id:
            return safe_output
        try:
            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                session_context = self._load_current_chat_session()
                if session_context:
                    safe_output = self._store_tool_receipt(
                        session_context,
                        call_id=call_id,
                        tool_name=tool_name,
                        arguments_hash=arguments_hash,
                        output=safe_output,
                        status=status,
                    )
        except Exception as exc:
            logger.error(
                "[VOICE] Tool receipt persistence failed call_ref=%s error_type=%s",
                _safe_reference(call_id),
                type(exc).__name__,
            )
        return safe_output

    def _find_tool_receipt_for_session(
        self,
        *,
        call_id: object,
        tool_name: str,
        arguments_hash: str,
    ) -> tuple[str, str | None]:
        state, output = self._find_tool_receipt(
            None,
            call_id=call_id,
            tool_name=tool_name,
            arguments_hash=arguments_hash,
        )
        if state != "missing" or not self.chat_session_id:
            return state, output
        try:
            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                session_context = self._load_current_chat_session()
                return self._find_tool_receipt(
                    session_context,
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments_hash=arguments_hash,
                )
        except Exception as exc:
            logger.error(
                "[VOICE] Tool receipt lookup failed call_ref=%s error_type=%s",
                _safe_reference(call_id),
                type(exc).__name__,
            )
            return "missing", None

    @staticmethod
    def _bounded_tool_output(output: object) -> str:
        return str(output or "")[:REALTIME_TOOL_OUTPUT_MAX_CHARS]

    def _realtime_tool_receipt_scope(
        self,
        session_context: ChatSessionContext | None,
    ) -> tuple[int, str] | None:
        tenant_value = getattr(self.tenant_profile, "id", None)
        if tenant_value is None and session_context is not None:
            tenant_value = getattr(session_context, "tenant_id", None)
        try:
            tenant_id = int(tenant_value)
        except (TypeError, ValueError):
            return None
        session_id = str(
            self.chat_session_id
            or getattr(session_context, "chat_session_id", "")
            or ""
        ).strip()
        if tenant_id <= 0 or not session_id:
            return None
        return tenant_id, hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _realtime_tool_receipt_query(
        *,
        tenant_id: int,
        session_id_hash: str,
        call_id_hash: str,
    ):
        return RealtimeToolCallReceipt.query.filter_by(
            tenant_id=tenant_id,
            session_id_hash=session_id_hash,
            call_id_hash=call_id_hash,
        )

    def _classify_realtime_tool_receipt(
        self,
        receipt: RealtimeToolCallReceipt,
        *,
        tool_name: str,
        arguments_hash: str,
        effect_idempotency_key: str,
    ) -> tuple[str, str | None]:
        if (
            receipt.tool_name != tool_name
            or receipt.arguments_hash != arguments_hash
            or receipt.effect_idempotency_key != effect_idempotency_key
        ):
            return "conflict", None
        if receipt.status == RealtimeToolCallReceipt.STATUS_COMPLETED:
            if not isinstance(receipt.output_text, str):
                return "conflict", None
            return "replay", receipt.output_text
        if receipt.status == RealtimeToolCallReceipt.STATUS_UNKNOWN:
            output = receipt.output_text or _realtime_tool_error_output(
                "tool_execution_unknown"
            )
            return "unknown", self._bounded_tool_output(output)
        if receipt.status == RealtimeToolCallReceipt.STATUS_RESERVED:
            return "reserved", None
        return "conflict", None

    def _claim_realtime_tool_call(
        self,
        session_context: ChatSessionContext | None,
        *,
        call_id: str,
        tool_name: str,
        arguments_hash: str,
        effect_idempotency_key: str,
    ) -> tuple[str, str | None]:
        """Atomically reserve a call before any tool effect is attempted."""

        scope = self._realtime_tool_receipt_scope(session_context)
        if scope is None:
            return "scope_error", None
        tenant_id, session_id_hash = scope
        call_id_hash = self._tool_receipt_key(call_id)
        query = self._realtime_tool_receipt_query(
            tenant_id=tenant_id,
            session_id_hash=session_id_hash,
            call_id_hash=call_id_hash,
        )
        existing = query.first()
        if existing is not None:
            return self._classify_realtime_tool_receipt(
                existing,
                tool_name=tool_name,
                arguments_hash=arguments_hash,
                effect_idempotency_key=effect_idempotency_key,
            )

        # Import the bounded JSON receipt used by the previous implementation
        # before reserving a new effect. This prevents duplicates during rollout.
        legacy_state, legacy_output = self._find_tool_receipt(
            session_context,
            call_id=call_id,
            tool_name=tool_name,
            arguments_hash=arguments_hash,
        )
        if legacy_state == "conflict":
            return "conflict", None
        importing_legacy = legacy_state == "replay"
        now = datetime.now(timezone.utc)
        receipt = RealtimeToolCallReceipt(
            tenant_id=tenant_id,
            session_id_hash=session_id_hash,
            call_id_hash=call_id_hash,
            tool_name=tool_name,
            arguments_hash=arguments_hash,
            effect_idempotency_key=effect_idempotency_key,
            status=(
                RealtimeToolCallReceipt.STATUS_COMPLETED
                if importing_legacy
                else RealtimeToolCallReceipt.STATUS_RESERVED
            ),
            output_text=(
                self._bounded_tool_output(legacy_output)
                if importing_legacy
                else None
            ),
            completed_at=now if importing_legacy else None,
        )
        db.session.add(receipt)
        try:
            # This commit is intentionally before tool execution. A competing
            # worker either owns this reservation or observes its terminal row.
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = query.first()
            if existing is None:
                raise
            return self._classify_realtime_tool_receipt(
                existing,
                tool_name=tool_name,
                arguments_hash=arguments_hash,
                effect_idempotency_key=effect_idempotency_key,
            )
        if importing_legacy:
            return "replay", receipt.output_text or ""
        return "execute", None

    def _complete_realtime_tool_call(
        self,
        session_context: ChatSessionContext | None,
        *,
        call_id: str,
        tool_name: str,
        arguments_hash: str,
        effect_idempotency_key: str,
        output: object,
    ) -> str:
        scope = self._realtime_tool_receipt_scope(session_context)
        if scope is None:
            raise RuntimeError("realtime_tool_scope_unavailable")
        tenant_id, session_id_hash = scope
        receipt = self._realtime_tool_receipt_query(
            tenant_id=tenant_id,
            session_id_hash=session_id_hash,
            call_id_hash=self._tool_receipt_key(call_id),
        ).first()
        if receipt is None:
            raise RuntimeError("realtime_tool_reservation_missing")
        state, stored_output = self._classify_realtime_tool_receipt(
            receipt,
            tool_name=tool_name,
            arguments_hash=arguments_hash,
            effect_idempotency_key=effect_idempotency_key,
        )
        if state == "replay":
            return stored_output or ""
        if state == "unknown":
            return stored_output or _realtime_tool_error_output("tool_execution_unknown")
        if state != "reserved":
            raise RuntimeError("realtime_tool_reservation_conflict")

        safe_output = self._bounded_tool_output(output)
        receipt.status = RealtimeToolCallReceipt.STATUS_COMPLETED
        receipt.output_text = safe_output
        receipt.last_error_code = None
        receipt.completed_at = datetime.now(timezone.utc)
        self._store_tool_receipt(
            session_context,
            call_id=call_id,
            tool_name=tool_name,
            arguments_hash=arguments_hash,
            output=safe_output,
            status="completed",
            commit=False,
        )
        db.session.add(receipt)
        db.session.commit()
        return safe_output

    def _mark_realtime_tool_call_unknown_for_session(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments_hash: str,
        effect_idempotency_key: str,
        output: str,
        error_code: str,
    ) -> str:
        safe_output = self._bounded_tool_output(output)
        try:
            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                session_context = (
                    self._load_current_chat_session()
                    if self.chat_session_id
                    else None
                )
                scope = self._realtime_tool_receipt_scope(session_context)
                if scope is None:
                    return safe_output
                tenant_id, session_id_hash = scope
                receipt = self._realtime_tool_receipt_query(
                    tenant_id=tenant_id,
                    session_id_hash=session_id_hash,
                    call_id_hash=self._tool_receipt_key(call_id),
                ).first()
                if receipt is None:
                    return safe_output
                state, stored_output = self._classify_realtime_tool_receipt(
                    receipt,
                    tool_name=tool_name,
                    arguments_hash=arguments_hash,
                    effect_idempotency_key=effect_idempotency_key,
                )
                if state in {"replay", "unknown"}:
                    return stored_output or safe_output
                if state != "reserved":
                    return _realtime_tool_error_output("tool_call_conflict")
                receipt.status = RealtimeToolCallReceipt.STATUS_UNKNOWN
                receipt.output_text = safe_output
                receipt.last_error_code = str(error_code or "tool_execution_unknown")[:64]
                receipt.completed_at = datetime.now(timezone.utc)
                self._store_tool_receipt(
                    session_context,
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments_hash=arguments_hash,
                    output=safe_output,
                    status="error",
                    commit=False,
                )
                db.session.add(receipt)
                db.session.commit()
        except Exception as exc:
            try:
                db.session.rollback()
            except Exception:
                pass
            logger.error(
                "[VOICE] Tool unknown-state persistence failed call_ref=%s error_type=%s",
                _safe_reference(call_id),
                type(exc).__name__,
            )
        return safe_output

    def _send_tool_result(self, call_id, result: str, *, create_response: bool = True) -> bool:
        if not self.openai_ws:
            return False
        try:
            self.openai_ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": str(result or ""),
                        },
                    }
                )
            )
            if create_response:
                self.openai_ws.send(json.dumps({"type": "response.create"}))
                self.response_active = True
            return True
        except Exception as exc:
            logger.error(
                "[VOICE] Tool output delivery failed call_ref=%s error_type=%s",
                _safe_reference(call_id),
                type(exc).__name__,
            )
            return False

    def _build_chatboc_demo_greeting(self, tenant_name: str, user_name: str | None) -> str:
        name_prefix = f"Hola {user_name}. " if user_name else "Hola. "
        vertical = self._resolve_requested_vertical()
        if vertical == "municipio":
            menu = (
                "Estas en la demo telefonica de municipios. "
                "Puedo crear un reclamo completo, consultar estado o probar tramites. "
                "Decime por ejemplo: quiero hacer un reclamo por luminaria."
            )
        elif vertical == "colegio":
            menu = (
                "Estas en la demo telefonica de colegios. "
                "Puedo registrar admisiones, inasistencias, cuotas, certificados o consultas de secretaria. "
                "Decime que queres probar."
            )
        elif vertical == "pyme":
            menu = (
                "Estas en la demo telefonica de empresas. "
                "Puedo tomar un pedido, consultar productos, cotizar envio o revisar una compra. "
                "Decime que queres pedir o vender."
            )
        else:
            menu = (
                "Soy Chatboc.ar y esta es la demo por telefono. "
                "Podes probar municipios, colegios, empresas y pedidos, o hablar con ventas. "
                "Deci una opcion para empezar."
            )
        return f"{name_prefix}{menu} Te acompano con voz realtime de {tenant_name}."

    def _build_initial_voice_greeting(self, tenant_name: str, user_name: str | None) -> str:
        name_prefix = f"Hola {user_name}. " if user_name else "Hola. "
        if self._is_chatboc_demo_call():
            return self._build_chatboc_demo_greeting(tenant_name, user_name)

        tenant_label = tenant_name or "tu organizacion"
        vertical = self._resolve_requested_vertical() or self.voice_vertical
        if vertical == "municipio":
            return (
                f"{name_prefix}Te damos la bienvenida a {tenant_label}. "
                "Soy JUNI, el asistente telefonico municipal. "
                "Puedo ayudarte a iniciar un reclamo, consultar el estado de un ticket, "
                "pedir informacion de tramites o derivarte con un operador. "
                "Decime que queres hacer."
            )
        if vertical in {"pyme", "empresa", "ventas", "chatboc", "platform"}:
            return (
                f"{name_prefix}Soy Chatboc.ar. Puedo mostrar demos de municipios, colegios, "
                "empresas, encuestas o conectarte con ventas. Decime que queres probar."
            )
        if not user_name:
            return f"{name_prefix}Te saluda el asistente de {tenant_label}. Antes de empezar, decime tu nombre."
        return f"{name_prefix}Te saluda el asistente de {tenant_label}. Decime que queres hacer."

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

            provider_sender = self._provider_sender_for_voice_number(bot_phone_clean)
            if provider_sender and provider_sender.tenant:
                self.tenant_profile = provider_sender.tenant
                self.owner_user = self._owner_for_tenant(self.tenant_profile)
                self.whatsapp_sender = (
                    provider_sender.sender_id
                    or provider_sender.phone_number
                    or self.whatsapp_sender
                )

            # 1) Legacy: WhatsappNumero mapping (si existe)
            whatsapp_mapping = None
            if not self.tenant_profile:
                whatsapp_mapping = (
                    WhatsappNumero.query.options(joinedload(WhatsappNumero.user).joinedload(User.rubro))
                    .filter(
                        WhatsappNumero.numero_whatsapp.in_(
                            voice_phone_candidates(bot_phone_clean)
                        ),
                        WhatsappNumero.is_active.is_(True),
                    )
                    .first()
                )

            if whatsapp_mapping:
                self.owner_user = whatsapp_mapping.user
                self.whatsapp_sender = whatsapp_mapping.numero_whatsapp
                self.tenant_profile = getattr(self.owner_user, "tenant", None) or getattr(
                    self.owner_user, "tenant_profile", None
                )
            elif not self.tenant_profile:
                # 2) Buscar por TenantProfile.configuracion.twilio_voice_number
                candidates = TenantProfile.query.filter(TenantProfile.is_active == True).all()
                for t in candidates:
                    if t.configuracion and t.configuracion.get("twilio_voice_number") == bot_phone_clean:
                        self.tenant_profile = t
                        self.owner_user = t.municipio or t.pyme
                        break

            owner_slug = str(getattr(self.owner_user, "tenant_slug", "") or "").strip() if self.owner_user else ""
            if self.owner_user and not self.tenant_profile and owner_slug:
                self.tenant_profile = TenantProfile.query.filter_by(slug=owner_slug).first()
            if self.owner_user and not self.tenant_profile:
                owner_resolution = resolve_unique_tenant_for_owner(self.owner_user.id)
                if owner_resolution.status != "unique":
                    logger.error(
                        "[VOICE] Tenant resolution failed reason=owner_scope_%s",
                        owner_resolution.status,
                    )
                    return False
                self.tenant_profile = owner_resolution.tenant

            sender_tenant_profile = self.tenant_profile
            requested_slug = str(self.requested_tenant_slug or "").strip()
            if requested_slug:
                requested_profile = TenantProfile.query.filter_by(slug=requested_slug).first()
                if not requested_profile:
                    logger.error("[VOICE] Tenant resolution failed reason=requested_tenant_unknown")
                    return False
                sender_tenant_id = getattr(sender_tenant_profile, "id", None)
                requested_tenant_id = getattr(requested_profile, "id", None)
                tenant_mismatch = bool(
                    sender_tenant_profile
                    and (
                        sender_tenant_id != requested_tenant_id
                        if sender_tenant_id is not None and requested_tenant_id is not None
                        else str(getattr(sender_tenant_profile, "slug", "") or "")
                        != str(getattr(requested_profile, "slug", "") or "")
                    )
                )
                if tenant_mismatch and not self._is_safe_demo_tenant_override(requested_profile):
                    logger.error("[VOICE] Tenant resolution failed reason=requested_tenant_mismatch")
                    return False
                self.tenant_profile = requested_profile
                self.owner_user = self._owner_for_tenant(self.tenant_profile)

            if not self.owner_user and not self.tenant_profile and self._is_chatboc_demo_call():
                demo_candidates = [CHATBOC_DEMO_TENANT_SLUG, "chatboc-platform"]
                for slug in [candidate for candidate in dict.fromkeys(demo_candidates) if candidate]:
                    self.tenant_profile = TenantProfile.query.filter_by(slug=slug).first()
                    if self.tenant_profile:
                        self.owner_user = self._owner_for_tenant(self.tenant_profile)
                        break
                if not self.owner_user:
                    owner_email = (
                        current_app.config.get("CHATBOC_SUPERADMIN_EMAIL")
                        or current_app.config.get("SUPERADMIN_LEAD_EMAIL")
                        or CHATBOC_DEMO_OWNER_EMAIL
                    )
                    self.owner_user = User.query.filter_by(email=str(owner_email).strip().lower()).first()
                if self.owner_user and not self.tenant_profile:
                    owner_resolution = resolve_unique_tenant_for_owner(self.owner_user.id)
                    if owner_resolution.status != "unique":
                        logger.error(
                            "[VOICE] Tenant resolution failed reason=demo_owner_scope_%s",
                            owner_resolution.status,
                        )
                        return False
                    self.tenant_profile = owner_resolution.tenant
                if self.owner_user:
                    self.whatsapp_sender = bot_phone_clean

            if not self.owner_user and not self.tenant_profile:
                logger.error("[VOICE] Tenant resolution failed reason=sender_not_registered")
                return False

            if not self.owner_user and self.tenant_profile:
                self.owner_user = self._owner_for_tenant(self.tenant_profile)

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

            # 4) Canonical voice session. In rollout legacy mode the call-scoped
            # resolver remains available, but WhatsApp continuity is never
            # reconstructed from a global phone-derived session id.
            tenant_id = getattr(self.tenant_profile, "id", None)
            canonical_identity_enabled = bool(
                tenant_id
                and channel_session_identity_enabled(current_app.config)
            )
            if canonical_identity_enabled:
                voice_identity = resolve_channel_session_identity(
                    config=current_app.config,
                    tenant_id=tenant_id,
                    channel="voice",
                    provider="twilio",
                    provider_identity=user_phone_clean,
                    owner_user_id=empresa_id,
                )
                if voice_identity is None:
                    logger.error("[VOICE] Session identity resolution returned no binding")
                    return False
                chat_session_id = voice_identity.chat_session_id
            else:
                chat_session_id = resolve_voice_chat_session_id(
                    call_sid=call_sid,
                    from_number=user_phone_clean,
                    to_number=bot_phone_clean,
                )

            self.chat_session_id = chat_session_id

            # 5) Load a WhatsApp source only through the same tenant/provider
            # identity proof. A signed envelope is transport integrity, not
            # authorization to read an arbitrary chat_session_id.
            source_session = None
            requested_source_chat_session_id = self.source_chat_session_id
            source_chat_session_id = None

            if canonical_identity_enabled:
                whatsapp_identity = resolve_channel_session_identity(
                    config=current_app.config,
                    tenant_id=tenant_id,
                    channel="whatsapp",
                    provider="twilio",
                    provider_identity=user_phone_clean,
                    owner_user_id=empresa_id,
                    explicit_legacy_chat_session_id=requested_source_chat_session_id,
                    create_if_missing=False,
                )
                if whatsapp_identity is not None:
                    source_chat_session_id = whatsapp_identity.chat_session_id
                    source_session = ChatSessionContext.query.filter_by(
                        chat_session_id=source_chat_session_id,
                        tenant_id=tenant_id,
                    ).first()
            elif requested_source_chat_session_id and tenant_id:
                legacy_source = ChatSessionContext.query.filter_by(
                    chat_session_id=requested_source_chat_session_id,
                    tenant_id=tenant_id,
                    user_id=empresa_id,
                    anon_id=user_phone_clean,
                ).first()
                if legacy_source is not None:
                    source_session = legacy_source
                    source_chat_session_id = legacy_source.chat_session_id

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
                    tenant_id=tenant_id,
                    anon_id=user_phone_clean,
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

            if (
                session_context.tenant_id is not None
                and tenant_id is not None
                and int(session_context.tenant_id) != int(tenant_id)
            ):
                logger.error("[VOICE] Canonical session tenant mismatch")
                db.session.rollback()
                return False
            session_scope_changed = False
            if tenant_id and session_context.tenant_id is None:
                session_context.tenant_id = tenant_id
                session_scope_changed = True
            if not session_context.anon_id:
                session_context.anon_id = user_phone_clean
                session_scope_changed = True
            if session_scope_changed:
                db.session.add(session_context)
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
                    if self.tenant_profile is not None and getattr(
                        self.tenant_profile, "municipio_id", None
                    ):
                        ticket_match = (
                            scoped_municipio_ticket_query(self.tenant_profile)
                            .filter(
                                MunicipioTicket.telefono_vecino == user_phone_clean,
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
        except Exception as exc:
            logger.error(
                "[VOICE] Context resolution failed error_type=%s",
                type(exc).__name__,
            )
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
        self.voice_vertical = self._resolve_requested_vertical() or infer_realtime_voice_vertical(
            self.tenant_profile,
            tenant_tipo=getattr(self.tenant_profile, "tipo", None) if self.tenant_profile else None,
        )
        self.tools = build_realtime_voice_tools(self.voice_vertical)
        voice_cfg = self._resolve_voice_config()
        instructions = build_realtime_voice_instructions(
            tenant_name=tenant_name_for_voice,
            vertical=self.voice_vertical,
            user_name=user_name_for_voice,
            user_address=user_addr_for_voice,
            translation_policy=build_multilingual_translation_policy(voice_cfg, current_app.config),
        )
        if self._is_chatboc_demo_call():
            instructions += (
                " Esta llamada es una demo comercial de Chatboc. "
                "Habla siempre en espanol argentino claro aunque el motor detecte otro idioma. "
                "Al inicio ofrece rutas claras para probar: municipios, colegios, empresas y ventas. "
                "Si la persona ya eligio una vertical, guia una simulacion completa y accionable de esa vertical."
            )
        if self.voice_vertical == "municipio":
            instructions += (
                " En llamadas municipales, al inicio presenta opciones concretas: iniciar reclamo, "
                "consultar estado de ticket, informacion de tramites y derivacion humana. "
                "No canceles un reclamo por texto ambiguo; pedi confirmacion clara."
            )
        return instructions

    # ----------------------------
    # Main loop
    # ----------------------------
    def run(self):
        openai_thread = None
        try:
            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                if not voice_consent_lifecycle_enabled(self._voice_stream_runtime_config()):
                    self._reject_twilio_preflight("voice_consent_feature_disabled")
                    return
                preflight = self._await_validated_twilio_start()
                if preflight is None:
                    return
                start_event, verified_envelope = preflight
                self._apply_verified_start(start_event, verified_envelope)

                if not self._authorize_durable_voice_consent(verified_envelope):
                    return

                if not self._resolve_context(self.from_number, self.to_number, self.call_sid):
                    self._mark_authorized_lifecycle_failed("tenant_resolution_failed")
                    self._reject_twilio_preflight("tenant_resolution_failed")
                    return
                if int(getattr(self.tenant_profile, "id", 0) or 0) != int(
                    self._consent_lifecycle_tenant_id or 0
                ):
                    self._consent_authorized = False
                    self._reject_twilio_preflight("consent_tenant_mismatch")
                    return
                self._preflight_context_resolved = True

                api_key = _runtime_config_value("OPENAI_API_KEY")
                if not api_key:
                    logger.error("[VOICE] Missing OPENAI_API_KEY. Cannot start stream.")
                    self._mark_authorized_lifecycle_failed("openai_api_key_missing")
                    self._reject_twilio_preflight("openai_api_key_missing")
                    return

                try:
                    self.openai_ws = ws_connect(
                        _openai_realtime_url(),
                        additional_headers=_openai_realtime_headers(api_key),
                    )
                except Exception:
                    self._mark_authorized_lifecycle_failed("bridge_connect_failed")
                    raise
                logger.info("[VOICE] Connected to OpenAI Realtime API")

                # Initialize the Realtime session only after the authenticated
                # start event and tenant scope have both been accepted.
                self.handle_twilio_message(
                    start_event,
                    verified_envelope=verified_envelope,
                )

                import eventlet

                def listen_openai():
                    listener_ctx = self.app.app_context() if self.app else current_app.app_context()
                    with listener_ctx:
                        try:
                            while True:
                                msg = self.openai_ws.recv()
                                if not msg:
                                    break
                                data = json.loads(msg)
                                self.handle_openai_message(data)
                        except Exception as exc:
                            logger.error(
                                "[VOICE] OpenAI listener failed error_type=%s",
                                type(exc).__name__,
                            )

                openai_thread = eventlet.spawn(listen_openai)

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

        except Exception as exc:
            logger.error(
                "[VOICE] Stream failed error_type=%s",
                type(exc).__name__,
            )
        finally:
            if openai_thread is not None:
                try:
                    openai_thread.kill()
                except Exception:
                    pass
            if self.openai_ws:
                try:
                    self.openai_ws.close()
                except Exception:
                    pass

    # ----------------------------
    # Twilio -> OpenAI
    # ----------------------------
    def handle_twilio_message(self, data, *, verified_envelope=None):
        event_type = data.get("event")

        if event_type == "start":
            if not self._consent_authorized:
                self._reject_twilio_preflight("consent_not_authorized")
                return False
            if self._start_event_processed:
                self._reject_twilio_preflight("duplicate_start_event")
                return False
            if verified_envelope is None:
                start = data.get("start") if isinstance(data, dict) else None
                custom = start.get("customParameters") if isinstance(start, dict) else None
                try:
                    verified_envelope = verify_voice_stream_envelope(
                        custom,
                        start=start,
                        config=self._voice_stream_runtime_config(),
                    )
                except VoiceStreamEnvelopeError as exc:
                    self._reject_twilio_preflight(exc.code)
                    return False
            self._apply_verified_start(data, verified_envelope)
            self._start_event_processed = True

            logger.info(
                "[VOICE] Stream started stream_ref=%s call_ref=%s",
                _safe_reference(self.stream_sid),
                _safe_reference(self.call_sid),
            )
            if self.max_call_seconds:
                try:
                    import eventlet

                    eventlet.spawn_after(self.max_call_seconds, self._safe_end_call_twilio)
                    logger.info(
                        "[VOICE] Max call duration armed call_ref=%s max_seconds=%s",
                        _safe_reference(self.call_sid),
                        self.max_call_seconds,
                    )
                except Exception as exc:
                    logger.warning(
                        "[VOICE] Could not arm max duration error_type=%s",
                        type(exc).__name__,
                    )

            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                context_ready = self._preflight_context_resolved or self._resolve_context(
                    self.from_number,
                    self.to_number,
                    self.call_sid,
                )
                if context_ready:
                    voice_cfg = self._resolve_voice_config()
                    voice_name = resolve_realtime_voice(voice_cfg, current_app.config)
                    self.voice_vertical = self._resolve_requested_vertical() or infer_realtime_voice_vertical(
                        self.tenant_profile,
                        tenant_tipo=getattr(self.tenant_profile, "tipo", None) if self.tenant_profile else None,
                    )
                    self.tools = build_realtime_voice_tools(self.voice_vertical)
                    session_update = {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "model": resolve_realtime_model(voice_cfg, current_app.config),
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
                                        "model": resolve_realtime_input_transcription_model(
                                            voice_cfg,
                                            current_app.config,
                                        ),
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
                    initial_greeting = self._build_initial_voice_greeting(tenant_name, user_name)

                    self.openai_ws.send(
                        json.dumps(
                            {
                                "type": "response.create",
                                "response": {
                                    "output_modalities": ["audio"],
                                    "instructions": f"Deci exactamente: \"{initial_greeting}\"",
                                },
                            }
                        )
                    )
                    return True
            return False
        elif event_type == "media":
            if self._consent_authorized and self.openai_ws:
                self.openai_ws.send(
                    json.dumps({"type": "input_audio_buffer.append", "audio": data["media"]["payload"]})
                )

        elif event_type == "clear":
            if self._consent_authorized and self.openai_ws:
                self.openai_ws.send(json.dumps({"type": "input_audio_buffer.clear"}))

    # ----------------------------
    # OpenAI -> Twilio
    # ----------------------------
    def handle_openai_message(self, data):
        msg_type = data.get("type")

        if msg_type == "response.created":
            self.response_active = True
            response_payload = data.get("response") or {}
            self.response_id = data.get("response_id") or response_payload.get("id") or data.get("id")
            self.cancel_pending = False
            return

        if msg_type in ("response.canceled", "response.cancelled", "response.failed"):
            self.response_active = False
            self.response_id = None
            self.cancel_pending = False
            return

        if msg_type in ("response.audio.delta", "response.output_audio.delta"):
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
            return

        if msg_type == "input_audio_buffer.speech_started":
            self.ws.send(json.dumps({"event": "clear", "streamSid": self.stream_sid}))
            if self.response_active and not self.cancel_pending and self.openai_ws:
                cancel_payload = {"type": "response.cancel"}
                if self.response_id:
                    cancel_payload["response_id"] = self.response_id
                self.openai_ws.send(json.dumps(cancel_payload))
                self.response_active = False
                self.cancel_pending = True
            return

        if msg_type == "response.function_call_arguments.done":
            self.execute_tool(data.get("call_id"), data.get("name"), data.get("arguments"))
            return

        if msg_type in ("response.done", "response.completed"):
            self.response_active = False
            self.response_id = None
            self.cancel_pending = False
            if self.pending_end_call:
                self.pending_end_call = False
                self._safe_end_call_twilio()
            return

        if msg_type == "error":
            error_info = data.get("error", {})
            if error_info.get("code") == "response_cancel_not_active":
                logger.warning(
                    "[VOICE] OpenAI warning code=response_cancel_not_active"
                )
                self.response_active = False
                self.response_id = None
                self.cancel_pending = False
                return
            logger.error(
                "[VOICE] OpenAI realtime error code=%s error_type=%s",
                error_info.get("code") or "unknown",
                error_info.get("type") or "unknown",
            )
            return

        return

    # ----------------------------
    # Tools executor
    # ----------------------------
    def execute_tool(self, call_id, name, args_str):
        call_id = str(call_id or "").strip()
        name = str(name or "").strip()[:80]
        available_tool_names = {
            str(tool.get("name") or "")
            for tool in self.tools
            if isinstance(tool, dict)
        }
        tool_log_name = (
            name
            if name in available_tool_names
            else f"unavailable:{_safe_reference(name)}"
        )
        logger.info("[VOICE] Executing tool name=%s", tool_log_name)
        if not call_id:
            logger.error("[VOICE] Tool call rejected code=missing_call_id")
            return
        try:
            args = json.loads(args_str) if args_str else {}
            if not isinstance(args, dict):
                raise ValueError("arguments_not_object")
        except (TypeError, ValueError, json.JSONDecodeError):
            output = _realtime_tool_error_output("invalid_arguments")
            arguments_hash = hashlib.sha256(str(args_str or "").encode("utf-8")).hexdigest()
            receipt_state, receipt_output = self._find_tool_receipt_for_session(
                call_id=call_id,
                tool_name=name or "unknown",
                arguments_hash=arguments_hash,
            )
            if receipt_state == "replay":
                self._send_tool_result(call_id, receipt_output or output)
                return
            if receipt_state == "conflict":
                self._send_tool_result(
                    call_id,
                    _realtime_tool_error_output("tool_call_conflict"),
                )
                return
            output = self._store_tool_receipt_for_session(
                call_id=call_id,
                tool_name=name or "unknown",
                arguments_hash=arguments_hash,
                output=output,
                status="error",
            )
            logger.warning(
                "[VOICE] Tool call rejected code=invalid_arguments tool=%s",
                tool_log_name,
            )
            self._send_tool_result(call_id, output)
            return

        arguments_hash = _realtime_tool_arguments_hash(name, args)
        effect_idempotency_key = self._tool_effect_idempotency_key(call_id)
        try:
            result = "No se pudo procesar la acción."

            app_ctx = self.app.app_context() if self.app else current_app.app_context()
            with app_ctx:
                if self.owner_user_id:
                    self.owner_user = db.session.get(User, self.owner_user_id)
                if self.user_id:
                    self.user = db.session.get(User, self.user_id)

                session_context = (
                    self._load_current_chat_session()
                    if self.chat_session_id
                    else None
                )
                chat_data = session_context.context_data if session_context else {}
                if isinstance(chat_data, dict):
                    self.context_data_snapshot = chat_data

                if name not in available_tool_names:
                    output = _realtime_tool_error_output("tool_not_available")
                    self._store_tool_receipt(
                        session_context,
                        call_id=call_id,
                        tool_name=name or "unknown",
                        arguments_hash=arguments_hash,
                        output=output,
                        status="error",
                    )
                    logger.warning(
                        "[VOICE] Tool call rejected code=tool_not_available tool=%s",
                        tool_log_name,
                    )
                    self._send_tool_result(call_id, output)
                    return

                receipt_state, receipt_output = self._claim_realtime_tool_call(
                    session_context,
                    call_id=call_id,
                    tool_name=name,
                    arguments_hash=arguments_hash,
                    effect_idempotency_key=effect_idempotency_key,
                )
                if receipt_state == "replay":
                    logger.info(
                        "[VOICE] Replaying durable tool receipt tool=%s call_ref=%s",
                        tool_log_name,
                        _safe_reference(call_id),
                    )
                    self._send_tool_result(call_id, receipt_output or "")
                    return
                if receipt_state == "conflict":
                    logger.warning(
                        "[VOICE] Tool replay conflict tool=%s call_ref=%s",
                        tool_log_name,
                        _safe_reference(call_id),
                    )
                    self._send_tool_result(
                        call_id,
                        _realtime_tool_error_output("tool_call_conflict"),
                    )
                    return
                if receipt_state == "reserved":
                    output = _realtime_tool_error_output("tool_call_reserved")
                    logger.warning(
                        "[VOICE] Tool replay blocked status=reserved tool=%s call_ref=%s",
                        tool_log_name,
                        _safe_reference(call_id),
                    )
                    self._send_tool_result(call_id, output)
                    return
                if receipt_state == "unknown":
                    output = receipt_output or _realtime_tool_error_output(
                        "tool_execution_unknown"
                    )
                    logger.warning(
                        "[VOICE] Tool replay blocked status=unknown tool=%s call_ref=%s",
                        tool_log_name,
                        _safe_reference(call_id),
                    )
                    self._send_tool_result(call_id, output)
                    return
                if receipt_state == "scope_error":
                    output = _realtime_tool_error_output("tool_scope_unavailable")
                    logger.error(
                        "[VOICE] Tool call rejected code=tool_scope_unavailable tool=%s",
                        tool_log_name,
                    )
                    self._send_tool_result(call_id, output)
                    return
                if receipt_state != "execute":
                    raise RuntimeError("unexpected_realtime_tool_receipt_state")

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
                            municipal_scope = normalize_municipio_ticket_write_scope(
                                {
                                    "tenant_id": getattr(self.tenant_profile, "id", None),
                                    "municipio_id": getattr(self.tenant_profile, "municipio_id", None),
                                }
                            )
                            ticket = MunicipioTicket(
                                tenant_id=municipal_scope["tenant_id"],
                                municipio_id=municipal_scope["municipio_id"],
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
                                logger.warning(
                                    "[VOICE] Could not send school case summary error_type=%s",
                                    type(ex).__name__,
                                )

                # ----------------------------
                # MUNICIPIO: Reclamo
                # ----------------------------
                elif name == "crear_reclamo":
                    from services.actions.municipio_actions import CrearReclamoActionHandler

                    ctx = {
                        "user_obj": self.owner_user,
                        "viewer_user_obj": self.user,
                        "channel": "voice",
                        "tenant_id": getattr(self.tenant_profile, "id", None),
                        "idempotency_key": effect_idempotency_key,
                        "anon_id": self._normalize_phone(self.from_number),
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

                    # The action handler already has durable ticket-level
                    # replay by confirmation id. Bind it to this Realtime call
                    # instead of trusting a model-supplied key.
                    args["claim_confirmation_id"] = effect_idempotency_key

                    if self.last_ticket_nro:
                        logger.info(
                            "[VOICE] Skipping duplicate ticket creation ticket_ref=%s",
                            _safe_reference(self.last_ticket_nro),
                        )
                        result = (
                            f"Ya tenés registrado el reclamo número {self.last_ticket_nro}. "
                            "He tomado nota de los detalles adicionales."
                        )
                        result = self._complete_realtime_tool_call(
                            session_context,
                            call_id=call_id,
                            tool_name=name,
                            arguments_hash=arguments_hash,
                            effect_idempotency_key=effect_idempotency_key,
                            output=result,
                        )
                        self._send_tool_result(call_id, result)
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
                                    tracking_url = f"{tracking_url}#pin={data.get('consulta_pin')}"
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
                                logger.info(
                                    "[VOICE] Rich receipt accepted ticket_ref=%s",
                                    _safe_reference(nro),
                                )
                            except Exception as ex:
                                logger.error(
                                    "[VOICE] Could not send claim summary error_type=%s",
                                    type(ex).__name__,
                                )

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
                        "tenant_id": getattr(self.tenant_profile, "id", None),
                        "idempotency_key": effect_idempotency_key,
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
                                logger.warning(
                                    "[VOICE] Could not send order summary error_type=%s",
                                    type(ex).__name__,
                                )

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
                # MUNICIPIO: Estado de reclamo
                # ----------------------------
                elif name == "consultar_estado_reclamo":
                    ticket, reason = self._find_municipio_ticket_for_voice(
                        args.get("nro_ticket") or args.get("id_ticket_mencionado"),
                        args.get("pin") or args.get("consulta_pin"),
                    )
                    if reason == "missing_ticket":
                        result = "Necesito el numero de reclamo para consultar el estado."
                    elif reason == "missing_pin":
                        result = "Necesito el PIN de consulta para validar ese reclamo."
                    elif reason == "not_found":
                        result = "No encontre ese reclamo con los datos recibidos. Revisemos numero y PIN."
                    else:
                        asunto = getattr(ticket, "asunto", None) or getattr(ticket, "categoria", None) or "reclamo"
                        estado = getattr(ticket, "estado", None) or "sin estado"
                        result = self._voice_compact_text(
                            f"El reclamo {ticket.nro_ticket} sobre {asunto} esta en estado {estado}."
                        )

                # ----------------------------
                # MUNICIPIO: Tramites
                # ----------------------------
                elif name == "consultar_tramite":
                    from services.actions.municipio_actions import ConsultarInfoTramiteActionHandler

                    ctx = {
                        "user_obj": self.owner_user,
                        "viewer_user_obj": self.user,
                        "channel": "voice",
                        "chat_db_context_data": chat_data,
                    }
                    handler = ConsultarInfoTramiteActionHandler(ctx)
                    res = handler.execute(
                        {
                            "nombre_tramite": args.get("nombre_tramite")
                            or args.get("tramite")
                            or args.get("categoria")
                        }
                    )
                    result = self._voice_compact_text(
                        res.get("message_to_user") or "No encontre informacion de ese tramite."
                    )

                # ----------------------------
                # PYME: Estado de pedido
                # ----------------------------
                elif name == "consultar_estado_pedido":
                    pedido, reason = self._find_pyme_order_for_voice(args.get("nro_pedido"))
                    if reason == "missing_order":
                        result = "Necesito el numero de pedido para consultar el estado."
                    elif reason == "not_found":
                        result = "No encontre ese pedido para este comercio. Revisemos el numero."
                    else:
                        monto = getattr(pedido, "monto_total", None)
                        monto_text = f" Total registrado: {monto}." if monto is not None else ""
                        result = self._voice_compact_text(
                            f"El pedido {pedido.nro_pedido} esta en estado {pedido.estado}.{monto_text}"
                        )

                # ----------------------------
                # COLEGIO: Estado de caso escolar
                # ----------------------------
                elif name == "consultar_caso_escolar":
                    alias, ticket, reason = self._find_school_case_for_voice(args.get("school_case_id"))
                    if reason == "missing_case":
                        result = "Necesito el numero de caso escolar para consultar el seguimiento."
                    elif reason == "invalid_case":
                        result = "Ese numero de caso escolar no parece valido. Repetimelo por favor."
                    elif reason == "not_found":
                        result = "No encontre ese caso escolar para este colegio."
                    else:
                        estado = getattr(ticket, "estado", None) or "registrado"
                        case_type = getattr(alias, "case_type", None) or "consulta"
                        result = self._voice_compact_text(
                            f"El caso escolar {alias.id}, de tipo {case_type}, esta en estado {estado}."
                        )

                # ----------------------------
                # SaaS comercial: Lead Chatboc
                # ----------------------------
                elif name == "capturar_lead_comercial":
                    result = self._capture_commercial_lead(args, session_context)

                # ----------------------------
                # PYME: Cotizacion de envio
                # ----------------------------
                elif name == "cotizar_envio":
                    result = self._estimate_delivery_quote(args, session_context)

                # ----------------------------
                # COLEGIO: Intencion de pago
                # ----------------------------
                elif name == "registrar_intencion_pago_colegio":
                    result = self._register_school_payment_intent(args, session_context)

                # ----------------------------
                # Multi-rubro: solicitud operativa generica
                # ----------------------------
                elif name == "registrar_solicitud_operativa":
                    result = self._register_operational_request(args, session_context)

                # ----------------------------
                # Transferir humano
                # ----------------------------
                elif name == "transferir_humano":
                    motivo = args.get("motivo", "General")
                    target_number = None

                    if self.tenant_profile and self.tenant_profile.configuracion:
                        target_number = self.tenant_profile.configuracion.get("human_handoff_number")

                    target_number = _normalize_voice_e164(target_number)
                    if not target_number:
                        result = (
                            "No hay un número de atención humana configurado para esta organización. "
                            "Dejo tu solicitud registrada para seguimiento."
                        )
                    else:
                        result = "Voy a solicitar la transferencia con un agente."

                    account_sid = _runtime_config_value("TWILIO_ACCOUNT_SID")
                    auth_token = _runtime_config_value("TWILIO_AUTH_TOKEN")
                    if target_number and account_sid and auth_token and self.call_sid:
                        try:
                            client = TwilioClient(account_sid, auth_token)
                            transfer_twiml = VoiceResponse()
                            transfer_twiml.say(
                                "Te comunico con un representante. Aguarda un momento, por favor.",
                                language="es-AR",
                            )
                            transfer_twiml.dial(target_number)
                            client.calls(self.call_sid).update(twiml=str(transfer_twiml))
                            result = (
                                "La solicitud de transferencia fue aceptada. "
                                "Aguarda mientras el proveedor intenta conectarte con un agente."
                            )
                            logger.info(
                                "[VOICE] Transfer accepted call_ref=%s",
                                _safe_reference(self.call_sid),
                            )
                        except Exception as exc:
                            logger.error(
                                "[VOICE] Transfer outcome unknown error_type=%s",
                                type(exc).__name__,
                            )
                            result = (
                                "No pude confirmar la transferencia. Para evitar duplicarla, "
                                "dejo tu solicitud registrada para seguimiento."
                            )
                    elif target_number:
                        logger.error("[VOICE] Transfer refused reason=provider_not_configured")
                        result = (
                            "La transferencia no está disponible en este momento. "
                            "Dejo tu solicitud registrada para seguimiento."
                        )

                # ----------------------------
                # Finalizar llamada
                # ----------------------------
                elif name == "finalizar_llamada":
                    result = "Perfecto. Gracias, hasta luego."
                    self.pending_end_call = True

                result = self._complete_realtime_tool_call(
                    session_context,
                    call_id=call_id,
                    tool_name=name,
                    arguments_hash=arguments_hash,
                    effect_idempotency_key=effect_idempotency_key,
                    output=result,
                )

            # Return tool output to OpenAI
            self._send_tool_result(call_id, result)

        except Exception as exc:
            try:
                db.session.rollback()
            except Exception:
                pass
            output = _realtime_tool_error_output("tool_execution_unknown")
            output = self._mark_realtime_tool_call_unknown_for_session(
                call_id=call_id,
                tool_name=name or "unknown",
                arguments_hash=arguments_hash,
                effect_idempotency_key=effect_idempotency_key,
                output=output,
                error_code="tool_execution_unknown",
            )
            logger.error(
                "[VOICE] Tool execution outcome unknown tool=%s error_type=%s",
                tool_log_name,
                type(exc).__name__,
            )
            self._send_tool_result(call_id, output)
