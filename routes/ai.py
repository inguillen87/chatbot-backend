import math

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, or_
from werkzeug.exceptions import RequestEntityTooLarge

from models import PlantillasRespuesta, TenantProfile, User
from routes.auth import token_requerido
from services.common_utils import cosine_similarity
from services.embedding_service import embed_textos_llm
from services.ai_response_templates import (
    AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
    AITemplateContractError,
    MAX_SUGGESTION_CONTEXT_BYTES,
    MAX_SUGGESTION_REQUEST_BYTES,
    MAX_SUGGESTION_SUBJECT_BYTES,
    MAX_TOP_N,
    parse_bounded_json_object,
    prime_bounded_request_stream,
    validate_top_n,
    validate_utf8_text,
)
from utils.auth_decorators import _is_authorized_for_tenant
from utils.permissions import require_role


ai_bp = Blueprint("ai_bp", __name__, url_prefix="/api/ai")

MIN_SIMILARITY_THRESHOLD = 0.5


def _suggestion_contract_error(exc: AITemplateContractError):
    return (
        jsonify(
            exc.to_dict(
                contract_version=AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
            )
        ),
        exc.status_code,
    )


def _suggestion_body_too_large_error() -> AITemplateContractError:
    return AITemplateContractError(
        "request_body_too_large",
        field="body",
        status_code=413,
        message=(
            "El cuerpo de la solicitud supera el máximo de "
            f"{MAX_SUGGESTION_REQUEST_BYTES} bytes."
        ),
    )


@ai_bp.url_value_preprocessor
def _prime_suggestion_body_limit(endpoint, _values):
    if (
        endpoint == f"{ai_bp.name}.suggest_templates_route"
        and request.method == "POST"
    ):
        prime_bounded_request_stream(
            request,
            max_bytes=MAX_SUGGESTION_REQUEST_BYTES,
        )


@ai_bp.errorhandler(AITemplateContractError)
def _handle_suggestion_contract_error(exc: AITemplateContractError):
    return _suggestion_contract_error(exc)


@ai_bp.errorhandler(RequestEntityTooLarge)
def _handle_suggestion_stream_too_large(_exc: RequestEntityTooLarge):
    return _suggestion_contract_error(_suggestion_body_too_large_error())


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
            return None, (
                jsonify(
                    {
                        "contract_version": AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
                        "error": "Tenant no encontrado.",
                        "reason_code": "tenant_not_found",
                    }
                ),
                404,
            )
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
        return None, (
            jsonify(
                {
                    "contract_version": AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
                    "error": "Permiso denegado para este tenant.",
                    "reason_code": "tenant_forbidden",
                }
            ),
            403,
        )
    return tenant, None


def _template_scope(tenant: TenantProfile | None):
    if tenant:
        return or_(PlantillasRespuesta.tenant_id == tenant.id, PlantillasRespuesta.tenant_id.is_(None))
    return PlantillasRespuesta.tenant_id.is_(None)


@ai_bp.route("/suggest-templates", methods=["POST"])
@token_requerido
@require_role("admin", "empleado")
def suggest_templates_route(current_user: User):
    try:
        data = parse_bounded_json_object(
            request,
            max_bytes=MAX_SUGGESTION_REQUEST_BYTES,
        )
    except AITemplateContractError as exc:
        return _suggestion_contract_error(exc)

    tenant, tenant_error = _resolve_tenant_for_suggestions(current_user, data)
    if tenant_error:
        return tenant_error

    try:
        asunto = validate_utf8_text(
            data.get("asunto"),
            field="asunto",
            max_bytes=MAX_SUGGESTION_SUBJECT_BYTES,
        )
        contexto_ticket = validate_utf8_text(
            data.get("contexto_ticket", ""),
            field="contexto_ticket",
            max_bytes=MAX_SUGGESTION_CONTEXT_BYTES,
            required=False,
        )
        top_n = validate_top_n(data.get("top_n", 3))
    except AITemplateContractError as exc:
        return _suggestion_contract_error(exc)

    current_app.logger.info(
        "[SUGGEST_TEMPLATES] request rol=%s scope=%s asunto_bytes=%s contexto_bytes=%s top_n=%s",
        current_user.rol,
        "tenant" if tenant else "global",
        len(asunto.encode("utf-8")),
        len(contexto_ticket.encode("utf-8")),
        top_n,
    )

    texto_consulta = asunto
    if contexto_ticket.strip():
        texto_consulta = f"{asunto}\n\n{contexto_ticket}"
    if not texto_consulta.strip():
        return jsonify(
            {
                "contract_version": AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
                "sugerencias": [],
                "message": "El texto de consulta esta vacio.",
            }
        ), 200

    try:
        query_embedding_list = embed_textos_llm(textos=[texto_consulta], input_type="search_query")
        if not query_embedding_list or not query_embedding_list[0]:
            current_app.logger.error("[SUGGEST_TEMPLATES] No se pudo generar embedding para la consulta.")
            return jsonify(
                {
                    "contract_version": AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
                    "error": "Error al generar el embedding para la consulta.",
                    "reason_code": "query_embedding_unavailable",
                }
            ), 500
        query_embedding = query_embedding_list[0]
    except Exception as exc:
        current_app.logger.error(
            "[SUGGEST_TEMPLATES] embedding_failed error_type=%s",
            type(exc).__name__,
        )
        return jsonify(
            {
                "contract_version": AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
                "error": "Excepcion al procesar la consulta con IA.",
                "reason_code": "query_embedding_failed",
            }
        ), 500

    try:
        plantillas_activas = PlantillasRespuesta.query.filter(
            PlantillasRespuesta.is_active.is_(True),
            PlantillasRespuesta.embedding.is_not(None),
            _template_scope(tenant),
        ).all()
    except Exception as exc:
        current_app.logger.error(
            "[SUGGEST_TEMPLATES] template_query_failed error_type=%s",
            type(exc).__name__,
        )
        return jsonify(
            {
                "contract_version": AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
                "error": "Error al obtener plantillas de la base de datos.",
                "reason_code": "template_query_failed",
            }
        ), 500

    if not plantillas_activas:
        current_app.logger.info("[SUGGEST_TEMPLATES] No hay plantillas activas con embeddings disponibles.")
        return jsonify(
            {
                "contract_version": AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
                "sugerencias": [],
                "message": "No hay plantillas activas configuradas con embeddings.",
                "scope": {
                    "tenant_id": getattr(tenant, "id", None),
                    "tenant_slug": getattr(tenant, "slug", None),
                    "includes_global_fallback": bool(tenant),
                    "active_only": True,
                },
            }
        ), 200

    sugerencias_con_score = []
    for plantilla in plantillas_activas:
        if not plantilla.embedding or not isinstance(plantilla.embedding, list):
            current_app.logger.warning(
                "[SUGGEST_TEMPLATES] Plantilla ID %s no tiene embedding valido.",
                plantilla.id,
            )
            continue

        try:
            score = float(cosine_similarity(query_embedding, plantilla.embedding))
        except Exception as exc:
            current_app.logger.error(
                "[SUGGEST_TEMPLATES] similarity_failed template_id=%s error_type=%s",
                plantilla.id,
                type(exc).__name__,
            )
            score = 0.0

        if not math.isfinite(score):
            current_app.logger.warning(
                "[SUGGEST_TEMPLATES] similarity_non_finite template_id=%s",
                plantilla.id,
            )
            continue

        if score >= MIN_SIMILARITY_THRESHOLD:
            sugerencias_con_score.append(
                {
                    "id_plantilla": str(plantilla.id),
                    "tenant_id": plantilla.tenant_id,
                    "tenant_slug": getattr(plantilla.tenant, "slug", None),
                    "scope": "global" if plantilla.tenant_id is None else "tenant",
                    "name": plantilla.name,
                    "text": plantilla.text,
                    "score": score,
                }
            )

    sugerencias_ordenadas = sorted(
        sugerencias_con_score,
        key=lambda item: (
            -item["score"],
            0 if item["scope"] == "tenant" else 1,
            item["id_plantilla"],
        ),
    )
    final_sugerencias = [
        {**item, "score": round(item["score"], 4)}
        for item in sugerencias_ordenadas[:top_n]
    ]

    current_app.logger.info(
        "[SUGGEST_TEMPLATES] completed suggestions=%s scope=%s",
        len(final_sugerencias),
        "tenant" if tenant else "global",
    )
    return jsonify(
        {
            "contract_version": AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
            "sugerencias": final_sugerencias,
            "limits": {"top_n_max": MAX_TOP_N},
            "scope": {
                "tenant_id": getattr(tenant, "id", None),
                "tenant_slug": getattr(tenant, "slug", None),
                "includes_global_fallback": bool(tenant),
                "active_only": True,
            },
        }
    ), 200
