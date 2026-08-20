# routes/ai_templates.py

import json
from typing import Any

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, or_
from sqlalchemy.orm.attributes import flag_modified
from werkzeug.exceptions import RequestEntityTooLarge

from models import PlantillasRespuesta, TenantProfile, db
from services.embedding_service import embed_textos_llm
from services.llm_bridge import llamar_llm_para_generacion_texto
from services.ai_response_templates import (
    AI_TEMPLATES_CONTRACT_VERSION,
    AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION,
    AITemplateContractError,
    MAX_GENERATE_TEMPLATE_REQUEST_BYTES,
    MAX_IMPROVE_TEMPLATE_REQUEST_BYTES,
    MAX_PREVIEW_REQUEST_BYTES,
    MAX_TEMPLATE_MUTATION_REQUEST_BYTES,
    MAX_TEMPLATE_NAME_BYTES,
    MAX_TEMPLATE_TEXT_BYTES,
    build_ticket_template_preview,
    parse_bounded_json_object,
    prime_bounded_request_stream,
    validate_active_only,
    validate_keywords,
    validate_utf8_text,
)
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import admin_o_empleado_requerido, token_requerido


ai_templates_bp = Blueprint("ai_templates", __name__, url_prefix="/api/ai")


def _template_contract_error(
    exc: AITemplateContractError,
    *,
    contract_version: str = AI_TEMPLATES_CONTRACT_VERSION,
):
    response = jsonify(exc.to_dict(contract_version=contract_version))
    if contract_version == AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION:
        response.headers["Cache-Control"] = "no-store"
    return response, exc.status_code


def _template_request_limit(endpoint: str | None) -> tuple[int, str] | None:
    if endpoint == f"{ai_templates_bp.name}.create_template":
        return MAX_TEMPLATE_MUTATION_REQUEST_BYTES, AI_TEMPLATES_CONTRACT_VERSION
    if endpoint == f"{ai_templates_bp.name}.update_template":
        return MAX_TEMPLATE_MUTATION_REQUEST_BYTES, AI_TEMPLATES_CONTRACT_VERSION
    if endpoint == f"{ai_templates_bp.name}.preview_ticket_template":
        return MAX_PREVIEW_REQUEST_BYTES, AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION
    if endpoint == f"{ai_templates_bp.name}.generate_template_text_from_prompt":
        return MAX_GENERATE_TEMPLATE_REQUEST_BYTES, AI_TEMPLATES_CONTRACT_VERSION
    if endpoint == f"{ai_templates_bp.name}.improve_template_text":
        return MAX_IMPROVE_TEMPLATE_REQUEST_BYTES, AI_TEMPLATES_CONTRACT_VERSION
    return None


def _request_body_too_large_error(max_bytes: int) -> AITemplateContractError:
    return AITemplateContractError(
        "request_body_too_large",
        field="body",
        status_code=413,
        message=f"El cuerpo de la solicitud supera el máximo de {max_bytes} bytes.",
    )


@ai_templates_bp.url_value_preprocessor
def _prime_template_body_limit(endpoint, _values):
    limit_contract = _template_request_limit(endpoint)
    if request.method in {"POST", "PUT"} and limit_contract is not None:
        max_bytes, _contract_version = limit_contract
        prime_bounded_request_stream(request, max_bytes=max_bytes)


@ai_templates_bp.errorhandler(AITemplateContractError)
def _handle_template_contract_error(exc: AITemplateContractError):
    limit_contract = _template_request_limit(request.endpoint)
    contract_version = (
        limit_contract[1] if limit_contract is not None else AI_TEMPLATES_CONTRACT_VERSION
    )
    return _template_contract_error(exc, contract_version=contract_version)


@ai_templates_bp.errorhandler(RequestEntityTooLarge)
def _handle_template_stream_too_large(_exc: RequestEntityTooLarge):
    limit_contract = _template_request_limit(request.endpoint)
    max_bytes, contract_version = limit_contract or (
        MAX_TEMPLATE_MUTATION_REQUEST_BYTES,
        AI_TEMPLATES_CONTRACT_VERSION,
    )
    return _template_contract_error(
        _request_body_too_large_error(max_bytes),
        contract_version=contract_version,
    )


def _json_object_or_error(*, max_bytes: int):
    try:
        payload = parse_bounded_json_object(request, max_bytes=max_bytes)
    except AITemplateContractError as exc:
        return None, exc
    return payload, None


def _normalize_keywords(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item is not None]
        if parsed:
            return [str(parsed)]
    return []


def _tenant_hint_from_request(payload: dict | None = None) -> str | None:
    payload = payload if isinstance(payload, dict) else {}
    value = (
        request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or payload.get("tenant_slug")
        or payload.get("tenantSlug")
        or payload.get("tenant")
    )
    return str(value).strip() if value else None


def _apply_tenant_alias(slug: str | None) -> str | None:
    if not slug:
        return None
    try:
        from services.tenant_resolver import apply_tenant_alias

        return apply_tenant_alias(slug) or slug
    except Exception:
        return slug


def _resolve_template_tenant(
    user,
    payload: dict | None = None,
    *,
    contract_version: str = AI_TEMPLATES_CONTRACT_VERSION,
):
    hint = _apply_tenant_alias(_tenant_hint_from_request(payload))
    tenant = None

    if hint:
        tenant = (
            TenantProfile.query.filter(func.lower(TenantProfile.slug) == hint.lower())
            .order_by(TenantProfile.id.asc())
            .first()
        )
        if not tenant:
            return None, _template_contract_error(
                AITemplateContractError(
                    "tenant_not_found",
                    field="tenant",
                    status_code=404,
                    message="Tenant no encontrado.",
                ),
                contract_version=contract_version,
            )
    else:
        tenant_id = getattr(user, "tenant_id", None)
        user_slug = _apply_tenant_alias(getattr(user, "tenant_slug", None))
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
                    or_(TenantProfile.pyme_id == user.id, TenantProfile.municipio_id == user.id)
                )
                .order_by(TenantProfile.id.asc())
                .first()
            )

    if tenant and not _is_authorized_for_tenant(user, tenant_id=tenant.id, tenant_slug=tenant.slug):
        return None, _template_contract_error(
            AITemplateContractError(
                "tenant_forbidden",
                field="tenant",
                status_code=403,
                message="Permiso denegado para este tenant.",
            ),
            contract_version=contract_version,
        )

    return tenant, None


def _visible_template_scope(tenant):
    if tenant:
        return or_(PlantillasRespuesta.tenant_id == tenant.id, PlantillasRespuesta.tenant_id.is_(None))
    return PlantillasRespuesta.tenant_id.is_(None)


def _serialize_template(plantilla: PlantillasRespuesta, tenant: TenantProfile | None = None, **extra):
    is_global = plantilla.tenant_id is None
    editable = bool(
        not is_global
        and tenant is not None
        and int(plantilla.tenant_id) == int(tenant.id)
    )
    payload = {
        "contract_version": AI_TEMPLATES_CONTRACT_VERSION,
        "id": plantilla.id,
        "tenant_id": plantilla.tenant_id,
        "tenant_slug": getattr(plantilla.tenant, "slug", None),
        "scope": "global" if is_global else "tenant",
        "readonly": not editable,
        "editable": editable,
        "name": plantilla.name,
        "text": plantilla.text,
        "keywords": _normalize_keywords(plantilla.keywords),
        "is_active": plantilla.is_active,
        "created_at": plantilla.created_at.isoformat() if plantilla.created_at else None,
        "updated_at": plantilla.updated_at.isoformat() if plantilla.updated_at else None,
    }
    if tenant:
        payload["request_tenant_slug"] = tenant.slug
    payload.update(extra)
    return payload


def _generate_embedding(text: str, template_name: str) -> list | None:
    try:
        embeddings_list = embed_textos_llm(textos=[text.strip()], input_type="search_document")
        if embeddings_list and len(embeddings_list) > 0:
            return embeddings_list[0]
        current_app.logger.warning(
            "[AI_TEMPLATES] embedding_unavailable text_bytes=%s name_bytes=%s",
            len(text.encode("utf-8")),
            len(template_name.encode("utf-8")),
        )
    except Exception as exc:
        current_app.logger.error(
            "[AI_TEMPLATES] embedding_failed error_type=%s",
            type(exc).__name__,
        )
    return None


@ai_templates_bp.route("/templates", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def get_all_templates(user):
    tenant, error = _resolve_template_tenant(user)
    if error:
        return error

    try:
        active_only = validate_active_only(request.args.get("active_only"))
    except AITemplateContractError as exc:
        return _template_contract_error(exc)

    try:
        query = PlantillasRespuesta.query.filter(_visible_template_scope(tenant))
        if active_only:
            query = query.filter(PlantillasRespuesta.is_active.is_(True))
        plantillas = (
            query
            .order_by(PlantillasRespuesta.tenant_id.is_(None).asc(), PlantillasRespuesta.name.asc())
            .all()
        )
        return jsonify(
            {
                "contract_version": AI_TEMPLATES_CONTRACT_VERSION,
                # Legacy collection name remains the canonical compatibility field.
                "plantillas": [_serialize_template(item, tenant) for item in plantillas],
                "filters": {"active_only": active_only},
                "scope": {
                    "tenant_id": getattr(tenant, "id", None),
                    "tenant_slug": getattr(tenant, "slug", None),
                    "includes_global": True,
                    "global_templates_readonly": True,
                },
            }
        ), 200
    except Exception as exc:
        current_app.logger.error(
            "[AI_TEMPLATES] list_failed role=%s scope=%s error_type=%s",
            getattr(user, "rol", None),
            "tenant" if tenant else "global",
            type(exc).__name__,
        )
        return jsonify(
            {
                "contract_version": AI_TEMPLATES_CONTRACT_VERSION,
                "error": "Error interno al obtener las plantillas.",
                "reason_code": "template_list_failed",
            }
        ), 500


@ai_templates_bp.route("/templates", methods=["POST"])
@token_requerido
@admin_o_empleado_requerido
def create_template(user):
    data, payload_error = _json_object_or_error(
        max_bytes=MAX_TEMPLATE_MUTATION_REQUEST_BYTES,
    )
    if payload_error:
        return _template_contract_error(payload_error)

    tenant, error = _resolve_template_tenant(user, data)
    if error:
        return error

    if tenant is None:
        return _template_contract_error(
            AITemplateContractError(
                "tenant_required_for_mutation",
                field="tenant",
                status_code=403,
                message="Las plantillas globales son de solo lectura; seleccione un tenant.",
            )
        )

    is_active = data.get("is_active", True)
    if not isinstance(is_active, bool):
        return _template_contract_error(
            AITemplateContractError(
                "is_active_must_be_boolean",
                field="is_active",
                message="El campo 'is_active' debe ser booleano.",
            )
        )

    try:
        name = validate_utf8_text(
            data.get("name"),
            field="name",
            max_bytes=MAX_TEMPLATE_NAME_BYTES,
        )
        text = validate_utf8_text(
            data.get("text"),
            field="text",
            max_bytes=MAX_TEMPLATE_TEXT_BYTES,
        )
        keywords = validate_keywords(data.get("keywords", []))
    except AITemplateContractError as exc:
        return _template_contract_error(exc)

    embedding_vector = _generate_embedding(text, name)

    try:
        nueva_plantilla = PlantillasRespuesta(
            tenant_id=tenant.id,
            name=name,
            text=text,
            keywords=keywords,
            is_active=is_active,
            embedding=embedding_vector,
        )
        db.session.add(nueva_plantilla)
        db.session.commit()

        current_app.logger.info(
            "[AI_TEMPLATES] created template_id=%s role=%s scope=tenant",
            nueva_plantilla.id,
            getattr(user, "rol", None),
        )
        return jsonify(_serialize_template(nueva_plantilla, tenant, embedding_generated=bool(embedding_vector))), 201
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "[AI_TEMPLATES] create_failed role=%s error_type=%s",
            getattr(user, "rol", None),
            type(exc).__name__,
        )
        return jsonify(
            {
                "contract_version": AI_TEMPLATES_CONTRACT_VERSION,
                "error": "Error interno al guardar la plantilla.",
                "reason_code": "template_create_failed",
            }
        ), 500


@ai_templates_bp.route("/templates/<string:template_id>", methods=["PUT"])
@token_requerido
@admin_o_empleado_requerido
def update_template(user, template_id):
    data, payload_error = _json_object_or_error(
        max_bytes=MAX_TEMPLATE_MUTATION_REQUEST_BYTES,
    )
    if payload_error:
        return _template_contract_error(payload_error)

    tenant, error = _resolve_template_tenant(user, data)
    if error:
        return error

    try:
        normalized_template_id = validate_utf8_text(
            template_id,
            field="template_id",
            max_bytes=64,
        )
    except AITemplateContractError as exc:
        return _template_contract_error(exc)

    plantilla = (
        PlantillasRespuesta.query.filter(
            PlantillasRespuesta.id == normalized_template_id,
            _visible_template_scope(tenant),
        )
        .order_by(PlantillasRespuesta.id.asc())
        .first()
    )
    if not plantilla:
        return _template_contract_error(
            AITemplateContractError(
                "template_not_found",
                field="template_id",
                status_code=404,
                message="Plantilla no encontrada para el tenant actual.",
            )
        )
    if plantilla.tenant_id is None or tenant is None:
        return _template_contract_error(
            AITemplateContractError(
                "global_template_readonly",
                field="template_id",
                status_code=403,
                message="Las plantillas globales son de solo lectura.",
            )
        )

    updated_fields: list[str] = []
    try:
        new_name = (
            validate_utf8_text(
                data["name"],
                field="name",
                max_bytes=MAX_TEMPLATE_NAME_BYTES,
            )
            if "name" in data
            else plantilla.name
        )
        new_text = (
            validate_utf8_text(
                data["text"],
                field="text",
                max_bytes=MAX_TEMPLATE_TEXT_BYTES,
            )
            if "text" in data
            else plantilla.text
        )
        new_keywords = (
            validate_keywords(data["keywords"])
            if "keywords" in data
            else _normalize_keywords(plantilla.keywords)
        )
    except AITemplateContractError as exc:
        return _template_contract_error(exc)

    new_is_active = data.get("is_active", plantilla.is_active)
    if not isinstance(new_is_active, bool):
        return _template_contract_error(
            AITemplateContractError(
                "is_active_must_be_boolean",
                field="is_active",
                message="El campo 'is_active' debe ser booleano si se proporciona.",
            )
        )

    text_changed = plantilla.text != new_text
    if plantilla.name != new_name:
        updated_fields.append("name")
    if text_changed:
        updated_fields.append("text")
    if sorted(_normalize_keywords(plantilla.keywords)) != sorted(new_keywords):
        updated_fields.append("keywords")
    if plantilla.is_active != new_is_active:
        updated_fields.append("is_active")

    if not updated_fields and not text_changed:
        return jsonify(
            {
                "contract_version": AI_TEMPLATES_CONTRACT_VERSION,
                "mensaje": "No se proporcionaron cambios aplicables.",
            }
        ), 200

    embedding_regenerated = False
    if text_changed:
        embedding_vector = _generate_embedding(new_text, new_name)
        embedding_regenerated = bool(embedding_vector)

    # Apply only after the complete payload and any external embedding call
    # have finished, so rejected requests cannot leave an ORM row dirty.
    plantilla.name = new_name
    plantilla.text = new_text
    if "keywords" in updated_fields:
        plantilla.keywords = new_keywords
        flag_modified(plantilla, "keywords")
    plantilla.is_active = new_is_active
    if text_changed:
        plantilla.embedding = embedding_vector
        flag_modified(plantilla, "embedding")

    try:
        db.session.commit()
        current_app.logger.info(
            "[AI_TEMPLATES] updated template_id=%s role=%s fields=%s",
            plantilla.id,
            getattr(user, "rol", None),
            ", ".join(updated_fields),
        )
        return jsonify(_serialize_template(plantilla, tenant, embedding_regenerated=embedding_regenerated)), 200
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "[AI_TEMPLATES] update_failed template_id=%s role=%s error_type=%s",
            plantilla.id,
            getattr(user, "rol", None),
            type(exc).__name__,
        )
        return jsonify(
            {
                "contract_version": AI_TEMPLATES_CONTRACT_VERSION,
                "error": "Error interno al actualizar la plantilla.",
                "reason_code": "template_update_failed",
            }
        ), 500


@ai_templates_bp.route("/templates/<string:template_id>", methods=["DELETE"])
@token_requerido
@admin_o_empleado_requerido
def delete_template(user, template_id):
    tenant, error = _resolve_template_tenant(user)
    if error:
        return error

    try:
        normalized_template_id = validate_utf8_text(
            template_id,
            field="template_id",
            max_bytes=64,
        )
    except AITemplateContractError as exc:
        return _template_contract_error(exc)

    plantilla = (
        PlantillasRespuesta.query.filter(
            PlantillasRespuesta.id == normalized_template_id,
            _visible_template_scope(tenant),
        )
        .order_by(PlantillasRespuesta.id.asc())
        .first()
    )
    if not plantilla:
        return _template_contract_error(
            AITemplateContractError(
                "template_not_found",
                field="template_id",
                status_code=404,
                message="Plantilla no encontrada para el tenant actual.",
            )
        )
    if plantilla.tenant_id is None or tenant is None:
        return _template_contract_error(
            AITemplateContractError(
                "global_template_readonly",
                field="template_id",
                status_code=403,
                message="Las plantillas globales son de solo lectura.",
            )
        )

    try:
        nombre = plantilla.name
        db.session.delete(plantilla)
        db.session.commit()
        current_app.logger.info(
            "[AI_TEMPLATES] deleted template_id=%s role=%s",
            normalized_template_id,
            getattr(user, "rol", None),
        )
        return jsonify(
            {
                "contract_version": AI_TEMPLATES_CONTRACT_VERSION,
                "mensaje": f"Plantilla '{nombre}' eliminada correctamente.",
            }
        ), 200
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "[AI_TEMPLATES] delete_failed template_id=%s role=%s error_type=%s",
            normalized_template_id,
            getattr(user, "rol", None),
            type(exc).__name__,
        )
        return jsonify(
            {
                "contract_version": AI_TEMPLATES_CONTRACT_VERSION,
                "error": "Error interno al eliminar la plantilla.",
                "reason_code": "template_delete_failed",
            }
        ), 500


@ai_templates_bp.route("/templates/ticket-preview", methods=["POST"])
@token_requerido
@admin_o_empleado_requerido
def preview_ticket_template(user):
    """Render a canned response against one authorized ticket without sending.

    The server owns variable resolution. The response explicitly blocks client
    interpolation whenever the strict allowlist cannot resolve every variable.
    """

    data, payload_error = _json_object_or_error(max_bytes=MAX_PREVIEW_REQUEST_BYTES)
    if payload_error:
        return _template_contract_error(
            payload_error,
            contract_version=AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION,
        )

    tenant, tenant_error = _resolve_template_tenant(
        user,
        data,
        contract_version=AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION,
    )
    if tenant_error:
        return tenant_error
    if tenant is None:
        return _template_contract_error(
            AITemplateContractError(
                "tenant_required",
                field="tenant",
                status_code=400,
                message="Debe seleccionar un tenant para previsualizar la plantilla.",
            ),
            contract_version=AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION,
        )

    try:
        preview = build_ticket_template_preview(
            tenant=tenant,
            actor=user,
            template_id=data.get("template_id"),
            ticket_id=data.get("ticket_id"),
            source_model=data.get("source_model"),
        )
    except AITemplateContractError as exc:
        return _template_contract_error(
            exc,
            contract_version=AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION,
        )
    except Exception as exc:
        current_app.logger.error(
            "[AI_TEMPLATES] ticket_preview_failed role=%s source_model_valid=%s error_type=%s",
            getattr(user, "rol", None),
            data.get("source_model") in {"TenantTicket", "MunicipioTicket", "PymeTicket"},
            type(exc).__name__,
        )
        response = jsonify(
            {
                "contract_version": AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION,
                "error": "No se pudo generar la vista previa.",
                "reason_code": "ticket_preview_failed",
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response, 500

    response = jsonify(preview.to_dict(tenant=tenant))
    response.headers["Cache-Control"] = "no-store"
    return response, 200


@ai_templates_bp.route("/generate-template-text", methods=["POST"])
@token_requerido
@admin_o_empleado_requerido
def generate_template_text_from_prompt(user):
    data, payload_error = _json_object_or_error(
        max_bytes=MAX_GENERATE_TEMPLATE_REQUEST_BYTES,
    )
    if payload_error:
        return _template_contract_error(payload_error)

    prompt_usuario = data.get("prompt")
    if not prompt_usuario or not isinstance(prompt_usuario, str) or not prompt_usuario.strip():
        return jsonify({"error": "El campo 'prompt' es requerido y debe ser un string no vacio."}), 400

    max_prompt_length = 2000
    if len(prompt_usuario) > max_prompt_length:
        return jsonify({"error": f"El prompt excede la longitud maxima de {max_prompt_length} caracteres."}), 400

    try:
        current_app.logger.info(
            "[AI_TEMPLATES] generation_requested user_id=%s prompt_chars=%s",
            getattr(user, "id", None),
            len(prompt_usuario),
        )
        generated_text = llamar_llm_para_generacion_texto(
            system_prompt_especifico=(
                "Eres un asistente experto en redactar plantillas de respuesta. "
                "Genera un texto basado en la solicitud del usuario."
            ),
            user_prompt=prompt_usuario,
            temperature=0.7,
        )
        if generated_text:
            return jsonify({"generated_text": generated_text.strip()}), 200
        return jsonify({"error": "No se pudo generar el texto de la plantilla en este momento."}), 503
    except Exception as exc:
        current_app.logger.error(
            "[AI_TEMPLATES] generation_failed user_id=%s error_type=%s",
            getattr(user, "id", None),
            type(exc).__name__,
        )
        return jsonify({"error": "Error interno al procesar la solicitud de generacion."}), 500


@ai_templates_bp.route("/improve-template-text", methods=["POST"])
@token_requerido
@admin_o_empleado_requerido
def improve_template_text(user):
    data, payload_error = _json_object_or_error(
        max_bytes=MAX_IMPROVE_TEMPLATE_REQUEST_BYTES,
    )
    if payload_error:
        return _template_contract_error(payload_error)

    text_to_improve = data.get("text_to_improve")
    if not text_to_improve or not isinstance(text_to_improve, str) or not text_to_improve.strip():
        return jsonify({"error": "El campo 'text_to_improve' es requerido y debe ser un string no vacio."}), 400

    max_text_length = 4000
    if len(text_to_improve) > max_text_length:
        return jsonify({"error": f"El texto a mejorar excede la longitud maxima de {max_text_length} caracteres."}), 400

    prompt_para_llm = (
        "Reescribe el siguiente texto para que sea mas claro, conciso y profesional, "
        "manteniendo el significado original. Responde unicamente con el texto mejorado.\n\n"
        f"Texto a mejorar:\n\"\"\"\n{text_to_improve}\n\"\"\""
    )

    try:
        current_app.logger.info(
            "[AI_TEMPLATES] improvement_requested user_id=%s text_chars=%s",
            getattr(user, "id", None),
            len(text_to_improve),
        )
        improved_text = llamar_llm_para_generacion_texto(
            system_prompt_especifico=(
                "Eres un asistente experto en refinar plantillas de comunicacion profesional. "
                "Responde unicamente con el texto mejorado."
            ),
            user_prompt=prompt_para_llm,
            temperature=0.5,
        )
        if improved_text:
            return jsonify({"improved_text": improved_text.strip()}), 200
        return jsonify({"error": "No se pudo mejorar el texto de la plantilla en este momento."}), 503
    except Exception as exc:
        current_app.logger.error(
            "[AI_TEMPLATES] improvement_failed user_id=%s error_type=%s",
            getattr(user, "id", None),
            type(exc).__name__,
        )
        return jsonify({"error": "Error interno al procesar la solicitud de mejora."}), 500
