import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from twilio.rest import Client

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USERNAME)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")


def enviar_email(destino: str, asunto: str, cuerpo_html: str) -> bool:
    """Envía un email simple en formato HTML."""
    if not all([SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, destino]):
        logger.warning("[EMAIL] Faltan credenciales o destino.")
        return False

    mensaje = MIMEText(cuerpo_html, "html", "utf-8")
    mensaje["Subject"] = asunto
    mensaje["From"] = FROM_EMAIL
    mensaje["To"] = destino

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(mensaje)
        logger.info(f"[EMAIL] Enviado a {destino}")
        return True
    except Exception as e:
        logger.error(f"[EMAIL] Error enviando correo: {e}")
        return False


def enviar_email_con_adjunto(destino: str, asunto: str, cuerpo_html: str, nombre_archivo: str, contenido: bytes) -> bool:
    """Envía un email con un archivo adjunto."""
    if not all([SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, destino]):
        logger.warning("[EMAIL] Faltan credenciales o destino.")
        return False

    mensaje = MIMEMultipart()
    mensaje["Subject"] = asunto
    mensaje["From"] = FROM_EMAIL
    mensaje["To"] = destino
    mensaje.attach(MIMEText(cuerpo_html, "html", "utf-8"))

    if contenido:
        adj = MIMEApplication(contenido, _subtype="pdf")
        adj.add_header("Content-Disposition", "attachment", filename=nombre_archivo)
        mensaje.attach(adj)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(mensaje)
        logger.info(f"[EMAIL] Enviado a {destino} con adjunto {nombre_archivo}")
        return True
    except Exception as e:
        logger.error(f"[EMAIL] Error enviando correo: {e}")
        return False


def enviar_email_pedido_admin(pedido) -> bool:
    """Envía un correo al administrador con el nuevo pedido."""
    if not ADMIN_EMAIL:
        logger.warning("[EMAIL] ADMIN_EMAIL no configurado.")
        return False

    detalles = pedido.detalles
    asunto = f"Nuevo pedido {pedido.nro_pedido}"
    cuerpo = (
        f"<h3>Nuevo pedido recibido</h3>"
        f"<p><strong>Número:</strong> {pedido.nro_pedido}</p>"
        f"<p><strong>Cliente:</strong> {pedido.nombre_cliente} - {pedido.email_cliente} - {pedido.telefono_cliente}</p>"
        f"<p><strong>Monto estimado:</strong> ${pedido.monto_total:,.2f}</p>"
        f"<pre>{detalles}</pre>"
    )
    return enviar_email(ADMIN_EMAIL, asunto, cuerpo)


def enviar_email_pedido_cliente(pedido) -> bool:
    """Envía un correo al cliente confirmando su pedido."""
    destino = getattr(pedido, "email_cliente", None)
    if not destino:
        logger.warning("[EMAIL] Pedido sin email de cliente.")
        return False

    asunto = f"Confirmación de pedido {pedido.nro_pedido}"
    cuerpo = (
        f"<p>Hola {pedido.nombre_cliente or ''},</p>"
        f"<p>Recibimos tu pedido <strong>{pedido.nro_pedido}</strong> y está en proceso.</p>"
        "<p>Te avisaremos cuando esté listo para el envío.</p>"
    )
    return enviar_email(destino, asunto, cuerpo)


def enviar_email_ticket_admin(ticket) -> bool:
    """Envía un correo al administrador con el nuevo ticket."""
    if not ADMIN_EMAIL:
        logger.warning("[EMAIL] ADMIN_EMAIL no configurado.")
        return False

    asunto = f"Nuevo ticket {ticket.nro_ticket}"
    cuerpo = (
        f"<h3>Nuevo ticket registrado</h3>"
        f"<p><strong>Número:</strong> {ticket.nro_ticket}</p>"
        f"<p><strong>Asunto:</strong> {ticket.asunto}</p>"
        f"<p><strong>Categoría:</strong> {ticket.categoria}</p>"
        f"<p><strong>Pregunta:</strong> {ticket.pregunta}</p>"
    )
    if getattr(ticket, "telefono", None) or getattr(ticket, "email", None):
        cuerpo += (
            f"<p><strong>Contacto:</strong> {getattr(ticket, 'email', '')} "
            f"- {getattr(ticket, 'telefono', '')}</p>"
        )
    return enviar_email(ADMIN_EMAIL, asunto, cuerpo)


def enviar_email_ticket_cliente(ticket) -> bool:
    """Confirma al cliente que su reclamo fue recibido."""
    destino = getattr(ticket, "email", None)
    if not destino:
        logger.warning("[EMAIL] Ticket sin email de cliente.")
        return False

    asunto = f"Reclamo {ticket.nro_ticket} recibido"
    cuerpo = (
        f"<p>Hola,</p>"
        f"<p>Registramos tu reclamo con número <strong>{ticket.nro_ticket}</strong>.</p>"
        "<p>Nos comunicaremos pronto para darle seguimiento.</p>"
    )
    return enviar_email(destino, asunto, cuerpo)


def enviar_email_ticket_novedad(ticket, mensaje: str) -> bool:
    """Notifica al cliente que su ticket tiene una novedad."""
    destino = getattr(ticket, "email", None)
    if not destino and getattr(ticket, "user_id", None):
        from models import User
        usuario = User.query.get(ticket.user_id)
        destino = getattr(usuario, "email", None)
    if not destino:
        logger.warning("[EMAIL] Ticket sin email para notificar novedad.")
        return False

    asunto = f"Actualización en tu ticket {ticket.nro_ticket}"
    cuerpo = (
        f"<p>Hola,</p>"
        f"<p>Se registró una nueva actividad en tu ticket <strong>{ticket.nro_ticket}</strong>.</p>"
        f"<p>{mensaje}</p>"
    )
    return enviar_email(destino, asunto, cuerpo)


def enviar_sms(destino: str, mensaje: str) -> bool:
    """Envía un SMS usando Twilio."""
    if not destino:
        logger.warning("[SMS] Destino no proporcionado.")
        return False
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
        logger.error("[SMS] Faltan credenciales de Twilio. Verificar variables de entorno: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER.")
        return False

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(body=mensaje, from_=TWILIO_PHONE_NUMBER, to=destino)
        logger.info(f"[SMS] Enviado a {destino} (SID: {msg.sid}). Mensaje: '{mensaje[:30]}...'")
        return True
    except Exception as e:
        # Intentar obtener más detalles del error de Twilio si es posible
        error_message = str(e)
        if hasattr(e, 'status') and hasattr(e, 'uri') and hasattr(e, 'msg'): # TwilioRestException
            error_message = f"Twilio API Error: Status {e.status}, URI {e.uri}, Message: {e.msg}, Details: {getattr(e, 'details', {})}"
        logger.error(f"[SMS] Error enviando mensaje a {destino}: {error_message}")
        return False


def enviar_whatsapp(destino: str, mensaje: str) -> bool:
    """Envía un mensaje de WhatsApp usando Twilio."""
    if not destino:
        logger.warning("[WHATSAPP] Destino no proporcionado.")
        return False
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER]):
        logger.error("[WHATSAPP] Faltan credenciales de Twilio para WhatsApp. Verificar variables de entorno: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER.")
        return False

    numero_con_prefijo = f"whatsapp:{destino}" if not destino.startswith("whatsapp:") else destino

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            body=mensaje,
            from_=TWILIO_WHATSAPP_NUMBER, # Este debe ser el número de WhatsApp de Twilio
            to=numero_con_prefijo
        )
        logger.info(f"[WHATSAPP] Enviado a {numero_con_prefijo} (SID: {msg.sid}). Mensaje: '{mensaje[:30]}...'")
        return True
    except Exception as e:
        error_message = str(e)
        if hasattr(e, 'status') and hasattr(e, 'uri') and hasattr(e, 'msg'): # TwilioRestException
            error_message = f"Twilio API Error: Status {e.status}, URI {e.uri}, Message: {e.msg}, Details: {getattr(e, 'details', {})}"
        logger.error(f"[WHATSAPP] Error enviando mensaje a {numero_con_prefijo}: {error_message}")
        return False

# --- Nueva función para enviar WhatsApp para tickets ---
def enviar_whatsapp_ticket_novedad(ticket, mensaje: str) -> bool:
    """Envía un WhatsApp al cliente cuando hay movimiento en su ticket."""
    ticket_id_log = getattr(ticket, 'id', 'N/A')
    original_destino = getattr(ticket, "telefono", None)

    if not original_destino and getattr(ticket, "user_id", None):
        from models import User
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
    # normalize_phone ya devuelve el formato E.164 si el número es válido,
    # que es el preferido por Twilio para WhatsApp.
    numero_limpio = normalize_phone(original_destino)

    if not numero_limpio:
        logger.warning(f"[WHATSAPP] Teléfono inválido o no normalizable a E.164 para ticket {ticket_id_log} (original: {original_destino}).")
        return False

    logger.info(f"[WHATSAPP] Intentando enviar WhatsApp para ticket {ticket_id_log} a número original '{original_destino}', limpio como '{numero_limpio}'. Mensaje: '{mensaje[:30]}...'")
    return enviar_whatsapp(numero_limpio, mensaje)


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
