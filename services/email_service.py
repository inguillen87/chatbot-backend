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
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, destino]):
        logger.warning("[SMS] Faltan credenciales o destino.")
        return False
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(body=mensaje, from_=TWILIO_PHONE_NUMBER, to=destino)
        logger.info(f"[SMS] Enviado SID: {msg.sid}")
        return True
    except Exception as e:
        logger.error(f"[SMS] Error enviando mensaje: {e}")
        return False


def enviar_whatsapp(destino: str, mensaje: str) -> bool:
    """Envía un mensaje de WhatsApp usando Twilio."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER, destino]):
        logger.warning("[WHATSAPP] Faltan credenciales o destino.")
        return False
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            body=mensaje,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=f"whatsapp:{destino}" if not destino.startswith("whatsapp:") else destino,
        )
        logger.info(f"[WHATSAPP] Enviado SID: {msg.sid}")
        return True
    except Exception as e:
        logger.error(f"[WHATSAPP] Error enviando mensaje: {e}")
        return False


def enviar_sms_ticket_novedad(ticket, mensaje: str) -> bool:
    """Envía un SMS al cliente cuando hay movimiento en su ticket."""
    destino = getattr(ticket, "telefono", None)
    if not destino and getattr(ticket, "user_id", None):
        from models import User
        usuario = User.query.get(ticket.user_id)
        destino = getattr(usuario, "telefono", None)
    if not destino:
        logger.warning("[SMS] Ticket sin telefono para notificar novedad.")
        return False
    from utils.validators import normalize_phone
    numero = normalize_phone(destino)
    if not numero:
        logger.warning("[SMS] Telefono inválido para notificar novedad.")
        return False
    return enviar_sms(numero, mensaje)
