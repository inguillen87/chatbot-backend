import logging
import hashlib
import re
import os
from flask import current_app, has_app_context, url_for
from models import WhatsappNumero, User, ChatSessionContext, TenantProfile
from extensions import db
from utils.db_utils import safe_flag_modified
from sqlalchemy.orm import joinedload
from twilio.rest import Client
from utils.whatsapp import enviar_mensaje_whatsapp_con_fallback
from services.whatsapp_receipts import render_ticket_whatsapp
from services import promo_service
from services.config_loader import cargar_configuracion_municipio
from services.llm_provider_network_policy import provider_network_allowed
from services.voice_session_service import resolve_voice_chat_session_id
from services.voice_consent_lifecycle import voice_phone_candidates
from services.tenant_ticket_scope import (
    resolve_unique_tenant_for_owner,
    tenant_owner_ids,
)
from services.channel_session_identity import (
    channel_session_identity_enabled,
    resolve_channel_session_identity,
)

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
MESSAGING_SERVICE_SID = os.environ.get("MESSAGING_SERVICE_SID")


def _runtime_config_value(name: str, legacy_value: str | None = None) -> str | None:
    if has_app_context():
        configured = current_app.config.get(name)
        if configured not in (None, ""):
            return str(configured).strip()
    configured = os.environ.get(name)
    if configured not in (None, ""):
        return str(configured).strip()
    return str(legacy_value).strip() if legacy_value not in (None, "") else None


def _safe_reference(value: object) -> str:
    raw = str(value or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12] if raw else "missing"


def _voice_tenant_for_owner(owner_user: User | None) -> TenantProfile | None:
    if owner_user is None:
        return None
    try:
        owner_id = int(owner_user.id)
    except (TypeError, ValueError):
        return None
    tenant_id = getattr(owner_user, "tenant_id", None)
    if tenant_id:
        try:
            tenant = db.session.get(TenantProfile, int(tenant_id))
        except (TypeError, ValueError):
            return None
        return tenant if tenant is not None and owner_id in tenant_owner_ids(tenant) else None

    declared = [
        tenant
        for tenant in (
            getattr(owner_user, "tenant", None),
            getattr(owner_user, "tenant_profile", None),
            getattr(owner_user, "tenant_profile_municipio", None),
            getattr(owner_user, "tenant_profile_pyme", None),
        )
        if getattr(tenant, "id", None) is not None
    ]
    declared_ids = {int(tenant.id) for tenant in declared}
    if len(declared_ids) == 1:
        tenant = declared[0]
        return tenant if owner_id in tenant_owner_ids(tenant) else None
    if declared_ids:
        return None
    try:
        resolution = resolve_unique_tenant_for_owner(owner_id)
    except ValueError:
        return None
    return resolution.tenant if resolution.status == "unique" else None


def _resolve_unique_voice_whatsapp_mapping(bot_phone: str) -> WhatsappNumero | None:
    """Resolve one sender binding without selecting an arbitrary tenant row."""

    mappings = (
        WhatsappNumero.query.options(joinedload(WhatsappNumero.user))
        .filter(
            WhatsappNumero.numero_whatsapp.in_(voice_phone_candidates(bot_phone)),
            WhatsappNumero.is_active.is_(True),
        )
        .order_by(WhatsappNumero.id.asc())
        .all()
    )
    if not mappings:
        return None

    bindings = set()
    for mapping in mappings:
        tenant = _voice_tenant_for_owner(getattr(mapping, "user", None))
        tenant_id = getattr(tenant, "id", None)
        user_id = getattr(getattr(mapping, "user", None), "id", None)
        if tenant_id is None or user_id is None:
            return None
        bindings.add((int(tenant_id), int(user_id)))
    return mappings[0] if len(bindings) == 1 else None


def _resolve_voice_session_context(
    *,
    owner_user: User,
    user_phone_clean: str,
    bot_phone_clean: str,
    call_sid: str,
    create_if_missing: bool,
) -> tuple[ChatSessionContext | None, ChatSessionContext | None]:
    """Resolve voice and WhatsApp contexts without phone-derived global IDs."""

    tenant = _voice_tenant_for_owner(owner_user)
    tenant_id = getattr(tenant, "id", None)
    source_session = None
    if tenant_id and channel_session_identity_enabled(current_app.config):
        voice_identity = resolve_channel_session_identity(
            config=current_app.config,
            tenant_id=tenant_id,
            channel="voice",
            provider="twilio",
            provider_identity=user_phone_clean,
            owner_user_id=owner_user.id,
            create_if_missing=create_if_missing,
        )
        if voice_identity is None:
            return None, None
        session_context = ChatSessionContext.query.filter_by(
            chat_session_id=voice_identity.chat_session_id,
            tenant_id=tenant_id,
        ).first()
        whatsapp_identity = resolve_channel_session_identity(
            config=current_app.config,
            tenant_id=tenant_id,
            channel="whatsapp",
            provider="twilio",
            provider_identity=user_phone_clean,
            owner_user_id=owner_user.id,
            create_if_missing=False,
        )
        if whatsapp_identity is not None:
            source_session = ChatSessionContext.query.filter_by(
                chat_session_id=whatsapp_identity.chat_session_id,
                tenant_id=tenant_id,
            ).first()
    else:
        chat_session_id = resolve_voice_chat_session_id(
            call_sid=call_sid,
            from_number=user_phone_clean,
            to_number=bot_phone_clean,
        )
        session_context = ChatSessionContext.query.filter_by(
            chat_session_id=chat_session_id
        ).first()
        if session_context is None and create_if_missing:
            session_context = ChatSessionContext(
                chat_session_id=chat_session_id,
                user_id=owner_user.id,
                tenant_id=tenant_id,
                anon_id=user_phone_clean,
                context_data={},
            )
            db.session.add(session_context)
            db.session.commit()

    if session_context is None:
        return None, source_session
    if not isinstance(session_context.context_data, dict):
        session_context.context_data = {}
    if source_session and isinstance(source_session.context_data, dict):
        merged_context = dict(source_session.context_data)
        merged_context.update(session_context.context_data or {})
        merged_context["source_chat_session_id"] = source_session.chat_session_id
        session_context.context_data = merged_context
        safe_flag_modified(session_context, "context_data")
    changed = False
    if tenant_id and session_context.tenant_id is None:
        session_context.tenant_id = tenant_id
        changed = True
    if not session_context.anon_id:
        session_context.anon_id = user_phone_clean
        changed = True
    if changed or source_session is not None:
        db.session.add(session_context)
        db.session.commit()
    return session_context, source_session

def initiate_outbound_call(to_number, from_number, chat_session_id=None):
    """
    Triggers an outbound call to the user using Twilio.
    The call will connect to the /voice/welcome webhook.
    """
    account_sid = _runtime_config_value("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID)
    auth_token = _runtime_config_value("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN)
    if not account_sid or not auth_token:
        logger.error("Twilio credentials missing for voice call.")
        return False
    if not provider_network_allowed("twilio"):
        logger.info(
            "Outbound call blocked provider=twilio reason=test_network_disabled"
        )
        return False

    # In production, this must be the public HTTPS URL.
    # We use url_for with _external=True to generate absolute URL.
    # Note: Flask's SERVER_NAME or equivalent must be set correctly, or use APP_BASE_URL config.
    base_url = _runtime_config_value("APP_BASE_URL") or _runtime_config_value("BACKEND_URL")
    if not base_url:
        logger.error("APP_BASE_URL or BACKEND_URL not set. Cannot trigger voice call.")
        return False

    url = f"{base_url.rstrip('/')}/twilio/voice/inbound"
    if chat_session_id:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode({'chat_session_id': chat_session_id})}"

    try:
        # The action boundary already resolves a dedicated voice-enabled caller
        # ID. Never replace it with the generic WhatsApp sender: being valid for
        # messaging does not make a number valid for PSTN voice.
        to_number_voice = str(to_number or "").replace("whatsapp:", "").strip()
        from_number_voice = str(from_number or "").replace("whatsapp:", "").strip()
        e164_pattern = re.compile(r"^\+[1-9]\d{7,14}$")
        if not e164_pattern.fullmatch(to_number_voice):
            logger.error("Outbound call refused reason=invalid_destination")
            return False
        if not e164_pattern.fullmatch(from_number_voice):
            logger.error("Outbound call refused reason=invalid_voice_caller_id")
            return False

        status_callback = f"{base_url.rstrip('/')}/voice/status"

        client = Client(account_sid, auth_token)
        call = client.calls.create(
            to=to_number_voice,
            from_=from_number_voice,
            url=url,
            method="POST",
            status_callback=status_callback,
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )
        logger.info(
            "Outbound call accepted provider=twilio call_ref=%s",
            _safe_reference(getattr(call, "sid", None)),
        )
        return True
    except Exception as exc:
        logger.error(
            "Outbound call request failed provider=twilio error_type=%s outcome=unknown",
            type(exc).__name__,
        )
        return False

def handle_voice_interaction(user_speech, user_phone, bot_phone, call_sid):
    """
    Core logic to process voice input using the existing chatbot infrastructure.
    """
    try:
        # 1. Resolve Users (Tenant & End User) similar to WhatsApp Webhook
        user_phone_clean = user_phone.replace("whatsapp:", "").strip()
        bot_phone_clean = bot_phone.replace("whatsapp:", "").strip()

        # Find the Tenant (Owner) via the Bot's phone number
        whatsapp_mapping = _resolve_unique_voice_whatsapp_mapping(bot_phone_clean)

        if not whatsapp_mapping:
             logger.warning("Voice tenant resolution failed reason=sender_not_registered")
             return {"text": "Lo siento, hubo un error de configuración.", "audio_url": None}

        client_user = whatsapp_mapping.user
        whatsapp_sender = whatsapp_mapping.numero_whatsapp

        # Find End User (The Caller)
        from services.pymes import get_or_create_user_by_phone
        end_user = get_or_create_user_by_phone(user_phone_clean, client_user)

        # 2. Load/Create a tenant-scoped voice context and link WhatsApp only
        # through its verified identity binding.
        session_context, _source_session = _resolve_voice_session_context(
            owner_user=client_user,
            user_phone_clean=user_phone_clean,
            bot_phone_clean=bot_phone_clean,
            call_sid=call_sid,
            create_if_missing=True,
        )
        if session_context is None:
            logger.error("Voice session identity unavailable")
            return {
                "text": "No pude validar la sesion de esta organizacion.",
                "audio_url": None,
            }
        chat_session_id = session_context.chat_session_id

        # 3. Call Responder Logic
        from services.logic import responder_chatboc

        # Inject Voice-Specific Instructions into context for LLM
        context_data = session_context.context_data or {}
        # We add a transient flag or instruction.
        # Note: responder_chatboc uses this context.
        context_data["_voice_mode"] = True
        session_context.context_data = context_data

        response_dict = responder_chatboc(
            pregunta=user_speech,
            owner_user=client_user,
            current_user=end_user,
            rubro_obj=client_user.rubro,
            chat_db_context=session_context,
            chat_session_uuid=chat_session_id,
            channel="voice"
        )

        # 4. Process Response for Voice
        message_body = response_dict.get('message_body', "No tengo respuesta.")

        # Universal slot-based confirmation before execution-sensitive actions
        accion_backend = response_dict.get("accion_backend")
        datos = response_dict.get("datos_estructura") if isinstance(response_dict.get("datos_estructura"), dict) else {}
        needs_confirmation = accion_backend in {"crear_reclamo", "iniciar_reclamo", "crear_pedido", "finalizar_pedido_pyme"}
        if needs_confirmation and not bool(datos.get("confirmado_por_usuario")):
            if accion_backend in {"crear_reclamo", "iniciar_reclamo"}:
                confirmation = build_claim_confirmation_payload(
                    categoria=datos.get("categoria"),
                    ubicacion=datos.get("ubicacion") or datos.get("direccion"),
                    descripcion=datos.get("descripcion"),
                    nombre=datos.get("nombre") or datos.get("nombre_usuario_detectado"),
                    telefono=datos.get("telefono") or datos.get("telefono_detectado"),
                    email=datos.get("email") or datos.get("email_detectado"),
                    channel="voice",
                )
            else:
                confirmation = build_order_confirmation_payload(
                    cart_summary=datos.get("cart_summary") if isinstance(datos.get("cart_summary"), dict) else {},
                    customer={
                        "nombre": datos.get("nombre") or datos.get("nombre_usuario_detectado"),
                        "telefono": datos.get("telefono") or datos.get("telefono_detectado"),
                        "email": datos.get("email") or datos.get("email_detectado"),
                        "direccion": datos.get("ubicacion") or datos.get("direccion"),
                    },
                    delivery_address=datos.get("ubicacion") or datos.get("direccion"),
                    channel="voice",
                )
            message_body = confirmation.get("summary_voice", message_body) + " ¿Está correcto para continuar?"
            response_dict.setdefault("data", {})["voice_confirmation"] = confirmation
            response_dict["pedir_info"] = "confirmacion"


        # --- Detect URLs to send via Message (Out-of-band delivery) ---
        # Voice cannot convey URLs effectively. If the response contains links (e.g. payment, ticket),
        # we send them via WhatsApp/SMS and notify the user.
        found_urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', message_body)

        # Check options for URLs
        options_list = response_dict.get('options_list', [])
        for opt in options_list:
            if isinstance(opt, dict) and opt.get("url"):
                found_urls.append(opt["url"])

        if found_urls:
            try:
                # Send the full text (with links) to the user's phone
                kwargs = {}
                if whatsapp_sender:
                    kwargs["from_number"] = whatsapp_sender
                elif MESSAGING_SERVICE_SID:
                    kwargs["messaging_service_sid"] = MESSAGING_SERVICE_SID

                # Format the body to be friendly
                clean_body = message_body
                # If message body is just a URL or very short, add context
                if len(message_body) < 100 or message_body.startswith("http"):
                    clean_body = f"Aquí tienes la información de tu llamada:\n\n{message_body}"

                enviar_mensaje_whatsapp_con_fallback(
                    numero_destino=user_phone_clean,
                    cuerpo=clean_body,
                    messaging_service_sid=MESSAGING_SERVICE_SID,
                    **kwargs
                )

                # Append a spoken notification
                message_body += " Te acabo de enviar un mensaje con los enlaces y la información detallada para que la tengas a mano."
                logger.info("Voice out-of-band message accepted channel=whatsapp")
            except Exception as exc:
                logger.error(
                    "Voice out-of-band message failed error_type=%s",
                    type(exc).__name__,
                )

        # Check for Human Handoff Intent
        accion_backend = response_dict.get("accion_backend")
        transfer_to_human = False

        # Simple heuristic: specific action or keyword in text if action not explicit
        if accion_backend in ["transferir_agente", "hablar_con_humano"]:
            transfer_to_human = True

        # Fallback check on text if no action set
        if not transfer_to_human and "te comunico con un representante" in message_body.lower():
            transfer_to_human = True

        if transfer_to_human:
             # --- Plan Check for Handoff: Only "full" plans allow transfer ---
             plan = "free"
             tenant_profile = (
                getattr(client_user, "tenant", None)
                or getattr(client_user, "tenant_profile", None)
             )
             if tenant_profile:
                 plan = str(tenant_profile.plan or "free").lower()
             elif hasattr(client_user, "plan"):
                 plan = str(client_user.plan or "free").lower()

             # Simplified plans: gratuito, pro, full
             allowed_plans = {"full"}

             # Map legacy high-tier plans to full
             if plan in {"premium", "enterprise", "municipio_full"}:
                 plan = "full"

             if plan in allowed_plans:
                 # Return special signal for Dial
                 # We need a configured phone number for the agent.
                 # This should ideally be in TenantConfig or User profile.
                 # Fallback to a placeholder or specific field if exists.
                 agent_number = None
                 if tenant_profile and tenant_profile.configuracion:
                     agent_number = tenant_profile.configuracion.get("telefono_atencion")

                 if not agent_number:
                     agent_number = client_user.telefono # Fallback to owner phone

                 if agent_number:
                    return {
                        "type": "handoff",
                        "target": agent_number,
                        "text": (
                            "Voy a intentar comunicarte con un representante. "
                            "Aguarda un momento."
                        ),
                    }

             # If plan not allowed or no number found, fall through to standard response
             message_body = "Lo siento, la transferencia a humanos no está disponible en este momento. Por favor deja tu mensaje."

        # Handling Pedir Info (Explicitly ask if needed)
        pedir_info = response_dict.get("pedir_info")

        # Clean up text for speech
        speech_text = _clean_text_for_speech(message_body)

        if pedir_info:
            # If the bot is asking for info but the message body is short or doesn't seem to ask clearly,
            # append a specific question.
            # Convert list to single string if needed, or pick first item
            if isinstance(pedir_info, list):
                info_needed = pedir_info[0]
            else:
                info_needed = str(pedir_info)

            # Simple heuristic: if '?' not in text, append question
            if "?" not in speech_text:
                friendly_map = {
                    "direccion": "¿Me decís la dirección exacta?",
                    "ubicacion": "¿Dónde estás ahora mismo? Decime la dirección.",
                    "nombre": "¿Me decís tu nombre completo?",
                    "telefono": "¿Me dictás tu número de teléfono?",
                    "dni": "¿Cuál es tu DNI?",
                    "email": "¿Me decís tu email?",
                    "descripcion": "¿Me contás bien qué pasó? Dame más detalles.",
                    "categoria": "¿De qué se trata el reclamo?"
                }
                # Default fallback
                default_q = f"Por favor, indicame: {info_needed.replace('_', ' ')}."

                question = friendly_map.get(info_needed, default_q)

                # Append with a conversational connector
                speech_text += f" {question}"

        # Handling Options
        options = response_dict.get('options_list', [])
        if options:
            speech_text += " Puedes decir: "
            option_texts = [opt.get('texto', '') for opt in options[:3]]
            speech_text += ", o ".join(option_texts)

        # Save context
        safe_flag_modified(session_context, "context_data")
        db.session.commit()

        # Try to generate premium audio
        from services.tts_orchestrator import generar_audio
        audio_url = None
        try:
            audio_url = generar_audio(speech_text)
        except Exception as exc:
            logger.error("Voice TTS failed error_type=%s", type(exc).__name__)

        return {"text": speech_text, "audio_url": audio_url}

    except Exception as exc:
        logger.error(
            "Voice interaction failed error_type=%s",
            type(exc).__name__,
            exc_info=True,
        )
        return {"text": "Hubo un error al procesar tu solicitud.", "audio_url": None}

def handle_call_status(call_sid, call_status, to_number, from_number, direction):
    """
    Handles call status updates (e.g. 'completed') to send a summary via WhatsApp.
    """
    if call_status not in ['completed']:
        return

    try:
        user_phone = to_number if direction == 'outbound-api' else from_number
        bot_phone = from_number if direction == 'outbound-api' else to_number

        user_phone_clean = user_phone.replace("whatsapp:", "").strip()
        bot_phone_clean = bot_phone.replace("whatsapp:", "").strip()

        # Identify Tenant
        whatsapp_mapping = _resolve_unique_voice_whatsapp_mapping(bot_phone_clean)

        if not whatsapp_mapping:
            logger.warning("Voice status tenant resolution failed reason=sender_not_registered")
            return

        client_user = whatsapp_mapping.user
        whatsapp_sender = whatsapp_mapping.numero_whatsapp
        # Retrieve only contexts proven for this tenant/provider identity. A
        # completion callback must never recover state by reconstructing a
        # phone-derived global WhatsApp session id.
        session_context, source_session = _resolve_voice_session_context(
            owner_user=client_user,
            user_phone_clean=user_phone_clean,
            bot_phone_clean=bot_phone_clean,
            call_sid=call_sid,
            create_if_missing=False,
        )

        context_data = {}

        if source_session and isinstance(source_session.context_data, dict):
            context_data.update(source_session.context_data)
        if session_context and isinstance(session_context.context_data, dict):
            context_data.update(session_context.context_data)

        if context_data.get("receipt_sent"):
            if context_data.get("latest_ticket_nro"):
                logger.info("Receipt already sent during stream. Skipping duplicate summary.")
                return
            logger.info("Receipt already sent during stream. Sending final summary anyway.")

        # Check for Municipio Ticket
        municipio_ctx = context_data.get("contexto_municipio_v2", {})
        created_ticket_nro = context_data.get("latest_ticket_nro")

        message_body = None
        media_url = None

        if created_ticket_nro:
            # Attempt to build a Rich Receipt
            try:
                # Resolve config
                municipio_id = client_user.municipio_id if hasattr(client_user, 'municipio_id') else None
                municipio_cfg = {}
                if municipio_id:
                    municipio_cfg = cargar_configuracion_municipio(str(municipio_id), "config.json")

                # Get promo info
                promo_section = promo_service.build_ticket_promo_section(
                    ticket_number=created_ticket_nro,
                    owner_user=client_user,
                    municipio_config=municipio_cfg
                )
                promo_image_url = None
                promo_text = None
                if promo_section:
                    promo_image_url = promo_section.get("image_url")
                    promo_text = promo_section.get("message_body")

                # Retrieve ticket details from context if available (fallback to generic)
                # context data usually has 'datos_reclamo' or 'datos_parciales_llm_reclamo'
                datos_reclamo = municipio_ctx.get("reclamo_flow_v2", {}).get("datos_reclamo", {})
                if not datos_reclamo:
                    datos_reclamo = municipio_ctx.get("datos_parciales_llm_reclamo", {})

                # Render receipt
                receipt = render_ticket_whatsapp(
                    kind="reclamo",
                    nombre=datos_reclamo.get("nombre") or municipio_ctx.get("contacto_usuario", {}).get("nombre") or "Vecino/a",
                    ticket_nro=created_ticket_nro,
                    categoria=datos_reclamo.get("categoria", "General"),
                    descripcion=datos_reclamo.get("descripcion", "Reclamo registrado telefónicamente"),
                    direccion=datos_reclamo.get("direccion") or datos_reclamo.get("ubicacion"),
                    dni=datos_reclamo.get("dni"),
                    consulta_pin=context_data.get("latest_ticket_pin"),
                    base_chat_url=municipio_cfg.get("base_chat_url", "https://www.chatboc.ar/chat"),
                    promo_image_url=promo_image_url,
                    promo_text=promo_text,
                    info_url=municipio_cfg.get("link_web") or municipio_cfg.get("url_web"),
                )

                message_body = receipt.get("body_text")
                media_url = receipt.get("media_url")

                # Append photo prompt
                if "M-" in str(created_ticket_nro):
                     message_body += "\n\n📷 Si tenés una foto del problema, podés enviarla respondiendo a este mensaje."

            except Exception as e_rich:
                logger.error(
                    "Voice fallback receipt build failed error_type=%s",
                    type(e_rich).__name__,
                    exc_info=True,
                )
                # Fallback to basic text if rich receipt fails
                message_body = (
                    f"Gracias por tu llamada.\n\n✅ *Ticket generado con éxito*\n"
                    f"Número: *{created_ticket_nro}*\n"
                )
                if context_data.get("latest_ticket_pin"):
                    message_body += f"PIN: *{context_data.get('latest_ticket_pin')}*\n"

        else:
            # Fallback/Recovery message logic
            summary_text = "Gracias por tu llamada."
            datos_parciales = municipio_ctx.get("datos_parciales_llm_reclamo", {})
            pyme_ctx = context_data.get("contexto_pyme_v2", {})

            if datos_parciales.get("descripcion") and not datos_parciales.get("ubicacion"):
                summary_text = "⚠️ Se cortó la llamada y me faltó la ubicación para terminar tu reclamo. Por favor escribí la dirección o compartí tu ubicación por acá."
            elif datos_parciales.get("descripcion") and not municipio_ctx.get("ultimo_ticket_creado"):
                summary_text = "⚠️ Se cortó la llamada antes de confirmar el reclamo. Por favor escribí 'continuar' para terminarlo."
            elif pyme_ctx.get("estado_conversacion") and pyme_ctx.get("estado_conversacion") != "IDLE":
                 summary_text = "⚠️ Se cortó la llamada. Si querés retomar tu pedido, escribí 'hola' por acá."

            message_body = f"{summary_text}\n\nSi necesitas algo más, podés escribirnos por aquí."

        enviar_mensaje_whatsapp_con_fallback(
            numero_destino=user_phone_clean,
            cuerpo=message_body,
            image_url=media_url,
            from_number=whatsapp_sender,
            messaging_service_sid=None if whatsapp_sender else MESSAGING_SERVICE_SID,
        )
        logger.info("Voice post-call summary accepted channel=whatsapp")

    except Exception as exc:
        logger.error(
            "Voice status handling failed error_type=%s",
            type(exc).__name__,
            exc_info=True,
        )

def _clean_text_for_speech(text):
    """
    Removes Markdown, URLs, and formatting to make text suitable for TTS.
    """
    if not text: return ""

    # Remove URLs
    text = re.sub(r'http\S+', '', text)
    # Remove Markdown bold/italic
    text = text.replace('*', '').replace('_', '')
    # Normalize newlines
    text = text.replace('\n', ' ')
    # Replace visual cues with audio cues
    text = text.replace('Hacé click en', 'Selecciona')
    text = text.replace('hacé click', 'seleccioná')
    # Remove emojis (basic range, can be improved)
    text = re.sub(r'[^\w\s,.\?!¡¿:;áéíóúÁÉÍÓÚñÑ-]', '', text)

    return text.strip()
