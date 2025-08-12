"""
Handles the multi-turn flow for creating a "reclamo" (claim).
"""
from enum import Enum, auto
from services.geo import reverse as geo_reverse
from services.integrations import tickets as tickets_integration
import hashlib
import time
import logging

logger = logging.getLogger(__name__)

# In-memory cache for idempotency
IDEMPOTENCY_CACHE = {}
CACHE_TTL = 60  # 60 seconds

class ReclamoState(Enum):
    START = auto()
    ESPERANDO_CATEGORIA = auto()
    ESPERANDO_UBICACION = auto()
    ESPERANDO_DISTRITO = auto()
    ESPERANDO_DESCRIPCION = auto()
    CONFIRMACION = auto()
    CREAR_TICKET = auto()
    DONE = auto()

def _get_reclamos_menu():
    opciones = [
        {"texto": "1. 💡 Luminaria", "id": "Luminaria"},
        {"texto": "2. 🌳 Arbolado", "id": "Arbolado"},
        {"texto": "3. 🧹 Limpieza y riego", "id": "Limpieza y riego"},
        {"texto": "4. 🚧 Arreglo de calle", "id": "Arreglo de calle"},
        {"texto": "5. 💧 Pérdida de agua", "id": "Pérdida de agua"},
        {"texto": "6. 📋 Otros", "id": "Otros"},
    ]
    return {
        "message_body": "Por favor, elegí una categoría para tu reclamo:",
        "options_list": opciones, "message_type": "interactive_buttons",
    }

def _direccion_en_texto(text):
    return len(text.split()) > 2

def _resumen(slots):
    return (
        f"Por favor, confirmá los datos de tu reclamo:\n\n"
        f"**Categoría:** {slots.get('categoria', 'N/A')}\n"
        f"**Ubicación:** {slots.get('direccion_normalizada', 'N/A')}\n"
        f"**Distrito:** {slots.get('distrito', 'N/A')}\n"
        f"**Descripción:** {slots.get('descripcion', 'N/A')}\n\n"
        "¿Son correctos los datos?"
    )

def _confirma(text):
    return text.lower().strip() in ["sí", "si", "ok", "dale", "confirmo", "✅", "correcto", "si, son correctos"]

def _idem_hash(phone, slots):
    key_str = f"{phone}:{slots.get('categoria')}:{slots.get('direccion_normalizada')}:{slots.get('distrito')}"
    return hashlib.md5(key_str.encode()).hexdigest()

def handle(msg, ctx):
    if 'flow' not in ctx or ctx['flow'] != 'reclamos':
        ctx.update({'flow': 'reclamos', 'state': ReclamoState.START.name, 'slots': {'pregunta_inicial': msg.get('text'), 'start_time': time.time()}})

    state = ReclamoState[ctx['state']]
    slots = ctx['slots']
    text = msg.get('text', '')

    if state == ReclamoState.START:
        if slots.get('categoria'):
            ctx['state'] = ReclamoState.ESPERANDO_UBICACION.name
            state = ReclamoState.ESPERANDO_UBICACION
        else:
            logger.info("[TELEMETRY] Asking for slot: categoria")
            ctx['state'] = ReclamoState.ESPERANDO_CATEGORIA.name
            return _get_reclamos_menu()

    if state == ReclamoState.ESPERANDO_CATEGORIA:
        slots['categoria'] = text
        ctx['state'] = ReclamoState.ESPERANDO_UBICACION.name
        state = ReclamoState.ESPERANDO_UBICACION

    if state == ReclamoState.ESPERANDO_UBICACION:
        if slots.get('direccion_normalizada'):
            ctx['state'] = ReclamoState.ESPERANDO_DISTRITO.name
            state = ReclamoState.ESPERANDO_DISTRITO
        elif msg.get('type') == 'location' and msg.get('lat') and msg.get('lon'):
            geo_info = geo_reverse.reverse(msg['lat'], msg['lon'])
            if geo_info:
                slots.update({
                    'lat': msg['lat'], 'lon': msg['lon'],
                    'direccion_normalizada': geo_info.get('direccion'),
                    'distrito': geo_info.get('distrito')
                })
                ctx['state'] = ReclamoState.ESPERANDO_DESCRIPCION.name
                state = ReclamoState.ESPERANDO_DESCRIPCION
            else:
                return { "message_body": "No pude obtener la dirección. Por favor, escribila." }
        elif text and _direccion_en_texto(text):
            slots['direccion_normalizada'] = text
            logger.info("[TELEMETRY] Asking for slot: distrito")
            ctx['state'] = ReclamoState.ESPERANDO_DISTRITO.name
            return { "message_body": "Gracias. ¿A qué distrito pertenece?" }
        else:
            logger.info("[TELEMETRY] Asking for slot: ubicacion")
            return { "message_body": "No entendí la ubicación. Por favor, enviame tu ubicación o escribí la dirección completa." }

    if state == ReclamoState.ESPERANDO_DISTRITO:
        if slots.get('distrito'):
            ctx['state'] = ReclamoState.ESPERANDO_DESCRIPCION.name
            state = ReclamoState.ESPERANDO_DESCRIPCION
        else:
            slots['distrito'] = text
            ctx['state'] = ReclamoState.ESPERANDO_DESCRIPCION.name
            state = ReclamoState.ESPERANDO_DESCRIPCION

    if state == ReclamoState.ESPERANDO_DESCRIPCION:
        if slots.get('descripcion'):
            ctx['state'] = ReclamoState.CONFIRMACION.name
            state = ReclamoState.CONFIRMACION
        else:
            logger.info("[TELEMETRY] Asking for slot: descripcion")
            if text.lower() in ["no", "listo", "continuar", "saltar", "sin descripcion"]:
                slots['descripcion'] = "Sin descripción adicional."
            else:
                slots['descripcion'] = text
            ctx['state'] = ReclamoState.CONFIRMACION.name
            return {
                "message_body": _resumen(slots),
                "options_list": [{"texto": "Sí, son correctos"}, {"texto": "No, quiero editar"}],
                "message_type": "interactive_buttons"
            }

    if state == ReclamoState.CONFIRMACION:
        if _confirma(text):
            ctx['state'] = ReclamoState.CREAR_TICKET.name
            state = ReclamoState.CREAR_TICKET
        else:
            ctx.update({'state': ReclamoState.START.name, 'slots': {}})
            return { "message_body": "Ok, empecemos de nuevo.", **_get_reclamos_menu() }

    if state == ReclamoState.CREAR_TICKET:
        idem_key = _idem_hash(ctx.get('phone'), slots)
        cached_ticket = IDEMPOTENCY_CACHE.get(idem_key)
        if cached_ticket and (time.time() - cached_ticket['timestamp']) < CACHE_TTL:
            logger.info(f"[TELEMETRY] Idempotency hit for key: {idem_key}")
            ticket_id = cached_ticket['ticket_id']
            return { "message_body": f"Ya existe un reclamo con estos datos (N° {ticket_id}). ¿Necesitás algo más?" }

        ticket_data = {
            "user_id": ctx.get('viewer_user_obj').id if ctx.get('viewer_user_obj') else None,
            "municipio_id": ctx.get('user_obj').municipio_id if ctx.get('user_obj') else None,
            "anon_id": ctx.get('phone'),
            "asunto": f"Reclamo por {slots.get('categoria')}",
            "categoria": slots.get('categoria'),
            "pregunta": slots.get('pregunta_inicial'),
            "detalles": slots.get('descripcion'),
            "direccion": slots.get('direccion_normalizada'),
            "latitud": slots.get('lat'),
            "longitud": slots.get('lon'),
            "nombre_vecino": ctx.get('profile_name', 'Vecino/a'),
            "telefono_vecino": ctx.get('phone'),
            "email_vecino": None,
            "canal_ingreso": ctx.get('channel', 'web')
        }

        created_ticket = tickets_integration.create(ticket_data)

        if created_ticket and created_ticket.get('id'):
            IDEMPOTENCY_CACHE[idem_key] = {'ticket_id': created_ticket['id'], 'timestamp': time.time()}
            ctx['state'] = ReclamoState.DONE.name
            creation_time = time.time() - slots.get('start_time', time.time())
            logger.info(f"[TELEMETRY] Ticket creation time: {creation_time:.2f}s")
            return {
                "message_body": f"¡Tu reclamo fue generado con éxito! N° de Ticket: {created_ticket['id']}. "
                                f"Podés agregar una foto o video respondiendo a este mensaje.",
                "options_list": [
                    {"texto": "Ver estado del reclamo", "id": f"status_{created_ticket['id']}"},
                    {"texto": "Hacer otro reclamo", "id": "iniciar_reclamo"}
                ],
                "message_type": "interactive_buttons"
            }
        else:
            ctx['state'] = ReclamoState.START.name
            return { "message_body": "Hubo un error al crear tu ticket. Por favor, intentá de nuevo más tarde." }

    return {
        "message_body": f"DEBUG: Estado no manejado {ctx['state']}",
        "message_type": "text", "options_list": []
    }
