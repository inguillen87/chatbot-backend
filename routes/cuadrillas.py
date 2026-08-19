from __future__ import annotations

import math
from typing import Optional
from flask import Blueprint, jsonify, request
from extensions import db
from models import MunicipioTicket, TicketComentario, User
from routes.auth import token_requerido
from utils.permissions import require_role
from utils.time_utils import get_local_now

cuadrillas_bp = Blueprint("cuadrillas", __name__, url_prefix="/api/cuadrillas")


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(radius * c, 2)


@cuadrillas_bp.route("/tareas", methods=["GET", "OPTIONS"])
@token_requerido
@require_role("admin", "empleado", "cuadrilla", "operador")
def listar_tareas_cuadrilla(current_user: User):
    if request.method == "OPTIONS":
        return "", 204

    tenant_id = getattr(current_user, "tenant_id", None) or getattr(current_user, "municipio_id", None)
    if not tenant_id:
        return jsonify({"error": "Usuario sin municipio o cuadrilla asignada."}), 400

    query = MunicipioTicket.query.filter(
        (MunicipioTicket.municipio_id == tenant_id) | (MunicipioTicket.tenant_id == tenant_id)
    ).filter(
        MunicipioTicket.estado.in_(["nuevo", "abierto", "en_proceso", "asignado"])
    )

    # Optional category filter
    categoria = request.args.get("categoria")
    if categoria:
        query = query.filter(MunicipioTicket.categoria.ilike(f"%{categoria}%"))

    tickets = query.order_by(MunicipioTicket.fecha.desc()).all()

    # User GPS coordinates for distance calculation
    user_lat = request.args.get("lat", type=float)
    user_lng = request.args.get("lng", type=float)

    tareas = []
    for t in tickets:
        dist_km = None
        if user_lat is not None and user_lng is not None and t.latitud is not None and t.longitud is not None:
            dist_km = _haversine_km(user_lat, user_lng, t.latitud, t.longitud)

        tareas.append({
            "id": t.id,
            "nro_ticket": t.nro_ticket,
            "categoria": t.categoria or "General",
            "direccion": t.direccion or "Sin dirección especificada",
            "distrito": t.distrito or "Centro",
            "latitud": t.latitud,
            "longitud": t.longitud,
            "distancia_km": dist_km,
            "estado": t.estado,
            "descripcion": t.detalles or t.pregunta or "",
            "foto_url_inicial": getattr(t, "foto_url_directa", None),
            "fecha": t.fecha.isoformat() if t.fecha else None,
        })

    if user_lat is not None and user_lng is not None:
        tareas.sort(key=lambda item: item["distancia_km"] if item["distancia_km"] is not None else 9999.0)

    return jsonify({
        "total_tareas": len(tareas),
        "ubicacion_operario": {"lat": user_lat, "lng": user_lng} if user_lat is not None else None,
        "tareas": tareas,
    })


@cuadrillas_bp.route("/tareas/<int:ticket_id>/iniciar", methods=["POST", "OPTIONS"])
@token_requerido
@require_role("admin", "empleado", "cuadrilla", "operador")
def iniciar_trabajo_cuadrilla(current_user: User, ticket_id: int):
    if request.method == "OPTIONS":
        return "", 204

    ticket = db.session.get(MunicipioTicket, ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404

    ticket.estado = "en_proceso"
    ticket.ultima_actividad = get_local_now()

    comentario = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario=f"Cuadrilla {current_user.name or current_user.email} arribó al lugar e inició trabajos.",
        user_id=current_user.id,
        es_admin=True,
        origen="cuadrilla_pwa",
        estado_ticket="en_proceso",
    )
    db.session.add(comentario)
    db.session.commit()

    return jsonify({
        "status": "ok",
        "message": "Trabajo iniciado en vía pública.",
        "nro_ticket": ticket.nro_ticket,
        "estado": "en_proceso",
    })


@cuadrillas_bp.route("/tareas/<int:ticket_id>/completar", methods=["POST", "OPTIONS"])
@token_requerido
@require_role("admin", "empleado", "cuadrilla", "operador")
def completar_trabajo_cuadrilla(current_user: User, ticket_id: int):
    if request.method == "OPTIONS":
        return "", 204

    ticket = db.session.get(MunicipioTicket, ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404

    data = request.get_json() or {}
    foto_resolucion = data.get("foto_resolucion_url")
    comentario_texto = data.get("comentario", "Trabajo finalizado con éxito por la cuadrilla en calle.")

    ticket.estado = "cerrado"
    ticket.ultima_actividad = get_local_now()
    if foto_resolucion and hasattr(ticket, "foto_resolucion_url"):
        ticket.foto_resolucion_url = foto_resolucion

    comentario = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario=f"[CIERRE DE CUADRILLA]: {comentario_texto}" + (f" Foto: {foto_resolucion}" if foto_resolucion else ""),
        user_id=current_user.id,
        es_admin=True,
        origen="cuadrilla_pwa",
        estado_ticket="cerrado",
    )
    db.session.add(comentario)
    db.session.commit()

    # Disparar notificación oficial
    try:
        from services.notification_dispatcher import dispatch_ticket_state_change
        dispatch_ticket_state_change(
            ticket,
            "cerrado",
            tenant=getattr(current_user, "tenant_profile", None),
            comentario_extra=comentario_texto,
        )
    except Exception as e:
        pass

    return jsonify({
        "status": "ok",
        "message": "Reclamo resuelto y ciudadano notificado automáticamente.",
        "nro_ticket": ticket.nro_ticket,
        "estado": "cerrado",
        "foto_resolucion": foto_resolucion,
    })


__all__ = ["cuadrillas_bp"]
