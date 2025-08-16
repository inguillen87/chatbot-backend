from flask import Blueprint, jsonify
from utils.auth_helpers import strict_token_requerido
from services.logic import es_rubro_publico

legacy_auth_bp = Blueprint('legacy_auth', __name__)

@legacy_auth_bp.route('/me', methods=['GET', 'OPTIONS'])
@legacy_auth_bp.route('/perfil', methods=['GET', 'OPTIONS'])
@legacy_auth_bp.route('/profile', methods=['GET', 'OPTIONS'])
@strict_token_requerido
def get_current_user(user):
    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    from utils.plan_limits import limite_para_usuario
    tipo_chat = getattr(user, "tipo_chat", None) or (
        "municipio" if es_rubro_publico(rubro_nombre) else "pyme"
    )
    catalogo_label = (
        "Cargar Catálogo de Trámites" if tipo_chat == "municipio" else "Cargar Catálogo de Productos"
    )
    return jsonify({
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "token": user.token,
        "rubro": rubro_nombre,
        "nombre_empresa": user.nombre_empresa,
        "rol": user.rol,
        "empresa_id": user.empresa_id,
        "telefono": user.telefono,
        "direccion": user.direccion,
        "ciudad": user.ciudad,
        "provincia": user.provincia,
        "pais": user.pais,
        "latitud": user.latitud,
        "longitud": user.longitud,
        "link_web": user.link_web,
        "plan": user.plan,
        "preguntas_usadas": user.preguntas_usadas,
        "limite_preguntas": limite_para_usuario(user),
        "horario_json": user.horario_json,
        "logo_url": getattr(user, "logo_url", ""),
        "color_primario": getattr(user, "color_primario", None),
        "color_secundario": getattr(user, "color_secundario", None),
        "badge_tipo": getattr(user, "badge_tipo", None),
        "categorias": user.ticket_categorias or "",
        "tipo_chat": tipo_chat,
        "catalogo_label": catalogo_label,
    })
