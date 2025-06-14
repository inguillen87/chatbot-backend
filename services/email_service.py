import os
import logging
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USERNAME)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")


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
