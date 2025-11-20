import os
import contextlib
import logging
import smtplib
from datetime import datetime
from email.mime.application import MIMEApplication # Para adjuntos
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import current_app # Para acceder a la configuración
from twilio.rest import Client

from services.config_loader import cargar_configuracion_municipio
from services.map_preview import generate_static_map

from models import ArchivoAdjunto, TicketComentario, User

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


def _coerce_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    """Convierte valores tipo string en booleanos confiables."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _format_datetime(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    try:
        return value.strftime("%d/%m/%Y %H:%M")
    except Exception:  # pragma: no cover - defensa ante objetos inesperados
        return str(value)


def _serialize_attachment(archivo: ArchivoAdjunto) -> Dict[str, Any]:
    nombre = getattr(archivo, "nombre_original", None) or getattr(archivo, "filename", None)
    url = getattr(archivo, "url", None)
    mime = getattr(archivo, "mime", None) or ""
    return {
        "nombre": nombre or "Archivo adjunto",
        "url": url,
        "mime": mime,
        "size": getattr(archivo, "tamano", None),
        "es_imagen": mime.startswith("image/"),
    }


def _serialize_comment(ticket: Any, comentario: Any) -> Dict[str, Any]:
    es_admin = bool(getattr(comentario, "es_admin", False))
    autor_nombre = getattr(comentario, "autor_nombre", None)
    if not autor_nombre:
        if es_admin:
            autor_nombre = "Municipio"
        else:
            autor_nombre = (
                getattr(ticket, "nombre_vecino", None)
                or getattr(ticket, "nombre_cliente", None)
                or "Vecino/a"
            )

    attachment = None
    archivo = getattr(comentario, "archivo_adjunto", None)
    if archivo:
        attachment = _serialize_attachment(archivo)

    return {
        "autor": autor_nombre,
        "rol": "Municipio" if es_admin else "Vecino/a",
        "fecha": _format_datetime(getattr(comentario, "fecha", None)),
        "mensaje": getattr(comentario, "comentario", ""),
        "estado": getattr(comentario, "estado_ticket", None),
        "adjunto": attachment,
    }


def _collect_ticket_attachments(ticket: Any, datos_ticket: Dict[str, Any]) -> List[Dict[str, Any]]:
    adjuntos: List[Dict[str, Any]] = []

    archivos_rel = getattr(ticket, "archivos", None)
    if archivos_rel is not None:
        archivos: Iterable[Any]
        if hasattr(archivos_rel, "order_by") and ArchivoAdjunto is not None:
            try:
                archivos = archivos_rel.order_by(ArchivoAdjunto.fecha.asc()).all()
            except Exception:  # pragma: no cover - comportamiento defensivo en tests
                archivos = list(archivos_rel)
        else:
            try:
                archivos = list(archivos_rel)
            except TypeError:
                archivos = []
        for archivo in archivos:
            if archivo:
                adjuntos.append(_serialize_attachment(archivo))

    foto_url = getattr(ticket, "foto_url_directa", None) or datos_ticket.get("foto_url")
    if foto_url:
        adjuntos.insert(0, {
            "nombre": "Imagen del reclamo",
            "url": foto_url,
            "mime": "image/*",
            "size": None,
            "es_imagen": True,
        })

    return adjuntos


def _collect_ticket_comments(ticket: Any, comentario_reciente: Any = None, limite: int = 6) -> List[Dict[str, Any]]:
    comentarios: List[Any] = []
    comentarios_rel = getattr(ticket, "comentarios", None)
    if comentarios_rel is not None:
        if hasattr(comentarios_rel, "order_by") and TicketComentario is not None:
            try:
                comentarios = list(
                    comentarios_rel.order_by(TicketComentario.fecha.desc()).limit(limite)
                )
            except Exception:  # pragma: no cover - defensivo si la relación es un mock simple
                try:
                    comentarios = list(comentarios_rel)
                except TypeError:
                    comentarios = []
        else:
            try:
                comentarios = list(comentarios_rel)
            except TypeError:
                comentarios = []

    if comentario_reciente and comentario_reciente not in comentarios:
        comentarios.append(comentario_reciente)

    comentarios_ordenados = sorted(
        comentarios,
        key=lambda c: getattr(c, "fecha", datetime.utcnow()) or datetime.utcnow(),
    )

    serializados: List[Dict[str, Any]] = []
    for comentario in comentarios_ordenados[-limite:]:
        if comentario:
            serializados.append(_serialize_comment(ticket, comentario))
    return serializados


def _build_ticket_email_context(
    ticket: Any,
    tipo_ticket: str,
    *,
    ticket_data: Optional[Dict[str, Any]] = None,
    admin_user: Any = None,
    comentario_reciente: Any = None,
) -> Dict[str, Any]:
    datos_ticket = ticket_data or {}
    ticket_numero = str(getattr(ticket, "nro_ticket", "") or datos_ticket.get("nro_ticket", "")).strip()
    prefijo = "M" if tipo_ticket == "municipio" else "P"
    ticket_codigo = f"{prefijo}-{ticket_numero}" if ticket_numero else ticket_numero

    estado_actual = getattr(ticket, "estado", None) or datos_ticket.get("estado")
    fecha_creacion = (
        getattr(ticket, "fecha", None)
        or datos_ticket.get("fecha")
    )
    ultima_actividad = getattr(ticket, "ultima_actividad", None)

    categoria = (
        getattr(ticket, "categoria", None)
        or datos_ticket.get("categoria")
        or getattr(ticket, "tipo_reclamo", None)
        or "General"
    )

    asunto = (
        getattr(ticket, "asunto", None)
        or datos_ticket.get("asunto")
        or categoria
    )

    descripcion = (
        getattr(ticket, "detalles", None)
        or getattr(ticket, "pregunta", None)
        or datos_ticket.get("detalles")
        or ""
    )

    direccion = getattr(ticket, "direccion", None) or datos_ticket.get("direccion")

    latitud = _coerce_float(
        getattr(ticket, "latitud", None)
        or datos_ticket.get("latitud")
        or datos_ticket.get("lat")
        or datos_ticket.get("latitude")
    )
    longitud = _coerce_float(
        getattr(ticket, "longitud", None)
        or datos_ticket.get("longitud")
        or datos_ticket.get("lon")
        or datos_ticket.get("lng")
        or datos_ticket.get("longitude")
    )

    mapa_url = mapa_alt = mapa_link = None
    if latitud is not None and longitud is not None:
        mapa_url, mapa_alt = generate_static_map(latitud, longitud)
        mapa_link = f"https://www.google.com/maps/search/?api=1&query={latitud},{longitud}"

    consulta_pin = getattr(ticket, "consulta_pin", None) or datos_ticket.get("consulta_pin")
    canal_ingreso = getattr(ticket, "canal_ingreso", None) or datos_ticket.get("canal_ingreso")

    nombre_cliente = (
        getattr(ticket, "nombre_vecino", None)
        or datos_ticket.get("nombre_vecino")
        or datos_ticket.get("nombre_cliente")
        or getattr(ticket, "nombre_cliente", None)
    )
    email_cliente = (
        getattr(ticket, "email_vecino", None)
        or getattr(ticket, "email", None)
        or datos_ticket.get("email_cliente")
    )
    telefono_cliente = (
        getattr(ticket, "telefono_vecino", None)
        or getattr(ticket, "telefono", None)
        or datos_ticket.get("telefono_cliente")
    )
    dni_cliente = (
        getattr(ticket, "dni_vecino", None)
        or getattr(ticket, "dni", None)
        or datos_ticket.get("dni")
    )

    contacto_email = None
    contacto_telefono = None
    contacto_horario = None
    contacto_enlace = None
    if admin_user:
        contacto_email = getattr(admin_user, "email", None)
        contacto_telefono = getattr(admin_user, "telefono", None)
        contacto_horario = getattr(admin_user, "horario", None)
        contacto_enlace = getattr(admin_user, "link_web", None)

    contacto_enlace = (
        contacto_enlace
        or getattr(ticket, "contacto_seguimiento", None)
        or datos_ticket.get("enlace_contacto")
    )

    base_url = current_app.config.get("APP_BASE_URL", "https://www.chatboc.ar")
    ticket_id = getattr(ticket, "id", None)
    chat_url = base_url
    if tipo_ticket == "municipio":
        chat_url = f"{base_url}/chat/{ticket_id}" if ticket_id else base_url
    else:
        chat_url = f"{base_url}/pyme/chat/{ticket_id}" if ticket_id else base_url

    panel_url = current_app.config.get("ADMIN_PORTAL_URL")
    if not panel_url:
        if tipo_ticket == "municipio":
            panel_url = (
                f"{base_url}/panel/municipio/tickets/{ticket_id}"
                if ticket_id
                else base_url
            )
        else:
            panel_url = (
                f"{base_url}/panel/pyme/tickets/{ticket_id}"
                if ticket_id
                else base_url
            )

    adjuntos_ticket = _collect_ticket_attachments(ticket, datos_ticket)
    historial = _collect_ticket_comments(ticket, comentario_reciente)
    comentario_destacado = _serialize_comment(ticket, comentario_reciente) if comentario_reciente else None

    return {
        "ticket_codigo": ticket_codigo,
        "ticket_numero": ticket_numero,
        "tipo_ticket": tipo_ticket,
        "estado": estado_actual,
        "fecha_creacion": _format_datetime(fecha_creacion),
        "ultima_actualizacion": _format_datetime(ultima_actividad),
        "categoria": categoria,
        "asunto": asunto,
        "descripcion": descripcion,
        "direccion": direccion,
        "mapa_url": mapa_url,
        "mapa_alt": mapa_alt,
        "mapa_link": mapa_link,
        "latitud": latitud,
        "longitud": longitud,
        "consulta_pin": consulta_pin,
        "chat_url": chat_url,
        "panel_url": panel_url,
        "adjuntos_ticket": adjuntos_ticket,
        "historial": historial,
        "comentario_reciente": comentario_destacado,
        "canal_ingreso": canal_ingreso,
        "cliente": {
            "nombre": nombre_cliente,
            "email": email_cliente,
            "telefono": telefono_cliente,
            "dni": dni_cliente,
        },
        "contacto_institucion": {
            "email": contacto_email,
            "telefono": contacto_telefono,
            "horario": contacto_horario,
            "enlace": contacto_enlace,
        },
        "soporte_email": current_app.config.get("MAIL_FROM_ADDRESS"),
    }


SMTP_CONFIG_ALIASES = {
    "SMTP_HOST": ["MAIL_SERVER", "EMAIL_HOST"],
    "SMTP_PORT": ["MAIL_PORT", "EMAIL_PORT"],
    "SMTP_USER": [
        "SMTP_USERNAME",
        "MAIL_USERNAME",
        "MAIL_FROM_ADDRESS",
        "EMAIL_HOST_USER",
        "SMTP_LOGIN",
        "SMTP_EMAIL",
    ],
    "SMTP_PASSWORD": ["SMTP_PASS", "MAIL_PASSWORD", "EMAIL_HOST_PASSWORD"],
    "SMTP_USE_TLS": ["MAIL_USE_TLS"],
    "SMTP_USE_SSL": ["MAIL_USE_SSL"],
    "MAIL_FROM_ADDRESS": ["MAIL_DEFAULT_SENDER"],
    "MAIL_FROM_NAME": ["MAIL_SENDER_NAME"],
}


def _clean_config_value(val: Optional[str]):
    """Normaliza valores de configuración devolviendo ``None`` si están vacíos."""

    if val is None:
        return None

    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None

    return val


def _resolve_config_key(key: str, *, campaign_specific: bool = False):
    """Intenta múltiples claves equivalentes para una configuración SMTP.

    Primero busca en la configuración de Flask y, como respaldo, revisa las
    variables de entorno. Esto ayuda cuando el contenedor tiene las
    credenciales como variables de entorno pero no se propagaron correctamente
    a ``current_app.config`` al inicializar la app.
    """

    keys_to_try = [key]
    keys_to_try.extend(SMTP_CONFIG_ALIASES.get(key, []))

    if campaign_specific:
        for candidate in keys_to_try:
            campaign_key = f"{candidate}_CAMPAIGN"
            if campaign_key in current_app.config:
                val = _clean_config_value(current_app.config.get(campaign_key))
                if val is not None:
                    return val
            # Respaldo directo a variables de entorno si la config no está poblada
            env_val = _clean_config_value(os.getenv(campaign_key))
            if env_val is not None:
                return env_val

    for candidate in keys_to_try:
        if candidate in current_app.config:
            val = _clean_config_value(current_app.config.get(candidate))
            if val is not None:
                return val

        env_val = _clean_config_value(os.getenv(candidate))
        if env_val is not None:
            return env_val

    return None


def _get_config_val(key, default=None, campaign_specific=False):
    """Helper para obtener valores de config, considerando alias comunes."""

    val = _resolve_config_key(key, campaign_specific=campaign_specific)
    if val is None:
        return default
    return val


class SMTPConfigurationError(RuntimeError):
    """Error personalizado para problemas de configuración SMTP."""


def validar_configuracion_smtp(require_auth: bool = True) -> Tuple[bool, Optional[str]]:
    """Valida la configuración SMTP mínima necesaria para enviar correos.

    Retorna una tupla (es_valida, mensaje_error). Si la configuración es válida,
    el mensaje de error es ``None``.
    """

    smtp_host = _get_config_val("SMTP_HOST")
    smtp_port = _get_config_val("SMTP_PORT", 587)
    smtp_user = _get_config_val("SMTP_USER")
    smtp_password = _get_config_val("SMTP_PASSWORD")
    from_email = _get_config_val("MAIL_FROM_ADDRESS")

    if not smtp_user and from_email:
        smtp_user = from_email

    if not smtp_host or not smtp_port:
        return False, "Configuración SMTP incompleta: faltan host o puerto."

    if require_auth and (not smtp_user or not smtp_password):
        return (
            False,
            "Configuración SMTP inválida: se requiere autenticación pero faltan credenciales.",
        )

    if not from_email:
        return False, "Configuración SMTP incompleta: falta el remitente (MAIL_FROM_ADDRESS)."

    return True, None


def _connect_smtp_server(
    *,
    host: str,
    port: int,
    use_tls: bool,
    use_ssl: bool,
    username: Optional[str],
    password: Optional[str],
    require_auth: bool = True,
):
    """Abre una conexión SMTP consistente y autenticada."""

    if not host or not port:
        raise SMTPConfigurationError("Host o puerto SMTP no configurados")

    if require_auth and (not username or not password):
        missing_parts = []
        if not username:
            missing_parts.append("usuario (SMTP_USER/MAIL_USERNAME/EMAIL_HOST_USER)")
        if not password:
            missing_parts.append("contraseña (SMTP_PASSWORD/MAIL_PASSWORD/EMAIL_HOST_PASSWORD)")

        raise SMTPConfigurationError(
            "Se requiere autenticación SMTP pero faltan credenciales: "
            + ", ".join(missing_parts)
        )

    server = smtplib.SMTP_SSL(host, port) if use_ssl else smtplib.SMTP(host, port)
    try:
        server.ehlo()
        if use_tls and not use_ssl:
            server.starttls()
            server.ehlo()

        if username and password:
            server.login(username, password)

        return server
    except Exception:
        with contextlib.suppress(Exception):
            server.quit()
        raise


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
    use_tls = _coerce_bool(
        _get_config_val("SMTP_USE_TLS", True, campaign_specific=es_campana),
        default=True,
    )
    use_ssl = _coerce_bool(
        _get_config_val("SMTP_USE_SSL", False, campaign_specific=es_campana)
    )

    if not smtp_user and from_email:
        smtp_user = from_email

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
        logger.info(
            f"{log_prefix} Intentando enviar a {destino} desde {from_email} via {smtp_host}:{smtp_port}"
        )

        server = _connect_smtp_server(
            host=smtp_host,
            port=smtp_port,
            use_tls=use_tls,
            use_ssl=use_ssl,
            username=smtp_user,
            password=smtp_password,
            require_auth=True,
        )

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
    smtp_host = _get_config_val("SMTP_HOST")
    smtp_port = int(_get_config_val("SMTP_PORT", 587))
    smtp_user = _get_config_val("SMTP_USER")
    smtp_password = _get_config_val("SMTP_PASSWORD")
    from_email = _get_config_val("MAIL_FROM_ADDRESS")
    from_name = _get_config_val("MAIL_FROM_NAME", from_email)
    use_tls = _coerce_bool(_get_config_val("SMTP_USE_TLS", True), default=True)
    use_ssl = _coerce_bool(_get_config_val("SMTP_USE_SSL", False))

    if not smtp_user and from_email:
        smtp_user = from_email

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
        logger.info(
            f"[EMAIL_ADJ] Intentando enviar a {destino} con adjunto {nombre_archivo}"
        )
        server = _connect_smtp_server(
            host=smtp_host,
            port=smtp_port,
            use_tls=use_tls,
            use_ssl=use_ssl,
            username=smtp_user,
            password=smtp_password,
            require_auth=True,
        )
        server.send_message(msg)  # send_message es mejor para MIME
        server.quit()
        logger.info(f"[EMAIL_ADJ] Enviado a {destino} con adjunto {nombre_archivo}")
        return True
    except Exception as e:
        logger.error(f"[EMAIL_ADJ] Error enviando correo con adjunto: {e}", exc_info=True)
        return False


def _render_items_html(pedido) -> str:
    try:
        from services.pedido_pdf import extraer_items_pedido
    except Exception:  # pragma: no cover - fallback if import fails
        extraer_items_pedido = None  # type: ignore

    if extraer_items_pedido is None:
        return f"<pre>{getattr(pedido, 'detalles', '')}</pre>"

    items = extraer_items_pedido(pedido)
    if not items:
        return f"<pre>{getattr(pedido, 'detalles', '')}</pre>"

    lines = ["<ul style='padding-left:20px'>"]
    for item in items:
        nombre = item.get("nombre", "Item")
        cantidad = item.get("cantidad", 0)
        precio = item.get("precio_unitario", 0)
        subtotal = item.get("subtotal", cantidad * precio)
        lines.append(
            f"<li><strong>{nombre}</strong>: {cantidad:g} x ${precio:,.2f} = ${subtotal:,.2f}</li>"
        )
    lines.append("</ul>")
    return "".join(lines)


def enviar_email_pedido_admin(pedido, *, pdf_bytes: bytes | None = None, empresa_info: Dict[str, Any] | None = None) -> bool:
    """Envía un correo al administrador con el nuevo pedido."""
    admin_email_val = current_app.config.get("ADMIN_EMAIL")
    if not admin_email_val or admin_email_val == "noreply@example.com":
        logger.warning("[EMAIL] ADMIN_EMAIL no configurado para notificación de pedido. Envío omitido.")
        return False

    asunto = f"Nuevo pedido {pedido.nro_pedido}"
    cuerpo_html_pedido = (
        f"<h3>Nuevo pedido recibido</h3>"
        f"<p><strong>Número:</strong> {pedido.nro_pedido}</p>"
        f"<p><strong>Cliente:</strong> {pedido.nombre_cliente} - {pedido.email_cliente} - {pedido.telefono_cliente}</p>"
        f"<p><strong>Monto estimado:</strong> ${pedido.monto_total:,.2f}</p>"
        f"{_render_items_html(pedido)}"
    )
    cuerpo_texto = (
        f"Nuevo pedido {pedido.nro_pedido} de {pedido.nombre_cliente or 'cliente'} "
        f"por ${pedido.monto_total or 0:,.2f}."
    )
    if pdf_bytes:
        nombre_archivo = f"Pedido-{pedido.nro_pedido}.pdf"
        return enviar_email_con_adjunto(admin_email_val, asunto, cuerpo_html_pedido, nombre_archivo, pdf_bytes, cuerpo_texto)
    # Para emails transaccionales, no marcamos como es_campana=True
    return enviar_email(admin_email_val, asunto, cuerpo_html_pedido, cuerpo_texto=cuerpo_texto)


def enviar_email_pedido_cliente(pedido, *, pdf_bytes: bytes | None = None, empresa_info: Dict[str, Any] | None = None) -> bool:
    """Envía un correo al cliente confirmando su pedido."""
    destino = getattr(pedido, "email_cliente", None)
    if not destino:
        logger.warning("[EMAIL] Pedido sin email de cliente.")
        return False

    empresa_nombre = None
    if empresa_info:
        empresa_nombre = empresa_info.get("nombre")
    asunto = f"Confirmación de pedido {pedido.nro_pedido}" if not empresa_nombre else f"{empresa_nombre} - Pedido {pedido.nro_pedido}"
    body_lines = [
        f"<p>Hola {pedido.nombre_cliente or ''},</p>",
        f"<p>Recibimos tu pedido <strong>{pedido.nro_pedido}</strong> y está en proceso.</p>",
    ]
    if pdf_bytes:
        body_lines.append("<p>Adjuntamos la nota de pedido en PDF para que la revises.</p>")
    else:
        body_lines.append("<p>Te avisaremos cuando esté listo para el envío.</p>")
    items_html = _render_items_html(pedido)
    if items_html:
        body_lines.append(items_html)
    cuerpo_html_confirmacion = "".join(body_lines)
    if pdf_bytes:
        cuerpo_texto = (
            f"Hola {pedido.nombre_cliente or ''}, tu pedido {pedido.nro_pedido} está en proceso. "
            "Adjuntamos la nota en PDF."
        )
    else:
        cuerpo_texto = (
            f"Hola {pedido.nombre_cliente or ''}, tu pedido {pedido.nro_pedido} está en proceso."
        )
    if pdf_bytes:
        nombre_archivo = f"Pedido-{pedido.nro_pedido}.pdf"
        return enviar_email_con_adjunto(destino, asunto, cuerpo_html_confirmacion, nombre_archivo, pdf_bytes, cuerpo_texto)
    return enviar_email(destino, asunto, cuerpo_html_confirmacion, cuerpo_texto=cuerpo_texto)


def enviar_email_ticket_admin(
    ticket,
    *,
    admin_user=None,
    tipo_ticket: Optional[str] = None,
    ticket_data: Optional[Dict[str, Any]] = None,
    comentario_reciente=None,
    mensaje_resumen: Optional[str] = None,
) -> bool:
    """Envía un correo al administrador con la información completa del ticket."""

    destino = None
    if admin_user and getattr(admin_user, "email", None):
        destino = admin_user.email

    if not destino:
        destino = current_app.config.get("ADMIN_EMAIL")

    if not destino or destino == "noreply@example.com":
        logger.warning(
            "[EMAIL] ADMIN_EMAIL no configurado para notificación de ticket. Envío omitido."
        )
        return False

    datos_ticket = ticket_data or {}
    tipo_ticket = (
        tipo_ticket
        or ("municipio" if getattr(ticket, "municipio_id", None) else "pyme")
    )

    contexto = _build_ticket_email_context(
        ticket,
        tipo_ticket,
        ticket_data=datos_ticket,
        admin_user=admin_user,
        comentario_reciente=comentario_reciente,
    )

    rubro_nombre = None
    if tipo_ticket == "pyme":
        if admin_user and getattr(admin_user, "rubro", None):
            rubro_nombre = getattr(admin_user.rubro, "nombre", None)
        if not rubro_nombre and getattr(ticket, "rubro_id", None):
            try:
                from models import Rubro  # Import local para evitar dependencias circulares

                rubro = Rubro.query.get(ticket.rubro_id)
                rubro_nombre = getattr(rubro, "nombre", None)
            except Exception:  # pragma: no cover - logging handled más adelante
                rubro_nombre = None

    contexto["rubro_nombre"] = rubro_nombre
    contexto["admin_nombre"] = getattr(admin_user, "name", None)
    contexto["destinatario"] = "admin"
    contexto["mostrar_historial"] = bool(contexto.get("historial"))
    contexto["es_actualizacion"] = comentario_reciente is not None
    mensaje_destacado = mensaje_resumen or (
        (contexto.get("comentario_reciente") or {}).get("mensaje")
    )
    contexto["mensaje_destacado"] = mensaje_destacado
    contexto["cta_url"] = contexto.get("panel_url")
    contexto["cta_label"] = "Abrir ticket en el panel"

    if comentario_reciente is not None:
        header_title = "Nuevo mensaje del vecino"
        intro_base = "Tenés una nueva actualización para revisar."
    else:
        header_title = "Nuevo ticket recibido"
        intro_base = "Se generó un nuevo ticket en tu panel de gestión."

    if contexto.get("cliente", {}).get("nombre"):
        intro_persona = f"Contacto: {contexto['cliente']['nombre']}"
    else:
        intro_persona = ""

    contexto["header_title"] = header_title
    contexto["header_intro"] = intro_base
    contexto["header_subtitle"] = intro_persona

    ticket_codigo = contexto.get("ticket_codigo") or contexto.get("ticket_numero")
    asunto_prefijo = "Actualización" if comentario_reciente else "Nuevo ticket"
    asunto_email = f"{asunto_prefijo} {ticket_codigo}".strip()

    cuerpo_html_ticket = render_template(
        "email/ticket_admin_notificacion.html",
        **contexto,
    )

    return enviar_email(destino, asunto_email, cuerpo_html_ticket)


import requests

def enviar_email_con_multiples_adjuntos(destinos: List[str], asunto: str, cuerpo_html: str, adjuntos: List[ArchivoAdjunto], cuerpo_texto: str = "") -> bool:
    """Envía un email con múltiples archivos adjuntos."""
    smtp_host = _get_config_val("SMTP_HOST")
    smtp_port = int(_get_config_val("SMTP_PORT", 587))
    smtp_user = _get_config_val("SMTP_USER")
    smtp_password = _get_config_val("SMTP_PASSWORD")
    from_email = _get_config_val("MAIL_FROM_ADDRESS")
    from_name = _get_config_val("MAIL_FROM_NAME", from_email)
    use_tls = _coerce_bool(_get_config_val("SMTP_USE_TLS", True), default=True)
    use_ssl = _coerce_bool(_get_config_val("SMTP_USE_SSL", False))

    if not smtp_user and from_email:
        smtp_user = from_email

    if not all([smtp_host, smtp_port, from_email, destinos]):
        logger.error("[EMAIL_MULTI_ADJ] Configuración SMTP incompleta o falta destino. Email no enviado.")
        return False

    msg = MIMEMultipart('mixed')
    msg['Subject'] = asunto
    msg['From'] = f"{from_name} <{from_email}>"
    msg['To'] = ", ".join(destinos)

    body_content = MIMEMultipart('alternative')
    if cuerpo_texto:
        body_content.attach(MIMEText(cuerpo_texto, "plain", "utf-8"))
    if cuerpo_html:
        body_content.attach(MIMEText(cuerpo_html, "html", "utf-8"))
    msg.attach(body_content)

    for adjunto in adjuntos:
        try:
            response = requests.get(adjunto.url, timeout=10)
            response.raise_for_status()
            contenido_adjunto = response.content

            main_type, sub_type = (adjunto.mime or 'application/octet-stream').split('/', 1)

            adj = MIMEApplication(contenido_adjunto, _subtype=sub_type)
            adj.add_header("Content-Disposition", "attachment", filename=adjunto.nombre_original)
            msg.attach(adj)
            logger.info(f"[EMAIL_MULTI_ADJ] Adjuntado archivo {adjunto.nombre_original} ({adjunto.mime}) desde {adjunto.url}")

        except requests.exceptions.RequestException as e:
            logger.error(f"[EMAIL_MULTI_ADJ] No se pudo descargar el adjunto desde {adjunto.url}: {e}")
            # Continuar sin este adjunto
            continue
        except Exception as e:
            logger.error(f"[EMAIL_MULTI_ADJ] Error procesando adjunto {adjunto.id}: {e}")
            continue

    try:
        logger.info(
            f"[EMAIL_MULTI_ADJ] Intentando enviar a {', '.join(destinos)} con {len(adjuntos)} adjuntos."
        )
        server = _connect_smtp_server(
            host=smtp_host,
            port=smtp_port,
            use_tls=use_tls,
            use_ssl=use_ssl,
            username=smtp_user,
            password=smtp_password,
            require_auth=True,
        )
        server.send_message(msg)
        server.quit()
        logger.info(f"[EMAIL_MULTI_ADJ] Enviado a {', '.join(destinos)} exitosamente.")
        return True
    except SMTPConfigurationError as config_err:
        logger.error(f"[EMAIL_MULTI_ADJ] Configuración SMTP inválida: {config_err}")
        return False
    except smtplib.SMTPSenderRefused as sender_error:
        if sender_error.smtp_code == 530:
            logger.error(
                "[EMAIL_MULTI_ADJ] El servidor exige autenticación SMTP (código 530). Verificar credenciales o IP permitida."
            )
        logger.error(
            f"[EMAIL_MULTI_ADJ] Servidor rechazó el remitente: {sender_error}",
            exc_info=True,
        )
        return False
    except Exception as e:
        logger.error(
            f"[EMAIL_MULTI_ADJ] Error enviando correo con múltiples adjuntos: {e}",
            exc_info=True,
        )
        return False

from flask import render_template

def enviar_email_ticket_cliente(
    ticket,
    *,
    tipo_ticket: Optional[str] = None,
    admin_user=None,
    ticket_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """Confirma al cliente que su reclamo o pedido fue recibido."""

    destino = (
        getattr(ticket, "email_vecino", None)
        or getattr(ticket, "email", None)
        or (ticket_data or {}).get("email_cliente")
    )
    if not destino:
        logger.warning(f"[EMAIL] Ticket {getattr(ticket, 'id', 'N/A')} sin email de cliente.")
        return False

    datos_ticket = ticket_data or {}
    tipo_ticket = (
        tipo_ticket
        or ("municipio" if getattr(ticket, "municipio_id", None) else "pyme")
    )

    contexto = _build_ticket_email_context(
        ticket,
        tipo_ticket,
        ticket_data=datos_ticket,
        admin_user=admin_user,
    )

    # Complementar datos de contacto desde la configuración municipal si fuera necesario
    if (
        tipo_ticket == "municipio"
        and not contexto["contacto_institucion"].get("enlace")
        and getattr(ticket, "municipio_id", None)
    ):
        cfg = cargar_configuracion_municipio(ticket.municipio_id, "config.json")
        if isinstance(cfg, dict):
            contexto["contacto_institucion"]["enlace"] = (
                contexto["contacto_institucion"].get("enlace") or cfg.get("web_url")
            )
            contexto["contacto_institucion"]["telefono"] = (
                contexto["contacto_institucion"].get("telefono") or cfg.get("telefono")
            )

    ticket_codigo = contexto.get("ticket_codigo") or contexto.get("ticket_numero")

    asunto_ticket = (
        getattr(ticket, "asunto", None)
        or datos_ticket.get("asunto")
        or contexto.get("categoria")
        or "Ticket registrado"
    )

    asunto = f"Confirmación de ticket {ticket_codigo}: {asunto_ticket}".strip()

    nombre_destinatario = contexto.get("cliente", {}).get("nombre") or "Vecino/a"
    contexto["destinatario"] = "ciudadano"
    contexto["es_actualizacion"] = False
    contexto["mostrar_historial"] = bool(contexto.get("historial"))
    contexto["header_title"] = "¡Recibimos tu reclamo!" if tipo_ticket == "municipio" else "Recibimos tu solicitud"
    contexto["header_intro"] = (
        f"Hola {nombre_destinatario}, confirmamos que registramos tu ticket."
    )
    contexto["header_subtitle"] = f"Ticket {ticket_codigo}" if ticket_codigo else ""
    contexto["cta_url"] = contexto.get("chat_url")
    contexto["cta_label"] = "Ver estado del ticket"
    contexto["mensaje_destacado"] = contexto.get("descripcion")

    try:
        cuerpo_html = render_template("email/ticket_creado.html", **contexto)
        return enviar_email(destino, asunto, cuerpo_html)
    except Exception as e:  # pragma: no cover - logged only
        logger.error(
            f"Error al renderizar la plantilla de email para ticket {getattr(ticket, 'id', 'N/A')}: {e}",
            exc_info=True,
        )
        return False


def enviar_email_ticket_novedad(
    ticket,
    mensaje: str,
    *,
    comentario_reciente=None,
) -> bool:
    """Notifica al cliente que su ticket tiene una novedad usando la plantilla principal."""

    destino = getattr(ticket, "email", None)
    if not destino and getattr(ticket, "user_id", None) and hasattr(User, "query"):
        usuario = User.query.get(ticket.user_id)
        destino = getattr(usuario, "email", None)
    if not destino:
        logger.warning("[EMAIL] Ticket sin email para notificar novedad.")
        return False

    tipo_ticket = "municipio" if getattr(ticket, "municipio_id", None) else "pyme"
    contexto = _build_ticket_email_context(
        ticket,
        tipo_ticket,
        comentario_reciente=comentario_reciente,
    )

    ticket_codigo = contexto.get("ticket_codigo") or contexto.get("ticket_numero") or getattr(ticket, "nro_ticket", "")
    nombre_destinatario = contexto.get("cliente", {}).get("nombre") or "Vecino/a"

    contexto["destinatario"] = "ciudadano"
    contexto["es_actualizacion"] = True
    contexto["mostrar_historial"] = bool(contexto.get("historial"))
    contexto["header_title"] = "Tu ticket tiene novedades"
    contexto["header_intro"] = f"Hola {nombre_destinatario}, tenemos una actualización para tu ticket."
    contexto["header_subtitle"] = f"Ticket {ticket_codigo}" if ticket_codigo else ""
    contexto["cta_url"] = contexto.get("chat_url")
    contexto["cta_label"] = "Responder al municipio"
    contexto["mensaje_destacado"] = mensaje or (
        (contexto.get("comentario_reciente") or {}).get("mensaje")
    )

    asunto = f"Actualización en tu ticket {ticket_codigo}".strip()

    try:
        cuerpo_html = render_template("email/ticket_creado.html", **contexto)
    except Exception as e:  # pragma: no cover - se registra para diagnóstico
        logger.error(
            f"Error al renderizar plantilla de novedad para ticket {getattr(ticket, 'id', 'N/A')}: {e}",
            exc_info=True,
        )
        cuerpo_html = (
            f"<p>{contexto['header_intro']}</p><p>{contexto.get('mensaje_destacado') or mensaje}</p>"
        )

    return enviar_email(destino, asunto, cuerpo_html)


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
