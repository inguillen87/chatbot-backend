import logging
import re
import os
from flask import current_app, url_for
from models import WhatsappNumero, User, ChatSessionContext
from extensions import db
from utils.db_utils import safe_flag_modified
from sqlalchemy.orm import joinedload
from twilio.rest import Client
from utils.whatsapp import enviar_mensaje_whatsapp_con_fallback

logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
MESSAGING_SERVICE_SID = os.environ.get("MESSAGING_SERVICE_SID")

def initiate_outbound_call(to_number, from_number):
    """
    Triggers an outbound call to the user using Twilio.
    The call will connect to the /voice/welcome webhook.
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.error("Twilio credentials missing for voice call.")
        return False

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    # In production, this must be the public HTTPS URL.
    # We use url_for with _external=True to generate absolute URL.
    # Note: Flask's SERVER_NAME or equivalent must be set correctly, or use APP_BASE_URL config.
    base_url = current_app.config.get("APP_BASE_URL") or current_app.config.get("BACKEND_URL")
    if not base_url:
        logger.error("APP_BASE_URL or BACKEND_URL not set. Cannot trigger voice call.")
        return False

    url = f"{base_url.rstrip('/')}/twilio/voice/inbound"

    try:
        # Sanitize numbers for Voice API (E.164 required, no whatsapp: prefix)
        to_number_voice = to_number.replace("whatsapp:", "").strip()

        # Determine valid Caller ID
        # If from_number is a WhatsApp ID (e.g. whatsapp:+1...), it cannot be used as Caller ID.
        # We must use a verified Twilio number.
        from_number_voice = from_number.replace("whatsapp:", "").strip()

        # Check if from_number is likely valid (e.g. matching configured numbers)
        # For simplicity/safety, we prefer the environment variable TWILIO_PHONE_NUMBER if set
        twilio_verified_number = os.environ.get("TWILIO_PHONE_NUMBER")

        if twilio_verified_number:
            from_number_voice = twilio_verified_number

        call = client.calls.create(
            to=to_number_voice,
            from_=from_number_voice,
            url=url,
            method="POST"
        )
        logger.info(f"Outbound call initiated SID: {call.sid}")
        return True
    except Exception as e:
        logger.error(f"Failed to initiate outbound call: {e}")
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
        whatsapp_mapping = WhatsappNumero.query.options(
             joinedload(WhatsappNumero.user).joinedload(User.rubro)
        ).filter(WhatsappNumero.numero_whatsapp.ilike(f"%{bot_phone_clean.replace('+','').replace(' ','')}%")).first()

        if not whatsapp_mapping:
             logger.warning(f"Could not find tenant for bot phone {bot_phone_clean}")
             return {"text": "Lo siento, hubo un error de configuración.", "audio_url": None}

        client_user = whatsapp_mapping.user

        # Find End User (The Caller)
        from services.pymes import get_or_create_user_by_phone
        end_user = get_or_create_user_by_phone(user_phone_clean, client_user)

        # 2. Load/Create Chat Session
        # Use a distinct session ID for voice to avoid state conflicts with WhatsApp
        empresa_id = client_user.id
        # FIX: Ensure ID fits in VARCHAR(36). Use CallSid directly if available (34 chars) or hash.
        # CallSid is typically CA... (34 chars). Prefixing it breaks the limit.
        if call_sid and len(call_sid) <= 36:
            chat_session_id = call_sid
        else:
            # Fallback: truncate/hash to fit
            import hashlib
            raw_id = f"v_{empresa_id}_{user_phone_clean}"
            chat_session_id = hashlib.md5(raw_id.encode()).hexdigest()

        session_context = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id).first()
        if not session_context:
            session_context = ChatSessionContext(
                chat_session_id=chat_session_id,
                user_id=empresa_id,
                context_data={}
            )
            db.session.add(session_context)
            db.session.commit()

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
                if MESSAGING_SERVICE_SID:
                    kwargs["messaging_service_sid"] = MESSAGING_SERVICE_SID

                # Format the body to be friendly
                clean_body = message_body
                # If message body is just a URL or very short, add context
                if len(message_body) < 100 or message_body.startswith("http"):
                    clean_body = f"Aquí tienes la información de tu llamada:\n\n{message_body}"

                enviar_mensaje_whatsapp_con_fallback(
                    numero_destino=user_phone_clean,
                    cuerpo=clean_body,
                    **kwargs
                )

                # Append a spoken notification
                message_body += " Te acabo de enviar un mensaje con los enlaces y la información detallada para que la tengas a mano."
                logger.info(f"Sent OOB message with URLs to {user_phone_clean}")
            except Exception as e:
                logger.error(f"Failed to send OOB message: {e}")

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
                     return {"type": "handoff", "target": agent_number, "text": "Te estoy transfiriendo con un representante. Aguarda un momento."}

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
        except Exception as e:
            logger.error(f"Failed to generate TTS audio: {e}")

        return {"text": speech_text, "audio_url": audio_url}

    except Exception as e:
        logger.error(f"Error in handle_voice_interaction: {e}", exc_info=True)
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
        whatsapp_mapping = WhatsappNumero.query.options(
             joinedload(WhatsappNumero.user)
        ).filter(WhatsappNumero.numero_whatsapp.ilike(f"%{bot_phone_clean.replace('+','').replace(' ','')}%")).first()

        if not whatsapp_mapping:
            logger.warning(f"Could not find tenant for bot phone {bot_phone_clean}")
            return

        client_user = whatsapp_mapping.user
        empresa_id = client_user.id

        # Determine chat_session_id used during voice call
        # Logic matches handle_voice_interaction
        if call_sid and len(call_sid) <= 36:
            chat_session_id = call_sid
        else:
            import hashlib
            raw_id = f"v_{empresa_id}_{user_phone_clean}"
            chat_session_id = hashlib.md5(raw_id.encode()).hexdigest()

        # Retrieve the session context to find created ticket info
        session_context = ChatSessionContext.query.filter_by(chat_session_id=chat_session_id).first()

        ticket_info_text = ""
        context_data = session_context.context_data if session_context else {}

        # Check for Municipio Ticket
        municipio_ctx = context_data.get("contexto_municipio_v2", {})
        # Not all flows save to 'ultimo_ticket_creado' but action handlers often return data
        # We rely on 'numero_ticket_creado_sesion' or similar if persisted.
        # Alternatively, we can check the last message body if it contains ticket info,
        # but structured data is better.

        # Let's check for standard keys we'll ensure are saved
        created_ticket_id = context_data.get("latest_ticket_id") or municipio_ctx.get("ultimo_ticket_creado")
        created_ticket_nro = context_data.get("latest_ticket_nro")
        tracking_url = context_data.get("latest_tracking_url")

        if created_ticket_nro:
            ticket_info_text = (
                f"✅ *Ticket generado con éxito*\n"
                f"Número: *{created_ticket_nro}*\n"
            )
            if tracking_url:
                ticket_info_text += f"Seguí el estado aquí: {tracking_url}\n"

        # Check for Pyme Order
        pyme_ctx = context_data.get("contexto_pyme_v2", {})
        created_order_id = pyme_ctx.get("ultimo_ticket_creado") # Usually stores ID
        # Pyme logic saves order number in 'nro_pedido' inside success payload
        # If we can't find it easily, we default to generic message.

        summary_text = "Gracias por tu llamada."

        # Determine incomplete status to recover
        # If ticket was NOT created, but we have partial data, prompt for it.
        if not ticket_info_text:
            # Check Municipio partials
            datos_parciales = municipio_ctx.get("datos_parciales_llm_reclamo", {})
            if datos_parciales.get("descripcion") and not datos_parciales.get("ubicacion"):
                summary_text = "⚠️ Se cortó la llamada y me faltó la ubicación para terminar tu reclamo. Por favor escribí la dirección o compartí tu ubicación por acá."
            elif datos_parciales.get("descripcion") and not municipio_ctx.get("ultimo_ticket_creado"):
                summary_text = "⚠️ Se cortó la llamada antes de confirmar el reclamo. Por favor escribí 'continuar' para terminarlo."

            # Check Pyme partials (simplified check)
            elif pyme_ctx.get("estado_conversacion") and pyme_ctx.get("estado_conversacion") != "IDLE":
                 summary_text = "⚠️ Se cortó la llamada. Si querés retomar tu pedido, escribí 'hola' por acá."

        if ticket_info_text:
            summary_text = f"{summary_text}\n\n{ticket_info_text}"

            # Append photo prompt if applicable (Municipality)
            if created_ticket_nro and "M-" in str(created_ticket_nro):
                 summary_text += "\n\n📷 Si tenés una foto del problema, podés enviarla respondiendo a este mensaje."

        enviar_mensaje_whatsapp_con_fallback(
            numero_destino=user_phone_clean,
            cuerpo=f"{summary_text}\n\nSi necesitas algo más, podés escribirnos por aquí."
        )
        logger.info(f"Sent post-call summary to {user_phone_clean}")

    except Exception as e:
        logger.error(f"Error handling call status: {e}", exc_info=True)

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
