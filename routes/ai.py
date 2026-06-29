from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, or_

from models import PlantillasRespuesta, TenantProfile, User
from routes.auth import token_requerido
from services.common_utils import cosine_similarity
from services.embedding_service import embed_textos_llm
from utils.auth_decorators import _is_authorized_for_tenant
from utils.permissions import require_role


ai_bp = Blueprint("ai_bp", __name__, url_prefix="/api/ai")

MIN_SIMILARITY_THRESHOLD = 0.5


def _apply_tenant_alias(slug: str | None) -> str | None:
    if not slug:
        return None
    try:
        from services.tenant_resolver import apply_tenant_alias

        return apply_tenant_alias(slug) or slug
    except Exception:
        return slug


def _tenant_hint(data: dict | None) -> str | None:
    data = data if isinstance(data, dict) else {}
    value = (
        request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or data.get("tenant_slug")
        or data.get("tenantSlug")
        or data.get("tenant")
    )
    return str(value).strip() if value else None


def _resolve_tenant_for_suggestions(current_user: User, data: dict | None):
    hint = _apply_tenant_alias(_tenant_hint(data))
    tenant = None

    if hint:
        tenant = (
            TenantProfile.query.filter(func.lower(TenantProfile.slug) == hint.lower())
            .order_by(TenantProfile.id.asc())
            .first()
        )
        if not tenant:
            return None, (jsonify({"error": f"Tenant '{hint}' no encontrado."}), 404)
    else:
        tenant_id = getattr(current_user, "tenant_id", None)
        user_slug = _apply_tenant_alias(getattr(current_user, "tenant_slug", None))
        if tenant_id:
            tenant = TenantProfile.query.get(tenant_id)
        if not tenant and user_slug:
            tenant = (
                TenantProfile.query.filter(func.lower(TenantProfile.slug) == user_slug.lower())
                .order_by(TenantProfile.id.asc())
                .first()
            )
        if not tenant:
            tenant = (
                TenantProfile.query.filter(
                    or_(TenantProfile.pyme_id == current_user.id, TenantProfile.municipio_id == current_user.id)
                )
                .order_by(TenantProfile.id.asc())
                .first()
            )

    if tenant and not _is_authorized_for_tenant(current_user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        return None, (jsonify({"error": "Permiso denegado para este tenant."}), 403)
    return tenant, None


def _template_scope(tenant: TenantProfile | None):
    if tenant:
        return or_(PlantillasRespuesta.tenant_id == tenant.id, PlantillasRespuesta.tenant_id.is_(None))
    return PlantillasRespuesta.tenant_id.is_(None)


@ai_bp.route("/suggest-templates", methods=["POST"])
@token_requerido
@require_role("admin", "empleado")
def suggest_templates_route(current_user: User):
    data = request.get_json()

    if not data or not data.get("asunto"):
        return jsonify({"error": "El campo 'asunto' es obligatorio."}), 400

    tenant, tenant_error = _resolve_tenant_for_suggestions(current_user, data)
    if tenant_error:
        return tenant_error

    asunto = data["asunto"]
    contexto_ticket = data.get("contexto_ticket", "")
    top_n = data.get("top_n", 3)

    if not isinstance(asunto, str) or not asunto.strip():
        return jsonify({"error": "El campo 'asunto' debe ser un string no vacio."}), 400
    if not isinstance(contexto_ticket, str):
        return jsonify({"error": "El campo 'contexto_ticket' debe ser un string."}), 400
    if not isinstance(top_n, int) or top_n <= 0:
        return jsonify({"error": "El campo 'top_n' debe ser un entero positivo."}), 400

    current_app.logger.info(
        "[SUGGEST_TEMPLATES] user=%s rol=%s tenant=%s asunto='%s...' top_n=%s",
        current_user.id,
        current_user.rol,
        getattr(tenant, "slug", None),
        asunto[:50],
        top_n,
    )

    texto_consulta = asunto
    if contexto_ticket.strip():
        texto_consulta = f"{asunto}\n\n{contexto_ticket}"
    if not texto_consulta.strip():
        return jsonify({"sugerencias": [], "message": "El texto de consulta esta vacio."}), 200

    try:
        query_embedding_list = embed_textos_llm(textos=[texto_consulta], input_type="search_query")
        if not query_embedding_list or not query_embedding_list[0]:
            current_app.logger.error("[SUGGEST_TEMPLATES] No se pudo generar embedding para la consulta.")
            return jsonify({"error": "Error al generar el embedding para la consulta."}), 500
        query_embedding = query_embedding_list[0]
    except Exception as exc:
        current_app.logger.error("[SUGGEST_TEMPLATES] Excepcion al generar embedding: %s", exc, exc_info=True)
        return jsonify({"error": "Excepcion al procesar la consulta con IA."}), 500

    try:
        plantillas_activas = PlantillasRespuesta.query.filter(
            PlantillasRespuesta.is_active == True,
            PlantillasRespuesta.embedding != None,
            _template_scope(tenant),
        ).all()
    except Exception as exc:
        current_app.logger.error("[SUGGEST_TEMPLATES] Error al consultar plantillas: %s", exc, exc_info=True)
        return jsonify({"error": "Error al obtener plantillas de la base de datos."}), 500

    if not plantillas_activas:
        current_app.logger.info("[SUGGEST_TEMPLATES] No hay plantillas activas con embeddings disponibles.")
        return jsonify({"sugerencias": [], "message": "No hay plantillas activas configuradas con embeddings."}), 200

    sugerencias_con_score = []
    for plantilla in plantillas_activas:
        if not plantilla.embedding or not isinstance(plantilla.embedding, list):
            current_app.logger.warning(
                "[SUGGEST_TEMPLATES] Plantilla ID %s no tiene embedding valido.",
                plantilla.id,
            )
            continue

        try:
            score = cosine_similarity(query_embedding, plantilla.embedding)
        except Exception as exc:
            current_app.logger.error(
                "[SUGGEST_TEMPLATES] Error calculando similaridad para plantilla ID %s: %s",
                plantilla.id,
                exc,
                exc_info=True,
            )
            score = 0.0

        if score >= MIN_SIMILARITY_THRESHOLD:
            sugerencias_con_score.append(
                {
                    "id_plantilla": str(plantilla.id),
                    "tenant_id": plantilla.tenant_id,
                    "tenant_slug": getattr(plantilla.tenant, "slug", None),
                    "scope": "global" if plantilla.tenant_id is None else "tenant",
                    "name": plantilla.name,
                    "text": plantilla.text,
                    "score": round(score, 4),
                }
            )

    sugerencias_ordenadas = sorted(sugerencias_con_score, key=lambda item: item["score"], reverse=True)
    final_sugerencias = sugerencias_ordenadas[:top_n]

    current_app.logger.info(
        "[SUGGEST_TEMPLATES] Devolviendo %s sugerencias tenant=%s",
        len(final_sugerencias),
        getattr(tenant, "slug", None),
    )
    return jsonify({"sugerencias": final_sugerencias}), 200
