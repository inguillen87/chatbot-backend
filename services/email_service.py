# import os # No es necesario si usamos current_app.config
import logging
import smtplib
from datetime import datetime
from email.mime.application import MIMEApplication # Para adjuntos
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from math import isfinite
from typing import Any, Dict, List, Optional

from flask import current_app, render_template # Para acceder a la configuración
from twilio.rest import Client

from services.config_loader import cargar_configuracion_municipio

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
    value = current_app.config.get(key, default)
    if value is None and key == "MAIL_FROM_ADDRESS":
        return "info@chatboc.ar"
    return value


def _coalesce(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _format_datetime(dt: Any) -> Optional[str]:
    if not dt:
        return None
    try:
        if isinstance(dt, datetime):
            if dt.tzinfo is not None:
                try:
                    dt = dt.astimezone()
                except ValueError:
                    pass
            return dt.strftime("%d/%m/%Y %H:%M")
        if hasattr(dt, "strftime"):
            return dt.strftime("%d/%m/%Y %H:%M")
        return str(dt)
    except Exception:  # pragma: no cover - formatting fallback
        return str(dt)


def _sanitize_coordinate(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        coord = float(value)
        if isfinite(coord):
            return coord
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    return None


def _build_map_assets(lat: Optional[float], lon: Optional[float]) -> tuple[Optional[str], Optional[str]]:
    if lat is None or lon is None:
        return None, None
    lat_str = f"{lat:.6f}"
    lon_str = f"{lon:.6f}"
    map_image = (
        "https://staticmap.openstreetmap.de/staticmap.php?center="
        f"{lat_str},{lon_str}&zoom=16&size=640x320&markers={lat_str},{lon_str},red-pushpin"
    )
    map_link = f"https://maps.google.com/?q={lat_str},{lon_str}"
    return map_image, map_link


def _format_attachment(adjunto) -> Optional[Dict[str, Any]]:
    if not adjunto:
        return None
    nombre = (
        getattr(adjunto, "nombre_original", None)
        or getattr(adjunto, "filename", None)
        or "Archivo adjunto"
    )
    url = getattr(adjunto, "url", None)
    mime = getattr(adjunto, "mime", None)
    tamano = getattr(adjunto, "tamano", None)
    es_imagen = bool(mime and str(mime).lower().startswith("image"))
    return {
        "id": getattr(adjunto, "id", None),
        "nombre": nombre,
        "url": url,
        "mime": mime,
        "tamano": tamano,
        "es_imagen": es_imagen,
    }


def _collect_ticket_attachments(ticket) -> List[Dict[str, Any]]:
    adjuntos: List[Dict[str, Any]] = []
    archivos_rel = getattr(ticket, "archivos", None)
    archivos_iterable = []
    if archivos_rel is not None:
        try:
            archivos_iterable = archivos_rel.all()
        except Exception:
            try:
                archivos_iterable = list(archivos_rel)
            except TypeError:
                archivos_iterable = []
    for adj in archivos_iterable:
        adj_dict = _format_attachment(adj)
        if adj_dict:
            adjuntos.append(adj_dict)

    foto_directa = getattr(ticket, "foto_url_directa", None)
    if foto_directa:
        if not any(a.get("url") == foto_directa for a in adjuntos):
            adjuntos.insert(
                0,
                {
                    "id": None,
                    "nombre": "Imagen inicial del reclamo",
                    "url": foto_directa,
                    "mime": None,
                    "tamano": None,
                    "es_imagen": True,
                    "es_foto_inicial": True,
                },
            )
    return adjuntos


def _resolve_comment_author(comentario) -> tuple[str, str]:
    if getattr(comentario, "origen", None) == "sistema":
        return "Sistema", "sistema"

    if getattr(comentario, "es_admin", False):
        base_nombre = "Equipo del Municipio" if getattr(comentario, "municipio_ticket_id", None) else "Equipo de la PyME"
        try:
            user_id = getattr(comentario, "user_id", None)
            if user_id:
                from models import User, db  # Import local para evitar ciclos

                usuario = db.session.get(User, user_id)
                if usuario:
                    if getattr(usuario, "name", None):
                        base_nombre = usuario.name
                    elif getattr(usuario, "email", None):
                        base_nombre = usuario.email
        except Exception:  # pragma: no cover - fallback genérico
            pass
        return base_nombre, "admin"

    nombre_vecino = None
    try:
        ticket_rel = getattr(comentario, "municipio_ticket", None) or getattr(comentario, "pyme_ticket", None)
        if ticket_rel:
            nombre_vecino = (
                getattr(ticket_rel, "nombre_vecino", None)
                or getattr(ticket_rel, "nombre_cliente", None)
            )
    except Exception:  # pragma: no cover - fallback
        nombre_vecino = None
    return nombre_vecino or "Vecino/a", "vecino"


def _format_comment(comentario) -> Optional[Dict[str, Any]]:
    if comentario is None:
        return None
    autor_nombre, autor_rol = _resolve_comment_author(comentario)
    adjuntos: List[Dict[str, Any]] = []
    if getattr(comentario, "archivo_adjunto", None):
        adj_dict = _format_attachment(comentario.archivo_adjunto)
        if adj_dict:
            adjuntos.append(adj_dict)
    mensaje = getattr(comentario, "comentario", "") or ""
    fecha = getattr(comentario, "fecha", None)
    return {
        "autor": autor_nombre,
        "rol": autor_rol,
        "mensaje": mensaje,
        "fecha": fecha,
        "fecha_formateada": _format_datetime(fecha),
        "estado": getattr(comentario, "estado_ticket", None),
        "adjuntos": adjuntos,
    }


def _collect_ticket_history(ticket) -> List[Dict[str, Any]]:
    comentarios_rel = getattr(ticket, "comentarios", None)
    if comentarios_rel is None:
        return []
    comentarios = []
    try:
        from models import TicketComentario  # Import local para evitar ciclos

        comentarios = comentarios_rel.order_by(TicketComentario.fecha.asc()).all()
    except Exception:
        try:
            comentarios = list(comentarios_rel)
        except TypeError:
            comentarios = []
    historial: List[Dict[str, Any]] = []
    for comentario in comentarios:
        comentario_fmt = _format_comment(comentario)
        if comentario_fmt:
            historial.append(comentario_fmt)
    return historial


def _build_ticket_common_context(
    ticket,
    *,
    tipo_ticket: Optional[str] = None,
    admin_user=None,
    ticket_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    datos_ticket = ticket_data or {}
    tipo_ticket = tipo_ticket or (
        "municipio" if getattr(ticket, "municipio_id", None) else "pyme"
    )

    ticket_numero = str(
        _coalesce(getattr(ticket, "nro_ticket", None), datos_ticket.get("nro_ticket"), "")
        or ""
    )
    prefijo = "M" if tipo_ticket == "municipio" else "P"
    ticket_codigo = f"{prefijo}-{ticket_numero}" if ticket_numero else ticket_numero

    categoria = _coalesce(
        getattr(ticket, "categoria", None), datos_ticket.get("categoria"), "General"
    )
    asunto_ticket = _coalesce(
        getattr(ticket, "asunto", None), datos_ticket.get("asunto"), categoria
    )
    detalles_ticket = _coalesce(
        getattr(ticket, "detalles", None),
        getattr(ticket, "pregunta", None),
        datos_ticket.get("detalles"),
        datos_ticket.get("pregunta"),
        "Sin descripción.",
    )
    direccion = _coalesce(
        getattr(ticket, "direccion", None), datos_ticket.get("direccion"), "No especificada"
    )
    consulta_pin = _coalesce(
        getattr(ticket, "consulta_pin", None), datos_ticket.get("consulta_pin")
    )
    canal_ingreso = _coalesce(
        getattr(ticket, "canal_ingreso", None), datos_ticket.get("canal_ingreso")
    )
    estado_actual = _coalesce(
        getattr(ticket, "estado", None), datos_ticket.get("estado"), "nuevo"
    )
    fecha_formateada = _format_datetime(getattr(ticket, "fecha", None))

    lat = _sanitize_coordinate(
        _coalesce(
            getattr(ticket, "latitud", None),
            datos_ticket.get("latitud"),
            datos_ticket.get("lat"),
            datos_ticket.get("latitude"),
        )
    )
    lon = _sanitize_coordinate(
        _coalesce(
            getattr(ticket, "longitud", None),
            datos_ticket.get("longitud"),
            datos_ticket.get("lon"),
            datos_ticket.get("lng"),
            datos_ticket.get("longitude"),
        )
    )
    map_image_url, map_link_url = _build_map_assets(lat, lon)

    cliente_nombre = _coalesce(
        getattr(ticket, "nombre_vecino", None),
        datos_ticket.get("nombre_vecino"),
        datos_ticket.get("nombre_cliente"),
        getattr(ticket, "nombre_cliente", None),
        "Vecino/a",
    )
    email_cliente = _coalesce(
        getattr(ticket, "email_vecino", None),
        getattr(ticket, "email", None),
        datos_ticket.get("email_cliente"),
        getattr(ticket, "email_cliente", None),
    )
    telefono_cliente = _coalesce(
        getattr(ticket, "telefono_vecino", None),
        getattr(ticket, "telefono", None),
        datos_ticket.get("telefono_cliente"),
        getattr(ticket, "telefono_cliente", None),
    )
    dni_cliente = _coalesce(
        getattr(ticket, "dni_vecino", None), getattr(ticket, "dni", None), datos_ticket.get("dni")
    )

    base_url = current_app.config.get("APP_BASE_URL", "https://www.chatboc.ar")
    chat_url = (
        f"{base_url}/chat/{getattr(ticket, 'id', '')}"
        if getattr(ticket, "id", None)
        else base_url
    )
    if tipo_ticket == "pyme":
        chat_url = (
            f"{base_url}/pyme/chat/{getattr(ticket, 'id', '')}"
            if getattr(ticket, "id", None)
            else base_url
        )

    panel_url = current_app.config.get("ADMIN_PORTAL_URL")
    if not panel_url:
        if tipo_ticket == "municipio":
            panel_url = (
                f"{base_url}/panel/municipio/tickets/{getattr(ticket, 'id', '')}"
                if getattr(ticket, "id", None)
                else base_url
            )
        else:
            panel_url = (
                f"{base_url}/panel/pyme/tickets/{getattr(ticket, 'id', '')}"
                if getattr(ticket, "id", None)
                else base_url
            )

    telefono_contacto = getattr(admin_user, "telefono", None) if admin_user else None
    horario_contacto = getattr(admin_user, "horario", None) if admin_user else None
    enlace_contacto = getattr(admin_user, "link_web", None) if admin_user else None

    if tipo_ticket == "municipio" and getattr(ticket, "municipio_id", None):
        cfg = cargar_configuracion_municipio(ticket.municipio_id, "config.json")
        if isinstance(cfg, dict):
            enlace_contacto = enlace_contacto or cfg.get("web_url")
            telefono_contacto = telefono_contacto or cfg.get("telefono")
            horario_contacto = horario_contacto or cfg.get("horario")

    rubro_nombre = None
    if tipo_ticket == "pyme":
        if admin_user and getattr(admin_user, "rubro", None):
            rubro_nombre = getattr(admin_user.rubro, "nombre", None)
        if not rubro_nombre and getattr(ticket, "rubro_id", None):
            try:
                from models import Rubro  # Import local para evitar ciclos

                rubro = Rubro.query.get(ticket.rubro_id)
                rubro_nombre = getattr(rubro, "nombre", None)
            except Exception:  # pragma: no cover - fallback silencioso
                rubro_nombre = None

    adjuntos_ticket = _collect_ticket_attachments(ticket)
    historial = _collect_ticket_history(ticket)

    return {
        "tipo_ticket": tipo_ticket,
        "ticket_codigo": ticket_codigo,
        "ticket_numero": ticket_numero,
        "categoria": categoria,
        "asunto": asunto_ticket,
        "detalles": detalles_ticket,
        "direccion": direccion,
        "consulta_pin": consulta_pin,
        "canal_ingreso": canal_ingreso,
        "estado": estado_actual,
        "fecha_formateada": fecha_formateada,
        "map_image_url": map_image_url,
        "map_link_url": map_link_url,
        "latitud": lat,
        "longitud": lon,
        "cliente_info": {
            "nombre": cliente_nombre,
            "email": email_cliente,
            "telefono": telefono_cliente,
            "dni": dni_cliente,
        },
        "contacto_info": {
            "telefono": telefono_contacto,
            "horario": horario_contacto,
            "enlace": enlace_contacto,
        },
        "panel_url": panel_url,
        "chat_url": chat_url,
        "adjuntos": adjuntos_ticket,
        "historial": historial,
        "rubro_nombre": rubro_nombre,
    }


def _build_ticket_email_context(
    ticket,
    *,
    tipo_ticket: Optional[str],
    audience: str,
    header_title: str,
    intro_text: str,
    admin_user=None,
    ticket_data: Optional[Dict[str, Any]] = None,
    highlight_message: Optional[str] = None,
    comentario=None,
    extra_adjuntos: Optional[List[Any]] = None,
    primary_button_text: Optional[str] = None,
    primary_button_url: Optional[str] = None,
    secondary_button_text: Optional[str] = None,
    secondary_button_url: Optional[str] = None,
    footer_text: Optional[str] = None,
) -> Dict[str, Any]:
    context = _build_ticket_common_context(
        ticket,
        tipo_ticket=tipo_ticket,
        admin_user=admin_user,
        ticket_data=ticket_data,
    )
    context.update(
        {
            "audience": audience,
            "header_title": header_title,
            "intro_text": intro_text,
            "primary_button": (
                {"text": primary_button_text, "url": primary_button_url}
                if primary_button_text and primary_button_url
                else None
            ),
            "secondary_button": (
                {"text": secondary_button_text, "url": secondary_button_url}
                if secondary_button_text and secondary_button_url
                else None
            ),
            "footer_text": footer_text,
        }
    )

    highlight_block = None
    comentario_formateado = _format_comment(comentario) if comentario is not None else None
    if comentario_formateado:
        highlight_block = comentario_formateado
    if highlight_message:
        if highlight_block:
            highlight_block["mensaje"] = highlight_message
        else:
            highlight_block = {
                "autor": None,
                "rol": None,
                "mensaje": highlight_message,
                "fecha": None,
                "fecha_formateada": _format_datetime(datetime.utcnow()),
                "estado": None,
                "adjuntos": [],
            }

    formatted_extra_adjuntos: List[Dict[str, Any]] = []
    if extra_adjuntos:
        for adj in extra_adjuntos:
            adj_dict = _format_attachment(adj)
            if adj_dict:
                formatted_extra_adjuntos.append(adj_dict)
    if formatted_extra_adjuntos:
        if highlight_block:
            highlight_block.setdefault("adjuntos", []).extend(formatted_extra_adjuntos)
        else:
            highlight_block = {
                "autor": None,
                "rol": None,
                "mensaje": highlight_message
                or "Se agregaron nuevos archivos adjuntos al ticket.",
                "fecha": None,
                "fecha_formateada": _format_datetime(datetime.utcnow()),
                "estado": None,
                "adjuntos": formatted_extra_adjuntos,
            }

    context["highlight"] = highlight_block
    context["mostrar_historial"] = bool(context.get("historial"))
    context["total_adjuntos"] = len(context.get("adjuntos", []))
    return context

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


def enviar_email_ticket_admin(
    ticket,
    *,
    admin_user=None,
    tipo_ticket: Optional[str] = None,
    ticket_data: Optional[Dict[str, Any]] = None,
    mensaje_destacado: Optional[str] = None,
    comentario=None,
    adjuntos: Optional[List[Any]] = None,
    es_creacion: Optional[bool] = None,
) -> bool:
    """Envía un correo al administrador con la información del ticket."""

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

    tipo_ticket = tipo_ticket or (
        "municipio" if getattr(ticket, "municipio_id", None) else "pyme"
    )

    base_context = _build_ticket_common_context(
        ticket,
        tipo_ticket=tipo_ticket,
        admin_user=admin_user,
        ticket_data=ticket_data,
    )

    ticket_codigo = base_context.get("ticket_codigo") or base_context.get("ticket_numero")
    es_creacion = (
        es_creacion
        if es_creacion is not None
        else comentario is None and not mensaje_destacado and not adjuntos
    )

    if es_creacion:
        asunto_email = f"Nuevo ticket {ticket_codigo or ''}".strip() or "Nuevo ticket"
        header_title = "Nuevo ticket recibido"
        if admin_user and getattr(admin_user, "name", None):
            intro_text = (
                f"Hola {admin_user.name}, se registró un nuevo ticket "
                f"{ticket_codigo or ''}."
            ).strip()
        else:
            intro_text = "Se generó un nuevo ticket en tu panel de gestión."
        highlight_text = (
            mensaje_destacado
            or "Revisá los detalles completos para asignarlo y darle seguimiento."
        )
    else:
        asunto_email = (
            f"Actualización ticket {ticket_codigo or ''}".strip()
            or "Actualización de ticket"
        )
        if comentario is not None and getattr(comentario, "estado_ticket", None):
            header_title = "Cambio de estado del ticket"
        else:
            header_title = "Nueva actividad en el ticket"
        intro_text = (
            f"Hay novedades en el ticket {ticket_codigo or ''}. "
            "Te compartimos el resumen actualizado."
        ).strip()
        highlight_text = (
            mensaje_destacado or "Se registró una nueva actividad en el ticket."
        )

    primary_button_text = (
        "Ver ticket en el panel" if base_context.get("panel_url") else None
    )
    primary_button_url = base_context.get("panel_url")
    secondary_button_text = (
        "Ver ubicación" if base_context.get("map_link_url") else None
    )
    secondary_button_url = base_context.get("map_link_url")

    context = _build_ticket_email_context(
        ticket,
        tipo_ticket=tipo_ticket,
        audience="admin",
        header_title=header_title,
        intro_text=intro_text,
        admin_user=admin_user,
        ticket_data=ticket_data,
        highlight_message=highlight_text,
        comentario=comentario,
        extra_adjuntos=adjuntos,
        primary_button_text=primary_button_text,
        primary_button_url=primary_button_url,
        secondary_button_text=secondary_button_text,
        secondary_button_url=secondary_button_url,
        footer_text="Recibís este correo porque estás configurado como administrador del asistente virtual.",
    )

    cuerpo_html_ticket = render_template("email/ticket_resumen.html", **context)
    return enviar_email(destino, asunto_email, cuerpo_html_ticket)


import requests
from models import ArchivoAdjunto

def enviar_email_con_multiples_adjuntos(destinos: List[str], asunto: str, cuerpo_html: str, adjuntos: List[ArchivoAdjunto], cuerpo_texto: str = "") -> bool:
    """Envía un email con múltiples archivos adjuntos."""
    smtp_host = current_app.config.get("SMTP_HOST")
    smtp_port = current_app.config.get("SMTP_PORT", 587)
    smtp_user = current_app.config.get("SMTP_USER")
    smtp_password = current_app.config.get("SMTP_PASSWORD")
    from_email = current_app.config.get("MAIL_FROM_ADDRESS")
    from_name = current_app.config.get("MAIL_FROM_NAME", from_email)
    use_tls = current_app.config.get("SMTP_USE_TLS", True)
    use_ssl = current_app.config.get("SMTP_USE_SSL", False)

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
        logger.info(f"[EMAIL_MULTI_ADJ] Intentando enviar a {', '.join(destinos)} con {len(adjuntos)} adjuntos.")
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
        logger.info(f"[EMAIL_MULTI_ADJ] Enviado a {', '.join(destinos)} exitosamente.")
        return True
    except Exception as e:
        logger.error(f"[EMAIL_MULTI_ADJ] Error enviando correo con múltiples adjuntos: {e}", exc_info=True)
        return False


def enviar_email_ticket_cliente(
    ticket,
    *,
    tipo_ticket: Optional[str] = None,
    admin_user=None,
    ticket_data: Optional[Dict[str, Any]] = None,
    mensaje_destacado: Optional[str] = None,
    comentario=None,
    adjuntos: Optional[List[Any]] = None,
    es_creacion: Optional[bool] = None,
) -> bool:
    """Envía un correo al vecino/cliente con el resumen del ticket."""

    datos_ticket = ticket_data or {}
    destino = (
        getattr(ticket, "email_vecino", None)
        or getattr(ticket, "email", None)
        or datos_ticket.get("email_cliente")
    )
    if not destino and getattr(ticket, "user_id", None):
        try:
            from models import User, db  # Import local para evitar ciclos

            usuario = db.session.get(User, ticket.user_id)
            if usuario and getattr(usuario, "email", None):
                destino = usuario.email
        except Exception:  # pragma: no cover - fallback silencioso
            destino = destino

    if not destino:
        logger.warning(
            f"[EMAIL] Ticket {getattr(ticket, 'id', 'N/A')} sin email de cliente."
        )
        return False

    tipo_ticket = tipo_ticket or (
        "municipio" if getattr(ticket, "municipio_id", None) else "pyme"
    )

    base_context = _build_ticket_common_context(
        ticket,
        tipo_ticket=tipo_ticket,
        admin_user=admin_user,
        ticket_data=ticket_data,
    )

    ticket_codigo = base_context.get("ticket_codigo") or base_context.get("ticket_numero")
    nombre_vecino = base_context.get("cliente_info", {}).get("nombre") or "Vecino/a"
    asunto_ticket = base_context.get("asunto") or (
        "Reclamo registrado" if tipo_ticket == "municipio" else "Pedido recibido"
    )

    es_creacion = (
        es_creacion
        if es_creacion is not None
        else comentario is None and not mensaje_destacado and not adjuntos
    )

    if es_creacion:
        asunto = (
            f"Ticket #{ticket_codigo or ''} Recibido: {asunto_ticket}".strip()
            or "Ticket recibido"
        )
        header_title = "Tu reclamo está en marcha" if tipo_ticket == "municipio" else "Tu pedido fue registrado"
        intro_text = (
            f"Hola {nombre_vecino}, recibimos tu ticket {ticket_codigo or ''} "
            "y ya lo estamos gestionando."
        ).strip()
        highlight_text = (
            mensaje_destacado
            or "Podés seguir el estado y responder desde el portal en cualquier momento."
        )
    else:
        asunto = (
            f"Actualización de tu ticket {ticket_codigo or ''}".strip()
            or "Actualización de tu ticket"
        )
        if comentario is not None and getattr(comentario, "estado_ticket", None):
            header_title = "El estado de tu ticket cambió"
        else:
            header_title = "Tenemos novedades sobre tu ticket"
        intro_text = (
            f"Hola {nombre_vecino}, hay novedades en tu ticket {ticket_codigo or ''}."
        ).strip()
        highlight_text = (
            mensaje_destacado
            or "Revisá la actualización y respondé si necesitás sumar más información."
        )

    primary_button_text = (
        "Ver estado del ticket" if base_context.get("chat_url") else None
    )
    primary_button_url = base_context.get("chat_url")
    secondary_button_text = (
        "Ver ubicación" if base_context.get("map_link_url") else None
    )
    secondary_button_url = base_context.get("map_link_url")

    context = _build_ticket_email_context(
        ticket,
        tipo_ticket=tipo_ticket,
        audience="cliente",
        header_title=header_title,
        intro_text=intro_text,
        admin_user=admin_user,
        ticket_data=ticket_data,
        highlight_message=highlight_text,
        comentario=comentario,
        extra_adjuntos=adjuntos,
        primary_button_text=primary_button_text,
        primary_button_url=primary_button_url,
        secondary_button_text=secondary_button_text,
        secondary_button_url=secondary_button_url,
        footer_text="Este es un mensaje automático. Te avisaremos ante cada novedad del ticket.",
    )

    try:
        cuerpo_html = render_template("email/ticket_resumen.html", **context)
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
    comentario=None,
    adjuntos: Optional[List[Any]] = None,
) -> bool:
    """Notifica al cliente que su ticket tiene una novedad."""

    tipo_ticket = "municipio" if getattr(ticket, "municipio_id", None) else "pyme"
    return enviar_email_ticket_cliente(
        ticket,
        tipo_ticket=tipo_ticket,
        mensaje_destacado=mensaje,
        comentario=comentario,
        adjuntos=adjuntos,
        es_creacion=False,
    )


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
