# routes/ai_templates.py

import json
from typing import Any

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func, or_
from sqlalchemy.orm.attributes import flag_modified

from models import PlantillasRespuesta, TenantProfile, db
from services.embedding_service import embed_textos_llm
from services.llm_bridge import llamar_llm_para_generacion_texto
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import admin_o_empleado_requerido, token_requerido


ai_templates_bp = Blueprint("ai_templates", __name__, url_prefix="/api/ai")


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


def _resolve_template_tenant(user, payload: dict | None = None):
    hint = _apply_tenant_alias(_tenant_hint_from_request(payload))
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
        return None, (jsonify({"error": "Permiso denegado para este tenant."}), 403)

    return tenant, None


def _visible_template_scope(tenant):
    if tenant:
        return or_(PlantillasRespuesta.tenant_id == tenant.id, PlantillasRespuesta.tenant_id.is_(None))
    return PlantillasRespuesta.tenant_id.is_(None)


def _editable_template_scope(tenant):
    if tenant:
        return PlantillasRespuesta.tenant_id == tenant.id
    return PlantillasRespuesta.tenant_id.is_(None)


def _serialize_template(plantilla: PlantillasRespuesta, tenant: TenantProfile | None = None, **extra):
    payload = {
        "id": plantilla.id,
        "tenant_id": plantilla.tenant_id,
        "tenant_slug": getattr(plantilla.tenant, "slug", None),
        "scope": "global" if plantilla.tenant_id is None else "tenant",
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
            "No se pudo generar embedding para plantilla '%s': '%s...'",
            template_name.strip(),
            text.strip()[:50],
        )
    except Exception:
        current_app.logger.exception("Error al generar embedding para plantilla '%s'", template_name.strip())
    return None


@ai_templates_bp.route("/templates", methods=["GET"])
@token_requerido
@admin_o_empleado_requerido
def get_all_templates(user):
    tenant, error = _resolve_template_tenant(user)
    if error:
        return error

    try:
        plantillas = (
            PlantillasRespuesta.query.filter(_visible_template_scope(tenant))
            .order_by(PlantillasRespuesta.tenant_id.is_(None).asc(), PlantillasRespuesta.name.asc())
            .all()
        )
        return jsonify({"plantillas": [_serialize_template(item, tenant) for item in plantillas]}), 200
    except Exception as exc:
        current_app.logger.error(
            "Error al obtener plantillas para user=%s tenant=%s: %s",
            getattr(user, "id", None),
            getattr(tenant, "slug", None),
            exc,
            exc_info=True,
        )
        return jsonify({"error": "Error interno al obtener las plantillas."}), 500


@ai_templates_bp.route("/templates", methods=["POST"])
@token_requerido
@admin_o_empleado_requerido
def create_template(user):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body debe ser JSON"}), 400

    tenant, error = _resolve_template_tenant(user, data)
    if error:
        return error

    name = data.get("name")
    text = data.get("text")
    keywords = data.get("keywords")
    is_active = data.get("is_active", True)

    if not name or not isinstance(name, str) or not name.strip():
        return jsonify({"error": "El campo 'name' es requerido y debe ser un string no vacio."}), 400
    if not text or not isinstance(text, str) or not text.strip():
        return jsonify({"error": "El campo 'text' es requerido y debe ser un string no vacio."}), 400
    if keywords is not None and not isinstance(keywords, list):
        return jsonify({"error": "El campo 'keywords' debe ser una lista de strings si se proporciona."}), 400
    if keywords and any(not isinstance(kw, str) for kw in keywords):
        return jsonify({"error": "Todos los elementos en 'keywords' deben ser strings."}), 400
    if not isinstance(is_active, bool):
        return jsonify({"error": "El campo 'is_active' debe ser booleano."}), 400

    embedding_vector = _generate_embedding(text, name)

    try:
        nueva_plantilla = PlantillasRespuesta(
            tenant_id=getattr(tenant, "id", None),
            name=name.strip(),
            text=text.strip(),
            keywords=keywords if keywords else [],
            is_active=is_active,
            embedding=embedding_vector,
        )
        db.session.add(nueva_plantilla)
        db.session.commit()

        current_app.logger.info(
            "Plantilla '%s' creada con ID %s por user=%s tenant=%s",
            nueva_plantilla.name,
            nueva_plantilla.id,
            getattr(user, "id", None),
            getattr(tenant, "slug", None),
        )
        return jsonify(_serialize_template(nueva_plantilla, tenant, embedding_generated=bool(embedding_vector))), 201
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "Error al guardar plantilla '%s' para user=%s tenant=%s: %s",
            name.strip(),
            getattr(user, "id", None),
            getattr(tenant, "slug", None),
            exc,
            exc_info=True,
        )
        return jsonify({"error": "Error interno al guardar la plantilla."}), 500


@ai_templates_bp.route("/templates/<string:template_id>", methods=["PUT"])
@token_requerido
@admin_o_empleado_requerido
def update_template(user, template_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body debe ser JSON y no estar vacio."}), 400

    tenant, error = _resolve_template_tenant(user, data)
    if error:
        return error

    plantilla = (
        PlantillasRespuesta.query.filter(
            PlantillasRespuesta.id == template_id,
            _editable_template_scope(tenant),
        )
        .order_by(PlantillasRespuesta.id.asc())
        .first()
    )
    if not plantilla:
        return jsonify({"error": "Plantilla no encontrada para el tenant actual."}), 404

    updated_fields: list[str] = []
    text_changed = False

    if "name" in data:
        new_name = data["name"]
        if not isinstance(new_name, str) or not new_name.strip():
            return jsonify({"error": "El campo 'name' debe ser un string no vacio si se proporciona."}), 400
        if plantilla.name != new_name.strip():
            plantilla.name = new_name.strip()
            updated_fields.append("name")

    if "text" in data:
        new_text = data["text"]
        if not isinstance(new_text, str) or not new_text.strip():
            return jsonify({"error": "El campo 'text' debe ser un string no vacio si se proporciona."}), 400
        if plantilla.text != new_text.strip():
            plantilla.text = new_text.strip()
            text_changed = True

    if "keywords" in data:
        new_keywords = data["keywords"]
        if not isinstance(new_keywords, list):
            return jsonify({"error": "El campo 'keywords' debe ser una lista de strings si se proporciona."}), 400
        if any(not isinstance(kw, str) for kw in new_keywords):
            return jsonify({"error": "Todos los elementos en 'keywords' deben ser strings."}), 400
        if sorted(_normalize_keywords(plantilla.keywords)) != sorted(new_keywords):
            plantilla.keywords = new_keywords
            flag_modified(plantilla, "keywords")
            updated_fields.append("keywords")

    if "is_active" in data:
        new_is_active = data["is_active"]
        if not isinstance(new_is_active, bool):
            return jsonify({"error": "El campo 'is_active' debe ser booleano si se proporciona."}), 400
        if plantilla.is_active != new_is_active:
            plantilla.is_active = new_is_active
            updated_fields.append("is_active")

    if not updated_fields and not text_changed:
        return jsonify({"mensaje": "No se proporcionaron cambios aplicables."}), 200

    embedding_regenerated = False
    if text_changed:
        updated_fields.append("text")
        embedding_vector = _generate_embedding(plantilla.text, plantilla.name)
        plantilla.embedding = embedding_vector
        flag_modified(plantilla, "embedding")
        embedding_regenerated = bool(embedding_vector)

    try:
        db.session.commit()
        current_app.logger.info(
            "Plantilla '%s' actualizada por user=%s tenant=%s. Campos: %s",
            plantilla.id,
            getattr(user, "id", None),
            getattr(tenant, "slug", None),
            ", ".join(updated_fields),
        )
        return jsonify(_serialize_template(plantilla, tenant, embedding_regenerated=embedding_regenerated)), 200
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "Error al actualizar plantilla '%s' por user=%s: %s",
            plantilla.id,
            getattr(user, "id", None),
            exc,
            exc_info=True,
        )
        return jsonify({"error": "Error interno al actualizar la plantilla."}), 500


@ai_templates_bp.route("/templates/<string:template_id>", methods=["DELETE"])
@token_requerido
@admin_o_empleado_requerido
def delete_template(user, template_id):
    tenant, error = _resolve_template_tenant(user)
    if error:
        return error

    plantilla = (
        PlantillasRespuesta.query.filter(
            PlantillasRespuesta.id == template_id,
            _editable_template_scope(tenant),
        )
        .order_by(PlantillasRespuesta.id.asc())
        .first()
    )
    if not plantilla:
        return jsonify({"error": "Plantilla no encontrada para el tenant actual."}), 404

    try:
        nombre = plantilla.name
        db.session.delete(plantilla)
        db.session.commit()
        current_app.logger.info(
            "Plantilla '%s' (ID %s) eliminada por user=%s tenant=%s",
            nombre,
            template_id,
            getattr(user, "id", None),
            getattr(tenant, "slug", None),
        )
        return jsonify({"mensaje": f"Plantilla '{nombre}' eliminada correctamente."}), 200
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "Error al eliminar plantilla '%s' por user=%s: %s",
            template_id,
            getattr(user, "id", None),
            exc,
            exc_info=True,
        )
        return jsonify({"error": "Error interno al eliminar la plantilla."}), 500


@ai_templates_bp.route("/generate-template-text", methods=["POST"])
@token_requerido
@admin_o_empleado_requerido
def generate_template_text_from_prompt(user):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body debe ser JSON"}), 400

    prompt_usuario = data.get("prompt")
    if not prompt_usuario or not isinstance(prompt_usuario, str) or not prompt_usuario.strip():
        return jsonify({"error": "El campo 'prompt' es requerido y debe ser un string no vacio."}), 400

    max_prompt_length = 2000
    if len(prompt_usuario) > max_prompt_length:
        return jsonify({"error": f"El prompt excede la longitud maxima de {max_prompt_length} caracteres."}), 400

    try:
        current_app.logger.info(
            "Usuario %s solicita generacion de plantilla: '%s...'",
            getattr(user, "id", None),
            prompt_usuario[:100],
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
            "Error al generar texto de plantilla para user=%s: %s",
            getattr(user, "id", None),
            exc,
            exc_info=True,
        )
        return jsonify({"error": "Error interno al procesar la solicitud de generacion."}), 500


@ai_templates_bp.route("/improve-template-text", methods=["POST"])
@token_requerido
@admin_o_empleado_requerido
def improve_template_text(user):
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body debe ser JSON"}), 400

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
            "Usuario %s solicita mejora de plantilla: '%s...'",
            getattr(user, "id", None),
            text_to_improve[:100],
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
            "Error al mejorar texto de plantilla para user=%s: %s",
            getattr(user, "id", None),
            exc,
            exc_info=True,
        )
        return jsonify({"error": "Error interno al procesar la solicitud de mejora."}), 500
