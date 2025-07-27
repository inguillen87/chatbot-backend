# import os # No es necesario si usamos current_app.config
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication # Para adjuntos
from twilio.rest import Client
from flask import current_app # Para acceder a la configuración

logger = logging.getLogger(__name__)

# Las variables de configuración SMTP y Twilio ahora se leerán de current_app.config
# SMTP_HOST = os.getenv("SMTP_HOST")
# SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
# SMTP_USERNAME = os.getenv("SMTP_USERNAME")
# SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
# FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USERNAME)
ADMIN_EMAIL = None # Se podría cargar desde config también: current_app.config.get("ADMIN_EMAIL")

# TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
# TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
# TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
# TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")


def _get_config_val(key, default=None, campaign_specific=False):
    """Helper para obtener valores de config, con fallback a campaña si se especifica."""
    if campaign_specific:
        val = current_app.config.get(f"{key}_CAMPAIGN", None)
        if val is not None:
            return val
    return current_app.config.get(key, default)


def enviar_email(destino: str, asunto: str, cuerpo_html: str, cuerpo_texto: str = "", es_campana: bool = False) -> bool:
    """
    Envía un email simple en formato HTML y opcionalmente texto plano.
    Si es_campana es True, intenta usar configuraciones SMTP específicas para campañas.
    """

    smtp_host = _get_config_val("SMTP_HOST", campaign_specific=es_campana)
    smtp_port = int(_get_config_val("SMTP_PORT", 587, campaign_specific=es_campana))
    smtp_user = _get_config_val("SMTP_USER", campaign_specific=es_campana)
    smtp_password = _get_config_val("SMTP_PASSWORD", campaign_specific=es_campana)
    from_email = _get_config_val("MAIL_FROM_ADDRESS", campaign_specific=es_campana)
    from_name = _get_config_val("MAIL_FROM_NAME", campaign_specific=es_campana)
    use_tls = _get_config_val("SMTP_USE_TLS", True, campaign_specific=es_campana)
    use_ssl = _get_config_val("SMTP_USE_SSL", False, campaign_specific=es_campana)

    if not all([smtp_host, smtp_port, from_email, destino]):
        logger.error(f"[EMAIL{' CAMPAIGN' if es_campana else ''}] Configuración SMTP incompleta o falta destino. Email no enviado a {destino}.")
        return False

    if not cuerpo_html and not cuerpo_texto:
        logger.error(f"Intento de enviar email a {destino} sin cuerpo_html ni cuerpo_texto.")
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = asunto
    msg['From'] = f"{from_name} <{from_email}>"
    msg['To'] = destino

    if cuerpo_texto:
        part_text = MIMEText(cuerpo_texto, 'plain', _charset='utf-8')
        msg.attach(part_text)

    if cuerpo_html: # HTML es la parte preferida si ambos existen
        part_html = MIMEText(cuerpo_html, 'html', _charset='utf-8')
        msg.attach(part_html)

    log_prefix = f"[EMAIL{' CAMPAIGN' if es_campana else ''}]"
    try:
        logger.info(f"{log_prefix} Intentando enviar a {destino} desde {from_email} via {smtp_host}:{smtp_port}")

        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port)

        if use_tls and not use_ssl:
            server.starttls()

        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)

        server.send_message(msg)
        server.quit()

        logger.info(f"{log_prefix} Enviado exitosamente a {destino}")
        return True
    except smtplib.SMTPAuthenticationError as e_auth:
        logger.error(f"{log_prefix} Error de autenticación SMTP: {e_auth}")
    except smtplib.SMTPServerDisconnected as e_disconnect:
        logger.error(f"{log_prefix} Servidor SMTP desconectado: {e_disconnect}")
    except smtplib.SMTPException as e_smtp:
        logger.error(f"{log_prefix} Error SMTP general: {e_smtp}", exc_info=True)
    except Exception as e:
        logger.error(f"{log_prefix} Error enviando correo: {e}", exc_info=True)
    return False


def enviar_email_con_adjunto(destino: str, asunto: str, cuerpo_html: str, nombre_archivo: str, contenido_adjunto: bytes, cuerpo_texto: str = "") -> bool:
    """Envía un email con un archivo adjunto."""
    smtp_host = current_app.config.get("SMTP_HOST")
    smtp_port = current_app.config.get("SMTP_PORT", 587)
    smtp_user = current_app.config.get("SMTP_USER")
    smtp_password = current_app.config.get("SMTP_PASSWORD")
    from_email = current_app.config.get("MAIL_FROM_ADDRESS")
    from_name = current_app.config.get("MAIL_FROM_NAME", from_email)
    use_tls = current_app.config.get("SMTP_USE_TLS", True)
    use_ssl = current_app.config.get("SMTP_USE_SSL", False)

    if not all([smtp_host, smtp_port, from_email, destino]):
        logger.error("[EMAIL_ADJ] Configuración SMTP incompleta o falta destino. Email no enviado.")
        return False

    msg = MIMEMultipart('mixed') # mixed para adjuntos, alternative para html/texto
    msg['Subject'] = asunto
    msg['From'] = f"{from_name} <{from_email}>"
    msg['To'] = destino

    # Contenido del cuerpo (HTML y/o texto)
    body_content = MIMEMultipart('alternative')
    if cuerpo_texto:
        body_content.attach(MIMEText(cuerpo_texto, "plain", "utf-8"))
    if cuerpo_html:
        body_content.attach(MIMEText(cuerpo_html, "html", "utf-8"))
    msg.attach(body_content)

    if contenido_adjunto:
        adj = MIMEApplication(contenido_adjunto, _subtype="pdf") # Asume PDF, podría ser parametrizado
        adj.add_header("Content-Disposition", "attachment", filename=nombre_archivo)
        msg.attach(adj)
    else:
        logger.warning(f"[EMAIL_ADJ] Contenido de adjunto vacío para {nombre_archivo}. Enviando sin adjunto.")


    try:
        logger.info(f"[EMAIL_ADJ] Intentando enviar a {destino} con adjunto {nombre_archivo}")
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port)
        if use_tls and not use_ssl:
            server.starttls()
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        server.send_message(msg) # send_message es mejor para MIME
        server.quit()
        logger.info(f"[EMAIL_ADJ] Enviado a {destino} con adjunto {nombre_archivo}")
        return True
    except Exception as e:
        logger.error(f"[EMAIL_ADJ] Error enviando correo con adjunto: {e}", exc_info=True)
        return False


def enviar_email_pedido_admin(pedido) -> bool:
    """Envía un correo al administrador con el nuevo pedido."""
    admin_email_val = current_app.config.get("ADMIN_EMAIL")
    if not admin_email_val or admin_email_val == "noreply@example.com":
        logger.warning("[EMAIL] ADMIN_EMAIL no configurado para notificación de pedido. Envío omitido.")
        return False

    detalles = pedido.detalles
    asunto = f"Nuevo pedido {pedido.nro_pedido}"
    cuerpo_html_pedido = (
        f"<h3>Nuevo pedido recibido</h3>"
        f"<p><strong>Número:</strong> {pedido.nro_pedido}</p>"
        f"<p><strong>Cliente:</strong> {pedido.nombre_cliente} - {pedido.email_cliente} - {pedido.telefono_cliente}</p>"
        f"<p><strong>Monto estimado:</strong> ${pedido.monto_total:,.2f}</p>"
        f"<pre>{detalles}</pre>"
    )
    # Para emails transaccionales, no marcamos como es_campana=True
    return enviar_email(admin_email_val, asunto, cuerpo_html_pedido)


def enviar_email_pedido_cliente(pedido) -> bool:
    """Envía un correo al cliente confirmando su pedido."""
    destino = getattr(pedido, "email_cliente", None)
    if not destino:
        logger.warning("[EMAIL] Pedido sin email de cliente.")
        return False

    asunto = f"Confirmación de pedido {pedido.nro_pedido}"
    cuerpo_html_confirmacion = (
        f"<p>Hola {pedido.nombre_cliente or ''},</p>"
        f"<p>Recibimos tu pedido <strong>{pedido.nro_pedido}</strong> y está en proceso.</p>"
        "<p>Te avisaremos cuando esté listo para el envío.</p>"
    )
    return enviar_email(destino, asunto, cuerpo_html_confirmacion)


def enviar_email_ticket_admin(ticket) -> bool:
    """Envía un correo al administrador con el nuevo ticket."""
    admin_email_val = current_app.config.get("ADMIN_EMAIL")
    if not admin_email_val or admin_email_val == "noreply@example.com":
        logger.warning("[EMAIL] ADMIN_EMAIL no configurado para notificación de ticket. Envío omitido.")
        return False

    asunto = f"Nuevo ticket {ticket.nro_ticket}"
    cuerpo_html_ticket = (
        f"<h3>Nuevo ticket registrado</h3>"
        f"<p><strong>Número:</strong> {ticket.nro_ticket}</p>"
        f"<p><strong>Asunto:</strong> {ticket.asunto}</p>"
        f"<p><strong>Categoría:</strong> {ticket.categoria}</p>"
        f"<p><strong>Pregunta:</strong> {ticket.pregunta}</p>"
    )
    if getattr(ticket, "telefono", None) or getattr(ticket, "email", None):
        cuerpo_html_ticket += (
            f"<p><strong>Contacto:</strong> {getattr(ticket, 'email', '')} "
            f"- {getattr(ticket, 'telefono', '')}</p>"
        )
    return enviar_email(admin_email_val, asunto, cuerpo_html_ticket)


def enviar_email_ticket_cliente(ticket) -> bool:
    """Confirma al cliente que su reclamo fue recibido."""
    destino = getattr(ticket, "email", None)
    if not destino:
        logger.warning("[EMAIL] Ticket sin email de cliente.")
        return False

    asunto = f"Reclamo {ticket.nro_ticket} recibido"
    cuerpo_html_confirmacion_ticket = (
        f"<p>Hola,</p>"
        f"<p>Registramos tu reclamo con número <strong>{ticket.nro_ticket}</strong>.</p>"
        "<p>Nos comunicaremos pronto para darle seguimiento.</p>"
    )
    return enviar_email(destino, asunto, cuerpo_html_confirmacion_ticket)


def enviar_email_ticket_novedad(ticket, mensaje: str) -> bool:
    """Notifica al cliente que su ticket tiene una novedad."""
    destino = getattr(ticket, "email", None)
    if not destino and getattr(ticket, "user_id", None):
        from models import User # Importar User aquí para evitar importación circular a nivel de módulo
        usuario = User.query.get(ticket.user_id)
        destino = getattr(usuario, "email", None)
    if not destino:
        logger.warning("[EMAIL] Ticket sin email para notificar novedad.")
        return False

    asunto = f"Actualización en tu ticket {ticket.nro_ticket}"
    cuerpo_html_novedad = (
        f"<p>Hola,</p>"
        f"<p>Se registró una nueva actividad en tu ticket <strong>{ticket.nro_ticket}</strong>.</p>"
        f"<p>{mensaje}</p>"
    )
    return enviar_email(destino, asunto, cuerpo_html_novedad)


def enviar_sms(destino: str, mensaje: str) -> bool:
    """Envía un SMS usando Twilio."""
    twilio_account_sid = current_app.config.get("TWILIO_ACCOUNT_SID")
    twilio_auth_token = current_app.config.get("TWILIO_AUTH_TOKEN")
    twilio_phone_number = current_app.config.get("TWILIO_PHONE_NUMBER")

    if not destino:
        logger.warning("[SMS] Destino no proporcionado.")
        return False
    if not all([twilio_account_sid, twilio_auth_token, twilio_phone_number]):
        logger.error("[SMS] Faltan credenciales de Twilio. Verificar configuración de la app.")
        return False

    try:
        client = Client(twilio_account_sid, twilio_auth_token)
        msg = client.messages.create(body=mensaje, from_=twilio_phone_number, to=destino)
        logger.info(f"[SMS] Enviado a {destino} (SID: {msg.sid}). Mensaje: '{mensaje[:30]}...'")
        return True
    except Exception as e:
        error_message = str(e)
        if hasattr(e, 'status') and hasattr(e, 'uri') and hasattr(e, 'msg'):
            error_message = f"Twilio API Error: Status {e.status}, URI {e.uri}, Message: {e.msg}, Details: {getattr(e, 'details', {})}"
        logger.error(f"[SMS] Error enviando mensaje a {destino}: {error_message}")
        return False


def enviar_whatsapp(destino: str, mensaje: str, media_urls: list[str] | None = None) -> bool:
    """
    Envía un mensaje de WhatsApp usando Twilio.
    Puede incluir un mensaje de texto y/o URLs de medios.
    """
    twilio_account_sid = current_app.config.get("TWILIO_ACCOUNT_SID")
    twilio_auth_token = current_app.config.get("TWILIO_AUTH_TOKEN")
    twilio_whatsapp_number = current_app.config.get("TWILIO_WHATSAPP_NUMBER")

    if not destino:
        logger.warning("[WHATSAPP] Destino no proporcionado.")
        return False
    if not all([twilio_account_sid, twilio_auth_token, twilio_whatsapp_number]):
        logger.error("[WHATSAPP] Faltan credenciales de Twilio para WhatsApp. Verificar configuración de la app.")
        return False

    if not mensaje and not media_urls:
        logger.warning("[WHATSAPP] Se intentó enviar un mensaje vacío (sin texto ni media_urls).")
        return False

    numero_con_prefijo = f"whatsapp:{destino}" if not destino.startswith("whatsapp:") else destino

    message_params = {
        "from_": twilio_whatsapp_number,
        "to": numero_con_prefijo
    }

    if mensaje: # Twilio permite enviar texto y media juntos. El texto actúa como caption.
        message_params["body"] = mensaje

    # Twilio's `media_url` parameter can be a list of URLs.
    # For simplicity, if multiple are passed, we'll just use the first one for now,
    # as sending multiple media items in a single WhatsApp "message" via API can have nuanced behavior
    # depending on client rendering. Sending one primary media item with a caption is common.
    # To send multiple distinct media, one would typically send multiple API calls.
    if media_urls:
        message_params["media_url"] = media_urls # Pass the list directly

    try:
        client = Client(twilio_account_sid, twilio_auth_token)
        msg = client.messages.create(**message_params)

        log_parts = []
        if mensaje:
            log_parts.append(f"Texto: '{mensaje[:30]}...'")
        if media_urls:
            log_parts.append(f"Media URLs: {media_urls}")

        logger.info(f"[WHATSAPP] Enviado a {numero_con_prefijo} (SID: {msg.sid}). {' | '.join(log_parts)}")
        return True
    except Exception as e:
        error_message = str(e)
        if hasattr(e, 'status') and hasattr(e, 'uri') and hasattr(e, 'msg'): # TwilioException attributes
            error_message = f"Twilio API Error: Status {e.status}, URI {e.uri}, Message: {e.msg}, Details: {getattr(e, 'details', {})}"
        logger.error(f"[WHATSAPP] Error enviando mensaje a {numero_con_prefijo}: {error_message}", exc_info=True)
        return False

# --- Nueva función para enviar WhatsApp para tickets ---
def enviar_whatsapp_ticket_novedad(ticket, mensaje: str, archivos_adjuntos: list = None) -> bool:
    """
    Envía un WhatsApp al cliente cuando hay movimiento en su ticket.
    Puede incluir archivos adjuntos si se proporcionan.
    `archivos_adjuntos` debe ser una lista de objetos ArchivoAdjunto.
    """
    ticket_id_log = getattr(ticket, 'id', 'N/A')
    original_destino = getattr(ticket, "telefono", None)

    if not original_destino and getattr(ticket, "user_id", None):
        from models import User # Importar User aquí para evitar importación circular a nivel de módulo
        usuario = User.query.get(ticket.user_id)
        if usuario:
            original_destino = getattr(usuario, "telefono", None)
            logger.info(f"[WHATSAPP] Obteniendo teléfono del usuario {usuario.id} para ticket {ticket_id_log} para WhatsApp: {original_destino}")
        else:
            logger.warning(f"[WHATSAPP] Usuario {getattr(ticket, 'user_id', 'N/A')} no encontrado para ticket {ticket_id_log} (WhatsApp).")

    if not original_destino:
        logger.warning(f"[WHATSAPP] Ticket {ticket_id_log} sin teléfono para notificar novedad por WhatsApp (original: {original_destino}).")
        return False

    from utils.validators import normalize_phone
    numero_limpio = normalize_phone(original_destino)

    if not numero_limpio:
        logger.warning(f"[WHATSAPP] Teléfono inválido o no normalizable a E.164 para ticket {ticket_id_log} (original: {original_destino}).")
        return False

    media_urls_para_envio = []
    if archivos_adjuntos:
        app_base_url = current_app.config.get("APP_BASE_URL", "")
        if not app_base_url:
            logger.error("[WHATSAPP] APP_BASE_URL no está configurada. No se pueden generar URLs completas para adjuntos y es probable que Twilio no pueda acceder a ellos.")
            # Decide if you want to proceed without app_base_url or return False
            # For now, let's log and proceed, Twilio might fail to fetch relative URLs.

        for adjunto_obj in archivos_adjuntos:
            if hasattr(adjunto_obj, 'url') and adjunto_obj.url:
                full_url = adjunto_obj.url
                if not full_url.startswith(('http://', 'https://')) and app_base_url: # Only prepend if not already absolute and app_base_url is available
                    full_url = app_base_url.rstrip('/') + adjunto_obj.url
                elif not full_url.startswith(('http://', 'https://')) and not app_base_url:
                    logger.warning(f"[WHATSAPP] No se pudo construir URL absoluta para adjunto {adjunto_obj.id} ({adjunto_obj.nombre_original}) debido a APP_BASE_URL faltante. Usando URL relativa: {full_url}")

                media_urls_para_envio.append(full_url)
            else:
                logger.warning(f"[WHATSAPP] Archivo adjunto para ticket {ticket_id_log} sin URL válida: {adjunto_obj}")

    if not mensaje and not media_urls_para_envio: # This check is fine here if it's meant to be before processing attachments
        logger.info(f"[WHATSAPP] No hay mensaje ni adjuntos válidos para enviar para ticket {ticket_id_log} (chequeo inicial). Envío omitido.")
        # return False # Let's not return yet, process attachments first, then re-check.

    # The loop for processing attachments should be here.
    # The original code had the attachment processing logic incorrectly indented.

    # Corrected logic for processing attachments:
    if archivos_adjuntos: # Ensure this block is processed only if there are attachments
        app_base_url = current_app.config.get("APP_BASE_URL", "") # Get it once
        # The following check for app_base_url was part of the original problem block,
        # but it's better to have it here if we decide to make it critical.
        # For now, let's assume it's logged if missing, and URLs might be relative.
        if not app_base_url:
             logger.error("[WHATSAPP] APP_BASE_URL no está configurada. URLs de adjuntos podrían no ser absolutas.")

        for adjunto_obj in archivos_adjuntos: # This is the loop where the error was.
            if hasattr(adjunto_obj, 'url') and adjunto_obj.url:
                full_url = adjunto_obj.url # This was the problematic line (line 388 in original error)
                if not full_url.startswith(('http://', 'https')) and app_base_url:
                    full_url = app_base_url.rstrip('/') + '/' + adjunto_obj.url.lstrip('/')
                elif not full_url.startswith(('http://', 'https')) and not app_base_url:
                     logger.warning(f"[WHATSAPP] No se pudo construir URL absoluta para adjunto ID {getattr(adjunto_obj, 'id', 'N/A')} ({getattr(adjunto_obj, 'nombre_original', 'N/A')}) debido a APP_BASE_URL faltante. Usando URL relativa: {full_url}")
                media_urls_para_envio.append(full_url)
            else:
                logger.warning(f"[WHATSAPP] Archivo adjunto para ticket {ticket_id_log} sin URL válida: {adjunto_obj}")

    # Final check after processing attachments
    if not mensaje and not media_urls_para_envio:
        logger.info(f"[WHATSAPP] No hay mensaje ni adjuntos válidos para enviar para ticket {ticket_id_log} (chequeo final). Envío omitido.")
        return False # Evitar enviar un mensaje completamente vacío

    logger.info(
        f"[WHATSAPP] Intentando enviar WhatsApp para ticket {ticket_id_log} "
        f"a número original '{original_destino}', limpio como '{numero_limpio}'. "
        f"Mensaje: '{mensaje[:30]}...' | Adjuntos: {len(media_urls_para_envio)}"
    )
    return enviar_whatsapp(numero_limpio, mensaje, media_urls=media_urls_para_envio if media_urls_para_envio else None)


def enviar_sms_ticket_novedad(ticket, mensaje: str) -> bool:
    """Envía un SMS al cliente cuando hay movimiento en su ticket."""
    destino = getattr(ticket, "telefono", None)
    ticket_id_log = getattr(ticket, 'id', 'N/A')
    original_destino = getattr(ticket, "telefono", None)

    if not original_destino and getattr(ticket, "user_id", None):
        from models import User
        usuario = User.query.get(ticket.user_id)
        if usuario:
            original_destino = getattr(usuario, "telefono", None)
            logger.info(f"[SMS] Obteniendo teléfono del usuario {usuario.id} para ticket {ticket_id_log}: {original_destino}")
        else:
            logger.warning(f"[SMS] Usuario {getattr(ticket, 'user_id', 'N/A')} no encontrado para ticket {ticket_id_log}.")

    if not original_destino:
        logger.warning(f"[SMS] Ticket {ticket_id_log} sin teléfono para notificar novedad (original: {original_destino}).")
        return False

    from utils.validators import normalize_phone
    numero_normalizado = normalize_phone(original_destino)

    if not numero_normalizado:
        logger.warning(f"[SMS] Teléfono inválido o no normalizable para ticket {ticket_id_log} (original: {original_destino}).")
        return False

    logger.info(f"[SMS] Intentando enviar SMS para ticket {ticket_id_log} a número original '{original_destino}', normalizado como '{numero_normalizado}'. Mensaje: '{mensaje[:30]}...'")
    return enviar_sms(numero_normalizado, mensaje)
